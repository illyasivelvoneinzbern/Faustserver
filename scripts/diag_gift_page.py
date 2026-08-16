# -*- coding: utf-8 -*-
"""分析 E.G.O饰品 页面：提取饰品列表结构与强化标记。"""
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
print("HTML 长度:", len(html))

soup = BeautifulSoup(html, "lxml")
mw = soup.select_one(".mw-parser-output")
if not mw:
    print("无 .mw-parser-output")
    sys.exit(1)

# 1) 页面中的"强化"字样出现处
text = mw.get_text(" ", strip=True)
for kw in ["强化", "升级", "Ⅱ级", "Ⅲ级", "III级", "II级"]:
    cnt = text.count(kw)
    print(f"  '{kw}': {cnt} 次")
print()

# 2) 结构：找表格/卡片容器
tables = mw.select("table")
print(f"表格数: {len(tables)}")
if tables:
    t = tables[0]
    print("首表前 3 行:")
    for tr in t.select("tr")[:3]:
        cells = [c.get_text(strip=True)[:20] for c in tr.select("th, td")]
        print("   ", cells)

# 3) 搜索"强化"上下文
for m in re.finditer(r"强化", text):
    s = max(0, m.start() - 80)
    print("  ctx:", text[s:m.end() + 80][:160])
    break

# 4) 图标文件名里 9001 等（可能表示强化版）
imgs = mw.select("img")
ids = sorted(set(re.findall(r"饰品-(\d{4})\.png", " ".join(i.get("src", "") for i in imgs))))
print("\n饰品图标 id 样本:", ids[:30], "... 共", len(ids))
