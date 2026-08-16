# -*- coding: utf-8 -*-
"""
探索事件结构化直答模块（Event Direct Answer 运行时）。

绕开向量检索，直接从 ``data/structured/events/event_*.json`` 精确取数，
按确定性规范格式输出（不经过 LLM、无幻觉）。用于根治
"事件查询检索失效 / 检索召回片段化"问题，提供完整事件正文。

组成：
- ``extract_event_name``  从查询中锁定具体事件名（去"事件-"前缀 + 剥噪词）
- ``EventDirectStore``    运行时索引（懒加载扫描 data/structured/events 目录）
- ``format_event_full``   确定性格式化（标题 / 触发地点 / 关联异想体 / 描述 / 选项与判定）
- ``try_direct_answer``   查询入口：命中具体事件名 → 直答文本；否则 None（回落 RAG）

依赖：``rag.query_processor`` 的 ``classify_intent``（is_listing 检测）。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from crawler.structured_exporter import DEFAULT_OUT_DIR, load_event_index
from rag.query_processor import classify_intent

logger = logging.getLogger(__name__)

# 事件名提取：从查询中剥离的噪声词（出现在事件名周围但不属于名称）
# 含"事件"前缀本身与常见问句词，与 gift_direct 的 _GIFT_NOISE_RE 对齐。
_EVENT_NOISE_RE = re.compile(
    r"(事件|触发|地点|有什么|是什么|介绍|展示|查询|说说|讲讲|看下|给我|看看"
    r"|怎么样|怎么触发|在哪儿|在哪|哪里|吗|呢|？|\?)"
)


def extract_event_name(query: str) -> Optional[str]:
    """从查询中提取「具体事件名」候选关键词。

    匹配逻辑：
    1. 去掉 "事件-" 前缀与常见噪声词，得到候选关键词（如 "1.76兆赫事件是什么" → "1.76兆赫"）
    2. 精确/包含匹配由 ``EventDirectStore.try_direct_answer`` 基于索引完成（本函数不持有索引）

    Returns:
        清洗后的候选关键词；无法提取返回 None。
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None

    # 去掉 "事件-" 前缀（title 形态：事件-1.76兆赫）
    q = re.sub(r"^事件[\-－—]", "", q)
    # 剥离去噪词后作为候选关键词
    cleaned = _EVENT_NOISE_RE.sub("", q).strip()
    if not cleaned:
        cleaned = q
    return cleaned


def _render_option_lines(opt: dict) -> list[str]:
    """渲染单个选项的判定信息行（无判定时返回空列表）。

    判定形态：check_type（如"有利判定"）+ check_sin（已去 .png）+ check_threshold。
    阈值 0 表示未设定，省略。
    """
    if not isinstance(opt, dict):
        return []
    lines: list[str] = []
    check_type = (opt.get("check_type") or "").strip()
    check_sin = (opt.get("check_sin") or "").strip()
    try:
        threshold = int(opt.get("check_threshold") or 0)
    except (TypeError, ValueError):
        threshold = 0
    if check_type or check_sin:
        head = check_type or "判定"
        parts = [head]
        if check_sin:
            parts.append(check_sin)
        if threshold:
            parts.append(f"≥ {threshold}")
        lines.append(f"　【{' '.join(parts)}】")
    for tag, outcomes in (("成功", opt.get("success_outcomes")), ("失败", opt.get("failure_outcomes"))):
        for line in outcomes or []:
            line = str(line).strip()
            if line:
                lines.append(f"　· {tag}：{line}")
    return lines


def _render_ego_gifts(gifts) -> list[str]:
    """降级渲染 ego_gifts（源数据 name/effect 字段错位，仅单行文本展示）。"""
    lines: list[str] = []
    for g in gifts or []:
        if isinstance(g, dict):
            # 字段错位：name/effect 实为两段效果文本，直接取任一非空值展示
            text = (g.get("name") or g.get("effect") or "").strip()
            if text:
                lines.append(f"　- {text}")
        elif isinstance(g, str) and g.strip():
            lines.append(f"　- {g.strip()}")
    return lines


def format_event_full(record: dict) -> str:
    """确定性格式化：将单条事件记录输出为规范纯文本。

    输出字段：事件名 / 标题 / 触发地点 / 关联异想体 / 事件描述 / 选项与判定 / E.G.O饰品。

    空占位事件（narration 与 options 均空）追加提示，避免误导用户。
    """
    if not isinstance(record, dict):
        return ""
    name = record.get("event_name") or record.get("title") or ""
    title = record.get("title") or ""

    lines: list[str] = []
    lines.append(f"【事件】{name}")
    if title and title != name:
        lines.append(f"【标题】{title}")

    loc = (record.get("trigger_location") or "").strip()
    if loc:
        lines.append(f"【触发地点】{loc}")

    abnos = record.get("related_abnormalities") or []
    abnos = [a for a in abnos if isinstance(a, str) and a.strip()]
    if abnos:
        lines.append(f"【关联异想体】{'、'.join(abnos)}")

    narration = (record.get("narration") or "").strip()
    if narration:
        lines.append("【事件描述】")
        lines.extend(f"　{ln.rstrip()}" for ln in narration.splitlines() if ln.strip())

    options = record.get("options") or []
    valid_options = [o for o in options if isinstance(o, dict) and (o.get("choice_text") or "").strip()]
    if valid_options:
        lines.append("【选项与判定】")
        for i, opt in enumerate(valid_options, 1):
            lines.append(f"{i}. {opt['choice_text'].strip()}")
            lines.extend(_render_option_lines(opt))

    # 说明：E.G.O 饰品已有独立 JSON（data/structured/gifts/），事件直答输出到
    # 所有选项结束后即止，不再重复渲染事件内嵌的 ego_gifts 段。
    # （保留 _render_ego_gifts 供需要时单独使用。）

    # 空占位事件提示
    if not narration and not valid_options:
        lines.append("（注：该事件暂无详细数据，可能为未完善页面。）")

    return "\n".join(lines)


