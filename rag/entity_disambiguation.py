# -*- coding: utf-8 -*-
"""模糊搜索消歧（P39）：数据类查询直答未命中时，从结构化库模糊检索候选供用户选择。

背景（P23 遗留问题）：敌方直答在多候选时已会"列清单"，但：
1. 人格/饰品/事件在模糊未命中时仍是静默回落 RAG → 可能被 LLM 编造或"未收录"；
2. 列出的清单**没有后续交互**——用户无法回复数字选择，只能重打全名。

本模块统一补齐：四类结构化数据（人格/敌方/饰品/事件）共享一个模糊索引，
返回 top-N 候选；用户在会话里回复数字即确定性作答（绕过 LLM，无幻觉）。

打分设计（0~100）——查询片段覆盖率：
- 查询去噪后取全部双字片段，按稀有度分档加权（炎拳 df=1 → 1.0 / 管家 → 0.4 /
  收尾 → 0.2…），候选名称覆盖的片段权重之和 ÷ 查询片段总权重 × 100；
- 为什么不用 rapidfuzz partial_ratio 为主：它对"XX事务所收尾人"这类尾巴雷同
  的名称给 85+ 分、对真目标"XX炎拳事务所幸存者"只给 62 分，基础分差距无法用
  加分弥补（"炎拳事务所收尾人的数据"实证）。覆盖率天然偏好包含稀有片段的名称；
- rapidfuzz 保留为**同分决胜器**（覆盖率相同时比名称相似度）。

相关实体扩展（"卯魁首"歧义修复）：
- 从 wiki 页面"相关人格/相关敌方"链接段构建反向索引（如 子路 → 浮士德黑兽-卯魁首）；
- 查询命中某实体时，把链接它的**可作答实体**也列为候选（分数 -8）——"卯魁首"
  既指浮士德人格也指敌方子路，必须列两个让用户选，不能直接作答。

触发条件（agent/core.py）：
- 意图 == data，且四个直答 store 全部未命中；
- 候选 ≥2 或唯一候选置信不足 → 列清单 + 存会话待确认；
- 唯一候选且分数 ≥ direct_threshold → 直接确定性作答（不再走 RAG 防编造）。

依赖：rapidfuzz（requirements.txt 已加入）；缺失时降级 difflib（纯标准库）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _RAPIDFUZZ = True
except Exception:  # pragma: no cover
    _RAPIDFUZZ = False
    logger.warning("rapidfuzz 未安装（pip install rapidfuzz），模糊搜索降级 difflib")

WIKI_JSONL = Path("data/raw/wiki_pages.jsonl")
# wiki 页面"相关人格/相关敌方"链接段（如 子路 页面 → link：浮士德黑兽-卯魁首）
_RELATED_SECTION_RE = re.compile(r"(?:相关人格|相关敌方)\s*\n((?:link[：:]\s*[^\n]+\n?)+)")
_RELATED_ITEM_RE = re.compile(r"link[：:]\s*(.+)")
# 相关实体加入候选时的分数降幅：保证 top 与第二名的分差 < direct_gap → 必走"询问"
_RELATED_SCORE_OFFSET = 8.0

# 列表/泛指查询不触发消歧（避免把"有哪些人格"劫持成候选清单）
_LISTING_RE = re.compile(r"(哪些|有什么|有哪些|列表|全部|所有|大全|一览|盘点|谁能|谁最强|谁最厉害)")
# 查询侧噪声词：数据查询的泛化后缀/语气词（仅作用于打分查询，不影响展示；
# 与 persona_direct._FUZZY_NOISE_RE / query_processor 剥噪逻辑对齐）
_QUERY_NOISE_RE = re.compile(
    r"(我想知道|我想要|请问一下|请告诉我|给我|查一下|查询一下|"
    r"的数据|的资料|的信息|的技能|的属性|的属性数据|数据|技能|能力|介绍|属性|"
    r"是什么|是啥|怎么样|如何|有哪些|哪个|哪个人格|"
    r"看看|看一下|告诉我|介绍一下|说说|"
    r"的|呢|吗|啊|呀|吧|？|\?)"
)
# 敌方泛化名（非真实实体名，命中即重罚，防"普通模式数据"这类噪声顶上来；
# 注意不可包含"阶段"——那是真实敌方名的一部分，如"亚哈（第一阶段）"）
_GENERIC_NAME_PENALTY = re.compile(r"(普通模式|测试|示例)")
# 查询里出现明确的类别词 → 该类别加权（与 intent_gate / query_processor 词表对齐）
_KIND_HINTS = {
    "persona": ("人格", "EGO", "ego", "技能"),
    "enemy": ("敌方", "敌人", "BOSS", "boss", "怪物", "关卡", "boss战"),
    "gift": ("饰品", "装备", "道具"),
    "event": ("事件", "遭遇", "故事"),
}


def _norm(s: str) -> str:
    """规范化：去空白 / 间隔号 / 中缀（匹配忽略排版差异）。"""
    return (s or "").replace(" ", "").replace("·", "").replace("-", "").replace("　", "")


def _strip_query_noise(q: str) -> str:
    """剥离查询侧噪声词，得到用于打分的核心片段（"浮士德黑兽的技能" → "浮士德黑兽"）。"""
    s = _norm(q)
    s = _QUERY_NOISE_RE.sub("", s)
    return s.strip()


def _bigrams(s: str) -> set[str]:
    """双字连续片段（中文模糊匹配的最小可靠单元）。"""
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _tokenize(s: str) -> set[str]:
    """jieba 分词（≥2 字词；缺失时降级双字片段）。"""
    try:
        import jieba
        return {t for t in jieba.cut(s) if len(t.strip()) >= 2}
    except Exception:
        return _bigrams(s)


def _score_fuzzy(query: str, name: str) -> float:
    """rapidfuzz 综合打分（0~100）；缺失时降级 difflib。"""
    if _RAPIDFUZZ:
        return max(
            _rf_fuzz.ratio(query, name),
            _rf_fuzz.partial_ratio(query, name),
            _rf_fuzz.token_set_ratio(query, name),
        )
    # 降级：difflib（慢但可用）
    from difflib import SequenceMatcher
    return round(SequenceMatcher(None, query, name).ratio() * 100, 1)


class DisambiguationEngine:
    """四类结构化数据的模糊检索 + 候选选择作答。"""

    def __init__(
        self,
        persona_store: Optional[object] = None,
        enemy_store: Optional[object] = None,
        gift_store: Optional[object] = None,
        event_store: Optional[object] = None,
        top_k: int = 5,
        ask_threshold: float = 55.0,
        direct_threshold: float = 85.0,
        direct_gap: float = 10.0,
    ):
        self._stores = {
            "persona": persona_store,
            "enemy": enemy_store,
            "gift": gift_store,
            "event": event_store,
        }
        self.top_k = max(1, min(int(top_k), 9))
        self.ask_threshold = ask_threshold
        self.direct_threshold = direct_threshold
        self.direct_gap = direct_gap
        self._names: Optional[dict[str, list[str]]] = None  # kind → 名称列表（懒加载）
        self._token_stats: Optional[dict] = None            # 分词稀有度统计（懒加载）

    # ── 名称枚举（懒加载，复用各 store 的 search("") 全量列举） ──

    def _ensure_names(self) -> dict[str, list[str]]:
        if self._names is not None:
            return self._names
        names: dict[str, list[str]] = {}
        for kind, store in self._stores.items():
            if store is None:
                continue
            try:
                hits = store.search("")  # 空串包含匹配 = 全量列举
                names[kind] = [h for h in hits if h and _norm(h)]
            except Exception as e:
                logger.debug(f"模糊索引列举失败 {kind}: {e}")
                names[kind] = []
        self._names = names
        self._build_token_stats()
        total = sum(len(v) for v in names.values())
        logger.info(
            f"模糊搜索索引就绪: 人格{len(names.get('persona', []))} "
            f"敌方{len(names.get('enemy', []))} 饰品{len(names.get('gift', []))} "
            f"事件{len(names.get('event', []))}（共 {total} 项）"
        )
        return names

    def _build_token_stats(self):
        """双字片段 + 单字统计：片段 → 出现在多少名称里（df）。按稀有度分档加权——
        稀有片段（"炎拳""卯魁"）是判别力所在，常见片段（"事务""收尾""尾人"）
        权重极低，避免"XX事务所收尾人"这类尾巴雷同的候选靠常见词刷分。
        单字 df 供"跳字子序列"兜底使用（"月记"→"月之记忆"，缩写跳字匹配）。
        """
        names = self._names or {}
        df: dict[str, int] = {}
        name_tokens: dict[str, set[str]] = {}
        for items in names.values():
            for n in items:
                toks = _tokenize(n) | _bigrams(n)
                # 单字（中文/字母数字）加入 df：跳字兜底按字符稀有度加权
                for ch in n:
                    if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
                        toks.add(ch)
                name_tokens[n] = toks
                for t in toks:
                    df[t] = df.get(t, 0) + 1
        # 稀有度分档（比连续 idf 更锐利，避免小语料下常见词仍拿到高分）
        def _tier(c: int) -> float:
            if c <= 1:
                return 1.0
            if c <= 3:
                return 0.9
            if c <= 8:
                return 0.7
            if c <= 20:
                return 0.4
            if c <= 50:
                return 0.2
            return 0.1
        tier = {t: _tier(c) for t, c in df.items()}
        self._token_stats = {"df": df, "name_tokens": name_tokens, "tier": tier, "total": sum(len(v) for v in names.values())}

    # ── 检索 ────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: Optional[int] = None) -> list[dict]:
        """返回候选列表：[{kind, name, score, display}, ...]（按分数降序）。

        打分 = 查询片段覆盖率 × 100：
        对查询去噪后取全部双字片段，按稀有度分档加权（炎拳 1.0 / 管家 0.4 /
        收尾 0.2…），候选名称覆盖的片段权重之和 ÷ 查询片段总权重。
        为什么不用 rapidfuzz partial_ratio 为主：它对"XX事务所收尾人"这类
        尾巴雷同的名称给 85+ 分、对真目标"XX炎拳事务所幸存者"只给 62 分，
        基础分差距无法用加分弥补（"炎拳事务所收尾人的数据"实证）。
        覆盖率天然偏好"包含查询中稀有片段"的名称，尾巴雷同的噪声项权重极低。
        """
        q = (query or "").strip()
        if len(_norm(q)) < 2:
            return []
        if _LISTING_RE.search(q):
            return []
        # 去噪后的打分核心片段（"XX的数据" → "XX"）
        q_core = _strip_query_noise(q)
        if len(q_core) < 2:
            return []
        q_frags = _bigrams(q_core)

        names = self._ensure_names()  # 先建索引（含稀有度统计），再取 tier
        stats = self._token_stats or {}
        tier = stats.get("tier", {})
        q_total = sum(tier.get(b, 0.0) for b in q_frags)
        if q_total <= 0.0:
            # 连续双字片段完全无命中（缩写/跳字查询，如"月记"→"月之记忆"）：
            # 直接走跳字子序列兜底，而不是返回空让 RAG 泛化回复。
            return self._char_subseq_search(q_core, names, tier, q, top_k or self.top_k)

        top_k = top_k or self.top_k
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for kind, items in names.items():
            bonus = 2.0 if any(h in q for h in _KIND_HINTS.get(kind, ())) else 0.0
            for name in items:
                n = _norm(name)
                if not n:
                    continue
                # 覆盖率：查询稀有片段在名称中的覆盖比例
                covered = sum(tier.get(b, 0.0) for b in q_frags if b in n)
                score = 100.0 * covered / q_total
                if score < self.ask_threshold:
                    continue
                # 敌方泛化名重罚（"普通模式数据"这类不是真实实体名的记录）
                if kind == "enemy" and _GENERIC_NAME_PENALTY.search(n):
                    score -= 15.0
                    if score < self.ask_threshold:
                        continue
                score = min(100.0, score + bonus)
                if (kind, name) in seen:
                    continue
                seen.add((kind, name))
                results.append({
                    "kind": kind,
                    "name": name,
                    "score": round(score, 1),
                    "display": self._display_name(kind, name),
                })

        # 主覆盖率未命中时，跳字子序列兜底："月记"（月之记忆 的缩写，省掉"之"）
        # 连续双字匹配不到，但字符按顺序都出现在名称里 → 也应列为候选。
        if not results:
            results = self._char_subseq_search(q_core, names, tier, q, top_k)

        # 排序：覆盖率为主；覆盖率相同时用 rapidfuzz 相似度决胜（如"浮士德"
        # 命中多个浮士德人格时，名称更像查询的排前面）
        # ── 相关实体扩展（"卯魁首"歧义）：命中实体被其它可作答实体链接时，
        #    把链接方也列为候选（分数 -8）——"卯魁首"可能指人格也可能指敌方子路，
        #    必须同时列出让用户选，不能直接作答。 ──
        related = self._ensure_related()
        if related:
            name_sets = {kind: set(items) for kind, items in names.items()}
            result_keys = {(c["kind"], c["name"]) for c in results}
            extra: list[dict] = []
            for c in results:
                for src in related.get(c["name"], ()):
                    for kind2, name_set in name_sets.items():
                        if src in name_set and (kind2, src) not in result_keys:
                            extra.append({
                                "kind": kind2,
                                "name": src,
                                "score": round(max(0.0, c["score"] - _RELATED_SCORE_OFFSET), 1),
                                "display": self._display_name(kind2, src),
                            })
                            result_keys.add((kind2, src))
                            break
            results.extend(extra)

        # 排序：覆盖率为主；覆盖率相同时用 rapidfuzz 相似度决胜（如"浮士德"
        # 命中多个浮士德人格时，名称更像查询的排前面）
        results.sort(
            key=lambda x: (x["score"], _score_fuzzy(q_core, _norm(x["name"]))),
            reverse=True,
        )
        return results[:top_k]

    # ── 跳字子序列兜底（"月记" → "月之记忆"） ───────────────────────────

    # 兜底分数上限：始终低于直接作答阈值（85），保证缩写命中永远走"询问"让用户确认
    _CHAR_FALLBACK_CAP = 78.0

    def _char_subseq_search(
        self,
        q_core: str,
        names: dict[str, list[str]],
        tier: dict[str, float],
        q: str,
        top_k: int,
    ) -> list[dict]:
        """连续双字匹配落空时的跳字兜底：查询字符按**顺序**出现在名称中即可。

        场景：缩写/跳字查询——"月记"是饰品"月之记忆"的简称（省掉"之"），
        连续双字片段（月之/之记/记忆）匹配不到"月记"，但字符 月→记 在
        "月之记忆"中按顺序出现。按字符稀有度加权覆盖率打分（上限 78，必走询问）。

        噪声防护：
        - 至少 2 个查询字符命中且按顺序出现（子序列匹配）；
        - 单字符权重按稀有度分档（常见字"月"权重低、稀有字"记"权重高）；
        - 分数封顶 78 → 永不直接作答，仅作为候选列出。
        """
        q_chars = [c for c in q_core if c.isalnum() or "\u4e00" <= c <= "\u9fff"]
        if len(q_chars) < 2:
            return []
        q_total = sum(tier.get(c, 0.1) for c in q_chars)
        if q_total <= 0.0:
            return []

        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for kind, items in names.items():
            bonus = 2.0 if any(h in q for h in _KIND_HINTS.get(kind, ())) else 0.0
            for name in items:
                n = _norm(name)
                if not n:
                    continue
                # 子序列匹配（顺序可跳字）：月记 → 月之记忆
                idx = -1
                matched = 0
                weight = 0.0
                for c in q_chars:
                    j = n.find(c, idx + 1)
                    if j < 0:
                        break
                    idx = j
                    matched += 1
                    weight += tier.get(c, 0.1)
                if matched < 2:
                    continue
                score = 100.0 * weight / q_total
                if score < self.ask_threshold - 20:
                    continue
                score = min(self._CHAR_FALLBACK_CAP, score + bonus)
                if (kind, name) in seen:
                    continue
                seen.add((kind, name))
                results.append({
                    "kind": kind,
                    "name": name,
                    "score": round(score, 1),
                    "display": self._display_name(kind, name),
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    # ── 相关实体索引（wiki 页面"相关人格/相关敌方"链接，进程内懒加载） ──

    _related: Optional[dict[str, set[str]]] = None  # 目标名 → {来源名}

    @classmethod
    def _ensure_related(cls) -> dict[str, set[str]]:
        """从 wiki_pages.jsonl 的"相关人格/相关敌方"链接段构建反向索引。

        例：子路 页面含「相关人格 link：浮士德黑兽-卯魁首」
        → related["浮士德黑兽-卯魁首"] = {"子路"}（子路 是来源，可作答敌方）。
        """
        if cls._related is not None:
            return cls._related
        related: dict[str, set[str]] = {}
        if WIKI_JSONL.exists():
            for line in open(WIKI_JSONL, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                title = (d.get("title") or "").strip()
                content = d.get("content") or ""
                if not title or not content:
                    continue
                for m in _RELATED_SECTION_RE.finditer(content):
                    for im in _RELATED_ITEM_RE.finditer(m.group(0)):
                        target = im.group(1).strip()
                        if target:
                            related.setdefault(target, set()).add(title)
        cls._related = related
        logger.info(
            f"相关实体链接索引就绪: {sum(len(v) for v in related.values())} 条边"
            f"（如 子路→浮士德黑兽-卯魁首）"
        )
        return related

    @staticmethod
    def _display_name(kind: str, name: str) -> str:
        label = {"persona": "人格", "enemy": "敌方", "gift": "饰品", "event": "事件"}.get(kind, kind)
        return f"{label}｜{name}"

    # ── 展示 ────────────────────────────────────────────────────────────

    def format_choices(self, candidates: list[dict]) -> str:
        """生成带编号的候选清单（供用户回复数字选择）。"""
        if len(candidates) == 1:
            c = candidates[0]
            return f"你是指「{c['display']}」吗？回复 1 确认（或发送「取消」）。"
        lines = ["检测到多个可能的目标，请回复数字选择（或发送「取消」）："]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. {c['display']}")
        return "\n".join(lines)

    # ── 作答（确定性，绕过 LLM） ──────────────────────────────────────

    def answer(self, choice: dict) -> Optional[str]:
        """按候选 {kind, name} 从对应 store 取数并完整格式化。"""
        kind = choice.get("kind")
        name = choice.get("name")
        store = self._stores.get(kind)
        if store is None or not name:
            return None
        try:
            if kind == "persona":
                from rag.persona_direct import format_persona_full
                rec = store.get_persona(name)
                return format_persona_full(rec) if rec else None
            if kind == "enemy":
                from rag.enemy_direct import format_enemy_full
                recs = store.get_enemy(name)
                return format_enemy_full(recs) if recs else None
            if kind == "gift":
                from rag.gift_direct import format_gift_full
                recs = store.find_by_title(name)
                return format_gift_full(recs) if recs else None
            if kind == "event":
                from rag.event_direct import format_event_full
                rec = store.get_event(name)
                return format_event_full(rec) if rec else None
        except Exception as e:
            logger.warning(f"模糊消歧作答失败 {kind}｜{name}: {e}")
        return None


if __name__ == "__main__":
    # 独立验证：python -m rag.entity_disambiguation "卯魁首的数据"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from rag.persona_direct import PersonaDirectStore
    from rag.enemy_direct import EnemyDirectStore
    from rag.gift_direct import GiftDirectStore
    from rag.event_direct import EventDirectStore
    eng = DisambiguationEngine(
        persona_store=PersonaDirectStore(),
        enemy_store=EnemyDirectStore(),
        gift_store=GiftDirectStore(),
        event_store=EventDirectStore(),
    )
    q = sys.argv[1] if len(sys.argv) > 1 else "卯魁首"
    cands = eng.search(q)
    print(f"查询「{q}」→ {len(cands)} 个候选")
    for c in cands:
        print(f"  {c['score']:>5.1f}  {c['display']}")
