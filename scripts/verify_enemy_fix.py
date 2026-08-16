# -*- coding: utf-8 -*-
"""P38 验证：重爬后敌方技能 coin_power/coin_count/attack_weight 异常统计 + 里恩样例。"""
import json
import sys
from pathlib import Path
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

enemies_dir = Path("data/structured/enemies")
bad = []
total = 0
power_hist = Counter()

for f in enemies_dir.glob("enemy_*.json"):
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    for sk in data.get("skills") or []:
        total += 1
        try:
            power = int(sk.get("coin_power") or 0)
            atk = int(sk.get("attack_level") or 0)
            count = int(sk.get("coin_count") or 0)
            wt = int(sk.get("attack_weight") or 0)
        except (TypeError, ValueError):
            continue
        power_hist[power] += 1
        if power >= 100 or (atk and power > 30 and str(power).endswith(str(atk)[-2:])):
            bad.append((data.get("enemy_name"), sk.get("skill_name"), count, power, atk))

print(f"总技能数: {total}")
print(f"coin_power top15: {power_hist.most_common(15)}")
print(f"疑似异常: {len(bad)} 条")
for b in bad[:20]:
    print("  ", b)

print("\n=== 里恩（第一阶段）样例 ===")
for f in enemies_dir.glob("*里恩*第一*"):
    data = json.loads(f.read_text(encoding="utf-8"))
    print(f"\n[{data.get('enemy_name')}] {data.get('battle_stage')}")
    for i, sk in enumerate(data.get("skills") or [], 1):
        print(f"  {i}. {sk.get('skill_name')} {sk.get('coin_count')}×{sk.get('coin_power')}"
              f" (atk={sk.get('attack_level')}, base={sk.get('base_value')}, wt={sk.get('attack_weight')},"
              f" guard={'Y' if sk.get('is_guard') else 'N'})")
