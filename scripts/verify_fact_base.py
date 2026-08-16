# -*- coding: utf-8 -*-
"""验证 opinion 意图的剧情事实底座（P29b 最终版）。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.enemy_direct import EnemyDirectStore
from rag.story_facts import build_story_fact_base
from agent.core import LimbusAgent

store = EnemyDirectStore()

print("=== opinion：剧情事实底座（不注入单位数值） ===")
for q in ["你怎么看里恩？", "你怎么看食指父辈里恩？"]:
    real = store.resolve_enemy(q)
    print(f"--- {q} ---")
    if real:
        bare, identity = LimbusAgent._enemy_identity(real)
        print(f"实体: {real} → 裸名 {bare}，身份 {identity}")
        fact = build_story_fact_base(bare, identity_note=identity)
        print(fact if fact else "（无剧情台词）")
    else:
        print("（未解析敌方实体）")
    print()

print("=== 罪人 opinion（你怎么看浮士德）===")
from rag.query_processor import LCB_SINNERS
for s in sorted(LCB_SINNERS, key=len, reverse=True):
    if s in "你怎么看浮士德":
        fact = build_story_fact_base(s, identity_note=f"边狱公司LCB罪人·{s}")
        print(fact[:400])
        break

print()
print("=== 游戏意图仍走数据（雷横该怎么打 → 直答数据） ===")
from rag.intent_gate import classify_user_intent
print("'雷横该怎么打？' intent =", classify_user_intent("雷横该怎么打？"))
print("'你怎么看里恩？' intent =", classify_user_intent("你怎么看里恩？"))
