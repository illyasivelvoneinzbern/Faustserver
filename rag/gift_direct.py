# -*- coding: utf-8 -*-
"""
E.G.O 饰品结构化直答模块（Gift Direct Answer 运行时）。

绕开向量检索，直接从 ``data/structured/gift_*.json`` 精确取数，
按确定性规范格式输出（不经过 LLM、无幻觉）。用于根治
"饰品查询检索失效（page_type 三处不一致：accessory/ego_gift/ego）"问题。

组成：
- ``extract_gift_name``   从查询中锁定具体饰品名（精确 + 别名 + 模糊）
- ``GiftDirectStore``     运行时双索引（懒加载扫描 data/structured 目录）
- ``format_gift_full``    确定性格式化（基本信息 + 多版本效果文本）
- ``try_direct_answer``   查询入口：命中具体饰品名 → 直答文本；否则 None（回落 RAG）

依赖：``rag.query_processor`` 的 ``classify_intent``（is_listing 检测）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from crawler.structured_exporter import DEFAULT_OUT_DIR, load_gift_index
from rag.query_processor import classify_intent

logger = logging.getLogger(__name__)

# 版本显示名映射（stage 字段 → 中文标注）
# P38：tabx 的 desc_1/desc_2 才是真强化阶段（强化Ⅰ / 强化Ⅱ）；
# desc2/desc3 只是效果续段（已并入 base），不再产生 upgraded 版本。
_STAGE_LABELS: dict[str, str] = {
    "base": "未强化",
    "upgraded_2": "强化版·Ⅰ级",
    "upgraded_3": "强化版·Ⅱ级",
}
# 稀有度显示（rarity 0~6 → 星数）
_RARITY_STARS = ("☆", "★")
# 饰品名提取：从查询中剥离的噪声词（出现在饰品名周围但不属于名称）
_GIFT_NOISE_RE = re.compile(
    r"(饰品|效果|有什么|是什么|介绍|展示|查询|说说|讲讲|看下|给我|看看|的属性|是什么效果|技能|吗|呢|？|\?)"
)


def extract_gift_name(query: str) -> Optional[str]:
    """从查询中提取「具体饰品名」。

    匹配逻辑：
    1. 先精确匹配：若查询中直接出现某个完整饰品 title（长 title 优先），直接命中
    2. 模糊匹配：剥离去噪词后，查找索引中 title 与查询互为子串/包含的饰品
    3. 别名支持：调用方传入 title 索引，本函数做包含匹配

    Returns:
        匹配到的饰品 title（正式名称）；未识别到返回 None。
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None

    # 去掉常见噪声词后作为候选关键词
    cleaned = _GIFT_NOISE_RE.sub("", q).strip()
    if not cleaned:
        cleaned = q

    # 精确标题优先（长 title 优先，避免"血雾"被"血雾的叹息"等长名干扰——此处按索引精确匹配）
    # 说明：extract_gift_name 本身不持有索引，只做通用解析；
    # 精确/包含匹配由 GiftDirectStore.try_direct_answer 基于 title_index 完成。
    # 这里返回清洗后的关键词，供调用方二次匹配。
    return cleaned


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage or "未强化")


def _rarity_text(rarity) -> str:
    """稀有度 → "★★★（3）" 文本；无效值返回空。"""
    try:
        n = int(rarity)
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return ""
    n = min(n, 6)
    return f"{_RARITY_STARS[1] * n}（{n}）"


def _render_effect_lines(record: dict) -> list[str]:
    """渲染单条饰品记录的效果文本行（清洗后的 content）。"""
    content = record.get("content") or ""
    lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        lines = ["（暂无效果描述）"]
    return lines


def format_gift_full(records: list[dict]) -> str:
    """确定性格式化：将同 title 的多版本记录聚合为规范纯文本。

    输出字段：名称 / 稀有度 / 获取地点 / 效果类型 / 罪孽属性 / 经费 / 特殊 / 事件 /
    效果文本（按版本分节，base → 强化版·Ⅰ级 → 强化版·Ⅱ级）。

    Args:
        records: 同 title 的饰品记录列表（含多版本/多地点），按 base→upgraded 排序。
    """
    if not records:
        return ""
    base = records[0]

    lines: list[str] = []
    lines.append(f"【饰品】{base.get('title') or base.get('gift_name') or ''}")

    rarity_txt = _rarity_text(base.get("rarity"))
    if rarity_txt:
        lines.append(f"【稀有度】{rarity_txt}")

    if base.get("location"):
        lines.append(f"【获取地点】{base['location']}")
    if base.get("effect_types"):
        lines.append(f"【效果类型】{base['effect_types']}")
    if base.get("attack_type"):
        lines.append(f"【罪孽属性】{base['attack_type']}")
    if base.get("cost"):
        lines.append(f"【经费】{base['cost']}")
    if base.get("special"):
        lines.append(f"【特殊】{base['special']}")
    if base.get("event"):
        lines.append(f"【事件】{base['event']}")

    # 多版本效果分节
    has_multi_stage = len(records) > 1
    for i, rec in enumerate(records):
        stage = rec.get("stage") or "base"
        label = _stage_label(stage)
        if has_multi_stage:
            # 同 title 多地点（如 怀表：Type L）时，用地点区分布局
            loc = rec.get("location") or ""
            head = f"【效果（{label}）】" if not loc or loc == base.get("location") else f"【效果（{loc}·{label}）】"
            lines.append(head)
        else:
            lines.append("【效果】")
        lines.extend("　" + ln for ln in _render_effect_lines(rec))
        if i < len(records) - 1:
            lines.append("")

    return "\n".join(lines)


