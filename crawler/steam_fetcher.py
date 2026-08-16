"""
Steam 社区 RSS 拉取：https://steamcommunity.com/games/{appid}/rss（P37 新增）。

- 主源：Steam 官方社区 RSS（公开，无需登录 / API key / cookie）。
- 覆盖：游戏公告（announcements）+ 新闻（news）条目。
- 每条标准化为 dict：announcement_id / title / text / permalink / published_at /
  image_urls（真实图片，过滤 YouTube 占位图等噪音）。
- 幂等：以公告 id（guid 尾部数字）去重，支持 pushed_ids 过滤（后台轮询防重推）。
- 兼容旧字段：id / url / content / categories / source / appid / account_name。

Steam RSS 条目特征（实测 2026-08）：
- description 多为 <img> 链（clan.fastly.steamstatic.com 真实公告图）+ 少量文本；
- 视频预告类条目只有 YouTube 占位 gif（youtube_16x9_placeholder.gif），
  无真实图片/视频直链 → 仅保留标题 + 公告页链接，避免推送"模糊占位图"。
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

logger = logging.getLogger(__name__)

# ── 默认值（config.yaml steam_fetcher 段可覆盖）──
# Limbus Company（边狱巴士）Steam AppID
DEFAULT_APPID = 1973530
# RSS 模板：支持 {appid} 占位符
DEFAULT_RSS_TEMPLATE = "https://steamcommunity.com/games/{appid}/rss"

# 常见 User-Agent（steamcommunity 对部分 UA 返回 403/空 feed）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 占位/噪音图片：YouTube 封面占位 gif、spacer、空白图等直接跳过
_IMG_SKIP_PATTERNS = (
    "youtube_16x9_placeholder",
    "placeholder",
    "spacer",
    "blank.gif",
    "1x1.gif",
    "/emoji/",
    "_icon",
    "/icon",
)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_VIDEO_TAG_RE = re.compile(r"<video\b[^>]*>.*?</video>", re.IGNORECASE | re.DOTALL)
_VIDEO_SELF_RE = re.compile(r"<video\b[^>]*/>", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(r"<source\b[^>]*>", re.IGNORECASE)
_LINK_RE = re.compile(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(
    r"(?i)<br\s*/?>|</p>|<p[^>]*>|</div>|<div[^>]*>|</li>|<li[^>]*>|<hr\s*/?>|<tr[^>]*>|</tr>"
)


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


def _absolute_url(url: str) -> str:
    """把协议相对 URL 补全为绝对 URL（Steam RSS 一般已是绝对 URL，兜底处理）。"""
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def extract_announcement_id(entry) -> str:
    """从 RSS item 提取公告/新闻 id（guid 或 link 形如 .../announcements/detail/<id>）。

    实测 Steam RSS 的 guid 即公告页 URL（如 .../announcements/detail/739305482942940924），
    幂等去重以尾部数字 id 为准；解析失败时回退完整 guid。
    """
    guid = entry.get("guid", entry.get("id", "")) or ""
    link = entry.get("link", "") or ""
    raw = str(guid or link).strip()
    m = re.search(r"/(?:announcements|news)/detail/(\d+)", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    return raw


def _parse_published(entry) -> str:
    """解析发布时间为 UTC ISO 字符串；失败回退当前时间。"""
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


def extract_images(summary: str) -> list[str]:
    """从 description HTML 中提取真实图片 URL（去重、补全、过滤占位图）。"""
    urls = []
    for m in _IMG_TAG_RE.finditer(summary or ""):
        src = m.group(1)
        low = src.lower()
        if any(skip in low for skip in _IMG_SKIP_PATTERNS):
            continue
        urls.append(_absolute_url(src))
    return _dedup_urls(urls)


def html_to_text(summary: str) -> str:
    """把 Steam description HTML 转纯文本。

    - <br>/<p>/<div>/<li> 等边界转换行，保留段落。
    - 超链接：去掉锚文本（如图片文件名噪音），保留链接本身（href）。
    - 剥掉 <img>/<video>/<source> 等媒体标签（媒体 URL 单独提取）。
    """
    text = summary or ""
    text = _BREAK_RE.sub("\n", text)
    text = _LINK_RE.sub(lambda m: (m.group(1) or "").strip(), text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = _VIDEO_TAG_RE.sub("", text)
    text = _VIDEO_SELF_RE.sub("", text)
    text = _SOURCE_TAG_RE.sub("", text)
    text = _ANY_TAG_RE.sub("", text)
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
    return "\n".join(out_lines).strip()


def parse_steam_entry(appid: str, entry, index: int = 0) -> dict:
    """把单个 RSS item 解析为标准化 Steam 公告 dict。

    返回字段：
        announcement_id / title / text / permalink / published_at / image_urls
        + 兼容旧字段：id / url / content / categories / source / appid / account_name
    """
    title = (entry.get("title", "") or "").strip()
    summary = entry.get("summary", entry.get("description", "")) or ""
    link = entry.get("link", "") or ""
    published = _parse_published(entry)
    announcement_id = extract_announcement_id(entry)

    text = html_to_text(summary)
    if not text:
        text = title

    return {
        "announcement_id": announcement_id,
        "title": title,
        "text": text,
        "permalink": link,
        "published_at": published,
        "image_urls": extract_images(summary),
        # ── 兼容旧字段 ──
        "id": f"steam_{appid}_{announcement_id}" if announcement_id else f"steam_{appid}_{published}_{index}",
        "url": link,
        "content": text,
        "categories": ["官方资讯", "Steam"],
        "source": "steam_rss",
        "appid": appid,
        "account_name": f"Steam AppID {appid}",
    }


class SteamFeedFetcher:
    """Steam 社区 RSS 拉取器（https://steamcommunity.com/games/{appid}/rss）。"""

    def __init__(
        self,
        appid: int = DEFAULT_APPID,
        rss_url: Optional[str] = None,
        state_path: str = "./data/steam_feed_state.json",
    ):
        self.appid = str(appid)
        self.rss_url = (rss_url or DEFAULT_RSS_TEMPLATE).format(appid=self.appid)
        self.state_path = Path(state_path)
        self.last_fetch: dict[str, str] = {}  # appid -> ISO timestamp
        self._load_state()

    def _load_state(self):
        if self.state_path.exists():
            try:
                self.last_fetch = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, KeyError):
                self.last_fetch = {}

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.last_fetch, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    async def _fetch_feed(self):
        """拉取 RSS feed；失败返回 None。"""
        try:
            return await asyncio.to_thread(feedparser.parse, self.rss_url)
        except Exception as e:
            logger.warning(f"Steam RSS 解析失败 (appid={self.appid}): {e}")
            return None

    async def fetch_latest(self, limit: int = 3) -> list[dict]:
        """拉取最近 limit 条公告/新闻（时间倒序，按 feed 顺序即为最新在前）。

        用于 /steam新闻 指令：直接返回，不改变水位线。
        """
        feed = await self._fetch_feed()
        if not feed or not feed.entries:
            logger.warning(f"Steam RSS 无内容 (appid={self.appid}, url={self.rss_url})")
            return []
        entries = [
            parse_steam_entry(self.appid, e, i)
            for i, e in enumerate(feed.entries[:limit])
        ]
        logger.info(f"Steam 拉取到 {len(entries)} 条公告 (appid={self.appid})")
        return entries

    async def fetch_new(
        self,
        pushed_ids: Optional[set[str]] = None,
        min_published_at: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """拉取未推送过的新公告（后台轮询用，幂等防重推）。

        - pushed_ids：已推送的 announcement_id 集合；None 表示不过滤（全部返回）。
        - min_published_at：仅返回发布时间 >= 该 ISO 时间戳的公告（首启水位线）。
        - 按发布时间升序返回（先推更早的），最多 limit 条。
        """
        feed = await self._fetch_feed()
        if not feed or not feed.entries:
            logger.debug(f"Steam RSS 无内容或拉取失败 (appid={self.appid})")
            return []

        items: list[dict] = []
        for i, entry in enumerate(feed.entries):
            item = parse_steam_entry(self.appid, entry, i)
            aid = item.get("announcement_id", "")
            if not aid:
                continue  # 无 id 的条目跳过（无法幂等）
            if pushed_ids is not None and aid in pushed_ids:
                continue
            if min_published_at:
                try:
                    ts = datetime.fromisoformat(item["published_at"])
                    ts_min = datetime.fromisoformat(min_published_at)
                    if ts < ts_min:
                        continue
                except (ValueError, TypeError):
                    pass
            items.append(item)

        items.sort(key=lambda t: t["published_at"])
        out = items[:limit]
        if out:
            self.last_fetch[self.appid] = out[-1]["published_at"]
            self._save_state()
        logger.info(f"Steam 拉取到 {len(out)} 条未推送公告 (appid={self.appid})")
        return out


# ── 便于 main.py / agent.core.py 调用的便捷函数 ──

async def fetch_steam_news(
    appid: int = DEFAULT_APPID,
    rss_url: Optional[str] = None,
    limit: int = 3,
) -> list[dict]:
    """便捷函数：拉取 Steam 最近公告/新闻（供 /steam新闻 指令调用）。"""
    fetcher = SteamFeedFetcher(appid=appid, rss_url=rss_url)
    return await fetcher.fetch_latest(limit=limit)


async def fetch_new_steam_news(
    appid: int = DEFAULT_APPID,
    rss_url: Optional[str] = None,
    state_path: str = "./data/steam_feed_state.json",
    pushed_ids: Optional[set[str]] = None,
    min_published_at: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """便捷函数：拉取未推送的新公告（供后台轮询调用）。"""
    fetcher = SteamFeedFetcher(appid=appid, rss_url=rss_url, state_path=state_path)
    return await fetcher.fetch_new(
        pushed_ids=pushed_ids,
        min_published_at=min_published_at,
        limit=limit,
    )
