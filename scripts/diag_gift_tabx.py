# -*- coding: utf-8 -*-
"""拉取 Data:Giftchoose.tabx 原始数据，分析 desc/desc2/desc3 字段与强化误判。"""
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import urlencode

from playwright.async_api import async_playwright

WIKI_BASE = "https://limbuscompany.huijiwiki.com"
API_BASE = f"{WIKI_BASE}/api.php"


async def main():
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
        content = ""
        for pid, pd in j.get("query", {}).get("pages", {}).items():
            revs = pd.get("revisions", [])
            if revs:
                content = revs[0].get("slots", {}).get("main", {}).get("*", "") or revs[0].get("*", "")
        if not content:
            print("tabx 获取失败")
            return
        data = json.loads(content)
        rows = data.get("data", [])
        fields = [f.get("name") for f in data.get("schema", {}).get("fields", [])]
        print(f"tabx rows: {len(rows)}, fields: {fields}")

        # desc 相关列索引
        def find(name):
            for i, f in enumerate(fields):
                if f == name:
                    return i
            return None

        i_desc, i_desc2, i_desc3 = find("desc"), find("desc2"), find("desc3")
        if i_desc is None:
            print("未找到 desc 列，字段:", fields)
            return
        print(f"desc@{i_desc} desc2@{i_desc2} desc3@{i_desc3}")

        # 统计 desc2/desc3 的取值分布
        from collections import Counter
        c2, c3 = Counter(), Counter()
        no_stage_suspicious = []
        for r in rows:
            if not isinstance(r, list):
                continue
            def val(idx):
                if idx is None or idx >= len(r):
                    return ""
                v = r[idx]
                if v is None:
                    return ""
                s = str(v).strip()
                return "" if s.lower() in ("none", "null") else s
            d2, d3 = val(i_desc2), val(i_desc3)
            c2[d2[:20] if d2 else "(空)"] += 1
            c3[d3[:20] if d3 else "(空)"] += 1
            if d2 or d3:
                name = val(1)
                no_stage_suspicious.append((name, d2[:30], d3[:30]))

        print()
        print("desc2 取值分布（前 15）:", c2.most_common(15))
        print("desc3 取值分布:", c3.most_common(10))
        print()
        print(f"desc2/desc3 非空的记录数: {len(no_stage_suspicious)}")
        for name, d2, d3 in no_stage_suspicious[:30]:
            print(f"  [{name}] desc2={d2!r} desc3={d3!r}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
