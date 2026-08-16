# -*- coding: utf-8 -*-
"""验证剧情台词打分机制（P30）。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import build_story_fact_base, score_story_line

print("=== 打分机制示例 ===")
tests = [
    "我亲爱的女儿啊，近来可好？",
    "差不多该结束玩耍，到回家的时间了。",
    "……",
    "我是这所公司的首席研究员，霍恩海姆。",
    "浮士德认为，日光浴是一项有益活动。",
    "通常，担任助手的职位意味着其能力不如其所协助的研究员霍恩海姆。",
    "这是彼此认识后初次达成的一致意见。",
]
for t in tests:
    print(f"  {score_story_line(t, focus_role='浮士德'):+.2f}  {t[:45]}")

print()
print("=== 霍恩海姆 打分后剧情台词（focus_role=浮士德）===")
fact = build_story_fact_base(
    "霍恩海姆", identity_note="边狱公司首席研究员", max_lines=4, focus_role="浮士德"
)
print(fact)