class GiftDirectStore:
    """运行时结构化饰品索引（懒加载 data/structured 目录）。

    用法（agent/core.py）：
        self.gift_direct = GiftDirectStore(
            data_dir=cfg.get("data_dir", "data/structured"),
            enabled=cfg.get("enabled", True),
        )
        direct = self.gift_direct.try_direct_answer(msg.text)
    """

    def __init__(self, data_dir: str = DEFAULT_OUT_DIR, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self._id_index: Optional[dict[str, dict]] = None
        self._title_index: Optional[dict[str, list[str]]] = None

    def _ensure_index(self) -> tuple[dict[str, dict], dict[str, list[str]]]:
        """懒加载：首次访问时扫描目录建立 {id: record} + {title: [id...]} 双索引。"""
        if self._id_index is None:
            self._id_index, self._title_index = load_gift_index(self.data_dir)
            if not self._id_index:
                logger.warning(
                    f"结构化饰品目录为空（{self.data_dir}），直答将自动失效并回落 RAG"
                )
        return self._id_index, self._title_index

    def reload(self):
        """重载索引（爬虫重建 data/structured 后调用）。"""
        self._id_index = None
        self._title_index = None
        self._ensure_index()

    def has_gift(self, title: str) -> bool:
        return title in self._ensure_index()[1]

    def get_gift(self, gift_id: str) -> Optional[dict]:
        return self._ensure_index()[0].get(gift_id)

    def find_by_title(self, title: str) -> list[dict]:
        """按 title 返回全部版本记录（多版本/多地点），base → upgraded 排序。"""
        id_idx, title_idx = self._ensure_index()
        ids = title_idx.get(title) or []
        records = []
        for gid in ids:
            rec = id_idx.get(gid)
            if rec:
                records.append(rec)
        # 排序：base 在前，upgraded_2 → upgraded_3（stage 字典序即 base < upgraded_2 < upgraded_3）
        records.sort(key=lambda r: r.get("stage") or "base")
        return records

    def search(self, name_like: str) -> list[str]:
        """前缀/包含模糊匹配（用于提示，非精确路径）。"""
        title_idx = self._ensure_index()[1]
        hits = [t for t in title_idx if name_like in t or t in name_like]
        return sorted(hits)

    def try_direct_answer(self, query: str) -> Optional[str]:
        """直答入口。

        1. 非启用 / 空查询 → None
        2. 穷举/列表查询（如"有哪些流血饰品"）→ None（避免误触发，回落 RAG 列表检索）
        3. extract_gift_name 锁定具体饰品名 → title_index 精确/包含匹配 →
           取全版本记录 → 确定性格式化
        4. 未命中 → None（回落 RAG）
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        # 穷举/列表查询不直答（"有哪些流血饰品" 等泛指）
        try:
            intent = classify_intent(q)
            if intent.get("is_listing"):
                logger.debug(f"饰品直答跳过（列表查询）: {q[:30]}")
                return None
            # ── 非饰品意图防劫持 ──
            # 当意图分类明确指向 人格/角色/敌方/事件 时，即使查询里包含某个
            # 饰品名（如 "人格希斯克利夫狐雨的数据" 含饰品 "狐雨"），也不应由
            # 饰品直答响应，避免把人格/角色查询劫持成饰品数据。
            pt = intent.get("page_type")
            if pt in ("personality", "character", "enemy", "event"):
                logger.debug(f"饰品直答跳过（意图={pt}，非饰品）: {q[:30]}")
                return None
        except Exception as e:
            logger.warning(f"classify_intent 异常，继续直答尝试: {e}")

        key = extract_gift_name(q)
        if not key:
            logger.debug(f"饰品直答未提取到饰品名，回落 RAG: {q[:30]}")
            return None

        # 精确 title 命中
        title_idx = self._ensure_index()[1]
        if key in title_idx:
            records = self.find_by_title(key)
            logger.info(f"饰品直答命中（精确）: {key}（{len(records)} 个版本）")
            return format_gift_full(records)

        # 包含匹配：key 是某 title 的子串 或 某 title 包含 key
        # （key 已剥离噪声词，如 "月之记忆" 精确命中；"月记" → 包含匹配）
        candidates: list[str] = []
        for title in title_idx:
            if key in title or title in key:
                candidates.append(title)
        if len(candidates) == 1:
            logger.info(f"饰品直答命中（包含匹配）: {key} → {candidates[0]}")
            return format_gift_full(self.find_by_title(candidates[0]))
        if len(candidates) > 1:
            # 多候选：优先完全相等（长度最短的 title 最可能是全名），否则不直答避免歧义
            exact = [c for c in candidates if c == key]
            if exact:
                logger.info(f"饰品直答命中（多候选取精确）: {key} → {exact[0]}")
                return format_gift_full(self.find_by_title(exact[0]))
            logger.debug(f"饰品直答多候选（{candidates}），回落 RAG 避免歧义: {key}")
            return None

        logger.debug(f"饰品直答未命中，回落 RAG: {key}")
        return None


if __name__ == "__main__":
    # 独立验证入口
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = GiftDirectStore()
    q = sys.argv[1] if len(sys.argv) > 1 else "月之记忆"
    out = store.try_direct_answer(q)
    if out:
        print(out)
    else:
        print("(未命中直答，应回落 RAG)")
