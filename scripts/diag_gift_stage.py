# -*- coding: utf-8 -*-
"""分析饰品强化状态：列出 upgraded_2/upgraded_3 记录，排查误判。"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

up2 = []
up3 = []
base_titles = set()
for line in open("data/raw/wiki_accessories.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    st = r.get("stage")
    if st == "upgraded_2":
        up2.append(r)
    elif st == "upgraded_3":
        up3.append(r)
    elif st == "base":
        base_titles.add(r.get("title"))

print(f"upgraded_2: {len(up2)} | upgraded_3: {len(up3)} | base: {len(base_titles)}")
print()

print("=== upgraded_2 记录（title + 内容前 80 字）===")
for r in up2:
    c = (r.get("content") or "").replace("\n", " ")[:80]
    print(f"  [{r.get('title')}] {c}")

print()
print("=== upgraded_3 记录 ===")
for r in up3:
    c = (r.get("content") or "").replace("\n", " ")[:80]
    print(f"  [{r.get('title')}] {c}")

print()
print("=== 疑似误判：upgraded 但内容与 base 几乎相同 或 含'2级'字样（非强化标记）===")
# 加载 tabx 原始 desc（从 gift JSON 的 base 对照）——这里直接检查 content 里是否含"2级"（强化标记应为 2级：/2级波次）
suspicious = []
for r in up2 + up3:
    c = r.get("content") or ""
    # 强化版内容应含 stage_label（强化版·Ⅱ级）或与 base 不同
    if "强化版" not in c:
        suspicious.append((r.get("title"), r.get("stage"), c[:60]))
for t, st, c in suspicious:
    print(f"  [{t}] {st}: {c}")
