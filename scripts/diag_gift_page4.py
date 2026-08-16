# -*- coding: utf-8 -*-
"""dump 页面表0-9 完整效果链，确认强化段落结构。"""
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

for i, t in enumerate(mw.select("table")[:10]):
    rows = t.select("tr")
    name = rows[0].get_text(" ", strip=True)[:40]
    print(f"===== 表{i}: {name} =====")
    # 第一行剩余部分（标签/经费）+ 第二行效果
    for ri, tr in enumerate(rows):
        txt = tr.get_text("\n", strip=True)
        print(f"  [行{ri}] {txt[:300]}")
    print()
