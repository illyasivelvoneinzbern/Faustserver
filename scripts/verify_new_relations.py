# -*- coding: utf-8 -*-
"""验证新增的人物关系全部加载并注入。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personas.manager import PersonaManager

m = PersonaManager()
loaded = m.load_all()
print("加载人格数:", len(loaded))

checks = {
    "don_quixote": ["桑丘（作为桑丘的变化）", "堂吉诃德（父亲）", "杜尔西内娅", "神父与理发师", "卡塞蒂"],
    "heathcliff": ["亨德利（辛德雷）", "林顿", "里卡多"],
    "honglu": ["贾母", "贾元春"],
    "ishmael": ["魁魁格"],
    "ryoshu": ["阿赖耶", "瓦伦希娜", "卡利斯托", "马蒂亚斯", "盐见"],
    "yisang": ["东柏"],
}
ok = True
for pid, names in checks.items():
    rels = loaded[pid].get("relationships") or {}
    for n in names:
        has = n in rels
        if not has:
            ok = False
        mark = "✓" if has else "✗ 缺失!"
        print(f"  {pid}: {n} -> {mark}")

print()
p = m.build_system_prompt("ryoshu")
print("良秀 prompt 含 盐见:", "盐见" in p)
p2 = m.build_system_prompt("don_quixote")
print("堂吉诃德 prompt 含 桑丘/卡塞蒂:", "桑丘" in p2 and "卡塞蒂" in p2)
print()
print("ALL OK" if ok else "有缺失")
