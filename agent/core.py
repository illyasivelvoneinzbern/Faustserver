"""
Agent 核心模块：整合 RAG + 人格 + 安全 + 消息接入的主逻辑。
支持运行时动态人格切换、LLM Reranker、置信度评估。
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from adapter.napcat import NapCatAdapter
from adapter.router import MessageRouter
from adapter.types import QQMessage
from agent.session import SessionManager
from agent.memory import create_memory, get_chat_history_text
from agent.forward import AgentReply, split_forward_sections
from agent.tools import (
    create_persona_switch_tool,
    needs_persona_switch_llm,
    resolve_persona_id,
    run_persona_switch_preempt,
    switch_persona_impl,
)
from personas.manager import PersonaManager
from rag.chain import build_rag_chain, run_rag_query
from rag.reranker import LLMReranker
from rag.retriever import LimBusRetriever
from utils.rate_limiter import RateLimiter
from utils.sensitive_filter import SensitiveFilter
from utils.typing_delay import TypingDelaySimulator
from utils.token_saver import SemanticCache
from utils.config import get_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 抽奖（Gacha）指令预拦截（P26）
# ═══════════════════════════════════════════════════════════════════════
# 强信号正则：明确的抽卡/抽奖句式 → 确定性调用 gacha_pull（零 LLM 成本）。
# 概率（用户配置）：三灯人格 3%、二灯人格 13%、一灯人格 81%、EGO 3%。
_GACHA_STRONG_RE = re.compile(
    r"(十连|单抽|抽十连|抽一发|抽一次|抽一下|抽个|抽卡|抽奖|来一发|"
    r"gacha|抽(?:个|次|下|一)?(?:十|三|五|二)?连|抽(?:个|次|下)?(?:三|五|十)次)"
)
# 中文数字 → 连抽次数
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "五": 5, "十": 10}


def _parse_gacha_times(text: str, matched: str) -> int:
    """从命中文本解析抽取次数：十连→10，三连→3，其余→1。"""
    m = re.search(r"([一二三五十]|\d+)\s*连", text)
    if m:
        raw = m.group(1)
        if raw.isdigit():
            return int(raw)
        return _CN_DIGITS.get(raw, 1)
    return 1


class LimbusAgent:
    """边狱巴士 RAG Agent 主控制器"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = get_config(config_path)
        self.log = logger

        # ── 基础设施初始化 ──
        self._init_llm()
        self._init_safety()
        self._init_persona()
        self._init_adapter()
        self._init_session()

        # ── RAG 组件（延迟初始化，依赖 embedder）─
        self.retriever: Optional[LimBusRetriever] = None
        self.reranker: Optional[LLMReranker] = None
        self.rag_chain = None
        self._current_persona_id: str = ""
        self._memory_store: dict[str, Any] = {}  # session_id → ConversationBufferWindowMemory

        # ── 人格结构化直答（Persona Direct Answer，懒加载 data/structured 目录）──
        # initialize_rag 中按 config.agent.persona_direct 初始化
        self.persona_direct = None

        # ── E.G.O 饰品结构化直答（Gift Direct Answer，懒加载 data/structured 目录）──
        # initialize_rag 中按 config.agent.gift_direct 初始化
        self.gift_direct = None

        # ── 敌方单位结构化直答（Enemy Direct Answer，懒加载 data/structured/enemies 目录）──
        # initialize_rag 中按 config.agent.enemy_direct 初始化
        self.enemy_direct = None

        # ── 语义缓存 ──
        token_cfg = self.config.get("token_saver", {})
        self.semantic_cache = SemanticCache(
            threshold=token_cfg.get("cache_similarity_threshold", 0.92),
            max_size=200,
        ) if token_cfg.get("enable_cache") else None

        # ── 直答打包转发（Forward Reply，P40）──
        # 人格/饰品/事件/敌方/比较直答命中（跳过 RAG）时，把规范文本按节拆分为
        # 多条转发 node，通过 NapCatQQ 合并转发（send_*_forward_msg）打包发送，
        # 避免单条超长文本被 QQ 静默拒收（见 _SEND_CHUNK_MAX 症状2诊断）。
        fr_cfg = self.config.get("agent", {}).get("forward_reply", {}) or {}
        self._forward_enabled = bool(fr_cfg.get("enabled", True))
        self._forward_sender_name = str(fr_cfg.get("sender_name", "") or "").strip()
        self._forward_sender_uin = str(fr_cfg.get("sender_uin", "") or "").strip()
        self._forward_max_chars = int(fr_cfg.get("max_chars_per_node", 1500))
        self._forward_min_nodes = max(1, int(fr_cfg.get("min_nodes", 2)))

        # ── 置信度 / 自我反思（默认关闭，initialize_rag 中按配置覆盖）──
        self._confidence_enabled = False
        self._confidence_low_threshold = 0.3
        self._confidence_enable_follow_up = True
        self._reflect_enabled = False
        self._reflect_max_attempts = 1

        # ── X/Twitter 新推轮询推送（P20-B）──
        self._x_poll_task: Optional[asyncio.Task] = None
        self._x_pushed_ids: set[str] = set()          # 已推送 tweet_id（幂等）
        self._push_group_ids: set[str] = set()        # 记忆到的群号（兜底）
        self._init_x_push()

        # ── Steam 社区 RSS 新公告轮询推送（P37）──
        self._steam_poll_task: Optional[asyncio.Task] = None
        self._steam_pushed_ids: set[str] = set()      # 已推送 announcement_id（幂等）
        self._init_steam_push()

    def _init_llm(self):
        """初始化 LLM"""
        llm_cfg = self.config["llm"]
        self.llm = ChatOpenAI(
            model=llm_cfg["model"],
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg.get("base_url"),
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 512),
        )

    def _init_safety(self):
        """初始化安全防护模块"""
        rl_cfg = self.config["rate_limit"]
        self.rate_limiter = RateLimiter(
            per_user_cooldown=rl_cfg["per_user_cooldown"],
            global_per_minute=rl_cfg["global_per_minute"],
            group_per_minute=rl_cfg["group_per_minute"],
        )

        td_cfg = self.config["typing_delay"]
        self.typing_delay = TypingDelaySimulator(
            base_delay=td_cfg["base_delay"],
            char_delay_min=td_cfg["char_delay_min"],
            char_delay_max=td_cfg["char_delay_max"],
            max_delay=td_cfg["max_delay"],
        )

        sf_cfg = self.config["sensitive_filter"]
        self.sensitive_filter = SensitiveFilter(
            wordlist_path=sf_cfg["wordlist_path"],
            enable_input_filter=sf_cfg.get("enable_input_filter", True),
            enable_output_filter=sf_cfg.get("enable_output_filter", True),
            enable_topic_guard=sf_cfg.get("enable_topic_guard", True),
            max_violations=sf_cfg.get("max_violations_before_block", 3),
            violation_block_minutes=sf_cfg.get("violation_block_minutes", 30),
        )

    def _init_persona(self):
        """初始化人格管理器"""
        persona_cfg = self.config["personas"]
        self.persona_manager = PersonaManager(
            config_dir=persona_cfg.get("config_dir", "./personas")
        )

    def _init_adapter(self):
        """初始化消息接入层"""
        napcat_cfg = self.config["napcat"]
        self.adapter = NapCatAdapter(
            ws_url=napcat_cfg["ws_url"],
            reconnect_interval=napcat_cfg.get("reconnect_interval", 5),
            token=napcat_cfg.get("token", ""),
        )
        self.router = MessageRouter(
            bot_qq=napcat_cfg.get("bot_qq", ""),
            trigger_keywords=napcat_cfg.get("trigger_keywords", []),
            command_prefix=napcat_cfg.get("command_prefix", "/"),
            require_at_in_group=napcat_cfg.get("require_at_in_group", True),
        )

    def _init_session(self):
        """初始化会话管理"""
        agent_cfg = self.config["agent"]
        self.session_manager = SessionManager(
            default_persona=agent_cfg.get("default_persona", ""),
            session_timeout=agent_cfg.get("session_timeout", 3600),
        )

    # ── X/Twitter 新推轮询推送（P20-B）──

    def _init_x_push(self):
        """初始化 X 推送：加载已推 id / 记忆群号，解析配置。"""
        # 已推 id 状态文件
        x_cfg = self.config.get("x_fetcher", {})
        self._x_pushed_path = Path(
            x_cfg.get("pushed_ids_path", "data/x_pushed_ids.json")
        ).resolve()
        self._push_groups_path = Path(
            x_cfg.get("push_groups_path", "data/push_groups.json")
        ).resolve()
        self._media_dir = str(Path(
            x_cfg.get("media_dir", "data/media")
        ).resolve())

        # 媒体定期清理配置（P20 收尾）
        mcu = x_cfg.get("media_cleanup", {}) or {}
        self._media_cleanup_enabled = bool(mcu.get("enabled", False))
        self._media_cleanup_max_age_days = float(mcu.get("max_age_days", 7))
        self._media_cleanup_interval = max(1, int(mcu.get("interval_cycles", 10)))
        self._media_cleanup_keep_newest = max(0, int(mcu.get("keep_newest", 20)))

        # 加载已推 id
        try:
            if self._x_pushed_path.exists():
                data = json.loads(self._x_pushed_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._x_pushed_ids = set(str(x) for x in data)
                elif isinstance(data, dict):
                    self._x_pushed_ids = set(str(x) for x in data.get("pushed_ids", []))
                self.log.info(f"已加载 {len(self._x_pushed_ids)} 个已推送 X 推文 id")
        except Exception as e:
            self.log.warning(f"加载 X 已推 id 失败: {e}")

        # 加载记忆群号
        try:
            if self._push_groups_path.exists():
                data = json.loads(self._push_groups_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._push_group_ids = set(str(x) for x in data)
                elif isinstance(data, dict):
                    self._push_group_ids = set(str(x) for x in data.get("groups", []))
                self.log.info(f"已加载 {len(self._push_group_ids)} 个记忆群号")
        except Exception as e:
            self.log.warning(f"加载记忆群号失败: {e}")

    def _save_x_pushed(self):
        """持久化已推 tweet_id 列表。"""
        try:
            self._x_pushed_path.parent.mkdir(parents=True, exist_ok=True)
            self._x_pushed_path.write_text(
                json.dumps({"pushed_ids": sorted(self._x_pushed_ids)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.log.warning(f"保存 X 已推 id 失败: {e}")

    def _save_push_groups(self):
        """持久化记忆群号。"""
        try:
            self._push_groups_path.parent.mkdir(parents=True, exist_ok=True)
            self._push_groups_path.write_text(
                json.dumps({"groups": sorted(self._push_group_ids)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.log.warning(f"保存记忆群号失败: {e}")

    def remember_group(self, group_id: str):
        """记忆一个群号（幂等，落盘 data/push_groups.json）。"""
        if not group_id:
            return
        gid = str(group_id)
        if gid in self._push_group_ids:
            return
        self._push_group_ids.add(gid)
        self._save_push_groups()
        self.log.info(f"已记忆推送群号: {gid}")

    def _resolve_push_groups(self) -> list[str]:
        """解析目标群号列表（优先级：配置默认群 → 记忆群）。"""
        x_cfg = self.config.get("x_fetcher", {})
        default_group = x_cfg.get("push_group_id", 0) or 0
        groups: list[str] = []
        if default_group:
            groups.append(str(default_group))
        else:
            groups.extend(sorted(self._push_group_ids))
        return groups

    def _format_tweet_body(self, tweet: dict) -> str:
        """推文正文 + 官方回复段落（P38）。

        若推文挂了 official_replies（官方自回帖线程），按顺序追加
        "── 官方回复 ──" 段落（正文 + 链接），随父推文一并转发。
        """
        text = (tweet.get("text") or "").strip()
        permalink = (tweet.get("permalink") or "").strip()
        body = text if text else "(无正文)"
        if permalink:
            body = f"{body}\n{permalink}"

        max_replies = max(
            0, int(self.config.get("x_fetcher", {}).get("max_replies_per_tweet", 3))
        )
        replies = (tweet.get("official_replies") or [])[:max_replies]
        for r in replies:
            rtext = (r.get("text") or "").strip()
            rlink = (r.get("permalink") or "").strip()
            section = "── 官方回复 ──\n" + (rtext or "(无正文)")
            if rlink:
                section = f"{section}\n{rlink}"
            body = f"{body}\n\n{section}"
        return body

    def _format_tweet_text(self, tweet: dict) -> str:
        """把推文组装成可读文本（含链接 + 官方回复段落，P38）。"""
        handle = (tweet.get("handle") or "").strip()
        if tweet.get("is_reply"):
            # 无法挂靠父推文的官方回复：独立成条，标题标明"官方回复"
            header = f"【官方回复】{handle or 'LimbusCompany_B'}"
        else:
            header = f"【新推】{handle or 'LimbusCompany_B'}"
        return f"{header}\n{self._format_tweet_body(tweet)}"

    def _prepare_tweet_media(self, tweet: dict, text: str) -> tuple[str, list, list]:
        """准备推文媒体（P33）：视频推文解析低清 mp4，封面不当作普通图片。

        P38：官方自回帖（official_replies）的配图并入（父推文图片在前）。

        Returns:
            (text, images, videos)
        """
        from crawler.x_fetcher import is_video_tweet, resolve_tweet_videos
        images = list(tweet.get("image_urls") or [])
        videos = list(tweet.get("video_urls") or [])
        if is_video_tweet(tweet):
            # 视频推文：解析真实 mp4（默认高清 1080p；P34 分开发送已保证发出）
            if not videos:
                quality = str(
                    self.config.get("x_fetcher", {}).get("video_quality", "high")
                )
                videos = resolve_tweet_videos(tweet, quality=quality)
            # 视频推文的封面缩略图不作为普通图片发送（避免"模糊图片"误导）
            images = [i for i in images if "amplify_video" not in i.lower()]
            if not videos:
                # 视频解析/下载失败：不发封面，正文提示查看原推文
                text = f"{text}\n（视频未能直接下载，请点击上方原推文链接查看）"

        # P38：官方自回帖配图并入（父推文图片在前，去重）
        max_replies = max(
            0, int(self.config.get("x_fetcher", {}).get("max_replies_per_tweet", 3))
        )
        for r in (tweet.get("official_replies") or [])[:max_replies]:
            for img in r.get("image_urls") or []:
                if img not in images:
                    images.append(img)
        return text, images, videos

    async def _push_tweet_to_groups(self, tweet: dict) -> bool:
        """把单条推文推送到所有目标群。"""
        groups = self._resolve_push_groups()
        if not groups:
            self.log.warning(f"无目标群可推送（tweet_id={tweet.get('tweet_id')}），跳过")
            return False

        text, images, videos = self._prepare_tweet_media(tweet, self._format_tweet_text(tweet))

        ok = True
        for gid in groups:
            try:
                sent = await self.adapter.send_group_msg_media(
                    group_id=int(gid),
                    text=text,
                    images=images,
                    videos=videos,
                    media_dir=self._media_dir,
                    max_video_mb=float(
                        self.config.get("x_fetcher", {}).get("max_video_mb", 100.0)
                    ),
                    fallback_video_to_file=bool(
                        self.config.get("x_fetcher", {}).get("fallback_video_to_file", True)
                    ),
                    prefer_local_images=bool(
                        self.config.get("x_fetcher", {}).get("prefer_local_images", True)
                    ),
                )
                if not sent:
                    self.log.warning(f"推文推送失败 group={gid} tweet_id={tweet.get('tweet_id')}")
                    ok = False
            except Exception as e:
                self.log.error(f"推文推送异常 group={gid} tweet_id={tweet.get('tweet_id')}: {e}")
                ok = False
        return ok

    async def _x_poll_loop(self, max_rounds: Optional[int] = None):
        """后台轮询：拉取新推 → 推送 → 记录已推 id（幂等防重推）。

        单轮异常不中断循环（try/except + 继续下一轮）。
        max_rounds：可选的最大轮数；为 None 时无限循环（生产场景），
        测试可传 1 只跑单轮以验证逻辑。
        """
        x_cfg = self.config.get("x_fetcher", {})
        interval = float(x_cfg.get("fetch_interval_minutes", 2)) * 60
        max_new = int(x_cfg.get("max_new_per_cycle", 5))
        accounts = x_cfg.get("accounts") or ["LimbusCompany_B"]
        rss_urls = x_cfg.get("rss_urls") or None
        # ── 改进计划 P1：RT 过滤 + 初始水位线 ──
        filter_retweets = bool(x_cfg.get("filter_retweets", True))
        backfill_hours = float(x_cfg.get("initial_backfill_hours", 24))
        # ── P38：官方自回帖合并到父推文线程，一并转发 ──
        attach_replies = bool(x_cfg.get("attach_official_replies", True))

        # 首启水位线：pushed_ids 为空（首次运行/状态丢失）时只推送
        # 最近 initial_backfill_hours 小时内的推文，避免历史推文刷屏。
        min_published_at: Optional[str] = None
        if not self._x_pushed_ids and backfill_hours > 0:
            from datetime import datetime, timedelta, timezone
            min_published_at = (
                datetime.now(timezone.utc) - timedelta(hours=backfill_hours)
            ).isoformat()
            self.log.info(
                f"首启水位线生效：仅推送最近 {backfill_hours:.0f}h 内推文"
            )

        self.log.info(
            f"X 新推轮询启动: interval={interval:.0f}s max_new={max_new} "
            f"accounts={accounts} groups={self._resolve_push_groups()} "
            f"filter_retweets={filter_retweets}"
        )

        rounds = 0
        while True:
            rounds += 1
            try:
                from crawler.x_fetcher import fetch_new_tweets as _fetch
                from crawler.x_fetcher import group_threads

                new_tweets = await _fetch(
                    state_path=str(self._x_pushed_path.parent / "x_feed_state.json"),
                    accounts=accounts,
                    rss_urls=rss_urls,
                    pushed_ids=self._x_pushed_ids,
                    filter_retweets=filter_retweets,
                    min_published_at=min_published_at,
                )
                # P38：官方自回帖合并到父推文线程（父推文 + 官方回复 一并转发）
                if attach_replies and new_tweets:
                    threads = group_threads(new_tweets)
                else:
                    threads = list(new_tweets)

                # 按时间正序，最多取 N 条线程（防刷屏）
                to_push = threads[:max_new]
                for thread in to_push:
                    tid = thread.get("tweet_id", "")
                    if not tid or tid in self._x_pushed_ids:
                        continue
                    sent = await self._push_tweet_to_groups(thread)
                    if sent:
                        # 线程内所有 id（父推文 + 官方回复）一并标记已推
                        ids = [tid] + [
                            r.get("tweet_id", "")
                            for r in thread.get("official_replies", [])
                        ]
                        for i in ids:
                            if i:
                                self._x_pushed_ids.add(i)
                        self._save_x_pushed()
                        self.log.info(
                            f"已推送并记录 tweet_id={tid}"
                            f"（含 {len(thread.get('official_replies') or [])} 条官方回复）"
                        )
                    else:
                        self.log.warning(f"推送失败，暂不记录已推 id（下轮重试）: {tid}")
                if not to_push:
                    self.log.debug("X 轮询: 无新推")

                # ── 媒体定期清理（P20 收尾）：按周期触发 ──
                if (
                    self._media_cleanup_enabled
                    and rounds % self._media_cleanup_interval == 0
                ):
                    self._cleanup_media()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.error(f"X 轮询异常（下轮继续）: {e}")

            if max_rounds is not None and rounds >= max_rounds:
                break
            await asyncio.sleep(interval)

    def _cleanup_media(self) -> int:
        """清理媒体目录中超过保留期、且不在『最近保留数』内的下载文件。

        策略：
        - 按文件修改时间从新到旧排序；
        - 保留最近 keep_newest 个文件（防误删刚推送的）；
        - 其余超过 max_age_days 天的文件删除。
        返回删除的文件数。
        """
        media_dir = Path(self._media_dir)
        if not media_dir.is_dir():
            return 0
        try:
            now = time.time()
            max_age_sec = self._media_cleanup_max_age_days * 86400
            keep_newest = self._media_cleanup_keep_newest

            files = [p for p in media_dir.iterdir() if p.is_file()]
            # 从新到旧排序
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            deleted = 0
            for idx, p in enumerate(files):
                if idx < keep_newest:
                    continue  # 保留最近文件
                try:
                    age = now - p.stat().st_mtime
                    if age > max_age_sec:
                        p.unlink()
                        deleted += 1
                except OSError:
                    continue
            if deleted:
                self.log.info(f"媒体清理完成：删除 {deleted} 个过期文件（{media_dir}）")
            return deleted
        except Exception as e:
            self.log.warning(f"媒体清理异常（忽略）: {e}")
            return 0

    # ── Steam 社区 RSS 新公告轮询推送（P37）──

    def _init_steam_push(self):
        """初始化 Steam 推送：加载已推公告 id，解析配置。"""
        steam_cfg = self.config.get("steam_fetcher", {})
        self._steam_pushed_path = Path(
            steam_cfg.get("pushed_ids_path", "data/steam_pushed_ids.json")
        ).resolve()
        self._steam_appid = str(steam_cfg.get("appid", 1973530))
        self._steam_rss_url = steam_cfg.get("rss_url") or None

        try:
            if self._steam_pushed_path.exists():
                data = json.loads(self._steam_pushed_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._steam_pushed_ids = set(str(x) for x in data)
                elif isinstance(data, dict):
                    self._steam_pushed_ids = set(str(x) for x in data.get("pushed_ids", []))
                self.log.info(f"已加载 {len(self._steam_pushed_ids)} 个已推送 Steam 公告 id")
        except Exception as e:
            self.log.warning(f"加载 Steam 已推 id 失败: {e}")

    def _save_steam_pushed(self):
        """持久化已推公告 id 列表。"""
        try:
            self._steam_pushed_path.parent.mkdir(parents=True, exist_ok=True)
            self._steam_pushed_path.write_text(
                json.dumps(
                    {"pushed_ids": sorted(self._steam_pushed_ids)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            self.log.warning(f"保存 Steam 已推 id 失败: {e}")

    def _resolve_steam_push_groups(self) -> list[str]:
        """解析 Steam 推送目标群号（优先级：steam.push_group_id → x.push_group_id → 记忆群）。"""
        steam_cfg = self.config.get("steam_fetcher", {})
        group = steam_cfg.get("push_group_id", 0) or 0
        if not group:
            group = self.config.get("x_fetcher", {}).get("push_group_id", 0) or 0
        groups: list[str] = []
        if group:
            groups.append(str(group))
        else:
            groups.extend(sorted(self._push_group_ids))
        return groups

    def _format_steam_item_text(self, item: dict) -> str:
        """把 Steam 公告组装成可读文本（标题 + 正文 + 公告页链接）。"""
        title = (item.get("title") or "").strip()
        text = (item.get("text") or "").strip()
        permalink = (item.get("permalink") or "").strip()
        header = f"【Steam公告】{title or '(无标题)'}"
        body = text if text and text != title else "(公告内容见图片)"
        if permalink:
            body = f"{body}\n{permalink}"
        return f"{header}\n{body}"

    async def _push_steam_item_to_groups(self, item: dict) -> bool:
        """把单条 Steam 公告推送到所有目标群（文本 + 配图）。"""
        groups = self._resolve_steam_push_groups()
        if not groups:
            self.log.warning(f"无目标群可推送（announcement_id={item.get('announcement_id')}），跳过")
            return False

        text = self._format_steam_item_text(item)
        images = list(item.get("image_urls") or [])
        max_images = max(
            0, int(self.config.get("steam_fetcher", {}).get("max_images_per_item", 5))
        )
        if max_images:
            images = images[:max_images]

        ok = True
        for gid in groups:
            try:
                if images:
                    sent = await self.adapter.send_group_msg_media(
                        group_id=int(gid),
                        text=text,
                        images=images,
                        media_dir=self._media_dir,
                        prefer_local_images=bool(
                            self.config.get("x_fetcher", {}).get("prefer_local_images", True)
                        ),
                    )
                else:
                    sent = await self.adapter.send_group_msg(int(gid), text)
                if not sent:
                    self.log.warning(
                        f"Steam 公告推送失败 group={gid} id={item.get('announcement_id')}"
                    )
                    ok = False
            except Exception as e:
                self.log.error(
                    f"Steam 公告推送异常 group={gid} id={item.get('announcement_id')}: {e}"
                )
                ok = False
        return ok

    async def _steam_poll_loop(self, max_rounds: Optional[int] = None):
        """后台轮询：拉取 Steam 新公告 → 推送 → 记录已推 id（幂等防重推）。

        单轮异常不中断循环（try/except + 继续下一轮）。
        max_rounds：可选的最大轮数；为 None 时无限循环（生产场景），
        测试可传 1 只跑单轮以验证逻辑。
        """
        steam_cfg = self.config.get("steam_fetcher", {})
        interval = float(steam_cfg.get("fetch_interval_minutes", 30)) * 60
        max_new = int(steam_cfg.get("max_new_per_cycle", 3))
        appid = int(self._steam_appid)

        # 首启水位线：pushed_ids 为空（首次运行/状态丢失）时只推送最近 72h 内公告，
        # 避免历史公告刷屏。
        min_published_at: Optional[str] = None
        if not self._steam_pushed_ids:
            from datetime import datetime, timedelta, timezone
            min_published_at = (
                datetime.now(timezone.utc) - timedelta(hours=72)
            ).isoformat()
            self.log.info("Steam 首启水位线生效：仅推送最近 72h 内公告")

        self.log.info(
            f"Steam 新公告轮询启动: interval={interval:.0f}s max_new={max_new} "
            f"appid={appid} groups={self._resolve_steam_push_groups()}"
        )

        rounds = 0
        while True:
            rounds += 1
            try:
                from crawler.steam_fetcher import fetch_new_steam_news as _fetch

                new_items = await _fetch(
                    appid=appid,
                    rss_url=self._steam_rss_url,
                    state_path=str(
                        self.config.get("steam_fetcher", {})
                        .get("feed_state_path", "data/steam_feed_state.json")
                    ),
                    pushed_ids=self._steam_pushed_ids,
                    min_published_at=min_published_at,
                    limit=max_new * 3,
                )
                # 按时间正序，最多取 N 条（防刷屏）
                to_push = new_items[:max_new]
                for item in to_push:
                    aid = item.get("announcement_id", "")
                    if not aid or aid in self._steam_pushed_ids:
                        continue
                    sent = await self._push_steam_item_to_groups(item)
                    if sent:
                        self._steam_pushed_ids.add(aid)
                        self._save_steam_pushed()
                        self.log.info(f"已推送并记录 Steam 公告 id={aid}")
                    else:
                        self.log.warning(f"Steam 公告推送失败，暂不记录已推 id（下轮重试）: {aid}")
                if not to_push:
                    self.log.debug("Steam 轮询: 无新公告")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.error(f"Steam 轮询异常（下轮继续）: {e}")

            if max_rounds is not None and rounds >= max_rounds:
                break
            await asyncio.sleep(interval)

    def _get_memory(self, session_id: str) -> Any:
        """获取或创建会话的对话记忆"""
        if session_id not in self._memory_store:
            self._memory_store[session_id] = create_memory()
        return self._memory_store[session_id]

    def _get_history_text(self, session_id: str) -> str:
        """获取会话的对话历史文本"""
        memory = self._get_memory(session_id)
        window = self.config["agent"].get("memory_window", 10)
        return get_chat_history_text(memory, window_size=window)

    def _save_to_memory(self, session_id: str, user_msg: str, assistant_msg: str):
        """将一轮对话写入记忆"""
        memory = self._get_memory(session_id)
        memory.add_user_message(user_msg)
        memory.add_ai_message(assistant_msg)

    async def initialize_rag(self):
        """延迟初始化 RAG 组件（需要 embedder API key 就绪）。
        支持运行时动态人格切换，chain 构建后不复建。"""
        from rag.embedder import create_embedder
        from rag.vector_store import build_bm25_index, get_or_create

        embed_cfg = self.config["embedding"]
        embedder = create_embedder(embed_cfg)

        vs_cfg = self.config["vector_store"]
        vector_store = get_or_create(
            embedder=embedder,
            persist_directory=vs_cfg["persist_directory"],
            collection_name=vs_cfg["collection_name"],
        )

        # ── Fix E: 构建并接线 BM25 关键词索引 ──
        # 此前仅 get_or_create() 拿到 Chroma，bm25_index 从未传给 LimBusRetriever，
        # 导致 config 的 hybrid_search.enabled=true 形同虚设、检索实际是纯向量模式。
        # 关键词重叠（如 "黑兽-卯魁首"）的确定性召回依赖 BM25 兜底。
        bm25_index = build_bm25_index(vector_store)

        retrieval_cfg = self.config["retrieval"]
        prog_cfg = retrieval_cfg.get("progressive", {})
        pc_cfg = retrieval_cfg.get("parent_child", {})
        noise_cfg = retrieval_cfg.get("noise_filter", {})
        qe_cfg = retrieval_cfg.get("query_expansion", {})

        # ── LLM 查询扩展器（HyDE 式，将口语化查询改写为多条搜索短语）──
        from rag.query_processor import LLMQueryExpander
        llm_query_expander = None
        if qe_cfg.get("enabled", True) and self.llm is not None:
            llm_query_expander = LLMQueryExpander(
                llm=self.llm,
                max_phrases=qe_cfg.get("max_phrases", 3),
                enabled=qe_cfg.get("enabled", True),
            )

        self.retriever = LimBusRetriever(
            vector_store=vector_store,
            top_k=retrieval_cfg.get("top_k", 6),
            similarity_threshold=retrieval_cfg.get("similarity_threshold", 0.35),
            max_context_chars=retrieval_cfg.get("max_context_chars", 600),
            # ── Fix E: 接线 BM25 混合检索 ──
            bm25_index=bm25_index,
            # 渐进式检索配置
            progressive_enabled=prog_cfg.get("enabled", True),
            max_retrieval_rounds=prog_cfg.get("max_rounds", 3),
            round_k_multipliers=tuple(prog_cfg.get("round_k_multipliers", [5, 20, 50])),
            round_bm25_multipliers=tuple(prog_cfg.get("round_bm25_multipliers", [10, 40, 100])),
            saturation_threshold=prog_cfg.get("saturation_threshold", 0.3),
            result_mult_cap=prog_cfg.get("result_mult_cap", 17),
            # LLM 查询扩展
            llm_query_expander=llm_query_expander,
            query_expansion_enabled=qe_cfg.get("enabled", True),
            # Parent-Child 穷举检索
            parent_child_enabled=pc_cfg.get("enabled", True),
            # 噪声过滤
            noise_filter_enabled=noise_cfg.get("enabled", True),
            noise_quality_threshold=noise_cfg.get("quality_threshold", 0.25),
        )

        # ── LLM Reranker ──
        rerank_cfg = retrieval_cfg.get("rerank", {})
        self.reranker = LLMReranker(
            llm=self.llm,
            top_n=rerank_cfg.get("top_n", 6),
            candidate_multiplier=rerank_cfg.get("candidate_multiplier", 2),
            enabled=rerank_cfg.get("enabled", False),
        )

        # ── 置信度评估配置 ──
        confidence_cfg = self.config.get("confidence", {})
        self._confidence_enabled = confidence_cfg.get("enabled", False)
        self._confidence_low_threshold = confidence_cfg.get("low_threshold", 0.3)
        self._confidence_enable_follow_up = confidence_cfg.get("enable_follow_up", True)

        # ── 自我反思闭环配置 ──
        # 当回答置信度低时：LLM 判定 → 改写查询 → 二次检索 → 再生成
        self._reflect_enabled = confidence_cfg.get("reflect_enabled", False)
        self._reflect_max_attempts = confidence_cfg.get("reflect_max_attempts", 1)

        # ── 拟真人格引擎（P38，见 plans/persona_realism_training.md）──
        # 数据底座：剧情/语音真实台词库；生成引擎：台词样本注入 + 内心反应 + 自检。
        # 默认关闭（persona_training.enabled=false），开启后行为与现有链路完全兼容。
        self.persona_engine = None
        try:
            pt_cfg = self.config.get("persona_training", {}) or {}
            if pt_cfg.get("enabled", False):
                from rag.persona_corpus import PersonaCorpus
                from rag.persona_engine import PersonaEngine
                self.persona_engine = PersonaEngine(
                    corpus=PersonaCorpus,
                    persona_manager=self.persona_manager,
                    llm=self.llm,
                    mode=pt_cfg.get("mode", "thinking"),
                    max_samples=pt_cfg.get("max_samples", 3),
                    consistency=pt_cfg.get("consistency", "rules"),
                )
                self.log.info(
                    f"拟真人格引擎已启用: mode={self.persona_engine.mode} "
                    f"samples={self.persona_engine.max_samples} "
                    f"consistency={self.persona_engine.consistency}"
                )
        except Exception as e:
            self.persona_engine = None
            self.log.warning(f"拟真人格引擎初始化失败，保持原链路: {e}")

        # 构建 RAG Chain（默认人格作为 fallback，运行时动态注入 persona_id）
        default_pid = self.config["agent"].get("default_persona", "")
        self.rag_chain = build_rag_chain(
            llm=self.llm,
            retriever=self.retriever,
            persona_manager=self.persona_manager,
            default_persona_id=default_pid,
            persona_engine=self.persona_engine,
        )
        self._current_persona_id = default_pid

        # ── 人格结构化直答（Persona Direct Answer）──
        # 绕开向量检索，直接从 data/structured/persona_*.json 精确取数。
        # 仅在配置开启且目录存在时启用；未命中时回落 RAG，不影响原链路。
        try:
            from rag.persona_direct import PersonaDirectStore
            pd_cfg = self.config["agent"].get("persona_direct", {})
            self.persona_direct = PersonaDirectStore(
                data_dir=pd_cfg.get("data_dir", "data/structured"),
                enabled=pd_cfg.get("enabled", True),
            )
            self.log.info(f"人格结构化直答已{'启用' if self.persona_direct.enabled else '停用'} (data_dir={self.persona_direct.data_dir})")
        except Exception as e:
            self.persona_direct = None
            self.log.warning(f"人格结构化直答初始化失败，回落 RAG: {e}")

        # ── E.G.O 饰品结构化直答（Gift Direct Answer）──
        # 绕开向量检索，直接从 data/structured/gift_*.json 精确取数。
        # 与人格直答并行；未命中时回落 RAG，不影响原链路。
        try:
            from rag.gift_direct import GiftDirectStore
            gd_cfg = self.config["agent"].get("gift_direct", {})
            self.gift_direct = GiftDirectStore(
                data_dir=gd_cfg.get("data_dir", "data/structured"),
                enabled=gd_cfg.get("enabled", True),
            )
            self.log.info(f"饰品结构化直答已{'启用' if self.gift_direct.enabled else '停用'} (data_dir={self.gift_direct.data_dir})")
        except Exception as e:
            self.gift_direct = None
            self.log.warning(f"饰品结构化直答初始化失败，回落 RAG: {e}")

        # ── 事件结构化直答（Event Direct Answer）──
        # 绕开向量检索，直接从 data/structured/events/event_*.json 精确取数。
        # 与人格/饰品直答并行；未命中时回落 RAG，不影响原链路。
        try:
            from rag.event_direct import EventDirectStore
            ed_cfg = self.config["agent"].get("event_direct", {})
            self.event_direct = EventDirectStore(
                data_dir=ed_cfg.get("data_dir", "data/structured"),
                enabled=ed_cfg.get("enabled", True),
            )
            self.log.info(f"事件结构化直答已{'启用' if self.event_direct.enabled else '停用'} (data_dir={self.event_direct.data_dir})")
        except Exception as e:
            self.event_direct = None
            self.log.warning(f"事件结构化直答初始化失败，回落 RAG: {e}")

        # ── 敌方单位结构化直答（Enemy Direct Answer）──
        # 绕开向量检索，直接从 data/structured/enemies/enemy_*.json 精确取数。
        # 敌方名（如雷横）命中 → 直接输出完整数据；与人格/饰品/事件直答并行；
        # 未命中时回落 RAG，不影响原链路。
        try:
            from rag.enemy_direct import EnemyDirectStore
            edr_cfg = self.config["agent"].get("enemy_direct", {})
            self.enemy_direct = EnemyDirectStore(
                data_dir=edr_cfg.get("data_dir", "data/structured"),
                enabled=edr_cfg.get("enabled", True),
            )
            self.log.info(f"敌方单位结构化直答已{'启用' if self.enemy_direct.enabled else '停用'} (data_dir={self.enemy_direct.data_dir})")
        except Exception as e:
            self.enemy_direct = None
            self.log.warning(f"敌方单位结构化直答初始化失败，回落 RAG: {e}")

        # ── 模糊搜索消歧（P39，rag/entity_disambiguation.py）──
        # 数据类查询在四类直答全部未命中时，用 rapidfuzz 从结构化库模糊检索
        # top-N 候选列给用户，回复数字即确定性作答（绕过 LLM，无幻觉）。
        self.disambig = None
        try:
            da_cfg = self.config["agent"].get("disambiguation", {}) or {}
            if da_cfg.get("enabled", True):
                from rag.entity_disambiguation import DisambiguationEngine
                self.disambig = DisambiguationEngine(
                    persona_store=self.persona_direct,
                    enemy_store=self.enemy_direct,
                    gift_store=self.gift_direct,
                    event_store=self.event_direct,
                    top_k=da_cfg.get("top_k", 5),
                    ask_threshold=da_cfg.get("ask_threshold", 55),
                    direct_threshold=da_cfg.get("direct_threshold", 85),
                    direct_gap=da_cfg.get("direct_gap", 10),
                )
                self.log.info(
                    f"模糊搜索消歧已启用: top_k={self.disambig.top_k} "
                    f"ask={self.disambig.ask_threshold} "
                    f"direct={self.disambig.direct_threshold} gap={self.disambig.direct_gap}"
                )
        except Exception as e:
            self.disambig = None
            self.log.warning(f"模糊搜索消歧初始化失败，跳过: {e}")

        self.log.info("RAG 组件初始化完成")

    # ── 消息处理主流程 ──

    async def handle_event(self, event: dict):
        """处理 NapCatQQ 推送的原始事件"""
        msg = self.router.parse_event(event)
        if not msg:
            return

        # ── P20-B: 记忆群消息的群号（用于 X 新推推送兜底）──
        if msg.is_group and msg.group_id:
            self.remember_group(msg.group_id)

        if not self.router.should_respond(msg):
            return

        session_id = self.router.get_session_id(msg)

        # ── 指令处理 ──
        if msg.command:
            await self._handle_command(msg, session_id)
            return

        # ── 安全检查：输入过滤 ──
        if not self.sensitive_filter.check_input(msg.text):
            self.log.info(f"输入敏感词拦截: session={session_id}")
            return

        # ── 安全检查：会话熔断 ──
        if self.sensitive_filter.is_session_blocked(session_id):
            return

        # ── 频率控制 ──
        allowed, reason = self.rate_limiter.check(msg.user_id, msg.group_id)
        if not allowed:
            self.log.debug(f"频率限制: {reason} (user={msg.user_id})")
            return

        # ── 获取会话（支持运行时人格切换）─
        self.session_manager.get_or_create(session_id)

        # ── Agent 推理 ──
        reply = await self._generate_reply(msg, session_id)

        # 直答命中返回 AgentReply（可能带打包转发分节）；普通链路返回纯文本
        if isinstance(reply, AgentReply):
            reply_text = reply.text
            forward_sections = reply.forward_sections
        else:
            reply_text = reply
            forward_sections = None

        if not reply_text:
            return

        # ── 安全检查：输出过滤 ──
        passed, safe_reply = self.sensitive_filter.check_output(reply_text, session_id)
        if not passed:
            self.log.warning(f"输出敏感词拦截: session={session_id}")
            return

        # ── 输出过滤改写文本时丢弃转发分节（分节基于过滤前文本构建，可能不一致）──
        if forward_sections and safe_reply != reply_text:
            self.log.debug(f"输出过滤改写文本，放弃打包转发分节: session={session_id}")
            forward_sections = None

        # ── 写入对话记忆 ──
        self._save_to_memory(session_id, msg.text, safe_reply)

        # ── 发送回复（直答命中 → 打包转发；失败自动回落普通分段发送）──
        await self._send_reply(msg, safe_reply, forward_sections=forward_sections)

    def _wrap_direct(self, text: str) -> AgentReply:
        """把直答命中文本包装为 AgentReply（附打包转发分节）。

        仅当配置启用且文本可拆出 >= min_nodes 个分节时才附带 forward_sections；
        否则 forward_sections=None，发送层按普通文本发送（兼容原链路）。
        """
        sections: Optional[list[str]] = None
        if self._forward_enabled and text:
            secs = split_forward_sections(text, max_chars=self._forward_max_chars)
            if len(secs) >= self._forward_min_nodes:
                sections = secs
        return AgentReply(text=text, forward_sections=sections)

    async def _generate_reply(self, msg: QQMessage, session_id: str) -> "str | AgentReply":
        """调用 LLM 生成回复（运行时动态注入当前会话的人格 ID）。

        直答命中（人格/饰品/事件/敌方/比较直答，跳过 RAG）时返回
        ``AgentReply``（附打包转发分节）；其余链路返回纯文本 str。
        """

        # ── P17 人格切换：预拦截 + LLM function-calling ──
        # 用户发出「切换人格为X / 变成X / 扮演X」等指令时直接走切换路径，
        # 返回确认/错误文本；未命中则回落常规问答链路（不影响原有功能）。
        persona_switch_reply = await self._try_persona_switch(msg.text, session_id)
        if persona_switch_reply is not None:
            return persona_switch_reply

        # ── P39 模糊消歧：用户回复数字选择候选 / 取消 ──
        # 上一条数据查询返回了候选清单并存入会话，本条是数字编号 → 确定性作答。
        disambig_reply = self._resolve_pending_choice(msg.text, session_id)
        if disambig_reply is not None:
            return disambig_reply

        # ── P26 抽奖（Gacha）指令预拦截 ──
        # 「抽卡/抽奖/十连/单抽」等明确句式 → 确定性调用 gacha_pull
        # （三灯3% / 二灯13% / 一灯81% / EGO 3%），零 LLM 成本。
        gacha_reply = self._try_gacha(msg.text)
        if gacha_reply is not None:
            return gacha_reply

        # ── P27 意图门控（改进计划 1.1/3.1）──
        # opinion（看法/评价）→ 跳过全部直答，走人格扮演链路；
        # compare（比较）→ 尝试比较直答，否则 RAG；
        # data/other → 正常直答链。
        from rag.intent_gate import classify_user_intent
        _intent = classify_user_intent(msg.text)
        # 纯数字消息（1~9）且无待确认候选 → 不进数据直答/消歧，
        # 避免"1"被饰品直答命中"1B型八角螺栓"（P39 数字选择语义应只属于候选流程）。
        _pure_digit = bool(re.fullmatch(r"[1-9]", (msg.text or "").strip()))
        _skip_direct = _intent in ("opinion", "compare") or _pure_digit
        if _skip_direct:
            self.log.debug(f"意图门控: {_intent}，跳过直答: '{msg.text[:30]}...'")

        # ── 人格结构化直答优先（Persona Direct Answer）──
        # 命中具体人格数据查询 → 直接返回完整规范文本（确定性、绕过 LLM 与向量检索）。
        # 未命中 → None，回落下方原 RAG 链路。opinion/compare 意图跳过。
        if not _skip_direct and self.persona_direct is not None:
            try:
                direct = self.persona_direct.try_direct_answer(msg.text)
                if direct:
                    self.log.info(f"人格直答命中，跳过 RAG: '{msg.text[:30]}...'")
                    return self._wrap_direct(direct)
            except Exception as e:
                self.log.warning(f"人格直答异常，回落 RAG: {e}")

        # ── E.G.O 饰品结构化直答优先（Gift Direct Answer）──
        # 命中具体饰品数据查询 → 直接返回固定规范格式（确定性、绕过 LLM 与向量检索）。
        # 未命中 → None，回落下方原 RAG 链路。opinion/compare 意图跳过。
        if not _skip_direct and self.gift_direct is not None:
            try:
                direct = self.gift_direct.try_direct_answer(msg.text)
                if direct:
                    self.log.info(f"饰品直答命中，跳过 RAG: '{msg.text[:30]}...'")
                    return self._wrap_direct(direct)
            except Exception as e:
                self.log.warning(f"饰品直答异常，回落 RAG: {e}")

        # ── 事件结构化直答优先（Event Direct Answer）──
        # 命中具体事件数据查询 → 直接返回固定规范格式（确定性、绕过 LLM 与向量检索）。
        # 未命中 → None，回落下方原 RAG 链路。opinion/compare 意图跳过。
        if not _skip_direct and self.event_direct is not None:
            try:
                direct = self.event_direct.try_direct_answer(msg.text)
                if direct:
                    self.log.info(f"事件直答命中，跳过 RAG: '{msg.text[:30]}...'")
                    return self._wrap_direct(direct)
            except Exception as e:
                self.log.warning(f"事件直答异常，回落 RAG: {e}")

        # ── 敌方单位结构化直答优先（Enemy Direct Answer）──
        # 命中具体敌方名（如雷横）数据查询 → 直接返回完整规范文本
        # （确定性、绕过 LLM 与向量检索）。未命中 → None，回落下方原 RAG 链路。
        # opinion/compare 意图跳过。
        if not _skip_direct and self.enemy_direct is not None:
            try:
                direct = self.enemy_direct.try_direct_answer(msg.text)
                if direct:
                    if isinstance(direct, list):
                        # P23 多候选 → 统一走"存会话待确认"（用户回复数字即
                        # 确定性作答）——根治"回复数字被误当饰品名查询"
                        # （如"1"命中"1B型八角螺栓"）。
                        return self._handle_enemy_candidates(direct, msg.text, session_id)
                    self.log.info(f"敌方直答命中，跳过 RAG: '{msg.text[:30]}...'")
                    return self._wrap_direct(direct)
            except Exception as e:
                self.log.warning(f"敌方直答异常，回落 RAG: {e}")

        # ── P27 比较直答（compare 意图）──
        if _intent == "compare":
            compare_reply = self._try_compare_answer(msg.text)
            if compare_reply is not None:
                return self._wrap_direct(compare_reply)

        # ── P39 模糊搜索消歧：四类直答全部未命中时，从结构化库模糊检索候选 ──
        # 仅 data 意图触发（避免劫持闲聊/观点）；候选清单存入会话，用户回复数字
        # 即确定性作答（绕过 LLM，无幻觉）——根治"模糊查询 → RAG 编造/未收录"。
        if self.disambig is not None and _intent == "data":
            try:
                choices = self.disambig.search(msg.text)
                if choices:
                    top = choices[0]
                    # 唯一高置信候选 → 直接作答；
                    # 多候选但 top 明显占优（分差 ≥ direct_gap）→ 也直接作答，避免多余提问
                    dominant = top["score"] >= self.disambig.direct_threshold and (
                        len(choices) == 1
                        or top["score"] - choices[1]["score"] >= self.disambig.direct_gap
                    )
                    if not dominant:
                        session = self.session_manager.get_session(session_id)
                        if session is not None:
                            session.pending_choice = {"choices": choices, "query": msg.text}
                            self.log.info(
                                f"模糊消歧: '{msg.text[:20]}...' → {len(choices)} 个候选"
                            )
                            return self.disambig.format_choices(choices)
                    # 占优候选直接确定性作答（绕过 LLM，防 RAG 编造）
                    self.log.info(
                        f"模糊消歧占优候选直答: {top['display']} (score={top['score']})"
                    )
                    answer = self.disambig.answer(top)
                    if answer:
                        return self._wrap_direct(answer)
            except Exception as e:
                self.log.warning(f"模糊搜索消歧异常，回落 RAG: {e}")

        # 语义缓存检查（轻量文本匹配）
        if self.semantic_cache is not None:
            cached = self.semantic_cache.hash_lookup(msg.text)
            if cached:
                self.log.debug(f"语义缓存命中: session={session_id}")
                return cached

        # 获取当前会话的人格 ID（运行时动态）
        persona_id = self.session_manager.get_persona(session_id)

        # 获取对话历史
        chat_history = self._get_history_text(session_id)

        # ── P29/P29b/P29c：观点/比较类意图注入实体事实底座 ──
        # opinion（你怎么看XX）→ 注入剧情事实（角色身份+剧情言行），且检索仅限
        #   剧情来源（story_only），避免把敌方单位数据拉进上下文淹没剧情
        #   （日志实证："你怎么看霍恩海姆"混入被动/战术数据）；
        # compare（谁更强）→ 注入单位数据（强度比较需要数值）。
        fact_base = ""
        story_only = False
        if _intent == "opinion":
            fact_base = self._build_opinion_fact_base(msg.text, session_id)
            story_only = True
            if fact_base:
                self.log.debug(f"观点剧情底座注入: '{msg.text[:30]}...'")
        elif _intent == "compare":
            fact_base = self._build_compare_fact_base(msg.text)
            if fact_base:
                self.log.debug(f"比较数据底座注入: '{msg.text[:30]}...'")

        try:
            reply = await run_rag_query(
                self.rag_chain, msg.text, chat_history,
                persona_id=persona_id, fact_base=fact_base,
                story_only=story_only,
                persona_engine=self.persona_engine,
            )
            reply = reply.strip() if reply else ""

            # ── 置信度评估 + 自我反思闭环 ──
            if self._confidence_enabled and reply:
                confidence = self._assess_confidence(reply)
                self.log.debug(
                    f"置信度评估: {confidence:.2f} (query='{msg.text[:30]}...')"
                )

                # ── P21-E 硬短路：回答已明确「未收录/未找到/不知道」→ 不再二次反思编造 ──
                # 此时 confidence 已低（含不确定表述），若再触发自我反思改写查询，
                # 可能让 LLM 在无数据时二次生成而编造答案。直接短路保留「未收录」回答。
                _miss_markers = ("未收录", "未找到", "找不到", "没有收录", "不知道", "无法回答", "资料中没有")
                if any(m in reply for m in _miss_markers):
                    self.log.info(f"数据类未命中硬短路，跳过自我反思: '{msg.text[:30]}...'")
                    if self.semantic_cache is not None:
                        self.semantic_cache.store(msg.text, reply)
                    return reply

                # ── 自我反思：低置信度时改写查询 → 二次检索 → 再生成 ──
                if confidence < self._confidence_low_threshold and self._reflect_enabled:
                    reflected = await self._reflect_rewrite_query(msg.text, reply)
                    if reflected and reflected != msg.text:
                        self.log.info(
                            f"自我反思改写查询: '{msg.text[:30]}...' → '{reflected[:30]}...'"
                        )
                        try:
                            reply2 = await run_rag_query(
                                self.rag_chain, reflected, chat_history, persona_id=persona_id,
                                persona_engine=self.persona_engine,
                            )
                            reply2 = reply2.strip() if reply2 else ""
                            if reply2:
                                conf2 = self._assess_confidence(reply2)
                                self.log.debug(
                                    f"自我反思二次回答置信度: {conf2:.2f} "
                                    f"(原 {confidence:.2f})"
                                )
                                # 仅当二次回答更优时才替换
                                if conf2 > confidence or (
                                    conf2 == confidence and len(reply2) > len(reply)
                                ):
                                    reply = reply2
                                    confidence = conf2
                        except Exception as e:
                            self.log.warning(f"自我反思二次生成异常，保留原回答: {e}")

                if confidence < self._confidence_low_threshold and self._confidence_enable_follow_up:
                    reply += "\n\n（以上回答的置信度较低，建议提供更具体的描述以便浮士德为你查询。）"

            # 写入语义缓存
            if self.semantic_cache is not None and reply:
                self.semantic_cache.store(msg.text, reply)

            return reply
        except Exception as e:
            self.log.error(f"LLM 推理异常: {e}")
            return "……（信号似乎被都市干扰了。请稍后再试。）"

    # ── P26：抽奖（Gacha）指令预拦截 ──

    @staticmethod
    def _strip_stage_directions(text: str) -> str:
        """确定性移除回复中的神态/动作描写（P32）。

        匹配含动作/神态关键词的括号段落（如（浮士德扫了一眼屏幕）（微微抬眸）（笑））
        并删除；不含动作词的括号（（第一阶段）（共 10 抽：…）（经理））保留。
        与 System Prompt 的"输出规则"形成双保险——LLM 无论怎么输出都会被清洗。
        """
        if not text:
            return text
        # 动作/神态关键词（覆盖常见舞台指示动词）
        action_words = (
            "扫|看|望|笑|抬|点|皱|叹|握|转|瞥|打量|审视|耸肩|摊手|挑眉|"
            "斜|低|垂|抚|抿|眯|睁|闭|哼|咳|挠|拍|坐|站|走|回|应|摇|点头|"
            "摇头|眼神|目光|表情|神色|沉思|沉默|看向|看向|盯着|愣住|"
            "转身|回头|整理|拿起|放下|翻开|合上|皱眉|勾起|咬"
        )
        pattern = re.compile(
            r"[（(][^（）()]{0,30}?(?:" + action_words + r")[^（）()]{0,30}?[）)]"
        )
        cleaned = pattern.sub("", text)
        # 清理删除后可能残留的多余空格/空行
        cleaned = re.sub(r" {2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _try_gacha(self, text: str) -> Optional[str]:
        """尝试通过『抽奖』指令路径响应（P26）。

        强信号正则命中（抽卡/抽奖/十连/单抽/来一发/gacha 等）→
        确定性调用 gacha_pull 返回抽取结果（零 LLM 成本、不依赖 function-calling）。
        未命中返回 None，由调用方回落常规问答链路。

        Returns:
            命中的抽奖结果文本；未命中返回 None。
        """
        try:
            m = _GACHA_STRONG_RE.search(text or "")
            if not m:
                return None
            times = _parse_gacha_times(text, m.group(1))
            from tools.gacha import gacha_pull
            result = gacha_pull(times)
            self.log.info(f"抽奖指令命中: '{text[:30]}...' → {times} 抽")
            return result
        except Exception as e:
            self.log.warning(f"抽奖指令处理异常，回落常规链路: {e}")
            return None

    # ── P39：模糊消歧的候选选择（回复数字编号 / 取消）──

    def _handle_enemy_candidates(
        self, names: list[str], query: str, session_id: str
    ) -> str:
        """敌方多候选清单（P23 由 enemy_direct 返回）：存会话待确认。

        用户回复数字编号 → _resolve_pending_choice 确定性作答；
        根治"回复数字被误当饰品名查询"（如"1"命中"1B型八角螺栓"）。
        """
        session = self.session_manager.get_session(session_id)
        if session is not None:
            choices = [
                {"kind": "enemy", "name": n, "score": 100.0, "display": f"敌方｜{n}"}
                for n in names
            ]
            session.pending_choice = {"choices": choices, "query": query}
            self.log.info(
                f"敌方多候选存会话待确认: {len(names)} 个 (session={session_id})"
            )
        if self.disambig is not None:
            return self.disambig.format_choices(choices if session is not None else [])
        # 降级（无消歧引擎时）：普通编号清单
        lines = ["检测到多个可能的目标，请回复数字选择（或发送「取消」）："]
        for i, n in enumerate(names, 1):
            lines.append(f"{i}. 敌方｜{n}")
        return "\n".join(lines)

    def _resolve_pending_choice(self, text: str, session_id: str) -> Optional[str]:
        """处理上一条模糊搜索候选清单的用户选择。

        - 回复数字（1~9）→ 从会话 pending_choice 中取对应候选，确定性作答；
        - 回复「取消/算了/不了」→ 清除待确认状态；
        - 其他消息 → 清除过期待确认，返回 None（按正常消息处理）。

        Returns:
            命中的回复文本（含 AgentReply 包装）；未命中返回 None。
        """
        session = self.session_manager.get_session(session_id)
        if session is None or not session.pending_choice:
            return None
        t = (text or "").strip()
        choices = session.pending_choice.get("choices") or []

        if t in ("取消", "算了", "不了", "不用了", "取消查询"):
            session.pending_choice = None
            self.log.info(f"模糊消歧取消: session={session_id}")
            return "好的，已取消这次查询。"

        if t.isdigit():
            idx = int(t) - 1
            session.pending_choice = None
            if not (0 <= idx < len(choices)):
                return "选项编号超出范围，请重新回复数字或发送「取消」。"
            choice = choices[idx]
            if self.disambig is None:
                return "该功能暂不可用，请稍后再试。"
            answer = self.disambig.answer(choice)
            if not answer:
                self.log.warning(f"模糊消歧选择无数据: {choice}")
                return "该选项暂无详细数据，请换一个试试。"
            self.log.info(f"模糊消歧选择: {choice['display']} (session={session_id})")
            return self._wrap_direct(answer)

        # 非数字/非取消：清掉过期待确认，按正常消息处理
        session.pending_choice = None
        return None

    # ── P27：比较直答（compare 意图，配合意图门控）──

    def _try_compare_answer(self, text: str) -> Optional[str]:
        """尝试『比较型直答』：人格比较优先，敌方单位比较兜底。

        任一直答未命中返回 None，由调用方回落 RAG（LLM 自由发挥比较）。
        """
        if self.persona_direct is not None:
            try:
                reply = self.persona_direct.try_compare_answer(text)
                if reply:
                    self.log.info(f"人格比较直答命中: '{text[:30]}...'")
                    return reply
            except Exception as e:
                self.log.warning(f"人格比较直答异常: {e}")
        if self.enemy_direct is not None:
            try:
                reply = self.enemy_direct.try_compare_answer(text)
                if reply:
                    self.log.info(f"敌方比较直答命中: '{text[:30]}...'")
                    return reply
            except Exception as e:
                self.log.warning(f"敌方比较直答异常: {e}")
        return None

    # ── P29：观点/比较类问题的实体事实底座 ──
    # 修复"观点编造身份"：LLM 裸奔发表看法时可能张冠李戴
    # （日志实证：『你怎么看里恩』→ 把食指父辈里恩说成 N 公司异端审判官）。
    # 修正（P29b，用户确认）：opinion 应**结合剧情**发表看法——
    #   注入该角色在剧情中的身份与言行（story_facts），**不注入单位数值**
    #   （HP/抗性/技能等仅"怎么打/弱点"类明确游戏意图才用，走直答/数据链路）。
    # compare（谁更强/对比）保留单位数据（强度比较需要数值）。

    def _build_opinion_fact_base(self, text: str, session_id: str = "") -> str:
        """opinion 意图：构建剧情事实底座（角色身份 + 剧情言行 + 人物互动）。"""
        from rag.story_facts import build_story_fact_base

        # 当前扮演罪人（如 faust → 浮士德），用于提取与被问角色的剧情互动
        focus_role = self._current_sinner_name(session_id)

        # 1) 敌方角色（里恩 → 裸名"里恩" + 身份"食指父辈（第一阶段）"）
        if self.enemy_direct is not None:
            try:
                real = self.enemy_direct.resolve_enemy(text)
                if real:
                    bare, identity = self._enemy_identity(real)
                    fact = build_story_fact_base(bare, identity_note=identity, focus_role=focus_role)
                    if fact:
                        return fact
            except Exception as e:
                self.log.debug(f"观点剧情底座-敌方解析异常: {e}")

        # 2) 罪人本体（如"你怎么看浮士德"）
        try:
            from rag.query_processor import LCB_SINNERS
            for s in sorted(LCB_SINNERS, key=len, reverse=True):
                if s in text:
                    fact = build_story_fact_base(s, identity_note=f"边狱公司LCB罪人·{s}", focus_role=focus_role)
                    if fact:
                        return fact
        except Exception as e:
            self.log.debug(f"观点剧情底座-罪人解析异常: {e}")

        # 3) 人格单位（问人格时回落罪人本体剧情，如"你怎么看兔浮"→浮士德）
        if self.persona_direct is not None:
            try:
                from rag.query_processor import extract_personality_name
                title = extract_personality_name(text)
                if title:
                    sinner = self._persona_sinner(title)
                    fact = build_story_fact_base(sinner, identity_note=title, focus_role=focus_role)
                    if fact:
                        return fact
            except Exception as e:
                self.log.debug(f"观点剧情底座-人格解析异常: {e}")

        return ""

    def _current_sinner_name(self, session_id: str = "") -> str:
        """当前会话扮演的罪人中文名（用于剧情互动提取）；无则返回空串。"""
        try:
            persona_id = self.session_manager.get_persona(session_id)
        except Exception:
            return ""
        p = self.persona_manager.get(persona_id) if persona_id else None
        if p:
            return p.get("name") or ""
        return ""

    @staticmethod
    def _enemy_identity(real_name: str) -> tuple[str, str]:
        """敌方真实名 → (裸名, 身份备注)。'食指 父辈 - 里恩（第一阶段）'
        → ('里恩', '食指父辈（第一阶段）')。"""
        import re as _re
        bare = real_name.split(" - ")[-1].strip()
        # 裸名剥阶段括号（'里恩（第一阶段）' → '里恩'）
        bare = _re.sub(r"[（(].*?[)）]", "", bare).strip()
        prefix = " - ".join(real_name.split(" - ")[:-1]).strip()
        stage = ""
        m = _re.search(r"[（(].*?[)）]", real_name)
        if m:
            stage = m.group(0)
        identity = f"{prefix}{stage}".strip() or ""
        return bare, identity

    @staticmethod
    def _persona_sinner(title: str) -> str:
        """人格标题 → 罪人名（前缀匹配；如 浮士德黑兽-卯魁首 → 浮士德）。"""
        try:
            from crawler.html_extractor import _SINNER_PREFIX_MAP
            for sinner, _sid in _SINNER_PREFIX_MAP:
                if title.startswith(sinner):
                    return sinner
        except Exception:
            pass
        return title.split("-")[0] if "-" in title else title

    def _build_compare_fact_base(self, text: str) -> str:
        """compare 意图：构建单位数据底座（强度比较需要数值）。"""
        parts: list[str] = []
        if self.enemy_direct is not None:
            try:
                real_name = self.enemy_direct.resolve_enemy(text)
                if real_name:
                    recs = self.enemy_direct.get_enemy(real_name)
                    if recs:
                        parts.append(self._format_enemy_fact(recs))
            except Exception as e:
                self.log.debug(f"比较事实底座-敌方解析异常: {e}")
        if self.persona_direct is not None:
            try:
                from rag.query_processor import extract_personality_name
                title = extract_personality_name(text)
                if title:
                    rec = self.persona_direct.get_persona(title)
                    if rec:
                        parts.append(self._format_persona_fact(rec))
            except Exception as e:
                self.log.debug(f"比较事实底座-人格解析异常: {e}")
        return "\n".join(parts).strip()

    @staticmethod
    def _format_enemy_fact(recs: list[dict]) -> str:
        """敌方事实摘要：身份/组织/阶段/数值/被动/技能（不掺观点）。"""
        if not recs:
            return ""
        r = recs[0]
        name = r.get("enemy_name") or r.get("name") or "?"
        lines = [f"- 敌方单位：{name}"]
        stage = (r.get("battle_stage") or "").strip()
        if stage:
            lines.append(f"  登场关卡：{stage}")
        for k in ("hp", "defense_level", "speed", "chaos_threshold"):
            v = r.get(k)
            if v not in (None, ""):
                lines.append(f"  {k}：{v}")
        pr = r.get("physical_resistances") or {}
        if pr:
            lines.append("  物理抗性：" + " ".join(f"{k}={v}" for k, v in pr.items() if k in ("斩击", "突刺", "打击")))
        pvs = r.get("passives") or []
        if pvs:
            lines.append(f"  被动（首条）：{str(pvs[0])[:80]}")
        skills = r.get("skills") or []
        if skills:
            names = [s.get("skill_name") for s in skills[:4] if s.get("skill_name")]
            if names:
                lines.append("  技能：" + "、".join(str(n) for n in names))
        return "\n".join(lines)

    @staticmethod
    def _format_persona_fact(rec: dict) -> str:
        """人格事实摘要：罪人/组织/抗性/技能（不掺观点）。"""
        pname = rec.get("personality_name") or rec.get("title") or "?"
        lines = [f"- 人格单位：{pname}"]
        sinner = rec.get("sinner") or ""
        if sinner:
            lines.append(f"  罪人：{sinner}")
        sa = rec.get("sin_affinities") or {}
        if sa:
            lines.append("  罪孽亲和：" + " ".join(f"{k}{v}" for k, v in sa.items()))
        pr = rec.get("physical_resistances") or {}
        if pr:
            lines.append("  物理抗性：" + " ".join(f"{k}={v}" for k, v in pr.items() if k in ("斩击", "突刺", "打击")))
        skills = rec.get("skills") or []
        if skills:
            names = [s.get("skill_name") for s in skills[:4] if s.get("skill_name")]
            if names:
                lines.append("  技能：" + "、".join(str(n) for n in names))
        return "\n".join(lines)

    # ── P17：切换人格工具调用（预拦截 + LLM function-calling）──

    async def _try_persona_switch(self, text: str, session_id: str) -> Optional[str]:
        """尝试通过『切换人格』工具路径响应（P17）。

        两条路径（按优先级）：
        1) 正则强信号预拦截：命中「切换人格为X / 变成X / 扮演X」等明确句式 →
           确定性切换会话人格，零 LLM 成本、不依赖模型 function-calling。
        2) LLM 原生 function-calling 兜底：仅当含弱信号意图词（如「怎么切换人格」）
           时绑定 switch_persona 工具让 DeepSeek 判断（其 API 兼容 OpenAI tools 格式）。
           无 tool_call / 调用失败 → 返回 None，由调用方回落常规 RAG 链路。

        Returns:
            命中的回复文本；未命中（或不应拦截）返回 None。
        """
        # 1) 正则强信号预拦截（确定性兜底，主路径）
        try:
            preempt = run_persona_switch_preempt(
                self.persona_manager, self.session_manager, session_id, text
            )
            if preempt is not None:
                return preempt
        except Exception as e:
            self.log.warning(f"人格切换预拦截异常，回落常规链路: {e}")

        # 2) LLM 原生 function-calling 兜底（仅弱信号触发，避免所有消息都走 LLM）
        if not needs_persona_switch_llm(text):
            return None
        if self.llm is None:
            return None

        try:
            tool = create_persona_switch_tool(
                self.persona_manager, self.session_manager, session_id
            )
            llm_with_tools = self.llm.bind_tools([tool], tool_choice="auto")
            response = await llm_with_tools.ainvoke([HumanMessage(content=text)])
            tool_calls = self._extract_tool_calls(response)
            if not tool_calls:
                self.log.debug(f"人格切换弱信号但 LLM 未调用工具，按常规问答处理: {text[:30]}")
                return None

            results: list[str] = []
            for tc in tool_calls:
                name = tc.get("name", "")
                if name != "switch_persona":
                    continue
                persona_name = (tc.get("args", {}) or {}).get("persona_name", "")
                pid = resolve_persona_id(self.persona_manager, persona_name)
                if pid is None:
                    results.append(
                        f"无法识别人格「{persona_name}」。可用人格：\n"
                        f"{self.persona_manager.get_persona_display_info()}"
                    )
                else:
                    results.append(
                        switch_persona_impl(
                            self.persona_manager, self.session_manager, session_id, pid
                        )
                    )
            if not results:
                return None
            return "\n".join(results)
        except Exception as e:
            self.log.warning(f"人格切换 function-calling 失败，回落常规链路: {e}")
            return None

    @staticmethod
    def _extract_tool_calls(response: Any) -> list[dict]:
        """从 LLM 响应中兼容提取 tool_calls（LangChain AIMessage / OpenAI 原生）。"""
        # LangChain AIMessage 标准属性（langchain-core >= 0.2）
        tc = getattr(response, "tool_calls", None)
        if tc:
            return [
                {"name": t.get("name", ""), "args": t.get("args", {})}
                for t in tc
                if isinstance(t, dict)
            ]
        # OpenAI 原生 additional_kwargs.tool_calls
        ak = getattr(response, "additional_kwargs", None) or {}
        raw_calls = ak.get("tool_calls") or []
        out: list[dict] = []
        for c in raw_calls:
            fn = (c or {}).get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
            out.append({"name": fn.get("name", ""), "args": args})
        return out

    async def _reflect_rewrite_query(self, question: str, answer: str) -> str:
        """自我反思：让 LLM 根据原问题与低置信度回答，改写为更具体的检索查询词。

        返回改写后的查询词；失败或未改写时返回空字符串。
        """
        if self.llm is None:
            return ""
        prompt = (
            "你是一个边狱巴士（Limbus Company）Wiki 搜索引擎的查询优化器。\n"
            "用户的问题得到的回答置信度较低。请根据用户原问题，改写为一个更具体、"
            "更适合向量检索的查询词。\n"
            "要求：\n"
            "1. 保留用户的核心意图\n"
            "2. 补充正式名称/关键词（如俗称→正式名、模糊描述→具体实体）\n"
            "3. 只输出改写后的查询词本身，不要任何解释或引号\n"
            "\n"
            "用户问题：{question}\n"
            "当前回答（供参考）：{answer}\n"
            "改写后的查询词："
        )
        try:
            response = await self.llm.ainvoke(
                prompt.format(question=question, answer=(answer or "")[:200])
            )
            text = response.content if hasattr(response, "content") else str(response)
            text = text.strip().strip('"\'“”')
            if not text or len(text) > 60:
                return ""
            return text
        except Exception as e:
            self.log.warning(f"自我反思改写查询失败: {e}")
            return ""

    def _assess_confidence(self, reply: str) -> float:
        """评估回答的置信度 (0.0~1.0)。

        基于规则而非 LLM 调用，零额外延迟：
        - 包含不确定表述 → 低置信度
        - 正常响应 → 默认中等置信度
        """
        # 规则 1: 如果回答包含不确定性表述 → 低置信度
        uncertainty_phrases = [
            "不确定", "没有足够", "无法回答", "不清楚", "无法确定",
            "不明确", "难以判断", "暂时没有", "对此没有足够",
        ]
        if any(p in reply for p in uncertainty_phrases):
            return 0.1

        # 规则 2: 空回答 → 极低置信度
        if len(reply) < 10:
            return 0.05

        # 规则 3: 短回答 → 偏低置信度
        if len(reply) < 30:
            return 0.4

        # 规则 4: 正常长度回答 → 默认中等置信度
        return 0.75

    # QQ 群单条消息的字符安全上限（中文 UTF-8 约 3 字节/字，4000 字 ≈ 12KB，
    # 仍处于 QQ/NapCat 可接受范围；实测人格直答全文可达 8314 字符/20KB，
    # 超长会被 QQ 侧静默拒收——见症状2诊断）
    _SEND_CHUNK_MAX = 4000

    def _split_reply(self, text: str) -> list[str]:
        """按字符长度 + 换行边界切分回复文本，避免截断技能效果描述。

        优先在 ``\\n`` 处切分；单行超长（如技能效果单行 1383 字符）时
        才按纯字符硬切，保证每段不超过阈值。
        """
        if len(text) <= self._SEND_CHUNK_MAX:
            return [text]

        chunks: list[str] = []
        rest = text
        while len(rest) > self._SEND_CHUNK_MAX:
            # 在当前上限内找最后一个换行，作为切分点
            slice_end = min(self._SEND_CHUNK_MAX, len(rest))
            cut = rest.rfind("\n", 0, slice_end)
            if cut < self._SEND_CHUNK_MAX // 2:
                # 换行太靠前（说明单行超长），按字符硬切
                cut = slice_end
            chunks.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip("\n")
        if rest:
            chunks.append(rest.rstrip())
        return [c for c in chunks if c]

    async def _send_reply(self, msg: QQMessage, text: str, forward_sections: Optional[list[str]] = None):
        """通过 NapCatQQ 发送回复（含打字延迟 + 超长分段）。

        直答打包转发（P40）：``forward_sections`` 非空时，优先以合并转发
        （打包转发）消息发送——把多节数据封装为转发卡片，替代单条超长文本；
        转发失败（NapCat 拒收/未回执/异常）自动回落普通分段发送，不影响主链路。

        修复症状2：人格直答全文可达 8314 字符/20KB，QQ 群单条消息超长会被
        静默拒收。此处按行边界分段发送，并逐段检查 adapter 返回值记录日志，
        避免"检索到但不返回"的静默失败。
        """
        # ── P32：确定性清洗神态/动作描写（双保险，LLM 输出不可控）──
        text = self._strip_stage_directions(text)

        # 模拟打字延迟
        await self.typing_delay.delay(text)

        target_id, is_group = self.router.get_response_target(msg)
        try:
            tid = int(target_id)
        except ValueError:
            return

        # ── 直答打包转发（合并转发卡片）：分节 >= min_nodes 且启用时优先 ──
        if (
            forward_sections
            and len(forward_sections) >= self._forward_min_nodes
        ):
            try:
                sent = await self._send_forward_reply(msg, tid, forward_sections)
                if sent:
                    self.log.info(
                        f"直答打包转发成功: {len(forward_sections)} 节 (target={tid})"
                    )
                    return
                self.log.warning(f"直答打包转发被拒，回落普通分段发送 (target={tid})")
            except Exception as e:
                self.log.error(f"直答打包转发异常，回落普通分段发送: {e}")

        parts = self._split_reply(text)
        total = len(parts)
        for i, part in enumerate(parts, 1):
            try:
                if is_group:
                    ok = await self.adapter.send_group_msg(tid, part)
                else:
                    ok = await self.adapter.send_private_msg(tid, part)
            except Exception as e:
                self.log.error(f"发送回复第 {i}/{total} 段失败(异常): {e}")
                continue
            if not ok:
                self.log.error(
                    f"发送回复第 {i}/{total} 段被拒收(len={len(part)}, 可能超长或 NapCat 未回执)"
                )
                continue
            self.log.debug(f"发送回复第 {i}/{total} 段成功(len={len(part)})")
            # 段间轻微间隔，避免被 QQ 频率风控
            if i < total:
                await asyncio.sleep(0.3)

    async def _send_forward_reply(self, msg: QQMessage, tid: int, sections: list[str]) -> bool:
        """以合并转发（打包转发）消息发送直答数据分节。

        Args:
            msg: 原始消息（用于判定群聊/私聊）。
            tid: 目标群号或用户 QQ。
            sections: 转发分节文本列表（每节一条 node）。

        Returns:
            NapCat 回执确认成功返回 True；失败返回 False（调用方回落普通发送）。
        """
        from adapter.napcat import build_forward_nodes

        # 转发卡片发送者：配置 sender_name/sender_uin 优先，否则用机器人自身信息
        sender_name = self._forward_sender_name or "边狱巴士"
        sender_uin = self._forward_sender_uin or str(
            self.config.get("napcat", {}).get("bot_qq", "") or ""
        )
        nodes = build_forward_nodes(sections, sender_name=sender_name, sender_uin=sender_uin)
        if not nodes:
            return False

        if msg.is_group and msg.group_id:
            return await self.adapter.send_group_forward_msg(tid, nodes)
        return await self.adapter.send_private_forward_msg(tid, nodes)

    # ── 指令处理 ──

    async def _handle_command(self, msg: QQMessage, session_id: str):
        """处理 Bot 指令（支持运行时人格切换）"""
        cmd = msg.command

        if cmd == "状态" or cmd == "status":
            reply = self._cmd_status()
        elif cmd == "help" or cmd == "帮助":
            reply = self._cmd_help()
        elif cmd == "人格列表" or cmd == "persona_list":
            reply = self._cmd_persona_list()
        elif cmd.startswith("人格切换") or cmd.startswith("persona"):
            # /人格切换 <id>  或  /persona <id>
            parts = cmd.split(None, 1)
            target_id = parts[1].strip() if len(parts) > 1 else ""
            reply = self._cmd_persona_switch(session_id, target_id)
        elif cmd.startswith("最新推文") or cmd.startswith("拉推文") or cmd.startswith("拉取推文"):
            # /最新推文 [N]  → 拉取官方账号最近 N 条推文（含图片/视频）发到本会话
            parts = cmd.split()
            n = 3
            if len(parts) > 1:
                try:
                    n = max(1, min(int(parts[1]), 5))
                except ValueError:
                    pass
            await self._cmd_latest_tweets(msg, n)
            return
        elif (
            cmd.startswith("steam新闻") or cmd.startswith("steam资讯")
            or cmd.startswith("steam公告") or cmd.startswith("steam更新")
            or cmd.startswith("Steam新闻")
        ):
            # /steam新闻 [N]  → 拉取 Steam 最近 N 条公告/新闻（含配图）发到本会话
            n = 3
            try:
                n = max(1, min(int(msg.command_args.strip().split()[0]), 5))
            except (ValueError, IndexError):
                pass
            await self._cmd_latest_steam_news(msg, n)
            return
        else:
            reply = f"未知指令: /{cmd}。输入 /帮助 查看可用指令。"

        if reply:
            await self._send_reply(msg, reply)

    async def _cmd_latest_tweets(self, msg: QQMessage, n: int = 3):
        """P31：/最新推文 N —— 拉取官方账号最近 N 条推文并发送（文本+图片+视频）。

        P38：官方自回帖（线程续写）合并到父推文一并展示。
        """
        try:
            from crawler.x_fetcher import fetch_new_tweets, group_threads
            x_cfg = self.config.get("x_fetcher", {})
            accounts = x_cfg.get("accounts") or ["LimbusCompany_B"]
            rss_urls = x_cfg.get("rss_urls") or None

            # 先回执（拉取需要数秒）
            await self._send_reply(msg, f"正在拉取官方最新推文（最多 {n} 条）……")

            tweets = await fetch_new_tweets(
                state_path=str(self._x_pushed_path.parent / "x_feed_state.json"),
                accounts=accounts,
                rss_urls=rss_urls,
                pushed_ids=None,  # 拉取查看，不过滤已推
                filter_retweets=True,
            )
            if not tweets:
                await self._send_reply(msg, "暂时没有拉取到推文（RSS 可能暂时不可用）。")
                return

            # P38：官方自回帖合并到父推文线程，一并展示
            if bool(x_cfg.get("attach_official_replies", True)):
                tweets = group_threads(tweets)

            # 最新 n 条（时间倒序，按父推文计数）
            latest = sorted(tweets, key=lambda t: t.get("published_at", ""), reverse=True)[:n]

            target_id, is_group = self.router.get_response_target(msg)
            try:
                tid = int(target_id)
            except ValueError:
                return

            for i, tweet in enumerate(latest, 1):
                header = f"【官方推文 {i}/{len(latest)}】{tweet.get('handle') or 'LimbusCompany_B'}"
                body = self._format_tweet_body(tweet)

                full_text, images, videos = self._prepare_tweet_media(
                    tweet, f"{header}\n{body}"
                )

                if is_group:
                    sent = await self.adapter.send_group_msg_media(
                        group_id=tid,
                        text=full_text,
                        images=images,
                        videos=videos,
                        media_dir=self._media_dir,
                        max_video_mb=float(x_cfg.get("max_video_mb", 100.0)),
                        fallback_video_to_file=bool(x_cfg.get("fallback_video_to_file", True)),
                        prefer_local_images=bool(x_cfg.get("prefer_local_images", True)),
                    )
                    if not sent:
                        self.log.warning(f"/最新推文 发送失败: {tweet.get('tweet_id')}")
                else:
                    # 私聊：无媒体通道，附图片/视频链接
                    link_note = ""
                    if images:
                        link_note += "\n图片: " + " ".join(images[:3])
                    if videos:
                        link_note += "\n视频: " + " ".join(videos[:1])
                    await self.adapter.send_private_msg(tid, f"{header}\n{body}{link_note}")
        except Exception as e:
            self.log.error(f"/最新推文 指令异常: {e}")
            await self._send_reply(msg, f"拉取推文失败：{e}")

    async def _cmd_latest_steam_news(self, msg: QQMessage, n: int = 3):
        """P37：/steam新闻 N —— 拉取 Steam 最近 N 条公告/新闻并发送（文本+配图）。"""
        try:
            from crawler.steam_fetcher import fetch_steam_news
            steam_cfg = self.config.get("steam_fetcher", {})
            appid = int(steam_cfg.get("appid", 1973530))
            rss_url = steam_cfg.get("rss_url") or None

            # 先回执（拉取需要数秒）
            await self._send_reply(msg, f"正在拉取 Steam 最新公告（最多 {n} 条）……")

            items = await fetch_steam_news(appid=appid, rss_url=rss_url, limit=n)
            if not items:
                await self._send_reply(msg, "暂时没有拉取到 Steam 公告（RSS 可能暂时不可用）。")
                return

            target_id, is_group = self.router.get_response_target(msg)
            try:
                tid = int(target_id)
            except ValueError:
                return

            max_images = max(
                0, int(steam_cfg.get("max_images_per_item", 5))
            )
            for i, item in enumerate(items, 1):
                text = self._format_steam_item_text(item)
                header = f"【Steam公告 {i}/{len(items)}】{(item.get('title') or '').strip()}"
                body = (item.get("text") or "").strip()
                if body and body != (item.get("title") or "").strip():
                    body = body[:800]  # 正文过长截断，完整内容见公告页
                if item.get("permalink"):
                    body = f"{body}\n{item['permalink']}" if body else item["permalink"]
                full_text = f"{header}\n{body}"
                images = list(item.get("image_urls") or [])
                if max_images:
                    images = images[:max_images]

                if is_group:
                    if images:
                        sent = await self.adapter.send_group_msg_media(
                            group_id=tid,
                            text=full_text,
                            images=images,
                            media_dir=self._media_dir,
                            prefer_local_images=bool(
                                self.config.get("x_fetcher", {}).get("prefer_local_images", True)
                            ),
                        )
                    else:
                        sent = await self.adapter.send_group_msg(tid, full_text)
                    if not sent:
                        self.log.warning(f"/steam新闻 发送失败: {item.get('announcement_id')}")
                else:
                    # 私聊：无媒体通道，附图片链接
                    link_note = ""
                    if images:
                        link_note += "\n图片: " + " ".join(images[:3])
                    await self.adapter.send_private_msg(tid, f"{full_text}{link_note}")
        except Exception as e:
            self.log.error(f"/steam新闻 指令异常: {e}")
            await self._send_reply(msg, f"拉取 Steam 公告失败：{e}")

    def _cmd_status(self) -> str:
        """状态查询"""
        persona_name = ""
        if self._current_persona_id:
            p = self.persona_manager.get(self._current_persona_id)
            persona_name = p.get("display_name", self._current_persona_id) if p else self._current_persona_id

        return (
            f"Bot 状态:\n"
            f"  当前人格: {persona_name or '（未设置）'}\n"
            f"  活跃会话: {self.session_manager.active_count}\n"
            f"  敏感词规则: {self.sensitive_filter.pattern_count}\n"
            f"  全局频率: {self.rate_limiter.global_count_last_minute}/分钟\n"
            f"  NapCatQQ: {'已连接' if self.adapter.connected else '未连接'}"
        )

    def _cmd_help(self) -> str:
        """帮助信息"""
        return (
            "可用指令:\n"
            "/状态 - 查看 Bot 运行状态\n"
            "/帮助 - 显示本帮助\n"
            "/人格列表 - 查看所有可用人格\n"
            "/人格切换 <id> - 切换当前会话的人格\n"
            "/最新推文 [N] - 拉取官方最新 N 条推文（含图片/视频，N 默认 3，最多 5）\n"
            "/steam新闻 [N] - 拉取 Steam 最新 N 条公告/新闻（含配图，N 默认 3，最多 5）\n\n"
            "直接发送消息即可与角色对话，支持边狱巴士知识问答。"
        )

    def _cmd_persona_list(self) -> str:
        """列出所有可用人格"""
        return self.persona_manager.get_persona_display_info()

    def _cmd_persona_switch(self, session_id: str, persona_id: str) -> str:
        """运行时动态切换人格（仅影响当前会话）。

        Args:
            session_id: 当前会话 ID
            persona_id: 目标人格 ID
        """
        if not persona_id:
            return "用法: /人格切换 <人格ID>\n输入 /人格列表 查看所有可用人格。"

        # 检查人格是否存在
        persona = self.persona_manager.get(persona_id)
        if not persona:
            available = "、".join(self.persona_manager.list_ids()) or "（无）"
            return f"未找到人格「{persona_id}」。可用人格: {available}"

        # 更新会话人格
        self.session_manager.set_persona(session_id, persona_id)
        display_name = persona.get("display_name", persona_id)
        self.log.info(f"人格切换: session={session_id} → {persona_id}")

        return f"已切换人格为「{display_name}」。{persona.get('identity', '')}"

    # ── 启动 ──

    async def start(self):
        """启动 Agent"""
        self.log.info("边狱巴士 RAG Agent 正在启动...")

        # 加载人格配置
        self.persona_manager.load_all()

        # 初始化 RAG
        try:
            await self.initialize_rag()
        except Exception as e:
            self.log.warning(f"RAG 初始化失败（消息功能可能受限）: {e}")

        # 注册消息处理回调
        self.adapter.on_message(self.handle_event)

        # ── P20-B: 挂载 X 新推轮询后台任务（不阻塞主事件循环）──
        if self.config.get("x_fetcher", {}).get("enabled", False):
            self._x_poll_task = asyncio.create_task(self._x_poll_loop())
            self.log.info("X 新推轮询后台任务已启动")

        # ── P37: 挂载 Steam 新公告轮询后台任务（不阻塞主事件循环）──
        if self.config.get("steam_fetcher", {}).get("enabled", False):
            self._steam_poll_task = asyncio.create_task(self._steam_poll_loop())
            self.log.info("Steam 新公告轮询后台任务已启动")

        # 连接 NapCatQQ
        self.log.info("Agent 启动完成，等待 NapCatQQ 连接...")
        await self.adapter.connect()

    async def shutdown(self):
        """关闭 Agent"""
        self.log.info("Agent 正在关闭...")

        # ── P20-B: 取消 X 新推轮询后台任务（吞掉 CancelledError）──
        if self._x_poll_task:
            self._x_poll_task.cancel()
            try:
                await self._x_poll_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.log.warning(f"X 轮询任务退出异常（忽略）: {e}")
            self._x_poll_task = None

        # ── P37: 取消 Steam 新公告轮询后台任务（吞掉 CancelledError）──
        if self._steam_poll_task:
            self._steam_poll_task.cancel()
            try:
                await self._steam_poll_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.log.warning(f"Steam 轮询任务退出异常（忽略）: {e}")
            self._steam_poll_task = None

        # 持久化推送状态
        self._save_x_pushed()
        self._save_push_groups()
        self._save_steam_pushed()

        await self.adapter.disconnect()
        self.log.info("Agent 已关闭")
