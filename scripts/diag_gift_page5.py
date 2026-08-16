# -*- coding: utf-8 -*-
"""查看表0-9 生成上下文 + 倒错症/尘归尘完整效果。"""
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

# 表0 前文（前一个元素/文本）
tables = mw.select("table")
t0 = tables[0]
prev = t0.find_previous()
print("表0 前一个元素:", prev.name if prev else None, "| 文本:", (prev.get_text(strip=True)[:80] if prev else ""))
# 表0 与 表10 之间的文本
t10 = tables[10]
between = []
node = t0.next_sibling
while node is not None and node != t10:
    if hasattr(node, "get_text"):
        txt = node.get_text(" ", strip=True)
        if txt:
            between.append(txt[:60])
    node = node.next_sibling
print("表0~表10 之间的文本:", between[:10])
print()

# 倒错症 与 尘归尘 完整效果
for i in (1, 2):
    rows = tables[i].select("tr")
    print(f"===== 表{i}: {rows[0].get_text(' ', strip=True)[:40]} 完整效果 =====")
    if len(rows) > 1:
        print(rows[1].get_text("\n", strip=True))
    print()
