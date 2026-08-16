# -*- coding: utf-8 -*-
"""查证：页面 10 个可强化饰品的 tabx 数据（desc/desc2/desc3）。"""
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

UPGRADABLE = ["地狱蝶之梦", "倒错症", "尘归尘", "采血包", "拟伤虫", "咖啡与纸鹤", "朱红蛾群", "染血铁钉", "炽热的羽毛", "鲜血装饰"]


def clean(t):
    t = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', t)
    t = re.sub(r'\{\{[^{}|]+\}\}', '', t)
    t = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', t)
    t = re.sub(r'\[\[([^\]]+)\]\]', r'\1', t)
    t = re.sub(r'<br\s*/?>', '\n', t)
    return t.strip()


async def get_tabx():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        page = await ctx.new_page()
        await page.goto(f"{WIKI_BASE}/wiki/%E9%A6%96%E9%A1%B5", wait_until="domcontentloaded")
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

    for name in UPGRADABLE:
        found = None
        for r in rows:
            if isinstance(r, list) and val(r, i_name) == name:
                found = r
                break
        if not found:
            print(f"[{name}] 未在 tabx 找到")
            continue
        d, d2, d3 = clean(val(found, i_desc)), clean(val(found, i_desc2)), clean(val(found, i_desc3))
        print(f"=== {name} ===")
        print(f"  desc ({len(d)}字): {d[:120]}")
        print(f"  desc2 ({len(d2)}字): {d2[:80] or '(空)'}")
        print(f"  desc3 ({len(d3)}字): {d3[:60] or '(空)'}")
        print()


if __name__ == "__main__":
    main()
