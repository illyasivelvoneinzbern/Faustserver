# -*- coding: utf-8 -*-
"""模拟 spider._split_versions 逻辑，找出内联拆分误判强化的案例。"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def clean_wikitext(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', text)
    text = re.sub(r'\{\{名词\|[^}]*\}\}', '', text)
    text = re.sub(r'\{\{[^{}|]+\}\}', '', text)
    text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def split_versions(desc: str, desc2: str = "", desc3: str = "") -> dict:
    """复刻 spider._split_versions 的完整逻辑。"""
    versions = {}
    desc = clean_wikitext(desc)
    desc2 = clean_wikitext(desc2)
    desc3 = clean_wikitext(desc3)
    if desc2 or desc3:
        versions["base"] = desc
        if desc2:
            versions["upgraded_2"] = desc2
        if desc3:
            versions["upgraded_3"] = desc3
        return versions
    parts2 = re.split(r'\s*2级(?:[：:]|波次)\s*', desc, maxsplit=1)
    if len(parts2) == 1:
        versions["base"] = parts2[0]
        return versions
    versions["base"] = parts2[0]
    remaining = parts2[1]
    parts3 = re.split(r'\s*3级(?:[：:]|波次)?\s*', remaining, maxsplit=1)
    if len(parts3) == 1:
        versions["upgraded_2"] = parts3[0]
    else:
        versions["upgraded_2"] = parts3[0]
        versions["upgraded_3"] = parts3[1]
    return versions


# 拉取 tabx（从已有脚本结果复用：重新拉太慢，直接分析之前抓的？）
# 这里从 wiki_accessories.jsonl 反推太麻烦——直接重新拉 tabx
import asyncio
from urllib.parse import urlencode
from playwright.async_api import async_playwright

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


def main():
    raw = asyncio.run(get_tabx())
    data = json.loads(raw)
    rows = data.get("data", [])
    fields = [f.get("name") for f in data.get("schema", {}).get("fields", [])]
    i_name = fields.index("name")
    i_desc = fields.index("desc")
    i_desc2 = fields.index("desc2")
    i_desc3 = fields.index("desc3")

    def val(row, idx):
        if idx >= len(row):
            return ""
        v = row[idx]
        if v is None:
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("none", "null") else s

    inline_upgraded = []  # 内联拆分生成强化（疑似误判）
    for r in rows:
        if not isinstance(r, list):
            continue
        d2, d3 = val(r, i_desc2), val(r, i_desc3)
        if d2 or d3:
            continue  # 有真实强化，跳过
        desc = val(r, i_desc)
        ver = split_versions(desc)
        if "upgraded_2" in ver or "upgraded_3" in ver:
            inline_upgraded.append((val(r, i_name), list(ver.keys())))

    print(f"desc2/desc3 均为空但内联拆分出强化状态的饰品: {len(inline_upgraded)} 个")
    print("（这些就是'没有强化却拥有强化状态'的误判候选）")
    print()
    for name, keys in inline_upgraded:
        print(f"  [{name}] -> {keys}")


if __name__ == "__main__":
    main()
