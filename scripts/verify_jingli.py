# -*- coding: utf-8 -*-
"""验证 ？？？的心象-关底BOSS（金笠，BuffPro 英文残留源页）提取全中文。"""
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

WIKI_BASE = "https://limbuscompany.huijiwiki.com"
PAGES = ["？？？的心象-关底BOSS"]


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

        for title in PAGES:
            print("=" * 50, title)
            await page.goto(f"{WIKI_BASE}/wiki/{quote(title)}",
                            wait_until="networkidle", timeout=40000)
            await asyncio.sleep(2)
            html = await page.content()

            url = f"{WIKI_BASE}/api.php?{urlencode({'action': 'query', 'prop': 'revisions', 'rvprop': 'content', 'rvslots': 'main', 'titles': title, 'format': 'json'})}"
            j = await page.evaluate(
                """async (url) => { const r = await fetch(url, { credentials: 'include' }); return await r.json(); }""", url)
            wt = ""
            for pid, pd in j.get("query", {}).get("pages", {}).items():
                revs = pd.get("revisions", [])
                if revs:
                    wt = revs[0].get("slots", {}).get("main", {}).get("*", "") or revs[0].get("*", "")

            import re
            print("wikitext BuffPro:", len(re.findall(r"\{\{\s*BuffPro\s*\|", wt)))

            from crawler.html_extractor import EnemyExtractor, _enemy_list_to_dict
            ex = EnemyExtractor(html, title, [], wt)
            print("buff_code_map:", len(ex._buff_code_map))
            enemies = ex.extract()
            result = _enemy_list_to_dict(enemies)
            recs = (result or {}).get("enemies") or []
            print("enemies:", len(recs))
            import json as _json
            for rec in recs[:3]:
                print("  --", rec.get("enemy_name") or rec.get("name"))
                for pv in (rec.get("passives") or [])[:3]:
                    print("    被动:", pv[:200])
                for sk in (rec.get("skills") or [])[:2]:
                    print("    技能:", sk.get("skill_name"), "| 效果:", (sk.get("coin_effects") or [])[:2])
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
