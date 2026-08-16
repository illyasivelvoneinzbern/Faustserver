# -*- coding: utf-8 -*-
"""验证 syndication token 参数必要性 + 提取 mp4。"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
TWEETS = {
    "2083120619185696820": "CHAPTER 10 PV",
    "2083115461672137197": "E.G.O 抽出 PV",
}


def parse(tid, with_token):
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=en"
    if with_token:
        url += "&token=abc"
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
    d = r.json() if r.text.strip() else {}
    top = list(d.keys())
    v = d.get("video") or {}
    variants = v.get("variants") or []
    print(f"[{tid}] token={with_token}: 顶层={len(top)} video变体={len(variants)}")
    if variants:
        mp4s = [x for x in variants if "mp4" in str(x.get("content_type", ""))]
        best = max(mp4s, key=lambda x: x.get("bitrate") or 0) if mp4s else None
        if best:
            print(f"   最佳 mp4 (bitrate={best.get('bitrate')}): {best.get('url')[:120]}")


if __name__ == "__main__":
    for tid in TWEETS:
        parse(tid, with_token=False)
        parse(tid, with_token=True)
        print()
