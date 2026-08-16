# -*- coding: utf-8 -*-
"""意图门控（Intent Gate）：区分 观点 / 比较 / 数据 / 其他 四类用户意图。

背景（改进计划 1.1/3.1）：
- 直答拦截（persona/gift/event/enemy direct answer）只按实体名命中即返回数据表，
  导致观点类问题（如「你觉得雷横这个人怎么样」）被数据表格劫持，无法发表人设看法。
- 本模块在直答链**之前**做确定性意图分类（零 LLM 成本）：

    opinion（观点/评价） → 跳过全部直答，走人格扮演链路（目标 3）
    compare（比较）      → 尝试比较直答（try_compare_answer），否则 RAG
    data（数据查询）     → 正常直答链（目标 1）
    other（闲聊/剧情等） → 正常链路

判定优先级：强观点词 > 比较词 > 弱观点词（需无数据强词）> 数据词 > 其他。
"""
from __future__ import annotations

import re

# ── 强观点词：命中几乎必为「看法/评价」类问题 ──
_STRONG_OPINION_WORDS = (
    "你觉得", "你怎么看", "评价一下", "如何评价", "你的看法", "你的意见",
    "你的评价", "感想", "欣赏", "好感", "怎么看", "你认为", "你是怎么看",
    "喜欢", "讨厌", "厌恶", "喜爱", "偏爱", "介意", "在意",
)

# ── 弱观点词：需与「无数据强词」组合才判 opinion（避免误伤数据查询）──
_WEAK_OPINION_WORDS = (
    "怎么样", "觉得", "认为", "好不好", "如何", "怎么样啊", "咋样", "咋样啊",
)

# ── 比较词：命中且含两个以上实体时判 compare ──
_COMPARE_WORDS = (
    "谁更强", "谁强", "谁厉害", "哪个强", "哪个更强", "谁更厉害", "谁更痛",
    "对比", "区别", "比较", "孰强", "哪个好", "谁好", "谁更", "比一比",
    "和谁", "与谁", "vs", "VS",
)

# ── 数据意图强词：命中说明用户在要具体数据 ──
_DATA_WORDS = (
    "技能", "数据", "数值", "抗性", "被动", "效果", "硬币", "基础值",
    "变动值", "攻击容量", "血量", "hp", "防御", "速度", "关卡", "敌人",
    "boss", "BOSS", "人格", "EGO", "ego", "饰品", "事件", "属性", "伤害",
    "奖励", "掉落", "等级", "资源", "阶段", "混乱", "恐慌", "弱化", "强化",
    "敌人数据", "单位数据",
)


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def classify_user_intent(text: str) -> str:
    """对用户消息做确定性意图分类。

    Returns:
        "opinion" | "compare" | "data" | "other"
    """
    t = (text or "").strip()
    if not t:
        return "other"

    # 1) 强观点词 → opinion（最高优先：如「你觉得雷横这个人怎么样」）
    if _contains_any(t, _STRONG_OPINION_WORDS):
        return "opinion"

    # 2) 比较词 → compare（需有比较语义，如「兔浮和W浮谁更强」）
    if _contains_any(t, _COMPARE_WORDS):
        return "compare"

    # 3) 弱观点词 + 无数据强词 → opinion（如「这个人怎么样」「这技能觉得如何」）
    if _contains_any(t, _WEAK_OPINION_WORDS) and not _contains_any(t, _DATA_WORDS):
        return "opinion"

    # 4) 数据意图词 → data（如「兔浮的技能」「雷横的数据」）
    if _contains_any(t, _DATA_WORDS):
        return "data"

    return "other"


# ── 便于测试的公开词表 ──
OPINION_WORDS = _STRONG_OPINION_WORDS + _WEAK_OPINION_WORDS
COMPARE_WORDS = _COMPARE_WORDS
DATA_WORDS = _DATA_WORDS
