# -*- coding: utf-8 -*-
"""查证：古良布洛/尼古莉娜/阿赖耶与良秀的剧情依据。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import StoryFactIndex

StoryFactIndex._ensure()

for name in ["古良布洛", "尼古莉娜"]:
    roles = [r for r in StoryFactIndex._full_index if name in r]
    lines = 0
    samples = []
    for title, seq in StoryFactIndex._pages.items():
        for role, text in seq:
            if name in text or name in role:
                lines += 1
                if len(samples) < 3:
                    samples.append(f"({title}) {role}:{text[:55]}")
    print(f"== {name}: role={roles} 台词={lines}次")
    for s in samples:
        print(f"   {s}")
    print()

# 良秀 ↔ 阿赖耶 互动（父女关系证据）
print("== 良秀 与 阿赖耶 相关互动 ==")
cnt = 0
for title, seq in StoryFactIndex._pages.items():
    for i, (role, text) in enumerate(seq):
        if "阿赖耶" in text or "阿赖耶" in role:
            print(f"({title}) {role}:{text[:60]}")
            cnt += 1
            if cnt >= 8:
                break
    if cnt >= 8:
        break
