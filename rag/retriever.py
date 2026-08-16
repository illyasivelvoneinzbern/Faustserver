"""
检索器封装模块：基于 ChromaDB 的向量检索 + BM25 混合检索 + 关键词重排 + 意图过滤。
支持渐进式多轮检索：首轮小候选池，若未饱和则自动扩池重试。
支持 Parent-Child 元数据直查：人格穷举列表查询走标题前缀匹配，保证 100% 召回。
支持 post-retrieval 噪声过滤：对检索结果做质量评分，过滤 JSON 残留、无信息密度 chunks。
"""
import logging
import re
from typing import Any, Optional

from langchain_chroma import Chroma

logger = logging.getLogger(__name__)

# ── 噪声检测正则（post-retrieval 质量过滤） ──
# 这些模式出现在 chunk 内容中说明该 chunk 信息密度低或为 Wiki 残留
_NOISE_PATTERNS_POST = [
    re.compile(r'\{"[^{}]*"\s*:'),                               # JSON 键值对残留
    re.compile(r"colspan|rowspan|style=|class=|border="),        # HTML 属性残留
    re.compile(r"__NOTOC__|__TOC__|折叠|展开"),                   # Wiki 模板关键字
    re.compile(r"^(?:第一波|第二波|第三波|第四波)敌人：", re.MULTILINE),  # 关卡敌人数
    re.compile(r"^(?:斩击|打击|贯穿)：\d", re.MULTILINE),        # 战斗属性数据
]

# ── format_context 层面的文本清理正则 ──
# 在最终输出给 LLM 之前，清理残留的 URL 和连续标点符号噪声
_URL_RE = re.compile(r"https?://[^\s]{10,}")                     # URL（>=10字符的http链接）
_CONSECUTIVE_PUNCT_RE = re.compile(r"([，。！？、；：""（）【】《》…—·,\.!\?;:\"\'\)\]\}\)])\1{2,}")  # 同一标点连续3+次


def _clean_context_noise(text: str) -> str:
    """
    在 format_context 层面清理表面噪声：
    1. 删除 URL（https://... 链接残留）
    2. 连续重复标点符号折叠为单个（如 "。。。" → "。"）
    不修改原始 chunk，仅影响输出给 LLM 的格式化文本。
    """
    # 删除 URL
    text = _URL_RE.sub("", text)
    # 连续重复标点折叠为单个
    text = _CONSECUTIVE_PUNCT_RE.sub(r"\1", text)
    # 清理 URL 删除后可能留下的多余空格
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _content_quality_score(text: str) -> float:
    """对检索到的 chunk 内容做质量评分（0.0~1.0）。

    高分：有意义的中文叙事内容
    低分：JSON 残留、Wiki 模板、纯导航/列表/属性数据

    score >= 0.25 视为「可接受」，低于此值的 chunk 在穷举模式下会被丢弃。
    """
    if not text:
        return 0.0

    # 有效中文字符数
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    # 有效英文词数（≥3 字母）
    en_words = len(re.findall(r"[A-Za-z]{3,}", text))
    # 总字符数（含标点）
    total_chars = len(text.strip())

    if total_chars < 20:
        return 0.0

    # 噪声模式命中次数
    noise_hits = sum(len(p.findall(text)) for p in _NOISE_PATTERNS_POST)

    # 基础分：中文字符密度
    cn_density = cn_chars / max(total_chars, 1)

    # 噪声惩罚
    noise_penalty = min(0.8, noise_hits * 0.15)

    # 综合分（中文密度权重 0.6，英文字贡献 0.15，噪声惩罚最大 -0.8）
    score = cn_density * 0.6 + min(en_words / max(total_chars, 1), 0.3) * 0.5 - noise_penalty

    return max(0.0, min(1.0, score))


def _extract_page_type_from_filter(filter_dict: Optional[dict]) -> Optional[str]:
    """从 ChromaDB filter 中提取 page_type 字段，兼容 $and 格式。

    用例：`{"page_type": "accessory"}` 和
    `{"$and": [{"page_type": "accessory"}, {"effect": "呼吸"}]}`
    都应返回 `"accessory"`。
    """
    if not filter_dict or not isinstance(filter_dict, dict):
        return None
    if "page_type" in filter_dict:
        return filter_dict["page_type"]
    if "$and" in filter_dict:
        for cond in filter_dict["$and"]:
            if isinstance(cond, dict) and "page_type" in cond:
                return cond["page_type"]
    return None


def _build_dedup_key(doc: Any) -> str:
    """构建细粒度去重键：page_title + section + 技能维度。

    同一人格的所有 chunk 共享同一个 page_title（info / 各技能 / 被动 / 语音），
    若只按 page_title 去重会把多个技能 chunk 砍到只剩 1 条
    （"七浮只返回技能三"的根因）。因此把 section 与技能标识也纳入键：

    - 有 section 的 chunk（人格技能/语音等）→ 键 = title|section|skill_index|skill_name|stage
    - 无 section 的普通页面（剧情/状态/饰品等）→ 退化为按 title 去重，保持原语义

    这样同一人格的多个不同 chunk 都能进入上下文，同时仍能去掉真正重复的内容。
    """
    meta = doc.metadata if hasattr(doc, "metadata") else {}
    if not meta:
        return str(getattr(doc, "page_content", ""))[:80]
    title = meta.get("page_title", "") or meta.get("title", "")
    section = meta.get("section", "") or ""
    if not section:
        return title
    skill_index = str(meta.get("skill_index", "") or "")
    skill_name = str(meta.get("skill_name", "") or "")
    stage = str(meta.get("stage", "") or "")
    return f"{title}|{section}|{skill_index}|{skill_name}|{stage}"


