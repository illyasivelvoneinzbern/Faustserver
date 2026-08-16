"""
边狱巴士 Wiki 爬虫：使用 Playwright 无头浏览器绕过 CloudFlare 防护。
通过真实 Chromium 浏览器的 TLS 指纹和 JS 引擎通过 WAF 验证。
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from crawler.parser import wikitext_to_markdown
from crawler.cleaner import clean_text
from crawler.export import load_jsonl
from crawler.html_extractor import extract_from_html, classify_page_type_from_categories, \
    extract_story_dialogue_from_wikitext
from crawler.structured_exporter import (
    export_gift_records,
    export_persona_records,
    rebuild_all,
    rebuild_enemies,
    rebuild_events,
    rebuild_gifts,
)
from crawler.passives_data import (
    fetch_passives,
    load_passives_index,
    save_passives_index,
    reload_passives_index,
)
from crawler.buffs_data import (
    fetch_buffs,
    load_buffs_index,
    save_buffs_index,
    reload_buffs_index,
)

# ── 需要走 HTML 结构化提取的页面类型（跳过 WikiText 流程）──
_STRUCTURED_PAGE_TYPES = {"personality", "ego", "story_note", "story_dialogue", "status_effect", "knowledge", "enemy", "event"}

logger = logging.getLogger(__name__)

WIKI_BASE = "https://limbuscompany.huijiwiki.com"
API_BASE = f"{WIKI_BASE}/api.php"

# 增量缓存文件（存储 {title: revid}，revid 跨 session 绝对稳定）
STATE_FILE = ".crawl_state.json"
# 默认输出文件名
OUTPUT_FILE = "wiki_pages.jsonl"
# 饰品数据文件名（从 Data:Giftchoose.tabx 解析）
ACCESSORIES_FILE = "wiki_accessories.jsonl"

# Data:Giftchoose.tabx 页面名（灰机 Wiki 的 Tabx 扩展数据页）
TABX_GIFT_PAGE = "Data:Giftchoose.tabx"

# Tabx data 数组字段索引映射（与 schema.fields 定义一致）
TABX_FIELD_ID = 0          # 饰品ID
TABX_FIELD_NAME = 1        # 饰品名
TABX_FIELD_PNG = 2         # 图片文件名
TABX_FIELD_RARITY = 3      # 稀有度 (0-6)
TABX_FIELD_COST = 4        # 镜牢经费
TABX_FIELD_AFF = 5         # 攻击类型 (愤怒/色欲/怠惰/暴食/忧郁/傲慢)
TABX_FIELD_EFFECT = 6      # 效果类型 (烧伤/流血/震颤/破裂/沉沦/呼吸/充能/泛用/斩击/突刺/打击)
TABX_FIELD_EVENT = 7       # 事件来源
TABX_FIELD_WHERE = 8       # 出现地点
TABX_FIELD_SPECIAL = 9     # 特定卡包/条件
TABX_FIELD_DESC = 10       # 描述/关键词 (未升级效果)
TABX_FIELD_DESC2 = 11      # 升级效果2
TABX_FIELD_DESC3 = 12      # 升级效果3
TABX_FIELD_DESC_1 = 13     # desc_1 (备用)
TABX_FIELD_DESC_2 = 14     # desc_2 (备用)
TABX_FIELD_SOURCE = 15     # 数据来源表名 (= "Gift")


class WikiSpider:
    """边狱巴士 Wiki 爬虫（Playwright 无头浏览器绕过 CloudFlare）"""

    # 排除的前缀（非内容页面）
    EXCLUDE_PREFIXES = [
        "模板:", "分类:", "文件:", "File:", "用户:", "User:",
        "MediaWiki:", "帮助:", "Help:", "特殊:", "Special:",
        "项目:", "模块:", "Widget:", "Gadget:",
    ]

    # 分类过滤白名单（仅保留以下分类的页面）
    TARGET_CATEGORIES = [
        "人格", "E.G.O", "罪人", "主线剧情", "异想体",
        "组织", "道具", "异常", "背景", "设定",
        "战斗数据", "主线战斗", "事件",  # 敌人数据、探索事件
        "基础数值", "攻击抗性与类型", "技能与拼点", "伤害计算",  # 基础机制
    ]

    def __init__(
        self,
        output_dir: str = "./data/raw",
        delay: float = 2.0,
        headless: bool = True,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._page = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        # 隐藏自动化痕迹，降低被检测概率
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
        """)
        self._page = await context.new_page()
        await self._warmup()
        return self

    async def _warmup(self):
        """预热：先访问首页建立会话，等待 CloudFlare JS 验证通过"""
        try:
            logger.info("正在预热浏览器，访问 Wiki 首页...")
            resp = await self._page.goto(
                f"{WIKI_BASE}/wiki/%E9%A6%96%E9%A1%B5",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            logger.info(f"首页响应状态: {resp.status}")
            # 等待 CloudFlare JS 验证完成（如有）
            await asyncio.sleep(3)
            title = await self._page.title()
            logger.info(f"当前页面标题: {title}")
        except Exception as e:
            logger.warning(f"预热访问异常（将继续尝试 API 调用）: {e}")

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _api_get_json(self, params: dict) -> dict:
        """在浏览器上下文中通过 fetch 调用 API，继承浏览器的 TLS 指纹和 Cookie"""
        query_string = urlencode(params)
        url = f"{API_BASE}?{query_string}"
        try:
            result = await self._page.evaluate("""
                async (url) => {
                    const resp = await fetch(url, { credentials: 'include' });
                    if (!resp.ok) return {};
                    return await resp.json();
                }
            """, url)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.warning(f"API JSON 请求失败: {e}")
            return {}

    async def _fetch_tabx_gifts(self) -> list[dict]:
        """从 Data:Giftchoose.tabx 页面获取所有 E.G.O 饰品结构化数据。

        灰机 Wiki 使用 Tabx 扩展存储结构化数据（而非 Cargo）。
        Data:Giftchoose.tabx 是一个 JSON 页面，包含：
        - schema.fields: 字段定义数组
        - data: 二维数组，每行是一条饰品记录
        """
        logger.info("正在从 Data:Giftchoose.tabx 获取饰品数据...")
        raw = await self.fetch_page_raw(TABX_GIFT_PAGE)
        if not raw:
            logger.error("Data:Giftchoose.tabx 获取失败")
            return []

        try:
            tabx = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Data:Giftchoose.tabx JSON 解析失败: {e}")
            return []

        rows = tabx.get("data", [])
        if not rows:
            logger.error("Data:Giftchoose.tabx 中 data 字段为空")
            return []

        logger.info(f"Data:Giftchoose.tabx 包含 {len(rows)} 条饰品记录")

        all_items = []

        def _val(row: list, idx: int) -> str:
            """安全获取字段值，None/null 返回空字符串"""
            if idx >= len(row):
                return ""
            v = row[idx]
            if v is None:
                return ""
            s = str(v).strip()
            return "" if s.lower() in ("none", "null") else s

        def _clean_wikitext(text: str) -> str:
            """清理 WikiText 语法为纯文本。
            
            - {{状态2|NAME|...}} → NAME
            - {{...}} 其他模板 → 移除
            - <br> → 换行
            - {{名词|...}} → 移除
            - [[...]] → 内部文本
            """
            if not text:
                return text
            # {{状态2|NAME|...}} → NAME
            text = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', text)
            # {{名词|...}} → 移除
            text = re.sub(r'\{\{名词\|[^}]*\}\}', '', text)
            # 其他简单模板 → 移除
            text = re.sub(r'\{\{[^{}|]+\}\}', '', text)
            # [[链接|显示]] → 显示, [[链接]] → 链接
            text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
            text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
            # <br> / <br/> / <br /> → 换行
            text = re.sub(r'<br\s*/?>', '\n', text)
            # 压缩多余空行
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()

        def _split_versions(desc: str, desc2: str = "", desc3: str = "") -> dict:
            """将 desc 字段按版本标记拆分为 base / upgraded_2 / upgraded_3。

            优先级：
            1. 独立 desc2/desc3 字段（部分饰品有）
            2. desc 内联标记：2级： / 3级
            """
            versions = {}
            desc = _clean_wikitext(desc)
            desc2 = _clean_wikitext(desc2)
            desc3 = _clean_wikitext(desc3)

            if desc2 or desc3:
                # 有独立字段，直接使用
                versions["base"] = desc
                if desc2:
                    versions["upgraded_2"] = desc2
                if desc3:
                    versions["upgraded_3"] = desc3
                return versions

            # 在 desc 中按内联标记拆分
            # 匹配 "2级" 后跟冒号（中/英）或游戏术语 "波次"
            # 不要求行首锚点，支持行内标记
            parts2 = re.split(r'\s*2级(?:[：:]|波次)\s*', desc, maxsplit=1)
            if len(parts2) == 1:
                # 没有升级标记，只有基础版
                versions["base"] = parts2[0]
                return versions

            versions["base"] = parts2[0]
            remaining = parts2[1]

            # 匹配 "3级" 后跟可选冒号（中/英）或游戏术语 "波次"
            parts3 = re.split(r'\s*3级(?:[：:]|波次)?\s*', remaining, maxsplit=1)
            if len(parts3) == 1:
                versions["upgraded_2"] = parts3[0]
            else:
                versions["upgraded_2"] = parts3[0]
                versions["upgraded_3"] = parts3[1]

            return versions

        def _build_tagline(where: str, effect: str) -> str:
            """构建标题标签行：名称：[出现地点][效果类型]"""
            tags = []
            if where:
                tags.append(where)
            if effect:
                tags.append(effect)
            if tags:
                return "[" + "][".join(tags) + "]"
            return ""

        def _build_gift_content(name: str, tagline: str, cost: str, desc_text: str,
                                stage_label: str = "") -> str:
            """构建单个版本的 content 文本。"""
            lines = [f"{name}：{tagline}" if tagline else name]
            cost_val = cost if cost else "?"
            lines.append(f"镜牢经费.png：{cost_val}")
            if desc_text:
                lines.append(desc_text)
            if stage_label:
                lines.append(stage_label)
            return "\n".join(lines)

        # 阶段标签映射
        STAGE_LABELS = {
            "base": "（未强化版）",
            "upgraded_2": "（强化版·Ⅱ级）",
            "upgraded_3": "（强化版·Ⅲ级）",
        }

        for row in rows:
            if not isinstance(row, list) or len(row) <= TABX_FIELD_NAME:
                continue

            item_id = _val(row, TABX_FIELD_ID)
            name = _val(row, TABX_FIELD_NAME)
            if not name:
                continue

            rarity = _val(row, TABX_FIELD_RARITY)
            cost = _val(row, TABX_FIELD_COST)
            effect = _val(row, TABX_FIELD_EFFECT)
            aff = _val(row, TABX_FIELD_AFF)
            where = _val(row, TABX_FIELD_WHERE)
            special = _val(row, TABX_FIELD_SPECIAL)
            event = _val(row, TABX_FIELD_EVENT)
            desc = _val(row, TABX_FIELD_DESC)
            desc2 = _val(row, TABX_FIELD_DESC2)
            desc3 = _val(row, TABX_FIELD_DESC3)

            # 稀有度数值 (0~6)
            try:
                rarity_int = int(rarity)
            except (ValueError, TypeError):
                rarity_int = -1

            # 拆分版本
            versions = _split_versions(desc, desc2, desc3)
            tagline = _build_tagline(where, effect)

            # 收集特殊条件（如 "时间杀人时间合成"）
            extra_metadata = {}
            if special:
                extra_metadata["special"] = _clean_wikitext(special)
            if event:
                extra_metadata["event"] = _clean_wikitext(event)

            # 为每个版本生成独立的记录
            for stage, stage_desc in versions.items():
                stage_label = STAGE_LABELS.get(stage, "")
                content = _build_gift_content(name, tagline, cost, stage_desc, stage_label)

                base_id = f"gift_{item_id}"
                if stage == "base":
                    record_id = base_id
                else:
                    record_id = f"{base_id}_{stage}"

                record = {
                    "id": record_id,
                    "title": name,
                    "url": f"{WIKI_BASE}/wiki/E.G.O饰品",
                    "categories": ["E.G.O饰品"],
                    "content": content,
                    "source": "tabx",
                    "_structured": True,
                    "page_type": "ego_gift",
                    # ── 结构化字段（供 ChromaDB 过滤）──
                    "gift_name": name,
                    "rarity": rarity_int,
                    "cost": cost,
                    "effect_types": effect,
                    "attack_type": aff,
                    "location": where,
                    "stage": stage,
                    **extra_metadata,
                }
                all_items.append(record)

        logger.info(f"✅ Tabx 饰品解析完成: 共 {len(all_items)} 条 E.G.O 饰品记录（{len(rows)} 种饰品）")
        return all_items

    async def fetch_cargo_accessories(self) -> list[dict]:
        """获取 E.G.O 饰品结构化数据（兼容旧接口名，实际从 Tabx 读取）。"""
        return await self._fetch_tabx_gifts()

    async def fetch_all_pages(self) -> list[str]:
        """通过 MediaWiki API 获取所有内容页面标题列表"""
        titles = []
        apcontinue = None

        logger.info("正在从 API 获取页面列表...")

        while True:
            params = {
                "action": "query",
                "list": "allpages",
                "aplimit": "500",
                "format": "json",
            }
            if apcontinue:
                params["apcontinue"] = apcontinue

            data = await self._api_get_json(params)
            if not data:
                logger.error("API 返回空数据，可能已被 CloudFlare 拦截")
                break

            for page in data.get("query", {}).get("allpages", []):
                title = page["title"]

                # 过滤非内容页面
                if any(title.startswith(p) for p in self.EXCLUDE_PREFIXES):
                    continue

                titles.append(title)

            # 每 200 页或每次 API 返回时报告进度
            if len(titles) % 200 < 500:
                logger.info(f"  📋 已发现 {len(titles)} 个内容页面...")

            if "continue" in data:
                apcontinue = data["continue"]["apcontinue"]
                await asyncio.sleep(0.5)
            else:
                break

        logger.info(f"✅ 页面列表获取完成：共 {len(titles)} 个内容页面")
        return titles

    async def _fetch_revids(self, titles: list[str]) -> dict[str, int]:
        """批量查询页面的最后修订版本号（revid）。

        revid 只在内容编辑时递增，跨 session 绝对稳定，
        是比内容 hash 更可靠的增量判断依据。
        每次查询最多 50 页。
        """
        revids = {}
        batch_size = 50

        for i in range(0, len(titles), batch_size):
            batch = titles[i:i + batch_size]
            params = {
                "action": "query",
                "prop": "revisions",
                "rvprop": "ids",
                "titles": "|".join(batch),
                "format": "json",
            }
            data = await self._api_get_json(params)
            pages = data.get("query", {}).get("pages", {})

            for page_id, page_data in pages.items():
                title = page_data.get("title", "")
                revs = page_data.get("revisions", [])
                if revs:
                    revids[title] = revs[0]["revid"]

            # 避免 API 频率限制
            if i + batch_size < len(titles):
                await asyncio.sleep(0.3)

        return revids

    async def fetch_page_raw(self, title: str) -> Optional[str]:
        """获取页面的原始 WikiText（通过 MediaWiki API query + revisions 端点）。

        注意：action=raw 不是 API 参数，必须用 query+revisions+rvprop=content。
        """
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": title,
            "format": "json",
        }
        data = await self._api_get_json(params)
        pages = data.get("query", {}).get("pages", {})
        for page_id, page_data in pages.items():
            revisions = page_data.get("revisions", [])
            if revisions:
                # 尝试从 slots/main 获取（MediaWiki 1.31+），失败则用顶级 * 字段
                slots = revisions[0].get("slots", {})
                main_slot = slots.get("main", {})
                content = main_slot.get("*", "") or revisions[0].get("*", "")
                if content:
                    return content
        return None

    async def fetch_page_html(self, title: str) -> Optional[str]:
        """获取页面的渲染后 HTML（通过 MediaWiki action=parse API）。

        用于人格/EGO 等需要结构化提取的页面类型。
        服务端渲染，返回的 HTML 包含所有 tab-pane 中的 I-IV 阶段数据。
        """
        params = {
            "action": "parse",
            "page": title,
            "prop": "text",
            "format": "json",
        }
        data = await self._api_get_json(params)
        parse_result = data.get("parse", {})
        text_data = parse_result.get("text", {})
        html = text_data.get("*", "")
        if html:
            return html
        return None

    # ── 增量缓存管理（基于 revid）──

    def _state_path(self) -> Path:
        return self.output_dir / STATE_FILE

    def _load_state(self) -> dict[str, int]:
        """加载增量缓存 {title: revid}"""
        path = self._state_path()
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("增量缓存损坏，将视为首次抓取")
        return {}

    def _save_state(self, state: dict[str, int]):
        """保存增量缓存"""
        path = self._state_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _load_existing_results(self) -> dict[str, dict]:
        """从已有 JSONL 加载已抓取的数据，构建 {title: record} 字典"""
        output_path = self.output_dir / OUTPUT_FILE
        existing = {}
        if output_path.exists():
            with open(output_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        existing[record["title"]] = record
                    except (json.JSONDecodeError, KeyError):
                        pass
        return existing

    async def crawl_page(self, title: str) -> Optional[dict]:
        """爬取单个页面，返回结构化数据。

        对人格/EGO/状态效果 页面：使用 action=parse 获取渲染 HTML + 结构化提取。
        对故事对话页面：直接从 WikiText 解析 Dialog 模板。
        对但丁笔记页面：使用 Playwright 渲染 JS 生成的 DOM。
        对其他页面：使用 action=query 获取 WikiText + 传统清洗流程。

        合并 content + categories 查询为一次 API 调用，
        减少 HTTP 往返次数（从 3 次降为 1 次）。
        """
        # ── 先获取 categories 判断页面类型（只需要一次 API 调用）──
        params = {
            "action": "query",
            "prop": "revisions|categories",
            "rvprop": "content",
            "rvslots": "main",
            "titles": title,
            "format": "json",
            "cllimit": "50",
        }
        data = await self._api_get_json(params)
        pages = data.get("query", {}).get("pages", {})
        for page_id, page_data in pages.items():
            # 1) 提取原始 WikiText
            revisions = page_data.get("revisions", [])
            raw = ""
            if revisions:
                slots = revisions[0].get("slots", {})
                main_slot = slots.get("main", {})
                raw = main_slot.get("*", "") or revisions[0].get("*", "")

            if not raw or not raw.strip():
                continue

            # 2) 提取分类标签
            cats = page_data.get("categories", [])
            categories = [c["title"].replace("分类:", "") for c in cats]

            # 3) 判断是否需要 HTML 结构化提取（剧情类需要 WikiText 辅助判断）
            page_type = classify_page_type_from_categories(categories, raw, title)

            if page_type in _STRUCTURED_PAGE_TYPES:
                logger.debug(f"页面 {title} 类型为 {page_type}，使用结构化提取")

                # ── 故事对话：直接从 WikiText 解析，无需 HTML ──
                if page_type == "story_dialogue":
                    structured = extract_story_dialogue_from_wikitext(raw, title, categories)
                    if structured:
                        structured["id"] = f"wiki_{title.replace('/', '_').replace(' ', '_')}"
                        structured["url"] = f"{WIKI_BASE}/wiki/{title}"
                        structured["categories"] = categories
                        structured["source"] = "wiki"
                        await asyncio.sleep(self.delay)
                        return structured
                    else:
                        logger.warning(f"故事对话解析失败，回退到 WikiText 流程: {title}")

                # ── 但丁笔记：用 Playwright 渲染 JS DOM ──
                elif page_type == "story_note":
                    html = await self._fetch_page_with_playwright(title)
                    if html:
                        structured = extract_from_html(
                            html, title, categories, wikitext=raw, page_type=page_type,
                        )
                        if structured:
                            structured["id"] = f"wiki_{title.replace('/', '_').replace(' ', '_')}"
                            structured["url"] = f"{WIKI_BASE}/wiki/{title}"
                            structured["categories"] = categories
                            structured["source"] = "wiki"
                            await asyncio.sleep(self.delay)
                            return structured
                        else:
                            logger.warning(f"但丁笔记 HTML 提取失败: {title}")
                    else:
                        logger.warning(f"Playwright 渲染失败，回退到 WikiText 流程: {title}")

                # ── 人格/EGO/状态效果：优先 Playwright 完整渲染（buff 渲染为中文）──
                #    浏览器完整渲染会执行 BuffPro JS gadget，将 {{BuffPro|Code}} 直接渲染为
                #    中文 buff 名，彻底摆脱手工映射表（P21-B「拉取」方案）。
                #    渲染失败时回退 action=parse + 映射表兜底（resolve_buff_codes_in_text）。
                elif page_type in ("personality", "ego", "status_effect"):
                    html = await self._fetch_page_with_playwright(title)
                    if not html:
                        logger.info(f"Playwright 渲染失败，回退 action=parse: {title}")
                        html = await self.fetch_page_html(title)
                    if html:
                        structured = extract_from_html(
                            html, title, categories, wikitext=raw, page_type=page_type,
                        )
                        if structured:
                            structured["id"] = f"wiki_{title.replace('/', '_').replace(' ', '_')}"
                            structured["url"] = f"{WIKI_BASE}/wiki/{title}"
                            structured["categories"] = categories
                            structured["source"] = "wiki"
                            await asyncio.sleep(self.delay)
                            return structured
                        else:
                            logger.warning(f"HTML 结构化提取失败，回退到 WikiText 流程: {title}")
                    else:
                        logger.warning(f"HTML 获取失败，回退到 WikiText 流程: {title}")

                # ── 敌方/事件：优先 Playwright 完整渲染（修复"状态效果显示英文"）──
                #   敌方技能 wikitext 用 {{BuffPro|Code}} 英文引用，中文化由站点
                #   JS gadget 执行；action=parse 服务端 HTML 只输出
                #   <span class="buffPro">Code</span> 英文占位。Playwright 渲染后
                #   buffPro span 为中文名，可构建页面级 code→中文 配对映射
                #   （html_extractor.EnemyExtractor._buff_code_map）。
                #   渲染失败时回退 action=parse（英文 code 由 resolve_buff_codes_in_text
                #   静态表兜底，专属 code 仍可能英文，但不影响主链路）。
                else:
                    html = await self._fetch_page_with_playwright(title)
                    if not html:
                        logger.info(f"Playwright 渲染失败，回退 action=parse: {title}")
                        html = await self.fetch_page_html(title)
                    if html:
                        structured = extract_from_html(
                            html, title, categories, wikitext=raw, page_type=page_type,
                        )
                        if structured:
                            structured["id"] = f"wiki_{title.replace('/', '_').replace(' ', '_')}"
                            structured["url"] = f"{WIKI_BASE}/wiki/{title}"
                            structured["categories"] = categories
                            structured["source"] = "wiki"
                            await asyncio.sleep(self.delay)
                            return structured
                        else:
                            logger.warning(f"HTML 结构化提取失败，回退到 WikiText 流程: {title}")
                    else:
                        logger.warning(f"HTML 获取失败，回退到 WikiText 流程: {title}")

            # 4) 传统流程：WikiText 解析 + 清洗
            markdown = wikitext_to_markdown(raw, title)
            content = clean_text(markdown)

            if len(content) < 50:
                logger.debug(f"页面内容过短，跳过: {title}")
                return None

            await asyncio.sleep(self.delay)

            return {
                "id": f"wiki_{title.replace('/', '_').replace(' ', '_')}",
                "title": title,
                "url": f"{WIKI_BASE}/wiki/{title}",
                "categories": categories,
                "content": content,
                "source": "wiki",
            }

        return None

    async def _fetch_page_with_playwright(self, title: str) -> Optional[str]:
        """用 Playwright 渲染页面，执行 JS 后获取完整 HTML。

        用于但丁笔记等依赖 {{#html:Dantenote}} JS 模板动态生成内容的页面。
        """
        url = f"{WIKI_BASE}/wiki/{title}"
        try:
            resp = await self._page.goto(url, wait_until="networkidle", timeout=30000)
            if resp and resp.status >= 400:
                logger.warning(f"Playwright 访问 {title} 返回状态 {resp.status}")
                return None
            # 等待 JS 渲染完成
            await asyncio.sleep(2)
            html = await self._page.content()
            return html
        except Exception as e:
            logger.warning(f"Playwright 渲染 {title} 失败: {e}")
            return None

    async def crawl_all(self, limit: int = 0) -> list[dict]:
        """爬取所有页面，默认增量模式（limit=0 表示不限制）。

        增量逻辑（revid 方案）：
        1. 获取所有页面标题
        2. 批量查询 revid（50页/次，revid 只在编辑时递增，跨 session 稳定）
        3. 与本地缓存 {title: old_revid} 比较
        4. revid 相同的直接从已有 JSONL 复用，不同的才 fetch→parse→clean
        5. 完成后更新缓存
        """
        titles = await self.fetch_all_pages()
        total_titles = len(titles)
        if limit > 0:
            titles = titles[:limit]

        # 加载增量状态 {title: revid}
        old_state = self._load_state()

        # 批量查询当前 revid
        logger.info(f"正在查询 {len(titles)} 个页面的修订版本号（{len(titles)//50 + 1} 批）...")
        current_revids = await self._fetch_revids(titles)
        logger.info(f"版本号查询完成，共获取 {len(current_revids)} 个页面的 revid")

        # 加载已有数据用于复用
        existing_data = self._load_existing_results()

        # 识别需要更新的页面：revid 不同 或 新页面 或 旧缓存缺失
        changed = 0
        new_pages = 0
        for title in titles:
            cur_rev = current_revids.get(title)
            old_rev = old_state.get(title) if old_state else None
            if cur_rev is not None and old_rev is not None and cur_rev == old_rev and title in existing_data:
                changed += 1  # 未变，待复用
            elif cur_rev is not None and old_rev is None:
                new_pages += 1  # 新页面

        to_fetch = len(titles) - changed  # 需要实际抓取的页面数
        logger.info(
            f"增量分析完成：{len(titles)} 个页面中，"
            f"{changed} 个未变更（复用），{new_pages} 个新页面，"
            f"{to_fetch} 个需抓取"
        )

        results = []
        failed = 0
        skipped = 0
        reused = 0
        start_time = time.time()
        total = len(titles)

        for i, title in enumerate(titles):
            idx = i + 1
            cur_rev = current_revids.get(title)

            # 判断是否可复用
            can_reuse = (
                old_state
                and cur_rev is not None
                and old_state.get(title) == cur_rev
                and title in existing_data
            )

            if can_reuse:
                results.append(existing_data[title])
                reused += 1
            else:
                try:
                    page = await self.crawl_page(title)
                    if page:
                        results.append(page)
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"抓取页面 {title} 异常: {e}")

            # 进度显示
            progress_pct = idx * 100 // total
            if idx % 20 == 0 or idx == total:
                elapsed = time.time() - start_time
                speed = idx / elapsed * 60 if elapsed > 0 else 0
                if idx < total:
                    eta_sec = (total - idx) / speed * 60 if speed > 0 else 0
                    eta_str = f"{eta_sec:.0f}s" if eta_sec < 120 else f"{eta_sec/60:.1f}min"
                else:
                    eta_str = "完成"

                new_count = len(results) - reused
                logger.info(
                    f"[{idx:>4}/{total}] {progress_pct:>3}% | "
                    f"新增:{new_count} 复用:{reused} 跳过:{skipped} 失败:{failed} | "
                    f"速度:{speed:.1f}页/分 | 剩余:{eta_str}"
                )

        # 保存增量状态（存储所有页面的最新 revid）
        self._save_state(current_revids)

        elapsed_total = time.time() - start_time
        new_count = len(results) - reused
        logger.info(
            f"✅ 抓取完成！耗时 {elapsed_total/60:.1f} 分钟 | "
            f"新增 {new_count} | 复用 {reused} | 跳过 {skipped} | 失败 {failed}"
        )
        return results

    def save_results(self, results: list[dict], filename: str = "wiki_pages.jsonl"):
        """保存爬取结果（覆盖写入，保证 JSONL 完整）"""
        output_path = self.output_dir / filename
        with open(output_path, "w", encoding="utf-8") as f:
            for item in results:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        logger.info(f"已保存 {len(results)} 条记录到 {output_path}")

    def save_results_incremental(self, new_results: list[dict], filename: str = "wiki_pages.jsonl"):
        """增量保存：合并已有数据 + 新/更新数据，按 title 去重"""
        output_path = self.output_dir / filename

        # 加载已有数据
        existing = self._load_existing_results()

        # 合并：新数据覆盖旧数据（同 title）
        for record in new_results:
            existing[record["title"]] = record

        # 写入合并后的完整数据
        merged = list(existing.values())
        with open(output_path, "w", encoding="utf-8") as f:
            for item in merged:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        logger.info(f"已保存 {len(merged)} 条记录到 {output_path}（新增/更新 {len(new_results)} 条，复用 {len(merged) - len(new_results)} 条）")

        # 同步导出人格结构化 JSON（新增/更新的人格按 title 覆盖）
        export_persona_records(new_results)


