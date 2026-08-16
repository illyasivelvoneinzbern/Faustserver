# -*- coding: utf-8 -*-
"""离线验证：用修复后的 EnemyExtractor 处理保存的 9-50 HTML + wikitext，检查里恩第一阶段技能。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.html_extractor import EnemyExtractor

html = (Path("logs") / "diag_enemy_9-50.html").read_text(encoding="utf-8")
wikitext = (Path("logs") / "diag_enemy_9-50.wikitext").read_text(encoding="utf-8")

ext = EnemyExtractor(html, "主线战斗9-50", ["主线战斗"], wikitext=wikitext)
enemies = ext.extract()
print(f"提取到 {len(enemies)} 个单位")

target = None
for e in enemies:
    if "里恩" in e.enemy_name and "第一" in e.enemy_name:
        target = e
        break
if target is None:
    print("未找到里恩第一阶段！")
    sys.exit(1)

print(f"\n=== {target.enemy_name} ===")
print(f"HP={target.hp} 防御={target.defense_level} 速度={target.speed_min}~{target.speed_max}")
print(f"物理抗性={target.physical_resistances}")
print(f"罪孽抗性={target.sin_resistances}")

print(f"\n技能数: {len(target.skills)}")
for i, sk in enumerate(target.skills, 1):
    print(f"{i}. {sk.get('skill_name')} 【{sk.get('sin_type')}/{sk.get('damage_type')}】"
          f" {sk.get('coin_count')}×{sk.get('coin_power')}"
          f" (atk={sk.get('attack_level')}, base={sk.get('base_value')}, wt={sk.get('attack_weight')},"
          f" guard={'Y:' + str(sk.get('guard_type')) if sk.get('is_guard') else 'N'})")
    for ce in sk.get("coin_effects") or []:
        print(f"     · {ce}")
