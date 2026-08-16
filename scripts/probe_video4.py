# -*- coding: utf-8 -*-
"""打印 syndication 响应中 video 字段的完整内容。"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
tid = "2083120619185696820"
url = f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=en"
r = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
d = r.json()
print("顶层字段:", list(d.keys()))
print()
v = d.get("video")
if v:
    print("video =", json.dumps(v, ensure_ascii=False, indent=1)[:1500])
else:
    print("video 为空")
print()
md = d.get("mediaDetails")
print("mediaDetails =", json.dumps(md, ensure_ascii=False)[:500] if md else "(空)")
