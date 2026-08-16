# -*- coding: utf-8 -*-
"""深入探测：syndication 原始响应 + Playwright 渲染 nitter 单推页。"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# 两个视频推文
TWEETS = {
    "2083120619185696820": "CHAPTER 10 PV",
    "2083115461672137197": "E.G.O 抽出 PV",
}


def syndication_raw(tweet_id):
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token=abc"
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
        print(f"[{tweet_id}] syndication HTTP {r.status_code}, len={len(r.text)}")
        if r.status_code == 200 and r.text.strip():
            try:
                d = r.json()
                print(f"  keys: {list(d.keys())[:12]}")
                if d.get("video"):
                    print(f"  video keys: {list(d['video'].keys())}")
                if d.get("photos"):
                    print(f"  photos: {len(d['photos'])}")
            except Exception as e:
                print(f"  JSON 解析失败: {e} body={r.text[:120]}")
        else:
            print(f"  body: {r.text[:120]}")
    except Exception as e:
        print(f"  syndication 异常: {e}")


async def nitter_playwright():
    """Playwright 渲染 nitter 单推页，JS 执行后找 video。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright 未安装")
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=UA)
        for tid in TWEETS:
            url = f"https://nitter.net/LimbusCompany_B/status/{tid}"
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)
                html = await page.content()
                print(f"[{tid}] Playwright 渲染长度: {len(html)}")
                mp4s = re.findall(r"https?://[^\"'\\s]+\.mp4[^\"'\\s]*", html)
                print(f"  mp4: {len(mp4s)} 个")
                for m in mp4s[:3]:
                    print(f"    {m[:110]}")
                vids = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('video')).map(v => v.src || v.currentSrc || '')"
                )
                print(f"  <video> 标签: {vids}")
                # 页面标题
                print(f"  标题: {await page.title()}")
            except Exception as e:
                print(f"[{tid}] Playwright 异常: {type(e).__name__}: {e}")
        await browser.close()


if __name__ == "__main__":
    print("=== syndication 原始响应 ===")
    for tid in TWEETS:
        syndication_raw(tid)
    print()
    print("=== Playwright 渲染 nitter 单推页 ===")
    asyncio.run(nitter_playwright())
