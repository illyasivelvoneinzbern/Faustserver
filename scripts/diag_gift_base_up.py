# -*- coding: utf-8 -*-
"""对比 base 与 upgraded 内容，判断 desc2/desc3 是真强化还是效果分段。"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def strip_label(c: str) -> str:
    """去除行首 [名称][tagline] 与 镜牢经费 行。"""
    lines = c.split("\n")
    out = []
    for ln in lines:
        if "镜牢经费" in ln:
            continue
        out.append(ln)
    return "\n".join(out).strip()


print("=== 抽查：base 与 upgraded 内容对比（判断是否升级关系）===")
for title in ["怀表：Type L", "怀表：Type Y", "旋转木马模型", "复仇账簿：附录", "家人的怨恨", "中指规矩"]:
    recs = by_title.get(title, [])
    if not recs:
        continue
    print(f"\n--- {title} ---")
    for r in recs:
        st = r.get("stage", "?")
        c = strip_label(r.get("content") or "")
        print(f"  [{st}] {c[:150]}")
