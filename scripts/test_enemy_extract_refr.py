# -*- coding: utf-8 -*-
"""离线验证：折射轨道6号线-第一区段 敌方技能提取（罗生蝶::蛹）。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.html_extractor import EnemyExtractor

base = Path("example") / "折射轨道6号线-第一区段 - 边狱公司中文维基 - 灰机wiki - 北京嘉闻杰诺网络科技有限公司"
html = (base.with_suffix(".html")).read_text(encoding="utf-8")
wikitext = (Path("example") / "折射轨道6号线-第一区段.txt").read_text(encoding="utf-8")

ext = EnemyExtractor(html, "折射轨道6号线-第一区段", ["折射轨道"], wikitext=wikitext)
enemies = ext.extract()
print(f"提取到 {len(enemies)} 个单位")
for e in enemies[:2]:
    print(f"\n=== {e.enemy_name} ===")
    for i, sk in enumerate(e.skills, 1):
        print(f"{i}. {sk.get('skill_name')} 【{sk.get('sin_type')}/{sk.get('damage_type')}】"
              f" {sk.get('coin_count')}×{sk.get('coin_power')}"
              f" (atk={sk.get('attack_level')}, base={sk.get('base_value')}, wt={sk.get('attack_weight')})")
        for ce in (sk.get("coin_effects") or [])[:4]:
            print(f"     · {ce}")
