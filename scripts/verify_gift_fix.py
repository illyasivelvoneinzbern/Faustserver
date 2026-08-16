# -*- coding: utf-8 -*-
"""验证 P37 修复结果。"""
import json
import glob
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

stages = Counter()
up = []
for f in glob.glob("data/structured/gifts/*.json"):
    try:
        g = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    st = g.get("stage") or "(无)"
    stages[st] += 1
    if st in ("upgraded_2", "upgraded_3"):
        up.append((g.get("title"), st))

print("修复后 stage 分布:", dict(stages))
print("保留强化的饰品:", [f"{t}({s})" for t, s in up])
print()

for name in ["旋转木马模型", "奉纳的雪茄", "乌云", "怀表：Type L"]:
    found = []
    for f in glob.glob("data/structured/gifts/*.json"):
        try:
            g = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if g.get("title") == name:
            found.append(g)
    for g in found:
        c = g.get("content") or ""
        print(
            f"[{name}] stage={g.get('stage')} 长度={len(c)} "
            f"含'神父 格里高尔'={'神父 格里高尔' in c} 含'强化版'={'强化版' in c}"
        )
