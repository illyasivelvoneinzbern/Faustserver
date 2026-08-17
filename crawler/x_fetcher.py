"""
X/Twitter 官方账号内容拉取：通过 RSS 桥接获取推文（P20-B 扩展）。

- 主源：Nitter RSS（公开、无需登录/cookie/付费 key）。
- 保留媒体：从 description HTML 中提取图片 / 视频 URL，不再全量剥成纯文本。
- 真推文 id（status id）：从 item.guid 提取，供幂等去重。
- 账号与 RSS 镜像 URL 均可配置（config.yaml x_fetcher 段）。
- 保留旧接口（fetch_account / fetch_all / fetch_x_feeds），新增 fetch_new_tweets()。
- P38：识别官方自回帖（R to @…），group_threads() 将回复合并到父推文线程。
"""

import asyncio
import html as html_module
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser

from crawler.cleaner import clean_text

logger = logging.getLogger(__name__)

# 常见 User-Agent（syndication API / 单推页请求）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 默认官方账号（ProjMoonOfficial 已在实测中 404，移除；如需可自行在 config 补充）
OFFICIAL_ACCOUNTS = [
    {
        "handle": "LimbusCompany_B",
        "name": "边狱巴士官方（日/英）",
        "rss_url": "https://nitter.net/LimbusCompany_B/rss",
    },
]

# RSS 镜像模板（config 可覆盖）：支持 {handle} 占位符
DEFAULT_RSS_URLS = [
    "https://nitter.net/{handle}/rss",
]

# 图片过滤：明显是表情/图标的跳过；其余（含 /pic/ /media/ /profile_images/ 等）宁可保留
_IMG_SKIP_PATTERNS = ("/emoji/", "twitter_emoji", "_icon", "/icon", "profile_imgs")
# 视频/封面 URL 后缀
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")