class EventDirectStore:
    """运行时结构化事件索引（懒加载 data/structured/events 目录）。

    用法（agent/core.py）：
        self.event_direct = EventDirectStore(
            data_dir=cfg.get("data_dir", "data/structured"),
            enabled=cfg.get("enabled", True),
        )
        direct = self.event_direct.try_direct_answer(msg.text)
    """

    def __init__(self, data_dir: str = DEFAULT_OUT_DIR, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self._name_index: Optional[dict[str, dict]] = None

    def _ensure_index(self) -> dict[str, dict]:
        """懒加载：首次访问时扫描目录建立 {event_name: record} 索引。"""
        if self._name_index is None:
            self._name_index = load_event_index(self.data_dir)
            if not self._name_index:
                logger.warning(
                    f"结构化事件目录为空（{self.data_dir}），直答将自动失效并回落 RAG"
                )
        return self._name_index

    def reload(self):
        """重载索引（爬虫重建 data/structured 后调用）。"""
        self._name_index = None
        self._ensure_index()

    def has_event(self, name: str) -> bool:
        return name in self._ensure_index()

    def get_event(self, name: str) -> Optional[dict]:
        return self._ensure_index().get(name)

    def find_by_name(self, name: str) -> Optional[dict]:
        """按 event_name（裸名）精确取记录；title（带"事件-"前缀）兜底。"""
        idx = self._ensure_index()
        rec = idx.get(name)
        if rec:
            return rec
        # title 兜底：查询带 "事件-" 前缀时映射到裸名
        for key, rec in idx.items():
            if rec.get("title") == name:
                return rec
        return None

    def search(self, name_like: str) -> list[str]:
        """包含模糊匹配（用于提示，非精确路径）。"""
        idx = self._ensure_index()
        hits = [n for n in idx if name_like in n or n in name_like]
        return sorted(hits)

    def try_direct_answer(self, query: str) -> Optional[str]:
        """直答入口。

        1. 非启用 / 空查询 → None
        2. 穷举/列表查询（如"有哪些事件"）→ None（避免误触发，回落 RAG 列表检索）
        3. extract_event_name 锁定候选事件名 → event_name 精确/包含匹配 → 确定性格式化
        4. 未命中 → None（回落 RAG）
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        # 穷举/列表查询不直答（"有哪些事件" 等泛指）
        try:
            intent = classify_intent(q)
            if intent.get("is_listing"):
                logger.debug(f"事件直答跳过（列表查询）: {q[:30]}")
                return None
        except Exception as e:
            logger.warning(f"classify_intent 异常，继续直答尝试: {e}")

        key = extract_event_name(q)
        if not key:
            logger.debug(f"事件直答未提取到事件名，回落 RAG: {q[:30]}")
            return None

        # 精确 event_name 命中
        idx = self._ensure_index()
        if key in idx:
            logger.info(f"事件直答命中（精确）: {key}")
            return format_event_full(idx[key])

        # 最小长度保护：过短关键词（单字符/极短）不做包含匹配，避免模糊误命中。
        # 与 gift 直答"月之"等过短模糊查询回落 RAG 的行为对齐。
        if len(key) < 2:
            logger.debug(f"事件直答关键词过短，回落 RAG: {key}")
            return None

        # 包含匹配：key 是某事件名的子串 或 某事件名包含 key
        candidates: list[str] = []
        for name in idx:
            if key in name or name in key:
                candidates.append(name)
        if len(candidates) == 1:
            logger.info(f"事件直答命中（包含匹配）: {key} → {candidates[0]}")
            return format_event_full(idx[candidates[0]])
        if len(candidates) > 1:
            # 多候选：优先完全相等（长度最短的 name 最可能是全名），否则不直答避免歧义
            exact = [c for c in candidates if c == key]
            if exact:
                logger.info(f"事件直答命中（多候选取精确）: {key} → {exact[0]}")
                return format_event_full(idx[exact[0]])
            logger.debug(f"事件直答多候选（{candidates}），回落 RAG 避免歧义: {key}")
            return None

        logger.debug(f"事件直答未命中，回落 RAG: {key}")
        return None


if __name__ == "__main__":
    # 独立验证入口：python -m rag.event_direct 1.76兆赫
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = EventDirectStore()
    q = sys.argv[1] if len(sys.argv) > 1 else "1.76兆赫"
    out = store.try_direct_answer(q)
    if out:
        print(out)
    else:
        print("(未命中直答，应回落 RAG)")
