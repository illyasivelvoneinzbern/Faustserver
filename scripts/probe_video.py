# -*- coding: utf-8 -*-
"""探测视频解析方案：nitter 单推页 mp4 + syndication API。"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx

VIDEO_TWEET = "2083120619185696820"  # CHAPTER 10 - PUNCTUM 视频推文
HANDLE = "LimbusCompany_B"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def try_nitter_page():
    """方案 A：单推页 HTML 提取 mp4。"""
    url = f"https://nitter.net/{HANDLE}/status/{VIDEO_TWEET}"
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
        print(f"nitter 单推页: HTTP {r.status_code}, 长度 {len(r.text)}")
        if r.status_code != 200:
            print(f"  body 前 200 字: {r.text[:200]}")
            return
        # 找 mp4
        mp4s = re.findall(r"https?://[^\"'\\s]+\.mp4[^\"'\\s]*", r.text)
        print(f"  mp4 直链: {len(mp4s)} 个")
        for m in mp4s[:3]:
            print(f"    {m[:120]}")
        # data-video-url / source
        for pat in [r'data-video-url="([^"]+)"', r'<source[^>]+src="([^"]+)"']:
            m = re.search(pat, r.text)
            if m:
                print(f"  {pat[:20]} → {m.group(1)[:120]}")
    except Exception as e:
        print(f"nitter 单推页异常: {type(e).__name__}: {e}")


def try_syndication():
    """方案 B：官方 syndication API（免鉴权）。"""
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={VIDEO_TWEET}&lang=en"
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
        print(f"syndication API: HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:150]}")
            return
        data = r.json()
        video = data.get("video") or {}
        variants = video.get("variants") or []
        print(f"  video 变体: {len(variants)} 个")
        mp4s = [v for v in variants if "mp4" in str(v.get("content_type", ""))]
        for v in mp4s[:3]:
            print(f"    {v.get('content_type')} bitrate={v.get('bitrate')} url={v.get('url','')[:110]}")
        # 图片
        photos = data.get("photos") or []
        print(f"  photos: {len(photos)}")
    except Exception as e:
        print(f"syndication 异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=== 方案 A: nitter 单推页 ===")
    try_nitter_page()
    print()
    print("=== 方案 B: syndication API ===")
    try_syndication()