class LimBusRetriever:
    """边狱巴士知识库检索器（向量检索 + BM25 混合 + 关键词重排 + 意图过滤 + 渐进检索 + Parent-Child直查）"""

    def __init__(
        self,
        vector_store: Chroma,
        top_k: int = 6,
        similarity_threshold: float = 0.35,
        max_context_chars: int = 600,
        deduplicate_by_title: bool = True,
        persona_name: Optional[str] = None,
        bm25_index: Optional[Any] = None,
        hybrid_weight: float = 0.6,  # BM25 权重，0.6 表示 BM25 占 60%
        # ── 渐进式检索 ──
        progressive_enabled: bool = True,
        max_retrieval_rounds: int = 2,
        round_k_multipliers: tuple[int, ...] = (5, 20),
        round_bm25_multipliers: tuple[int, ...] = (10, 40),
        saturation_threshold: float = 0.3,
        result_mult_cap: int = 4,
        # ── LLM 查询扩展 ──
        llm_query_expander: Optional[Any] = None,
        query_expansion_enabled: bool = True,
        # ── Parent-Child 穷举检索 ──
        parent_child_enabled: bool = True,
        # ── 噪声过滤 ──
        noise_filter_enabled: bool = True,
        noise_quality_threshold: float = 0.25,
    ):
        self.vector_store = vector_store
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.max_context_chars = max_context_chars
        self.deduplicate_by_title = deduplicate_by_title
        self.persona_name = persona_name
        self.bm25_index = bm25_index
        self.hybrid_weight = hybrid_weight

        # 渐进式检索配置
        self.progressive_enabled = progressive_enabled
        self.max_retrieval_rounds = max_retrieval_rounds
        self.round_k_multipliers = round_k_multipliers
        self.round_bm25_multipliers = round_bm25_multipliers
        self.saturation_threshold = saturation_threshold
        self.result_mult_cap = result_mult_cap

        # LLM 查询扩展
        self.llm_query_expander = llm_query_expander
        self.query_expansion_enabled = query_expansion_enabled

        # Parent-Child 穷举检索
        self.parent_child_enabled = parent_child_enabled

        # 噪声过滤
        self.noise_filter_enabled = noise_filter_enabled
        self.noise_quality_threshold = noise_quality_threshold

    def _extract_keywords(self, query: str) -> set[str]:
        """从查询中提取中英文关键词（用于标题匹配加分）"""
        # 提取所有中文词（2字及以上）和英文词
        cn_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
        en_words = set(re.findall(r"[A-Za-z]{2,}", query.lower()))
        return cn_words | en_words

    # ── 分 page_type 噪声阈值映射 ──
    _NOISE_THRESHOLD_BY_TYPE: dict[str, float] = {
        "personality": 0.35,   # 人格页面要求更高质量
        "character": 0.25,
        "ego": 0.30,
        "plot": 0.20,          # 剧情文本天然低密度
        "story_dialogue": 0.20,  # 故事对话文本天然低密度（章节层级候选）
        "accessory": 0.25,
        "other": 0.20,
    }

    def _get_noise_threshold(self, page_type: str) -> float:
        """根据 page_type 返回对应的噪声过滤阈值。

        不同页面类型的信息密度不同：
        - 人格/EGO 页面有大量结构化数值数据 → 高阈值过滤噪音
        - 剧情文本天然含大量对话和描述 → 低阈值保留更多内容
        """
        threshold = self._NOISE_THRESHOLD_BY_TYPE.get(page_type)
        if threshold is not None:
            return threshold
        # fallback: 使用全局默认阈值
        return self.noise_quality_threshold

    def _keyword_boost(self, query: str, metadata: dict) -> float:
        """根据查询关键词与文档标题的重合度计算加分（0~0.30）。

        策略：
        - 每个关键词匹配 +0.05（上限 0.15，即 3 个词）
        - 如果查询完全包含在标题中（或标题完全包含在查询中），额外 +0.15
        - 总上限 0.30
        """
        keywords = self._extract_keywords(query)
        if not keywords:
            return 0.0

        title = metadata.get("page_title", metadata.get("title", ""))
        if not title:
            return 0.0

        title_lower = title.lower()
        query_lower = query.lower().strip()

        # 关键词匹配加分
        matches = sum(1 for kw in keywords if kw.lower() in title_lower)
        if matches == 0:
            return 0.0

        boost = min(matches * 0.05, 0.15)

        # 完全包含加分：查询词整体出现在标题中，或标题整体出现在查询词中
        if query_lower in title_lower or title_lower in query_lower:
            boost += 0.15

        return min(boost, 0.30)

    # ── Parent-Child 穷举检索：元数据直查 ──

    def _exhaustive_personality_lookup(
        self,
        character_name: str,
    ) -> list[Any]:
        """
        通过 ChromaDB 元数据遍历 + Python 侧标题前缀匹配，
        确定性地捞出指定角色的所有人格页面。

        这是「Parent-Child 三级架构」的关键：
        - Tier 1 (Parent): 角色名（如 "浮士德"）
        - Tier 2 (Children): 以 "{角色名}" 开头且 page_type="personality" 的所有文档

        与语义搜索不同：此方法保证 100% 召回，不依赖向量相似度。
        """
        # 直接通过 ChromaDB 原生 API 获取全部文档（带 metadata）
        try:
            raw = self.vector_store._collection.get(
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"元数据直查失败: {e}")
            return []

        all_texts = raw.get("documents", [])
        all_metas = raw.get("metadatas", [])

        if not all_texts:
            logger.warning("ChromaDB 中无文档，跳过穷举查找")
            return []

        # Phase 1: 标题前缀匹配 → 找到所有人格页面的 chunk
        # 标题格式如 "浮士德LCB罪人"、"浮士德W公司2级清扫人员"
        prefix = character_name
        personality_docs: list[tuple[str, Any]] = []  # (dedup_key, doc)
        seen_titles: set[str] = set()

        from rag.bm25_index import _make_doc_key

        for text, meta in zip(all_texts, all_metas):
            title = meta.get("page_title", "") or meta.get("title", "")
            page_type = meta.get("page_type", "")

            # 必须是以角色名开头的人格页面
            if not (title.startswith(prefix) and page_type == "personality"):
                continue

            # 同一个 title 只保留一条（代表该人格页面）
            if title in seen_titles:
                continue
            seen_titles.add(title)

            # 构造 LangChain Document
            from langchain_core.documents import Document
            doc = Document(page_content=text, metadata=meta)
            personality_docs.append((_make_doc_key(doc), doc))

        logger.info(
            f"Parent-Child 穷举: '{character_name}' → {len(personality_docs)} 个人格页面 "
            f"(标题前缀匹配, 100% 召回，不做噪声过滤)"
        )

        return [doc for _, doc in personality_docs]

    def _exhaustive_ego_lookup(
        self,
        character_name: str,
    ) -> list[Any]:
        """
        通过 ChromaDB 元数据遍历 + Python 侧标题包含匹配，
        确定性地捞出指定角色的所有 E.G.O 装备页面。

        E.G.O 装备标题格式多样：
        - "{ego名}-{角色名}"   如 "红艳煞-浮士德"、"提灯-格里高尔"
        - "{角色名}E.G.O::{ego名}"  如 "浮士德LCE E.G.O::红艳煞"（但这属于 personality）
        - "{ego名}-{角色名}/2025愚人节"（特殊情况，page_type 通常为 other）

        筛选条件：page_type == "ego" 且 title 包含角色名（不要求前缀匹配）。
        """
        try:
            raw = self.vector_store._collection.get(
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"EGO 穷举元数据直查失败: {e}")
            return []

        all_texts = raw.get("documents", [])
        all_metas = raw.get("metadatas", [])

        if not all_texts:
            logger.warning("ChromaDB 中无文档，跳过 EGO 穷举查找")
            return []

        from rag.bm25_index import _make_doc_key

        ego_docs: list[tuple[str, Any]] = []
        seen_titles: set[str] = set()

        for text, meta in zip(all_texts, all_metas):
            title = meta.get("page_title", "") or meta.get("title", "")
            page_type = meta.get("page_type", "")

            # EGO 装备：page_type == "ego" 且标题包含角色名
            if page_type != "ego":
                continue
            # 标题包含角色名（如 "红艳煞-浮士德"）或 chunk 文本中包含角色名
            # （如 "表象放射器" 标题无角色名但 content 中有 "浮士德"）
            if character_name not in title and character_name not in text:
                continue

            if title in seen_titles:
                continue
            seen_titles.add(title)

            from langchain_core.documents import Document
            doc = Document(page_content=text, metadata=meta)
            ego_docs.append((_make_doc_key(doc), doc))

        logger.info(
            f"Parent-Child EGO 穷举: '{character_name}' → {len(ego_docs)} 个EGO装备页面 "
            f"(标题包含匹配, 100% 召回，不做噪声过滤)"
        )

        return [doc for _, doc in ego_docs]

    def _exhaustive_chapter_lookup(
        self,
        chapter_prefix: str,
    ) -> list[Any]:
        """章节层级穷举检索：按剧情页面编号前缀确定性召回。

        剧情页面 title 是点分式编号（如 "8-33-06" = 章-节-小节），
        天然构成层级树。本方法按前缀直查 story_dialogue 文档：

        - "8-33-06" → 该叶子页面（title.startswith("8-33-06")）
        - "8-33"    → 该节下的所有页面（title.startswith("8-33")）
        - "8"       → 第八章下的所有页面（title.startswith("8")）

        注意：不能依赖 chunk 的 chapter metadata（新格式页面 chapter 为空），
        必须基于 title 编号前缀匹配。此方法与罪人→人格/EGO 的 Parent-Child
        直查共用"元数据遍历 + Python 侧前缀匹配"模式。
        """
        # 仅匹配纯编号前缀，避免误伤 "主线战斗1-10" 等敌人页面
        prefix = str(chapter_prefix).strip()
        if not prefix or not re.match(r"^\d+(-\d+)*$", prefix):
            logger.debug(f"章节层级穷举跳过: 无效前缀 '{prefix}'")
            return []

        try:
            raw = self.vector_store._collection.get(
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"章节层级元数据直查失败: {e}")
            return []

        all_texts = raw.get("documents", [])
        all_metas = raw.get("metadatas", [])

        if not all_texts:
            logger.warning("ChromaDB 中无文档，跳过章节层级查找")
            return []

        from rag.bm25_index import _make_doc_key

        chapter_docs: list[tuple[str, Any]] = []
        seen_titles: set[str] = set()

        for text, meta in zip(all_texts, all_metas):
            title = meta.get("page_title", "") or meta.get("title", "")
            page_type = meta.get("page_type", "")

            # 章节层级仅针对 story_dialogue 剧情页面，且必须标题前缀匹配
            if page_type != "story_dialogue":
                continue
            if not title.startswith(prefix):
                continue

            if title in seen_titles:
                continue
            seen_titles.add(title)

            from langchain_core.documents import Document
            doc = Document(page_content=text, metadata=meta)
            chapter_docs.append((_make_doc_key(doc), doc))

        logger.info(
            f"章节层级穷举: '{prefix}' → {len(chapter_docs)} 个剧情页面 "
            f"(标题前缀匹配, 100% 召回)"
        )

        return [doc for _, doc in chapter_docs]

    # ── 单轮检索管道 ──

    def _retrieval_pipeline(
        self,
        rewritten_query: str,
        original_query: str,
        effective_filter: Optional[dict],
        vec_k: int,
        bm25_k: int,
        active_threshold: float,
        persona_name: Optional[str],
        personality_title: Optional[str] = None,
    ) -> list[tuple[str, float, Any]]:
        """
        单轮检索管道：向量搜索 → BM25 融合 → title boost → 评分 → 去重。

        将原始 retrieve() 中的核心逻辑提取为独立方法，
        供渐进式循环按不同 (vec_k, bm25_k, threshold) 参数反复调用。

        Args:
            rewritten_query: 意图分析后的改写查询词
            original_query: 用户原始查询（用于 keyword_boost 标题匹配）
            effective_filter: ChromaDB where 过滤条件
            vec_k: 向量检索候选数
            bm25_k: BM25 检索候选数
            active_threshold: 当前轮的相似度阈值
            persona_name: 角色名（用于 title boost）
            personality_title: 具体人格名（Fix A）。非空时对
                page_title/personality_name 精确匹配该人格名的 chunk
                追加高 boost（锁定），而非 persona 级统一加分。

        Returns:
            [(dedup_key, combined_score, doc), ...] 按评分降序，已标题去重。
            返回空列表表示本轮无结果通过阈值。
        """
        from rag.bm25_index import _make_doc_key, merge_and_rerank

        # 精确匹配人格名级锁定：page_title/personality_name 与 personality_title
        # 完全一致时视为确定性命中目标人格（Fix A）。
        def _exact_title_match(metadata: dict) -> bool:
            if not personality_title:
                return False
            title = metadata.get("page_title", "") or ""
            pname = metadata.get("personality_name", "") or ""
            return title == personality_title or pname == personality_title

        # 通用标题匹配（persona 级 substring，仅作弱 fallback）
        def _loose_title_match(metadata: dict, name: str) -> bool:
            title = metadata.get("page_title", "") or ""
            pname = metadata.get("personality_name", "") or ""
            return name in title or name in pname

        # 是否存在精确人格名锁定
        has_exact_lock = bool(personality_title)

        # ── 向量检索 ──
        raw_results = self.vector_store.similarity_search_with_score(
            rewritten_query,
            k=vec_k,
            filter=effective_filter,
        )

        # ── BM25 混合检索（如可用）──
        if self.bm25_index is not None and self.bm25_index.is_built:
            try:
                # 向量结果：距离 → 相似度
                vec_scored = [(1.0 - distance, doc) for doc, distance in raw_results]

                # BM25 检索
                bm25_raw = self.bm25_index.search(rewritten_query, k=bm25_k)

                # ── BM25 post-filter：按 ChromaDB filter 过滤，与向量侧保持一致 ──
                if effective_filter and isinstance(effective_filter, dict):
                    # 提取所有 page_type / effect / rarity 约束（兼容 $and 格式）
                    constraints: dict[str, object] = {}
                    if "$and" in effective_filter:
                        for cond in effective_filter["$and"]:
                            if isinstance(cond, dict):
                                constraints.update(cond)
                    else:
                        constraints = dict(effective_filter)

                    if constraints:
                        before_filter = len(bm25_raw)
                        filtered = []
                        for score, doc in bm25_raw:
                            meta = doc.metadata
                            match = True
                            for key, expected in constraints.items():
                                actual = meta.get(key)
                                if actual != expected:
                                    match = False
                                    break
                            if match:
                                filtered.append((score, doc))
                        bm25_raw = filtered
                        if len(bm25_raw) < before_filter:
                            logger.debug(
                                f"BM25 post-filter {constraints}: "
                                f"{before_filter} → {len(bm25_raw)}"
                            )

                # ── BM25 侧标题 Boost ──
                # 人格名级精确匹配 → 高倍率（×1.35 锁定）；否则 persona 级 substring → ×1.15
                name_bm25 = persona_name or self.persona_name
                if name_bm25 and effective_filter and isinstance(effective_filter, dict):
                    if _extract_page_type_from_filter(effective_filter) in ("character", "personality"):
                        bm25_raw = [
                            (s * 1.35
                             if personality_title and (
                                 d.metadata.get("page_title", "") == personality_title
                                 or d.metadata.get("personality_name", "") == personality_title
                             )
                             else s * 1.15
                             if name_bm25 in d.metadata.get("page_title", "")
                             or name_bm25 in d.metadata.get("personality_name", "")
                             else s, d)
                            for s, d in bm25_raw
                        ]

                # ── 动态 BM25 权重：梯度调整（根据候选数自动升降） ──
                effective_w_bm25 = self.hybrid_weight
                effective_w_vec = 1.0 - self.hybrid_weight
                bm25_count = len(bm25_raw)
                if bm25_count < 5:
                    effective_w_bm25 = 0.05   # BM25 几乎不可用
                    effective_w_vec = 0.95
                elif bm25_count < 10:
                    effective_w_bm25 = 0.15
                    effective_w_vec = 0.85
                elif bm25_count < 20:
                    effective_w_bm25 = 0.30
                    effective_w_vec = 0.70
                elif bm25_count < 40:
                    effective_w_bm25 = 0.50
                    effective_w_vec = 0.50
                # else: 保持默认 hybrid_weight
                if bm25_count < 40:
                    logger.debug(
                        f"BM25 梯度权重调整: count={bm25_count} → w_bm25={effective_w_bm25}"
                    )

                # 合并 + 归一化 + 加权融合
                merged = merge_and_rerank(
                    vec_scored,
                    bm25_raw,
                    weight_vec=effective_w_vec,
                    weight_bm25=effective_w_bm25,
                )

                # 替换 raw_results 为融合后的结果
                raw_results = [(doc, 1.0 - score) for score, doc in merged]
            except Exception as e:
                logger.warning(f"BM25 混合检索异常，回退到纯向量检索: {e}")

        # ── Python 侧 post-retrieval 标题 Boost（非硬过滤） ──
        name_for_boost = persona_name or self.persona_name
        if name_for_boost and effective_filter and isinstance(effective_filter, dict):
            if _extract_page_type_from_filter(effective_filter) in ("character", "personality"):
                before_boost = len(raw_results)
                boosted: list[tuple[Any, float]] = []
                exact_lock = 0
                loose_hit = 0
                for doc, dist in raw_results:
                    # 人格名级精确匹配 → 高 boost（Fix A：锁定具体人格）
                    if _exact_title_match(doc.metadata):
                        bonus = 0.45
                        exact_lock += 1
                    # 无具体人格名时保留 persona 级 substring 弱加分；或作为精确锁定的兜底
                    elif _loose_title_match(doc.metadata, name_for_boost):
                        bonus = 0.20
                        loose_hit += 1
                    else:
                        bonus = 0.0
                    # bonus 转换为降低 distance（distance 越小越好）
                    boosted.append((doc, dist - bonus))
                raw_results = boosted
                if has_exact_lock:
                    logger.debug(
                        f"人格名锁定 '{personality_title}': "
                        f"{before_boost} 条中 {exact_lock} 条精确命中 (高boost 0.45)，"
                        f"{loose_hit} 条 persona 级命中 (0.20)"
                    )
                else:
                    match_count = exact_lock + loose_hit
                    logger.debug(
                        f"标题 Boost '{name_for_boost}': {before_boost} 条中 {match_count} 条匹配"
                    )

        if not raw_results:
            return []

        # ── 距离 → 相似度，按阈值过滤，叠加 keyword boost 和 category penalty ──
        scored_docs: list[tuple[float, Any]] = []
        for doc, distance in raw_results:
            similarity = 1.0 - distance
            if similarity < active_threshold:
                continue
            boost = self._keyword_boost(original_query, doc.metadata)
            combined = similarity + boost

            # ── 人格/剧情页面降权（非初始人格） ──
            categories = doc.metadata.get("categories", "")
            if isinstance(categories, str) and categories:
                cat_set = set(categories.split(","))
                penalty_types = {"人格", "剧情"}
                if effective_filter and isinstance(effective_filter, dict):
                    ft = _extract_page_type_from_filter(effective_filter) or ""
                    if ft == "personality":
                        penalty_types = {"剧情"}
                    elif ft == "plot":
                        penalty_types = {"人格"}
                if cat_set & penalty_types:
                    combined -= 0.20

            scored_docs.append((combined, doc))

        if not scored_docs:
            return []

        # 按综合评分降序排列
        scored_docs.sort(key=lambda x: x[0], reverse=True)

        # ── 同标题去重（细粒度：保留同一人格的多个技能/信息/语音 chunk） ──
        if self.deduplicate_by_title:
            seen_keys: set[str] = set()
            deduped: list[tuple[float, Any]] = []
            for score, doc in scored_docs:
                key = _build_dedup_key(doc)
                if not key:
                    deduped.append((score, doc))
                    continue
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append((score, doc))
            scored_docs = deduped

        # 转为 (dedup_key, score, doc) 供外层渐进循环合并
        return [(_make_doc_key(doc), score, doc) for score, doc in scored_docs]

    # ── 渐进式检索主入口 ──

    def retrieve(
        self,
        query: str,
        filter_dict: Optional[dict] = None,
        persona_name: Optional[str] = None,
    ) -> list[Any]:
        """
        渐进式多轮检索 / Parent-Child 穷举检索（自动路由）。

        路由逻辑：
        - 人格穷举列表查询 (is_listing=True) → Parent-Child 元数据直查
        - 普通查询 → 渐进式多轮语义检索

        渐进式流程：
        1. 意图分析 → 查询改写 + page_type 过滤
        2. [第N轮] 单轮检索管道 → 跨轮合并去重 → 饱和检测
        3. 达到饱和或最大轮数 → 动态 final_k 输出

        饱和检测条件（同时满足则提前终止）：
        - 累计唯一标题数 >= top_k（已有足够的候选多样性）
        - 本轮新增标题数 < top_k × saturation_threshold（扩大候选池收益递减）
        """
        # ── Phase 1: 意图分析 + 查询改写 ──
        name = persona_name or self.persona_name
        rewritten_query, intent_filter, is_listing, target_char, effect_filter, rarity_filter, target_chapter = (
            self._analyze_intent(query, name)
        )

        # ── Phase 1b: 人格名级锁定（Fix A）──
        # 从原始查询提取具体人格名（如 "兔浮" → "浮士德黑兽-卯魁首"），
        # 使检索对该人格 chunk 精确匹配并高 boost，而非 persona 级统一加分。
        # 仅在目标 page_type 为人格/角色相关时启用，避免误锁定。
        personality_title: Optional[str] = None
        try:
            from rag.query_processor import extract_personality_name
            intent_pt = _extract_page_type_from_filter(intent_filter) or ""
            if intent_pt in ("personality", "character") or is_listing:
                personality_title = extract_personality_name(query) or extract_personality_name(rewritten_query)
                if personality_title:
                    logger.debug(
                        f"人格名锁定: query='{query[:30]}...' → '{personality_title}'"
                    )
        except Exception as e:
            logger.warning(f"人格名提取异常，跳过锁定: {e}")

        # ── Parent-Child 穷举检索（人格/EGO 列表查询快捷路径） ──
        if is_listing and self.parent_child_enabled and target_char:
            listing_type = _extract_page_type_from_filter(intent_filter) or ""
            if listing_type == "ego":
                logger.info(
                    f"触发 Parent-Child EGO 穷举: query='{query[:30]}...', target='{target_char}'"
                )
                docs = self._exhaustive_ego_lookup(target_char)
            else:
                logger.info(
                    f"触发 Parent-Child 人格穷举: query='{query[:30]}...', target='{target_char}'"
                )
                docs = self._exhaustive_personality_lookup(target_char)

            if docs:
                docs.sort(key=lambda d: d.metadata.get("page_title", ""))
                return docs
            # 直查无结果时回退到语义检索（罕见情况）
            logger.debug(
                f"穷举检索 0 结果，回退语义检索: target='{target_char}'"
            )

        # 合并用户传入的 filter 和意图 filter（用户 filter 优先）
        effective_filter = filter_dict
        if intent_filter:
            if effective_filter:
                effective_filter = {**intent_filter, **effective_filter}
            else:
                effective_filter = intent_filter

        # ── Phase 2: 渐进式多轮检索 ──
        all_keyed: dict[str, tuple[float, Any]] = {}  # dedup_key → (score, doc)

        # ── Phase 2.0a: 章节层级候选注入（剧情编号 → 章/节/小节）──
        # 当查询命中剧情编号（如 "8-33-06" / "8-33" / "第八章"）时，
        # 按 title 前缀确定性召回 story_dialogue 页面，并注入 all_keyed。
        # 关系命中者获得高优先评分（0.999），不受语义分淹没。
        if target_chapter:
            try:
                chapter_docs = self._exhaustive_chapter_lookup(target_chapter)
                from rag.bm25_index import _make_doc_key
                for doc in chapter_docs:
                    key = _make_doc_key(doc)
                    # 章节层级直查是确定性命中，给予接近 1.0 的评分
                    if key not in all_keyed:
                        all_keyed[key] = (0.999, doc)
                if chapter_docs:
                    logger.info(
                        f"章节层级候选注入: '{target_chapter}' → {len(chapter_docs)} 个页面 "
                        f"(已并入检索候选池)"
                    )
            except Exception as e:
                logger.warning(f"章节层级候选注入异常，继续语义检索: {e}")

        max_rounds = self.max_retrieval_rounds if self.progressive_enabled else 1
        round_k_mults = self.round_k_multipliers
        round_bm25_mults = self.round_bm25_multipliers

        # ── Phase 2.0: LLM 查询扩展（HyDE 式）──
        # 将改写后的查询扩展为 2~3 条搜索短语，多路并行检索后融合。
        # 仅对非穷举列表类查询启用（穷举走 Parent-Child 直查，无扩展必要）。
        search_queries: list[str] = [rewritten_query]
        if (
            not is_listing
            and self.query_expansion_enabled
            and self.llm_query_expander is not None
        ):
            try:
                expanded = self.llm_query_expander.expand_sync(rewritten_query)
                if expanded:
                    # 去重 + 保留改写后的原查询作为第一条
                    search_queries = [rewritten_query] + [
                        p for p in expanded if p.strip() and p != rewritten_query
                    ]
                    # 限制扩展查询数量，避免过多检索开销
                    search_queries = search_queries[:4]
                    if len(search_queries) > 1:
                        logger.debug(
                            f"LLM 查询扩展: '{rewritten_query[:30]}...' → "
                            f"{len(search_queries) - 1} 条补充查询"
                        )
            except Exception as e:
                logger.warning(f"LLM 查询扩展异常，使用原查询: {e}")
                search_queries = [rewritten_query]

        final_round = 0
        for round_idx in range(max_rounds):
            final_round = round_idx

            # 每轮使用不同的候选池大小和阈值
            vec_k = self.top_k * round_k_mults[min(round_idx, len(round_k_mults) - 1)]
            bm25_k = self.top_k * round_bm25_mults[min(round_idx, len(round_bm25_mults) - 1)]
            # 阈值逐轮递减 0.10，首轮=原始阈值，次轮放宽以捕获更多候选
            rthreshold = max(0.15, self.similarity_threshold - round_idx * 0.10)

            # ── 多路查询融合检索：对每条扩展查询执行一轮检索管道 ──
            round_docs = []
            seen_round_keys: set[str] = set()
            for sq in search_queries:
                sq_docs = self._retrieval_pipeline(
                    rewritten_query=sq,
                    original_query=query,
                    effective_filter=effective_filter,
                    vec_k=vec_k,
                    bm25_k=bm25_k,
                    active_threshold=rthreshold,
                    persona_name=name,
                    personality_title=personality_title,
                )
                # 跨路去重：同一 dedup_key 仅保留当前路内评分最高者
                for key, score, doc in sq_docs:
                    if key not in seen_round_keys:
                        seen_round_keys.add(key)
                        round_docs.append((key, score, doc))
            # 跨路融合后按评分降序（与单路行为一致）
            round_docs.sort(key=lambda x: x[1], reverse=True)

            if not round_docs:
                continue

            # 跨轮合并去重：同一 dedup_key 保留最高分
            new_in_round = 0
            for key, score, doc in round_docs:
                if key not in all_keyed or score > all_keyed[key][0]:
                    if key not in all_keyed:
                        new_in_round += 1
                    all_keyed[key] = (score, doc)

            # ── 饱和检测 ──
            if round_idx < max_rounds - 1:
                if len(all_keyed) >= self.top_k and new_in_round < self.top_k * self.saturation_threshold:
                    logger.debug(
                        f"渐进检索饱和: round={round_idx+1}/{max_rounds}, "
                        f"累计唯一={len(all_keyed)}, 新增={new_in_round}"
                    )
                    break

        # ── Phase 3: 输出 ──
        sorted_docs = sorted(all_keyed.values(), key=lambda x: x[0], reverse=True)

        # 动态 final_k：根据实际唯一标题数自适应放量
        unique_count = len(sorted_docs)
        if self.progressive_enabled:
            final_k = max(self.top_k, min(unique_count, self.top_k * self.result_mult_cap))
        else:
            final_k = self.top_k

        reranked = [doc for _, doc in sorted_docs[:final_k]]

        # ── Phase 3.5: 饰品结构化 post-filter ──
        # ChromaDB 的 WHERE 过滤对 effect/rarity 稀疏字段不可靠，
        # 改为 Python 侧根据 metadata 做硬过滤。
        if effect_filter or rarity_filter is not None:
            before_af = len(reranked)
            af_filtered: list[Any] = []
            for doc in reranked:
                meta = doc.metadata
                ok = True
                if effect_filter:
                    doc_eff = meta.get("effect", "")
                    # effect_filter 可能是别名（如"钉子"→"流血"），已在 query_processor 中完成映射
                    if doc_eff != effect_filter:
                        ok = False
                if ok and rarity_filter is not None:
                    doc_rar = meta.get("rarity")
                    if doc_rar != rarity_filter:
                        ok = False
                if ok:
                    af_filtered.append(doc)
            reranked = af_filtered
            logger.info(
                f"饰品 post-filter: effect={effect_filter!r}, rarity={rarity_filter} "
                f"→ {before_af} → {len(reranked)} 条"
            )

            # ── Phase 3.5b: 结构化过滤 0 结果时的深度回退扫描 ──
            # 当用户明确指定了 effect/rarity 约束但渐进检索的语义通路未召回
            # 任何匹配文档时，使用全量 accessory 扫描绕过相似度阈值。
            # accessory 类型总 chunks 约 500~600，k=1000 足够全覆盖。
            if not reranked and effective_filter:
                pt = _extract_page_type_from_filter(effective_filter)
                if pt == "accessory":
                    logger.info(
                        f"饰品结构化约束无结果，触发全量 accessory 回退扫描: "
                        f"effect={effect_filter!r}, rarity={rarity_filter}"
                    )
                    fallback_raw = self.vector_store.similarity_search_with_score(
                        rewritten_query,
                        k=1000,
                        filter=effective_filter,
                    )
                    # 对全量结果做 Python 侧 post-filter（不经过阈值）
                    fallback_filtered: list[Any] = []
                    for doc, _distance in fallback_raw:
                        meta = doc.metadata
                        ok = True
                        if effect_filter:
                            if meta.get("effect", "") != effect_filter:
                                ok = False
                        if ok and rarity_filter is not None:
                            if meta.get("rarity") != rarity_filter:
                                ok = False
                        if ok:
                            fallback_filtered.append(doc)
                    # 去重（按 page_title）
                    seen_titles: set[str] = set()
                    deduped: list[Any] = []
                    for doc in fallback_filtered:
                        t = doc.metadata.get("page_title", "")
                        if t and t not in seen_titles:
                            seen_titles.add(t)
                            deduped.append(doc)
                    reranked = deduped
                    logger.info(
                        f"全量 accessory 回退扫描: {len(fallback_raw)} chunks "
                        f"→ {len(fallback_filtered)} 条 (post-filter) "
                        f"→ {len(reranked)} 条 (去重)"
                    )

        # ── Phase 4: 噪声过滤（分 page_type 阈值，非穷举路径） ──
        # Fix B：personality 类型下对 section=skill 的 chunk 降低阈值至 0.25，
        # 避免结构化技能块（数据密度低但信息有效）被 0.35 的高阈值误过滤。
        # Fix D：将 skill_voice 一并豁免——技能语音 chunk 的 section 是
        # "skill_voice"（quality 仅 0.24~0.29），但内容包含正式技能名，是
        # 与查询向量相似度最高的关键候选。若按 personality 默认 0.35 阈值
        # 过滤会被系统性误杀（对照实验证实 Q2/Q3/Q4 目标 chunk 丢失主因）。
        if self.noise_filter_enabled and not is_listing:
            before_filter = len(reranked)
            filtered = []
            for d in reranked:
                pt = d.metadata.get("page_type", "")
                section = d.metadata.get("section", "")
                threshold = self._get_noise_threshold(pt)
                if pt == "personality" and section in ("skill", "passive", "skill_voice"):
                    threshold = 0.20
                if _content_quality_score(d.page_content) >= threshold:
                    filtered.append(d)
            reranked = filtered
            if len(reranked) < before_filter:
                logger.debug(
                    f"噪声过滤: {before_filter} → {len(reranked)}"
                )

        logger.debug(
            f"检索 '{query[:30]}...': rounds={final_round+1}/{max_rounds}, "
            f"唯一={unique_count}→输出={len(reranked)} 条 "
            f"(阈值≥{self.similarity_threshold})"
        )
        return reranked

    def _analyze_intent(
        self, query: str, persona_name: Optional[str] = None
    ) -> tuple[str, Optional[dict], bool, Optional[str], Optional[str], Optional[int], Optional[str]]:
        """封装意图分析调用，便于子类覆盖。"""
        from rag.query_processor import get_filter_and_query
        return get_filter_and_query(query, persona_name)

    # ── 辅助：轻量代理对象，让 truncate_context 能正常读取 .page_content / .metadata ──
    class _DocProxy:
        __slots__ = ("page_content", "metadata")
        def __init__(self, original: Any, cleaned_content: str):
            self.page_content = cleaned_content
            self.metadata = getattr(original, "metadata", {})

    def format_context(self, docs: list[Any]) -> str:
        """
        将检索结果格式化为 Prompt 可用的上下文字符串。

        处理流程：
        1. 对每个 doc 的 page_content 做表面噪声清理（URL 删除 + 连续标点折叠）
        2. 截断到 max_context_chars（硬上限，保证 token 可控）
        """
        from utils.token_saver import truncate_context

        # 表面噪声清理：不修改原始 doc 对象，只清理格式化后的文本
        cleaned_docs = []
        for doc in docs:
            content = doc.page_content if hasattr(doc, "page_content") else str(doc)
            content = _clean_context_noise(content)
            # 构造一个轻量代理对象，避免修改原始 Document
            cleaned_docs.append(self._DocProxy(doc, content))

        return truncate_context(cleaned_docs, self.max_context_chars)

    def search_with_source_filter(
        self,
        query: str,
        source: str = "wiki",
    ) -> tuple[list[Any], str]:
        """
        带来源过滤的检索（例如：仅搜索 Wiki 或 仅搜索 X）。

        返回 (文档列表, 格式化后的上下文)
        """
        docs = self.retrieve(query, filter_dict={"source": source})
        context = self.format_context(docs)
        return docs, context
