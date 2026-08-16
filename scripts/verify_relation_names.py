# -*- coding: utf-8 -*-
"""查证用户要求增补的角色名是否存在于剧情数据，及其与目标罪人的互动。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.story_facts import StoryFactIndex

# 需要查证的名字 → (目标罪人, 备注)
NAMES = [
    ("桑丘", "堂吉诃德", "堂吉诃德书中侍从/6.5章?"),
    ("卡塞蒂", "堂吉诃德", "6.5章(仲春夜之梦)角色"),
    ("杜尔西内娅", "堂吉诃德", "书中爱慕对象"),
    ("辛德雷", "希斯克利夫", "呼啸山庄养兄"),
    ("林顿", "希斯克利夫", "呼啸山庄林顿家族"),
    ("里卡多", "希斯克利夫", "?"),
    ("贾母", "鸿璐", "红楼梦贾母"),
    ("贾元春", "鸿璐", "红楼梦元春"),
    ("魁魁格", "以实玛利", "白鲸记同伴"),
    ("阿赖耶", "良秀", "?"),
    ("瓦伦希娜", "良秀", "?"),
    ("盐见夜", "良秀", "?"),
    ("东柏", "李箱", "?"),
]

StoryFactIndex._ensure()

for name, sinner, note in NAMES:
    # 剧情中出现次数（作为 role 或台词内容）
    role_hits = [r for r in StoryFactIndex._full_index if name in r]
    line_hits = 0
    samples = []
    for title, seq in StoryFactIndex._pages.items():
        for role, text in seq:
            if name in text:
                line_hits += 1
                if len(samples) < 2:
                    samples.append(f"({title}){role}:{text[:50]}")
    print(f"== {name}（目标:{sinner}，{note}）")
    print(f"   作为 role 出现: {role_hits}")
    print(f"   台词中提到: {line_hits} 次")
    for s in samples:
        print(f"     {s}")
    print()
