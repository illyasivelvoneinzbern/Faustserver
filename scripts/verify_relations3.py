# -*- coding: utf-8 -*-
"""补查：神父/理发师/里卡多↔希斯克利夫/东柏 在剧情中的互动依据。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import StoryFactIndex, _bare_role

StoryFactIndex._ensure()

# 1) 神父 / 理发师（堂吉诃德书角色，拉曼却领剧情?）
for name in ["神父", "理发师"]:
    roles = [r for r in StoryFactIndex._full_index if name in r]
    lines = 0
    samples = []
    for title, seq in StoryFactIndex._pages.items():
        for role, text in seq:
            if name in text:
                lines += 1
                if len(samples) < 2:
                    samples.append(f"({title}){role}:{text[:55]}")
    print(f"== {name}: role={roles} 台词={lines}次")
    for s in samples:
        print(f"   {s}")
    print()

# 2) 里卡多 ↔ 希斯克利夫 剧情互动
print("== 里卡多 剧情台词（5章中指）==")
for title, seq in StoryFactIndex._pages.items():
    for role, text in seq:
        if _bare_role(role) == "里卡多" and len(text.strip()) > 6:
            print(f"({title}) {role}:{text[:70]}")
print()

# 3) 东柏 在剧情中的任何痕迹（含 9 章李箱相关）
print("== 东柏/李箱 9章相关 ==")
hits = 0
for title, seq in StoryFactIndex._pages.items():
    for role, text in seq:
        if "东柏" in text or "东柏" in role:
            hits += 1
            print(f"({title}) {role}:{text[:60]}")
if not hits:
    print("（剧情数据中无『东柏』记录）")
