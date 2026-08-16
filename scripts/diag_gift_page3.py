# -*- coding: utf-8 -*-
"""dump E.G.O饰品 页面表0-9（强化展示）与表10 结构。"""
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

print("=== 表0-9（前 10 个表格，疑似强化饰品展示）完整内容 ===")
for i, t in enumerate(mw.select("table")[:10]):
    rows = t.select("tr")
    name = rows[0].get_text(" ", strip=True)[:60] if rows else "?"
    # 第二行 = 效果
    eff = rows[1].get_text(" ", strip=True)[:150] if len(rows) > 1 else ""
    print(f"表{i}: [{name}]")
    print(f"     效果: {eff}")
print()

print("=== 表10 前 8 行（完整列表）===")
t10 = mw.select("table")[10]
for tr in t10.select("tr")[:8]:
    cells = [c.get_text(" ", strip=True)[:40] for c in tr.select("th, td")]
    print("  ", cells)

print()
print("=== 表10 行数 & 是否含'升级'列 ===")
rows10 = t10.select("tr")
print(f"表10 行数: {len(rows10)}")
head = [c.get_text(strip=True) for c in rows10[0].select("th, td")]
print("表10 列头:", head)
