# -*- coding: utf-8 -*-
"""细分异常来源：base=0 / power>=100 / 两者。"""
import json
import sys
from pathlib import Path
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

enemies_dir = Path("data/structured/enemies")
c = Counter()
sample = []
for f in enemies_dir.glob("enemy_*.json"):
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    for sk in data.get("skills") or []:
        try:
            power = int(sk.get("coin_power") or 0)
            base = int(sk.get("base_value") or 0)
            coins = int(sk.get("coin_count") or 0)
        except (TypeError, ValueError):
            continue
        if base == 0 and coins > 0:
            c["base0"] += 1
        if abs(power) >= 100:
            c["power100"] += 1
        if base == 0 and coins > 0:
            if len(sample) < 12:
                sample.append((data.get("enemy_name"), sk.get("skill_name"), coins, power, base))

print(dict(c))
print("\nbase=0 样例:")
for s in sample:
    print(" ", s)
