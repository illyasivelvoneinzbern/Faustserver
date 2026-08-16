# -*- coding: utf-8 -*-
"""分析 44 条 desc2/desc3 非空饰品的 base↔desc2 关系，分类 真强化 vs 效果分段。"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from urllib.parse import urlencode

WIKI_BASE = "https://limbuscompany.huijiwiki.com"
API_BASE = f"{WIKI_BASE}/api.php"


async def get_tabx():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080}, locale="zh-CN",
        )
        page = await ctx.new_page()
        await page.goto(f"{WIKI_BASE}/wiki/%E9%A6%96%E9%A1%B5",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        url = f"{API_BASE}?{urlencode({'action': 'query', 'prop': 'revisions', 'rvprop': 'content', 'rvslots': 'main', 'titles': 'Data:Giftchoose.tabx', 'format': 'json'})}"
        j = await page.evaluate(
            """async (url) => { const r = await fetch(url, { credentials: 'include' }); return await r.json(); }""", url)
        await browser.close()
        for pid, pd in j.get("query", {}).get("pages", {}).items():
            revs = pd.get("revisions", [])
            if revs:
                return revs[0].get("slots", {}).get("main", {}).get("*", "") or revs[0].get("*", "")
        return ""


def clean(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', t)
    t = re.sub(r'\{\{[^{}|]+\}\}', '', t)
    t = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', t)
    t = re.sub(r'\[\[([^\]]+)\]\]', r'\1', t)
    t = re.sub(r'<br\s*/?>', '\n', t)
    t = re.sub(r'[（(]未[^）)]*[)）]', '', t)  # 去未强化/强化标签
    return t.strip()


def main():
    raw = asyncio.run(get_tabx())
    data = json.loads(raw)
    rows = data.get("data", [])
    fields = [f.get("name") for f in data.get("schema", {}).get("fields", [])]
    i_name, i_desc, i_desc2, i_desc3 = fields.index("name"), fields.index("desc"), fields.index("desc2"), fields.index("desc3")

    def val(row, idx):
        if idx >= len(row):
            return ""
        v = row[idx]
        if v is None:
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("none", "null") else s

    seg_list = []   # 疑似效果分段（desc2 以 - 列表项开头 = base 列表延续）
    upgrade_list = []  # 疑似真强化
    for r in rows:
        if not isinstance(r, list):
            continue
        d2 = val(r, i_desc2)
        d3 = val(r, i_desc3)
        if not (d2 or d3):
            continue
        name = val(r, i_name)
        desc = clean(val(r, i_desc))
        d2c = clean(d2)
        # 判据：desc2 以 '- ' 或 '− ' 开头，且 desc 也含 '- '（同一列表延续）→ 分段
        is_seg = bool(re.match(r'^-', d2c)) and bool(re.search(r'(?m)^-', desc))
        # 或 desc 以引导句结尾（'触发以下效果'/'根据人格触发'）且 desc2 是展开 → 分段
        if re.search(r'触发以下效果\s*$', desc) or re.search(r'以下效果[：:]?\s*$', desc):
            is_seg = True
        (seg_list if is_seg else upgrade_list).append((name, desc[-40:], d2c[:40], bool(d3)))

    print(f"=== 疑似『效果分段』（本无强化，被误判）: {len(seg_list)} 条 ===")
    for name, d_tail, d2_head, has3 in seg_list:
        print(f"  [{name}] base尾: {d_tail!r} | desc2头: {d2_head!r}")

    print()
    print(f"=== 疑似『真强化』: {len(upgrade_list)} 条 ===")
    for name, d_tail, d2_head, has3 in upgrade_list:
        mark = " (含Ⅲ级)" if has3 else ""
        print(f"  [{name}]{mark} | base尾: {d_tail!r} | desc2头: {d2_head!r}")


if __name__ == "__main__":
    main()
