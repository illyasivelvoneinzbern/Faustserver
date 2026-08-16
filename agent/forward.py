# -*- coding: utf-8 -*-
"""
直答打包转发模块（Forward Reply / 合并转发）。

当 Agent 命中结构化直答（人格 / 饰品 / 事件 / 敌方 / 比较直答，绕过 RAG）时，
把规范化的长文本按「节」拆分为多条转发 node，通过 NapCatQQ 的
``send_group_forward_msg`` / ``send_private_forward_msg`` 打包为**合并转发**
消息发送——QQ 端显示为一张可展开的转发卡片（每条 node 是一句气泡消息），
避免单条超长文本被 QQ 侧静默拒收（见 agent/core.py ``_SEND_CHUNK_MAX`` 症状2诊断）。

组成：
- ``AgentReply``              带可选打包转发分节的回复载体
- ``split_forward_sections``  纯函数：长文本 → 转发分节列表（按节标题/空行切分）

依赖：``adapter.napcat.build_forward_nodes`` 负责把分节拼成 OneBot node 段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# 节标题：方括号包裹的小节（如 【技能】【被动】【效果】【事件描述】【选项与判定】）
_SECTION_HEADER_RE = re.compile(r"^【[^】]*】")
# 人格直答的首行（（人格名）浮士德）也作为新节起点
_PERSONA_NAME_RE = re.compile(r"^（人格名）")
# 人格直答的分区标签（被动技能：/ 语音台词：/ 技能语音：）。
# 注意：战斗：/支援：是被动技能的子分组，不作为新节起点，避免碎片化节点。
_LABEL_HEADER_RE = re.compile(r"^(被动技能|语音台词|技能语音)：")
# 相邻短节合并阈值：小于该长度的相邻节合并为一个 node（防碎片化转发卡片）
_MIN_MERGE_CHARS = 120
# 不可合并的"大节"标题（技能/被动/效果等独立数据块），即使很短也单独成节。
# 仅元数据小段（饰品稀有度/获取地点、事件标题/触发地点等）参与相邻合并。
# 注意：【效果类型】/【效果】需区分：仅 `效果` 后紧跟 `】` 或 `（`（如
# 【效果】/【效果（未强化）】）才是大节；【效果类型】是饰品元数据，可合并。
_MAJOR_BLOCK_RE = re.compile(
    r"^【(技能|守备|强化|被动|效果(?:（|】)|事件描述|选项与判定|判定|语音台词|技能语音|关联异想体)"
)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """超长节按行边界硬切（单行超长时按字符切），保证每段 <= max_chars。"""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest.rstrip())
    return [c for c in chunks if c]


def split_forward_sections(text: str, max_chars: int = 1500) -> list[str]:
    """把直答规范文本拆分为转发分节列表（每节将作为合并转发的一条 node）。

    切分规则（确定性，不经过 LLM）：
    1. 行首为 ``【…】`` 的节标题（技能/被动/效果/事件描述等）开启新节；
    2. 人格直答首行 ``（人格名）…`` 与分区标签行（被动技能：等）开启新节；
    3. 空行也作为节边界（保留原有块结构）；
    4. 相邻的「短节」（如饰品各元数据行：稀有度/地点/效果类型…）自动合并，
       避免碎片化 node（小于 ``_MIN_MERGE_CHARS`` 的相邻节合并）；
    5. 单节超过 ``max_chars`` 字符时按行边界硬切，避免节点过长。

    Args:
        text: 直答规范文本（format_*_full 的输出）。
        max_chars: 单节点最大字符数（超出按行硬切）。

    Returns:
        分节列表（已去空节）；text 为空返回 []。
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    sections: list[str] = []
    cur: list[str] = []

    def flush():
        nonlocal cur
        if cur:
            sections.append("\n".join(cur))
            cur = []

    for ln in lines:
        s = ln.strip()
        if not s:
            # 空行：结束当前节（连续空行只产生一次边界）
            flush()
            continue
        if _SECTION_HEADER_RE.match(s) or _PERSONA_NAME_RE.match(s) or _LABEL_HEADER_RE.match(s):
            flush()
            cur.append(ln)
        else:
            cur.append(ln)
    flush()

    # 相邻短节合并（仅元数据小段；技能/被动/效果/分区标签等大节即使很短也独立成节）
    merged: list[str] = []
    for sec in sections:
        if not sec.strip():
            continue
        first = sec.strip().splitlines()[0] if sec.strip() else ""
        is_major = bool(
            _MAJOR_BLOCK_RE.match(first)
            or _LABEL_HEADER_RE.match(first)
            or _PERSONA_NAME_RE.match(first)
        )
        if (
            merged
            and not is_major
            and len(merged[-1]) < _MIN_MERGE_CHARS
            and len(sec) < _MIN_MERGE_CHARS
        ):
            merged[-1] = f"{merged[-1]}\n{sec}"
        else:
            merged.append(sec)

    # 超长节硬切
    out: list[str] = []
    for sec in merged:
        out.extend(_hard_split(sec, max_chars))
    return [s for s in out if s.strip()]


@dataclass
class AgentReply:
    """Agent 生成结果载体。

    - ``text``：实际回复文本（供敏感词过滤 / 会话记忆 / 普通发送兜底）。
    - ``forward_sections``：非 None 时表示该回复希望以合并转发（打包转发）发送，
      列表每项为一条转发 node 的文本内容；发送方按配置决定是否启用。
    """
    text: str
    forward_sections: Optional[list[str]] = None


if __name__ == "__main__":
    # 独立验证入口：python -m agent.forward
    import sys

    samples = [
        "【雷横】\n　关卡：5-15　HP：3000　防御等级：30　速度：3~5\n"
        "【被动】\n　· 灼烧：回合结束时…\n【技能】\n1. 斩击\n2. 突刺",
        "（人格名）浮士德\n罪人：浮士德\n罪孽亲和：色欲3 忧郁2\n\n"
        "【技能一】愚者\n攻击容量：1\n硬币：2\n\n【守备技能】护体\n\n"
        "被动技能：\n战斗：\n- 智库\n\n语音台词：\n- [问候] 你好",
    ]
    for t in samples:
        print("=" * 40)
        for i, sec in enumerate(split_forward_sections(t), 1):
            print(f"--- node {i} ({len(sec)} 字符) ---")
            print(sec)
