# -*- coding: utf-8 -*-
"""修正解析 syndication mediaDetails 提取 mp4。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

TWEETS = {
    "2083120619185696820": "CHAPTER 10 PV",
    "2083115461672137197": "E.G.O 抽出 PV",
    "2082028291201052699": "普通图片推文(对照)",
}


def parse(tweet_id):
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en"
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
    d = r.json()
    md = d.get("mediaDetails") or []
    print(f"[{tweet_id}] mediaDetails: {len(md)} 条")
    for m in md:
        mtype = m.get("type")
        print(f"  type={mtype} url={str(m.get('media_url_https'))[:70]}")
        if mtype == "video":
            vi = m.get("video_info") or {}
            variants = vi.get("variants") or []
            print(f"  variants: {len(variants)}")
            for v in sorted(variants, key=lambda x: x.get("bitrate") or 0, reverse=True)[:3]:
                print(f"    {v.get('content_type')} bitrate={v.get('bitrate')} url={str(v.get('url'))[:100]}")


if __name__ == "__main__":
    for tid in TWEETS:
        try:
            parse(tid)
        except Exception as e:
            print(f"[{tid}] 解析异常: {type(e).__name__}: {e}")
        print()
