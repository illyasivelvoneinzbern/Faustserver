# -*- coding: utf-8 -*-
"""诊断：对比 base 与 upgraded 记录，找被'2级'误拆/误判强化的饰品。"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 加载所有饰品记录（按 title 分组）
by_title: dict[str, list[dict]] = {}
for line in open("data/raw/wiki_accessories.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    by_title.setdefault(r.get("title", ""), []).append(r)

# 1) 找出 base 内容疑似被'2级'截断的：base 以"2级"或"2级:"结尾（后半被拆走）
print("=== 疑似误拆：base 记录内容被'2级'截断 ===")
cut_off = []
for title, recs in by_title.items():
    bases = [r for r in recs if r.get("stage") == "base"]
    ups = [r for r in recs if r.get("stage", "").startswith("upgraded")]
    if not ups or not bases:
        continue
    for b in bases:
        c = (b.get("content") or "").strip()
        # 内容以 2级 开头或 base 很短（<30字）且存在 upgraded —— 可能被拆
        if re.search(r"2级[：:波次]?\s*$", c) or len(c) < 30:
            cut_off.append((title, len(c), c[-40:], len(ups)))
for t, ln, tail, nu in cut_off[:30]:
    print(f"  [{t}] base长度={ln} 结尾={tail!r} upgraded数={nu}")

print()
# 2) 列出有 upgraded 的饰品中 base 内容很短（<20字，疑似不完整）的
print("=== upgraded 饰品中 base 内容过短（<20字，疑似被截断）===")
for title, recs in by_title.items():
    bases = [r for r in recs if r.get("stage") == "base"]
    ups = [r for r in recs if r.get("stage", "").startswith("upgraded")]
    if not ups or not bases:
        continue
    for b in bases:
        c = (b.get("content") or "").strip()
        if len(c) < 20:
            print(f"  [{title}] base={c!r}")

print()
# 3) 统计：有 upgraded 的饰品总数 vs 无 upgraded 的
with_up = sum(1 for recs in by_title.values() if any(r.get("stage", "").startswith("upgraded") for r in recs))
print(f"有 upgraded 记录的饰品数: {with_up} / {len(by_title)}")
