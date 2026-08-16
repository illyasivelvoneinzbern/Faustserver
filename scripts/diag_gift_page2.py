# -*- coding: utf-8 -*-
"""深入：E.G.O饰品 页面的强化标记上下文与表格结构。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

html = open(
    "example/E.G.O饰品 - 边狱公司中文维基 - 灰机wiki - 北京嘉闻杰诺网络科技有限公司.html",
    encoding="utf-8",
).read()
soup = BeautifulSoup(html, "lxml")
mw = soup.select_one(".mw-parser-output")

# 1) 每个表格的标题与首行
print("=== 11 个表格的结构 ===")
for i, t in enumerate(mw.select("table")):
    rows = t.select("tr")
    first = [c.get_text(strip=True)[:25] for c in rows[0].select("th, td")] if rows else []
    print(f"表{i}: 行数={len(rows)} 首行={first}")

# 2) "强化"出现的所有上下文（前 20 处）
print()
print("=== '强化' 上下文（前 20 处）===")
text = mw.get_text("\n", strip=True)
cnt = 0
for m in re.finditer(r"强化", text):
    s = max(0, m.start() - 60)
    ctx = text[s:m.end() + 60].replace("\n", " ")
    print(f"  ...{ctx}...")
    cnt += 1
    if cnt >= 20:
        break
