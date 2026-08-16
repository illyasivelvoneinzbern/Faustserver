# -*- coding: utf-8 -*-
"""进一步查证：辛德雷/盐见夜/东柏的别名，及桑丘/里卡多与目标罪人的互动。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import StoryFactIndex, _bare_role

StoryFactIndex._ensure()

# 别名查证
for name in ["亨德利", "盐见", "冬柏", "山茶花", "東柏", "东柏"]:
    roles = [r for r in StoryFactIndex._full_index if name in r]
    lines = 0
    for title, seq in StoryFactIndex._pages.items():
        for role, text in seq:
            if name in text:
                lines += 1
    print(f"== {name}: role={roles} 台词={lines} 次")

print()
# 桑丘 与 堂吉诃德 的互动
print("=== 桑丘 ↔ 堂吉诃德 互动样本 ===")
for title, seq in StoryFactIndex._pages.items():
    for i, (role, text) in enumerate(seq):
        if _bare_role(role) in ("桑丘", "堂吉诃德") and len(text.strip()) > 8:
            # 打印相邻块含对方的
            for j in range(max(0, i - 1), min(len(seq), i + 2)):
                if j == i:
                    continue
                if _bare_role(seq[j][0]) in ("桑丘", "堂吉诃德"):
                    print(f"({title}) {role}:{text[:55]}")
                    print(f"     {seq[j][0]}:{seq[j][1][:55]}")
                    break
            else:
                continue
            if sum(1 for _ in [1]) > 3:
                pass

print()
# 里卡多 ↔ 希斯克利夫 互动
print("=== 里卡多 ↔ 希斯克利夫 互动样本 ===")
cnt = 0
for title, seq in StoryFactIndex._pages.items():
    for i, (role, text) in enumerate(seq):
        if _bare_role(role) in ("里卡多", "希斯克利夫") and len(text.strip()) > 6:
            for j in range(max(0, i - 2), min(len(seq), i + 3)):
                if j == i:
                    continue
                if _bare_role(seq[j][0]) in ("里卡多", "希斯克利夫"):
                    print(f"({title}) {role}:{text[:60]}")
                    print(f"     {seq[j][0]}:{seq[j][1][:60]}")
                    cnt += 1
                    break
            if cnt >= 8:
                break
    if cnt >= 8:
        break
