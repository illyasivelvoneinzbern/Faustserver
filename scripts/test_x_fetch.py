# -*- coding: utf-8 -*-
"""测试 X/Twitter 推文拉取：fetch_new_tweets 端到端。"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.x_fetcher import fetch_new_tweets


async def main():
    print("正在拉取 @LimbusCompany_B 推文（nitter.net RSS）...")
    try:
        tweets = await asyncio.wait_for(
            fetch_new_tweets(
                state_path="data/x_feed_state_test.json",
                accounts=["LimbusCompany_B"],
                pushed_ids=set(),
                filter_retweets=True,
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        print("✗ 拉取超时（60s）——nitter.net 可能不可达或响应缓慢")
        return
    except Exception as e:
        print(f"✗ 拉取异常: {type(e).__name__}: {e}")
        return

    print(f"✓ 拉取到 {len(tweets)} 条未推送推文（RT 已过滤）")
    print("=" * 60)
    for i, t in enumerate(tweets[:8], 1):
        print(f"[{i}] tweet_id={t['tweet_id']}")
        print(f"    时间: {t['published_at']}")
        print(f"    RT: {t.get('retweet')}")
        print(f"    图片: {len(t.get('image_urls') or [])} 张, 视频: {len(t.get('video_urls') or [])} 个")
        text = (t.get('text') or '').replace('\n', ' ')
        print(f"    文本: {text[:100]}")
        if t.get('image_urls'):
            print(f"    图片URL: {t['image_urls'][0][:90]}")
        print()

    # 验证清洗效果
    if tweets:
        all_text = " ".join(t.get('text', '') for t in tweets)
        import re
        nitter_search = re.findall(r"nitter\.net/search", all_text)
        print(f"清洗验证: nitter 搜索链接残留 {len(nitter_search)} 处（应为 0）")


if __name__ == "__main__":
    asyncio.run(main())
