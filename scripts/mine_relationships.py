# -*- coding: utf-8 -*-
"""挖掘每个罪人的剧情互动对象（人物关系数据源）。

对每个罪人（含但丁），扫描剧情对话序列，统计与之相邻对话的角色
（邻接互动计数 Top N），并抽取互动台词片段供撰写 relationships 参考。

输出：data/gacha/tmp/relationships_stats.txt
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import StoryFactIndex, _bare_role, _STAGE_RE

SINNERS = [
    "李箱", "浮士德", "堂吉诃德", "良秀", "默尔索", "鸿璐", "希斯克利夫",
    "以实玛利", "罗佳", "但丁", "辛克莱", "奥提斯", "格里高尔",
]
OUT = Path("data/gacha/tmp/relationships_stats.txt")


def main():
    StoryFactIndex._ensure()
    lines: list[str] = []
    for sinner in SINNERS:
        # 统计与 sinner 相邻对话的角色
        counter: Counter[str] = Counter()
        samples: dict[str, list[str]] = {}
        for title, seq in StoryFactIndex._pages.items():
            for i, (role, text) in enumerate(seq):
                if _bare_role(role) != sinner:
                    continue
                for j in range(max(0, i - 1), min(len(seq), i + 2)):
                    if j == i:
                        continue
                    orole = _bare_role(seq[j][0])
                    if not orole or orole == sinner:
                        continue
                    counter[orole] += 1
                    if len(samples.get(orole, [])) < 3:
                        samples.setdefault(orole, []).append(
                            f"（{title}）{role}：{text[:40]} / {seq[j][0]}：{seq[j][1][:40]}"
                        )
        lines.append(f"## {sinner} 剧情互动 Top 15")
        for name, cnt in counter.most_common(15):
            lines.append(f"- {name}：互动 {cnt} 次")
            for s in samples.get(name, []):
                lines.append(f"    {s}")
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成互动统计 -> {OUT}")


if __name__ == "__main__":
    main()