async def crawl_wiki(output_dir: str = "./data/raw", limit: int = 0, full_crawl: bool = False,
                     fetch_accessories: bool = True):
    """便捷函数：爬取 Wiki 并保存

    Args:
        output_dir: 输出目录
        limit: 限制爬取页面数，0 表示不限制
        full_crawl: True=全量重抓（删除增量缓存 + 旧数据文件），False=增量模式
        fetch_accessories: 是否同时从 Tabx (Data:Giftchoose.tabx) 抓取 E.G.O 饰品结构化数据
    """
    async with WikiSpider(output_dir=output_dir) as spider:
        # 全量模式：删除增量缓存 + 旧 JSONL 数据，确保从头开始
        if full_crawl:
            state_path = spider._state_path()
            if state_path.exists():
                state_path.unlink()
                logger.info("已删除增量缓存 (.crawl_state.json)")
            jsonl_path = spider.output_dir / OUTPUT_FILE
            if jsonl_path.exists():
                jsonl_path.unlink()
                logger.info("已删除旧数据文件 (wiki_pages.jsonl)")
            acc_path = spider.output_dir / ACCESSORIES_FILE
            if acc_path.exists():
                acc_path.unlink()
                logger.info("已删除旧饰品数据文件 (wiki_accessories.jsonl)")
            # 全量模式：同时删除旧的被动/缓冲映射索引，确保下次定向抓取重写
            from crawler.passives_data import PASSIVES_INDEX_FILE, DEFAULT_INDEX_DIR
            from crawler.buffs_data import BUFFS_INDEX_FILE
            pv_path = Path(DEFAULT_INDEX_DIR) / PASSIVES_INDEX_FILE
            if pv_path.exists():
                pv_path.unlink()
                logger.info(f"已删除旧被动映射文件 ({PASSIVES_INDEX_FILE})")
                reload_passives_index()
            bf_path = Path(DEFAULT_INDEX_DIR) / BUFFS_INDEX_FILE
            if bf_path.exists():
                bf_path.unlink()
                logger.info(f"已删除旧 buff 映射文件 ({BUFFS_INDEX_FILE})")
                reload_buffs_index()

        results = await spider.crawl_all(limit=limit)

        # 全量模式：直接覆盖写入；增量模式：合并追加
        if full_crawl:
            spider.save_results(results)
        else:
            spider.save_results_incremental(results)

        logger.info(f"Wiki 爬取完成，共 {len(results)} 条有效记录")

        # 抓取饰品数据（从 Data:Giftchoose.tabx）
        accessories = []
        acc_path = spider.output_dir / ACCESSORIES_FILE
        if fetch_accessories:
            # 增量模式：如果饰品文件已存在则直接加载复用，避免重复 Tabx API 调用
            if not full_crawl and acc_path.exists():
                accessories = load_jsonl(str(acc_path))
                logger.info(f"饰品数据已存在，从缓存加载 {len(accessories)} 条")
            else:
                accessories = await spider.fetch_cargo_accessories()
                if accessories:
                    with open(acc_path, "w", encoding="utf-8") as f:
                        for item in accessories:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    logger.info(f"已保存 {len(accessories)} 条饰品数据到 {acc_path}")

        # E.G.O 饰品结构化导出：直接写入 data/structured/gift_*.json（供 Gift Direct Answer 直答）
        if accessories:
            n = export_gift_records(accessories)
            logger.info(f"已导出 {n} 条结构化饰品数据 (data/structured/gift_*.json)")

        # 抓取人格被动映射（Data:Personalitypassives.json）→ data/structured/passives.json
        # 增量模式若已存在则复用，避免重复 Data 页 API 调用；全量模式已在上方删除旧文件强制重写
        try:
            pv_existing = load_passives_index()
            if not full_crawl and pv_existing:
                logger.info(f"人格被动映射已存在，复用 {len(pv_existing)} 条（data/structured/passives.json）")
            else:
                passives = await fetch_passives(spider)
                if passives:
                    save_passives_index(passives)
                    reload_passives_index()
        except Exception as e:
            logger.warning(f"人格被动映射抓取失败（不影响主流程）: {e}")

        # ── BuffPro code→中文名 映射（修复"状态效果显示英文"）──
        # 勘误（2026-08-15）：Data:Buffchoose.tabx 实测是「人格→拥有的 buff 类型」
        # 布尔表（schema.fields = name/belong/origin/Combustion/Laceration/...），
        # 不是 BuffPro code→中文名 映射，parse_buffs_tabx 无法产生有效映射，
        # buffs.json 从未正确生成。因此不再调用 fetch_buffs。
        # 正确来源：页面渲染 HTML（JS gadget 输出中文名），由
        # html_extractor.EnemyExtractor._buff_code_map 按页配对构建（见
        # crawler/buffs_data.build_buff_code_map_from_html）。
        # 静态兜底 DEFAULT_BUFF_CODES 保留在 buffs_data 中供无渲染时使用。
        # try:
        #     bf_existing = load_buffs_index()
        #     if not full_crawl and bf_existing:
        #         logger.info(f"buff 映射已存在，复用 {len(bf_existing)} 条（data/structured/buffs.json）")
        #     else:
        #         buffs = await fetch_buffs(spider)
        #         if buffs:
        #             save_buffs_index(buffs)
        #             reload_buffs_index()
        # except Exception as e:
        #     logger.warning(f"buff 映射抓取失败（不影响主流程）: {e}")

        # 结构化数据兜底：从最新 jsonl 重建全部人格/饰品/事件/敌方单位 JSON，
        # 保证 data/structured 全量一致
        rebuild_all(output_dir + "/" + OUTPUT_FILE)
        rebuild_gifts(output_dir + "/" + ACCESSORIES_FILE)
        rebuild_events(output_dir + "/" + OUTPUT_FILE)
        rebuild_enemies(output_dir + "/" + OUTPUT_FILE)
        logger.info("已重建结构化人格/饰品/事件/敌方单位数据目录 (data/structured)")

        return results + accessories
