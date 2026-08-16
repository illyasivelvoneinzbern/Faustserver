# -*- coding: utf-8 -*-
"""端到端：拉推文 → 识别视频推文 → 解析真实 mp4。"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.x_fetcher import fetch_new_tweets, is_video_tweet, resolve_tweet_videos


async def main():
    tweets = await fetch_new_tweets(
        accounts=["LimbusCompany_B"], pushed_ids=set(), filter_retweets=True
    )
    print(f"拉取到 {len(tweets)} 条推文")
    vids = [t for t in tweets if is_video_tweet(t)]
    print(f"识别视频推文: {len(vids)} 条")
    for t in vids[:3]:
        v = resolve_tweet_videos(t)
        print(f"  {t['tweet_id']}: {len(v)} 个 mp4 -> {(v[0][:90] if v else '解析失败')}")
    if not vids:
        print("（本轮无视频推文）")


if __name__ == "__main__":
    asyncio.run(main())