_IMG_TAG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_VIDEO_SRC_RE = re.compile(r"<video\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_VIDEO_POSTER_RE = re.compile(r"<video\b[^>]*\bposter=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SOURCE_SRC_RE = re.compile(r"<source\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_RAW_VIDEO_URL_RE = re.compile(
    r"(https?://[^\s\"'<>]+\.(?:mp4|mov|webm|m4v)(?:\?[^\s\"'<>]*)?)", re.IGNORECASE
)
_LINK_RE = re.compile(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</p>|<p[^>]*>|</div>|<div[^>]*>|</li>|<li[^>]*>|<hr\s*/?>")


def _dedup_urls(urls: list[str]) -> list[str]:
    """去重并保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ═══════════════════════════════════════════════════════════════════════
# 视频链路（P31，目标 2 核心缺口）：真实 mp4 解析
# ═══════════════════════════════════════════════════════════════════════
# Nitter RSS 的 description 只有视频封面（amplify_video_thumb），没有 mp4。
# 方案（实测可行）：官方 syndication API（免鉴权）——
#   GET https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token={任意串}
#   返回 JSON 的 video.variants 含 4 个 mp4 变体（480p~1080p）与 HLS。
# 注意：token 参数必需（不带时返回空 JSON）。
_SYNDICATION_URL = (
    "https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}"
    "&lang=en&token=limbus-agent-2026"
)
# 视频推文标记：RSS 封面 URL 含 amplify_video 即视为视频推文
_VIDEO_TWEET_MARKERS = ("amplify_video", "video_thumb", "/video/", ".mp4")


def is_video_tweet(tweet: dict) -> bool:
    """判断推文是否为视频推文（有视频封面/已有视频 URL）。"""
    if tweet.get("video_urls"):
        return True
    for img in tweet.get("image_urls") or []:
        low = img.lower()
        if any(m in low for m in _VIDEO_TWEET_MARKERS):
            return True
    summary_text = " ".join([
        str(tweet.get("title") or ""),
        str(tweet.get("content") or ""),
        str(tweet.get("text") or ""),
    ])
    return any(m in summary_text.lower() for m in _VIDEO_TWEET_MARKERS)


def resolve_tweet_videos(tweet: dict, timeout: float = 20.0, quality: str = "high") -> list[str]:
    """通过官方 syndication API 解析推文的真实 mp4 URL（P31）。

    - 非视频推文 / 无 tweet_id → 返回 []
    - 成功 → 返回 mp4 列表（默认最高分辨率 1080p；P34 已修复视频分开发送，
      高清可正常发出。若网络/上传仍不稳可切 low/medium）
    - 失败（API 异常/无 mp4）→ 返回 []（调用方回落封面/链接）

    Args:
        tweet: 推文 dict
        timeout: 请求超时（秒）
        quality: "low"(480x270) | "medium"(640x360) | "high"(最高, 默认)

    Returns:
        真实 mp4 URL 列表（0 或 1 个）。
    """
    tid = str(tweet.get("tweet_id") or "")
    if not tid:
        return []
    if not is_video_tweet(tweet):
        return []

    try:
        import httpx
        url = _SYNDICATION_URL.format(tweet_id=tid)
        resp = httpx.get(
            url,
            headers={"User-Agent": _DEFAULT_UA},
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code != 200 or not resp.text.strip():
            logger.warning(f"syndication API 无响应（HTTP {resp.status_code}）: {tid}")
            return []
        data = resp.json()
        video = data.get("video") or {}
        variants = video.get("variants") or []
        mp4s = [
            v.get("src") for v in variants
            if v.get("type") == "video/mp4" and v.get("src")
        ]
        if not mp4s:
            logger.debug(f"推文 {tid} 无 mp4 变体（可能非视频推文）")
            return []
        # 去重（保序）；API 变体顺序由低到高（480p → 1080p）
        out = _dedup_urls(mp4s)
        if quality == "high":
            pick = out[-1]
        elif quality == "medium":
            pick = out[1] if len(out) > 1 else out[-1]
        else:  # low
            pick = out[0]
        logger.info(f"视频解析成功: {tid} → {pick.rsplit('/', 1)[-1]}（quality={quality}）")
        return [pick]
    except Exception as e:
        logger.warning(f"视频解析失败 {tid}: {type(e).__name__}: {e}")
        return []


def _absolute_url(url: str, base_url: str) -> str:
    """把相对/协议相对 URL 补全为绝对 URL。

    base_url 形如 https://nitter.net/LimbusCompany_B（来自 rss_url）。
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base_url)
        if m:
            return m.group(1) + url
    return url


def extract_images(summary: str, base_url: str = "") -> list[str]:
    """从 description HTML 中提取图片 URL（去重、补全、过滤表情/图标小图）。"""
    urls = []
    for m in _IMG_TAG_RE.finditer(summary or ""):
        src = m.group(1)
        low = src.lower()
        if any(skip in low for skip in _IMG_SKIP_PATTERNS):
            continue
        urls.append(_absolute_url(src, base_url))
    return _dedup_urls(urls)


def extract_videos(summary: str, base_url: str = "") -> list[str]:
    """从 description HTML 中提取视频/视频源/封面 URL。

    覆盖：<video src=...>、<video poster=...>、<source src=...>、裸 .mp4/.mov 直链。
    """
    urls: list[str] = []
    for m in _VIDEO_SRC_RE.finditer(summary or ""):
        urls.append(_absolute_url(m.group(1), base_url))
    for m in _SOURCE_SRC_RE.finditer(summary or ""):
        urls.append(_absolute_url(m.group(1), base_url))
    for m in _RAW_VIDEO_URL_RE.finditer(summary or ""):
        urls.append(m.group(1))
    for m in _VIDEO_POSTER_RE.finditer(summary or ""):
        urls.append(_absolute_url(m.group(1), base_url))
    return _dedup_urls(urls)


def html_to_text(summary: str) -> str:
    """把 Nitter description HTML 转纯文本。

    - <br>/<p>/<div> 等边界转换行，保留段落。
    - 超链接：去掉锚文本（如 https://t.co/xxx 的显示文字噪音），保留链接本身（href）。
    - 剥掉 <img>/<video> 等媒体标签（媒体 URL 单独提取）。
    """
    text = summary or ""
    # 边界标签 → 换行
    text = _BREAK_RE.sub("\n", text)
    # 超链接 → 保留 href，去掉锚文本
    text = _LINK_RE.sub(lambda m: (m.group(1) or "").strip(), text)
    # 媒体标签整体移除
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<video\b[^>]*>.*?</video>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<video\b[^>]*/>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<source\b[^>]*>", "", text, flags=re.IGNORECASE)
    # 剩余标签
    text = _ANY_TAG_RE.sub("", text)
    # 反转义
    text = html_module.unescape(text)
    # 清理空白：每行 strip、合并多余空行
    lines = [ln.strip() for ln in text.splitlines()]
    out_lines: list[str] = []
    prev_blank = False
    for ln in lines:
        if ln:
            out_lines.append(ln)
            prev_blank = False
        elif not prev_blank and out_lines:
            out_lines.append("")
            prev_blank = True
    text = "\n".join(out_lines).strip()
    return text


def extract_tweet_id(entry) -> str:
    """从 RSS item 提取真推文 status id（guid 形如 .../status/<id> 或纯数字）。"""
    guid = entry.get("guid", entry.get("id", "")) or ""
    guid = str(guid).strip()
    if not guid:
        return ""
    m = re.search(r"/status/(\d+)", guid)
    if m:
        return m.group(1)
    if guid.isdigit():
        return guid
    return guid


def _parse_published(entry) -> str:
    """解析发布时间为 UTC ISO 字符串；失败回退当前时间。

    entry 兼容 dict（FeedParserDict 为 dict 子类）与带属性对象。
    """
    struct = None
    if isinstance(entry, dict):
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
    else:
        struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if struct:
        try:
            dt = datetime(*struct[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def parse_tweet_entry(account: dict, entry, base_url: str = "") -> dict:
    """把单个 RSS item 解析为标准化推文 dict（P20-B 结构）。

    返回字段：
        tweet_id / text / permalink / published_at / image_urls / video_urls
        + retweet（是否转发，改进计划 P1）
        + 兼容旧字段：id / title / url / content / categories / source / handle / account_name
    """
    summary = entry.get("summary", entry.get("description", "")) or ""
    title = (entry.get("title", "") or "")[:100]
    tweet_id = extract_tweet_id(entry)
    link = entry.get("link", "") or ""
    published = _parse_published(entry)

    text = html_to_text(summary)
    if not text:
        text = clean_text(title)

    # ── 转发（RT）检测（改进计划 P1：RT 污染推送）──
    # Nitter 对 RT 的 title 为 "RT by @原账号: 原文标题"，
    # 转发的 permalink 指向原账号而非被关注账号。
    retweet = bool(re.match(r"^\s*RT\s+by\s+@", title)) or "RT by @" in text[:60]
    if retweet:
        text = re.sub(r"^\s*RT\s+by\s+@[^:：]+[：:]\s*", "", text)

    # ── 官方自回帖（线程续写）检测（P38）──
    # Nitter 对回复的 title 为 "R to @账号: 内容"；官方账号回复自己的推文
    # 即形成线程（如 公告 + 补充更正），推送时合并到父推文一并转发。
    # 回复自身也有独立 status id（guid），可参与幂等去重。
    reply_to_handle = ""
    m = re.match(r"^\s*R to @([A-Za-z0-9_]+)[：:]\s*(.*)$", title, re.DOTALL)
    if m:
        reply_to_handle = m.group(1)
        title = m.group(2)  # 剥掉 "R to @X: " 前缀，标题仅保留内容

    # ── 噪音清洗（改进计划 P1）：nitter 搜索链接等垃圾 URL 整段删除 ──
    # 例："https://nitter.net/search?f=tweets&q=%23LCB"（话题搜索噪音）
    text = re.sub(r"https?://[^\s]*nitter[^\s]*search[^\s]*", "", text, flags=re.IGNORECASE)
    # P31：删除行尾 nitter 化的原文链接（"https://nitter.net/xxx/status/123#m" 残留）
    text = re.sub(r"https?://[^\s]*nitter[^\s]*/status/\d+#m", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return {
        "tweet_id": tweet_id,
        "text": text,
        "permalink": link,
        "published_at": published,
        "image_urls": extract_images(summary, base_url),
        "video_urls": extract_videos(summary, base_url),
        "retweet": retweet,
        "is_reply": bool(reply_to_handle),
        "reply_to_handle": reply_to_handle,
        # ── 兼容旧字段 ──
        "id": f"x_{account['handle']}_{tweet_id}" if tweet_id else f"x_{account['handle']}_{published}",
        "title": title,
        "url": link,
        "content": text,
        "categories": ["官方资讯", "X/Twitter"],
        "source": "x_twitter",
        "handle": account["handle"],
        "account_name": account.get("name", account["handle"]),
    }


def group_threads(tweets: list[dict]) -> list[dict]:
    """把拉取到的推文按『父推文 + 官方自回帖』分组（P38）。

    输入 tweets 按发布时间升序（旧→新，与 fetch_new_tweets 输出一致）。
    规则：
    - 非回复、非转发的推文 → 新线程（父推文），并成为当前候选父推文；
    - 官方账号回复自己的推文（is_reply 且 reply_to_handle == 父推文 handle）
      → 附加到当前候选父推文的 official_replies 字段，不独立成线程；
    - 其余（转发、回复其它账号、无父可挂靠的回复）→ 独立线程。

    Returns:
        线程 dict 列表：每项或为普通推文，或为 {**parent, "official_replies": [...]}。
        分组后仍保持原时间顺序（升序）。
    """
    threads: list[dict] = []
    current: Optional[dict] = None  # 当前候选父推文
    for tw in tweets:
        if tw.get("retweet"):
            # 转发不进父推文匹配（filter_retweets=True 时通常已被上游过滤）
            threads.append(tw)
            continue
        if tw.get("is_reply"):
            if (
                current is not None
                and str(tw.get("reply_to_handle", "")).lower()
                == str(current.get("handle", "")).lower()
            ):
                # 官方自回帖：挂到当前父推文，随父推文一并转发
                current.setdefault("official_replies", []).append(tw)
                continue
            # 无法挂靠的回复（回复其它账号 / 父推文不在本批）→ 独立线程
            threads.append(tw)
            continue
        # 新的父推文：成为当前候选，同时作为独立线程
        threads.append(tw)
        current = tw
    return threads


class XFeedFetcher:
    """通过 RSS 桥接拉取 X/Twitter 账号内容"""

    # 默认账号（config 可覆盖）
    OFFICIAL_ACCOUNTS = list(OFFICIAL_ACCOUNTS)

    def __init__(
        self,
        state_path: str = "./data/x_feed_state.json",
        output_dir: str = "./data/raw",
        accounts: Optional[list[str]] = None,
        rss_urls: Optional[list[str]] = None,
    ):
        self.state_path = Path(state_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.last_fetch: dict[str, str] = {}  # handle -> ISO timestamp
        self.accounts = self._build_accounts(accounts, rss_urls)
        self._load_state()

    def _build_accounts(
        self, accounts: Optional[list[str]], rss_urls: Optional[list[str]]
    ) -> list[dict]:
        """根据账号句柄与镜像模板生成账号列表（含 rss_url）。

        accounts: ["LimbusCompany_B", ...]（config 默认）
        rss_urls: ["https://nitter.net/{handle}/rss", ...]（镜像模板，支持 {handle} 占位符）
        """
        handles = accounts or [a["handle"] for a in OFFICIAL_ACCOUNTS]
        templates = rss_urls or list(DEFAULT_RSS_URLS)
        built: list[dict] = []
        for handle in handles:
            for tmpl in templates:
                if "{handle}" in tmpl:
                    url = tmpl.format(handle=handle)
                else:
                    url = f"{tmpl.rstrip('/')}/{handle}/rss"
                built.append({
                    "handle": handle,
                    "name": handle,
                    "rss_url": url,
                })
        return built

    def _load_state(self):
        """加载上次拉取状态"""
        if self.state_path.exists():
            try:
                self.last_fetch = json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, KeyError):
                self.last_fetch = {}

    def _save_state(self):
        """保存拉取状态"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.last_fetch, indent=2, ensure_ascii=False))

    def _base_url(self, account: dict) -> str:
        """由 rss_url 推断 RSS 根 base（用于补全相对媒体 URL）。"""
        rss_url = account.get("rss_url", "")
        # https://nitter.net/LimbusCompany_B/rss → https://nitter.net/LimbusCompany_B
        base = re.sub(r"/rss/?$", "", rss_url)
        return base or rss_url

    async def _fetch_feed(self, account: dict):
        """拉取单个账号的 feed；失败返回 None。"""
        try:
            return await asyncio.to_thread(feedparser.parse, account["rss_url"])
        except Exception as e:
            logger.warning(f"RSS 解析失败 ({account['handle']}): {e}")
            return None

    async def fetch_account(self, account: dict) -> list[dict]:
        """拉取单个账号的最新推文（基于时间增量，向后兼容）。"""
        feed = await self._fetch_feed(account)
        if not feed or not feed.entries:
            logger.debug(f"{account['handle']}: 无新推文")
            return []

        last_ts = self.last_fetch.get(account["handle"], "1970-01-01T00:00:00+00:00")
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except ValueError:
            last_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

        base_url = self._base_url(account)
        new_entries = []
        for entry in feed.entries:
            tweet = parse_tweet_entry(account, entry, base_url)
            try:
                published = datetime.fromisoformat(tweet["published_at"])
            except ValueError:
                published = datetime.now(timezone.utc)
            if published <= last_dt:
                continue
            new_entries.append(tweet)

        if new_entries:
            self.last_fetch[account["handle"]] = new_entries[-1]["published_at"]

        logger.info(f"{account['handle']}: 拉取到 {len(new_entries)} 条新推文")
        return new_entries

    async def fetch_all(self) -> list[dict]:
        """拉取所有官方账号的新推文（向后兼容）。"""
        all_entries = []
        for account in self.accounts:
            entries = await self.fetch_account(account)
            all_entries.extend(entries)
        self._save_state()

        if all_entries:
            self._save_entries(all_entries)

        return all_entries

    async def fetch_new_tweets(
        self,
        pushed_ids: Optional[set[str]] = None,
        filter_retweets: bool = False,
        min_published_at: Optional[str] = None,
    ) -> list[dict]:
        """拉取所有账号推文，返回未推送（不在 pushed_ids）的新推文。

        - 按 published_at 升序排序（先推更早的）。
        - 若某个账号 RSS 失败：记录告警日志，不中断其它账号。
        - pushed_ids 为已推送 tweet_id 集合；None 表示不过滤（全部返回）。
        - filter_retweets=True 时跳过转发（RT）推文（改进计划 P1）。
        - min_published_at：仅返回发布时间 >= 该 ISO 时间戳的推文
          （首启水位线，改进计划 P1）。
        """
        all_tweets: list[dict] = []
        for account in self.accounts:
            feed = await self._fetch_feed(account)
            if not feed or not feed.entries:
                logger.debug(f"{account['handle']}: 无推文或拉取失败")
                continue
            base_url = self._base_url(account)
            for entry in feed.entries:
                tweet = parse_tweet_entry(account, entry, base_url)
                if not tweet["tweet_id"]:
                    # 无 id 的条目跳过（无法幂等）
                    continue
                if pushed_ids is not None and tweet["tweet_id"] in pushed_ids:
                    continue
                if filter_retweets and tweet.get("retweet"):
                    continue
                if min_published_at:
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(tweet["published_at"])
                        ts_min = datetime.fromisoformat(min_published_at)
                        if ts < ts_min:
                            continue
                    except (ValueError, TypeError):
                        pass
                all_tweets.append(tweet)

        all_tweets.sort(key=lambda t: t["published_at"])
        logger.info(f"拉取到 {len(all_tweets)} 条未推送推文")
        return all_tweets

    def _save_entries(self, entries: list[dict]):
        """追加保存到 JSONL 文件"""
        output_path = self.output_dir / "x_posts.jsonl"
        with open(output_path, "a", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        logger.info(f"已保存 {len(entries)} 条 X 推文到 {output_path}")


# ── 便于从 main.py 调用的便捷函数 ──

async def fetch_x_feeds(
    state_path: str = "./data/x_feed_state.json",
    output_dir: str = "./data/raw",
) -> list[dict]:
    """拉取所有 X 官方账号的最新推文（向后兼容）。"""
    fetcher = XFeedFetcher(state_path=state_path, output_dir=output_dir)
    return await fetcher.fetch_all()


async def fetch_new_tweets(
    state_path: str = "./data/x_feed_state.json",
    output_dir: str = "./data/raw",
    accounts: Optional[list[str]] = None,
    rss_urls: Optional[list[str]] = None,
    pushed_ids: Optional[set[str]] = None,
    filter_retweets: bool = False,
    min_published_at: Optional[str] = None,
) -> list[dict]:
    """便捷函数：拉取未推送的新推文（供后台轮询调用）。

    - filter_retweets=True 时跳过转发（RT）推文。
    - min_published_at：仅返回发布时间 >= 该 ISO 时间戳的推文（首启水位线）。
    """
    fetcher = XFeedFetcher(
        state_path=state_path,
        output_dir=output_dir,
        accounts=accounts,
        rss_urls=rss_urls,
    )
    return await fetcher.fetch_new_tweets(
        pushed_ids=pushed_ids,
        filter_retweets=filter_retweets,
        min_published_at=min_published_at,
    )
