"""
NapCatQQ WebSocket 适配器：与 NapCatQQ 客户端通信。

P20-B 新增多模态发送：
- `send_group_msg_media(group_id, text, images, videos, files)`：把文本 + 媒体拼成
  CQ 码字符串发送（图片支持远程 URL / 本地路径；视频必须本地，超大时降级为文件/纯文本）。
- CQ 码构造逻辑抽成可测纯函数：`cq_text()` / `cq_image()` / `cq_video()` / `cq_file()`。
- 本地路径统一转 `file:///绝对路径` 形式。
"""

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Awaitable, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# 消息处理回调类型
MessageCallback = Callable[[dict], Awaitable[None]]

# ─────────────────────────────────────────────────────────────
# CQ 码构造（纯函数，便于单元测试）
# ─────────────────────────────────────────────────────────────

# 需要转义的 CQ 码字符：& → &（注意顺序），[ ] , → 转义形式
_CQ_ESCAPE_RE = re.compile(r"[&\[\],]")


def cq_escape(text: str) -> str:
    """转义 CQ 码特殊字符（& 先转，避免二次转义）。

    注：本函数主要服务于媒体 CQ 码段构造（cq_text）；普通文本回复
    （send_group_msg/send_private_msg）不经过此处，避免过度转义。
    """
    if not text:
        return ""
    return text.replace(",", "&#44;")


def _to_file_url(path: str) -> str:
    """把本地路径转为 file:/// 绝对路径形式（CQ 码 file 字段标准）。"""
    p = Path(path).resolve()
    return "file:///" + p.as_posix()


def cq_text(text: str) -> str:
    """构造纯文本段（对 CQ 特殊字符转义）。"""
    return cq_escape(text)


def cq_image(file: str) -> str:
    """构造图片段：file 为远程 URL 或本地路径。"""
    if file.startswith("http://") or file.startswith("https://"):
        return f"[CQ:image,file={file}]"
    return f"[CQ:image,file={_to_file_url(file)}]"


def cq_video(file: str) -> str:
    """构造视频段：必须为本地路径（NapCat 对远程视频 URL 支持差）。"""
    if file.startswith("http://") or file.startswith("https://"):
        logger.warning(f"视频建议使用本地路径，收到远程 URL: {file[:80]}")
        return f"[CQ:video,file={file}]"
    return f"[CQ:video,file={_to_file_url(file)}]"


def cq_file(file: str) -> str:
    """构造文件段：本地路径。"""
    if file.startswith("http://") or file.startswith("https://"):
        return f"[CQ:file,file={file}]"
    return f"[CQ:file,file={_to_file_url(file)}]"


def _video_file_size_mb(path: str) -> Optional[float]:
    """返回本地文件大小（MB）；非本地文件返回 None（无法判断则视为不超限）。"""
    if path.startswith(("http://", "https://", "file://")):
        return None
    try:
        return Path(path).stat().st_size / (1024 * 1024)
    except OSError:
        return None


def _build_media_segments(
    text: str = "",
    images: Optional[list[str]] = None,
    videos: Optional[list[str]] = None,
    files: Optional[list[str]] = None,
    max_video_mb: float = 100.0,
    fallback_video_to_file: bool = True,
) -> list[str]:
    """把文本 + 图片 + 视频 + 文件组装成 CQ 码段列表（每段为独立行）。

    规则：
    - 文本在前。
    - 图片逐个生成 `[CQ:image,...]` 段。
    - 视频：本地且未超 `max_video_mb` → `[CQ:video,...]`；
      超限 → 降级为 `[CQ:file,...]`（fallback_video_to_file=True）或纯文本链接。
    - 文件 → `[CQ:file,...]`。

    Returns:
        CQ 码段字符串列表（空段已被过滤；无任何内容时返回 []）。
    """
    segments: list[str] = []

    if text and text.strip():
        segments.append(cq_text(text.strip()))

    for img in images or []:
        img = (img or "").strip()
        if img:
            segments.append(cq_image(img))

    for vid in videos or []:
        vid = (vid or "").strip()
        if not vid:
            continue
        size_mb = _video_file_size_mb(vid)
        if size_mb is not None and size_mb > max_video_mb:
            logger.warning(
                f"视频 {size_mb:.1f}MB 超过阈值 {max_video_mb}MB，"
                f"{'降级为文件发送' if fallback_video_to_file else '降级为文字+链接'}: {vid[:80]}"
            )
            if fallback_video_to_file:
                segments.append(cq_file(vid))
            else:
                segments.append(cq_text(f"（视频较大 {size_mb:.1f}MB，请查看链接）{vid}"))
        else:
            segments.append(cq_video(vid))

    for f in files or []:
        f = (f or "").strip()
        if f:
            segments.append(cq_file(f))

    # 过滤空段
    segments = [s for s in segments if s.strip()]
    return segments


def _split_image_segments(segments: list[str], max_images_per_msg: int) -> list[list[str]]:
    """把 CQ 段按图片数量拆分为多条消息（单条超限时分多条发送）。

    仅统计 `[CQ:image,...]` 段；文本/视频/文件段都跟随第一条。若 max_images_per_msg<=0
    视为不拆分（由调用方控制）。
    """
    if max_images_per_msg <= 0:
        return [segments]

    chunks: list[list[str]] = [[]]
    img_count = 0
    non_img_emitted = False
    for seg in segments:
        is_img = seg.startswith("[CQ:image,")
        if is_img:
            if img_count >= max_images_per_msg:
                chunks.append([])
                img_count = 0
            chunks[-1].append(seg)
            img_count += 1
        else:
            # 文本/视频/文件放第一条消息
            if not non_img_emitted:
                chunks[0].append(seg)
                non_img_emitted = True
    return [c for c in chunks if c]


# ─────────────────────────────────────────────────────────────
# 媒体下载辅助（httpx）
# ─────────────────────────────────────────────────────────────

# 常见 User-Agent（部分源对默认 UA 敏感）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_EXT_BY_KIND = {"image": ".jpg", "video": ".mp4", "file": ".bin"}


def guess_ext(url: str, kind: str = "file") -> str:
    """从 URL 路径推断扩展名；失败回退 kind 默认扩展。"""
    path = url.split("?", 1)[0]
    m = re.search(r"\.([a-zA-Z0-9]{2,5})$", path)
    if m:
        return "." + m.group(1).lower()
    return _EXT_BY_KIND.get(kind, ".bin")


async def download_media(
    url: str,
    media_dir: str,
    kind: str = "file",
    filename_hint: str = "",
    timeout: float = 120.0,
) -> Optional[str]:
    """下载远程媒体到 media_dir，返回本地绝对路径；失败返回 None。

    filename_hint 用于生成防冲突前缀（如 tweet_id）。
    """
    try:
        import httpx
    except ImportError:
        logger.error("httpx 未安装，无法下载媒体")
        return None

    Path(media_dir).mkdir(parents=True, exist_ok=True)

    # 生成文件名：hint + 随机/序号 + 扩展名
    hint = (filename_hint or "")
    m = re.search(r"(\d{5,})", hint)
    id_part = m.group(1) if m else ""
    ext = guess_ext(url, kind)

    import time
    ts = int(time.time() * 1000)
    base = f"{id_part or 'media'}-{ts}"
    dest = Path(media_dir) / f"{base}{ext}"

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": _DEFAULT_UA},
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        logger.info(f"媒体已下载: {url[:80]} → {dest}")
        return str(dest.resolve())
    except Exception as e:
        logger.warning(f"媒体下载失败 ({url[:80]}): {e}")
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        return None


class NapCatAdapter:
    """NapCatQQ WebSocket 客户端适配器"""

    def __init__(
        self,
        ws_url: str = "ws://localhost:3001",
        reconnect_interval: float = 5.0,
        token: str = "",
    ):
        self.ws_url = ws_url
        self.reconnect_interval = reconnect_interval
        self.token = token
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._callbacks: list[MessageCallback] = []
        self._running = False
        self._connected = False
        # OneBot 回执匹配：echo → asyncio.Future（等待 NapCat 返回 status/retcode）
        self._pending_acks: dict[str, asyncio.Future] = {}
        # 发送回执超时（秒）：超过视为发送失败（QQ 侧拒收/超长等）
        self._ack_timeout: float = 10.0
        self._echo_counter: int = 0

    def on_message(self, callback: MessageCallback):
        """注册消息回调"""
        self._callbacks.append(callback)

    async def connect(self):
        """连接到 NapCatQQ WebSocket 服务"""
        self._running = True
        # 将 token 拼接到 URL query 参数中（兼容旧版 websockets 库）
        ws_url = self.ws_url
        if self.token:
            separator = "?" if "?" not in ws_url else "&"
            ws_url = f"{ws_url}{separator}access_token={self.token}"
            logger.info(f"使用 Token 鉴权连接 NapCatQQ: {self.ws_url}")
        else:
            logger.info(f"正在连接 NapCatQQ: {self.ws_url}")
        while self._running:
            try:
                self.ws = await websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                )
                self._connected = True
                logger.info("NapCatQQ 已连接")
                await self._listen()
            except ConnectionClosed as e:
                logger.warning(f"NapCatQQ 连接断开: {e}")
                self._connected = False
            except Exception as e:
                logger.error(f"NapCatQQ 连接异常: {e}")
                self._connected = False

            if self._running:
                logger.info(f"将在 {self.reconnect_interval}s 后重连...")
                await asyncio.sleep(self.reconnect_interval)

    async def _listen(self):
        """监听 WebSocket 消息"""
        async for raw in self.ws:
            try:
                event = json.loads(raw)
                # 优先处理 OneBot 回执（带 echo 字段的 API 响应）
                if isinstance(event, dict) and event.get("echo") is not None:
                    future = self._pending_acks.pop(str(event.get("echo")), None)
                    if future is not None and not future.done():
                        future.set_result(event)
                    continue
                # 触发所有注册的回调
                for cb in self._callbacks:
                    try:
                        await cb(event)
                    except Exception as e:
                        logger.error(f"消息回调异常: {e}")
            except json.JSONDecodeError:
                logger.warning(f"无效的 JSON 消息: {raw[:200]}")
            except Exception as e:
                logger.error(f"消息处理异常: {e}")

    async def send_group_msg(self, group_id: int, message: str) -> bool:
        """发送群聊消息（文本，向后兼容）"""
        return await self._send({
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message,
            },
        })

    async def send_private_msg(self, user_id: int, message: str) -> bool:
        """发送私聊消息（文本，向后兼容）"""
        return await self._send({
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message,
            },
        })

    async def send_group_msg_media(
        self,
        group_id: int,
        text: str = "",
        images: Optional[list[str]] = None,
        videos: Optional[list[str]] = None,
        files: Optional[list[str]] = None,
        media_dir: str = "data/media",
        max_video_mb: float = 100.0,
        fallback_video_to_file: bool = True,
        prefer_local_images: bool = True,
        max_images_per_msg: int = 20,
    ) -> bool:
        """发送群聊多模态消息（文本 + 图片 + 视频 + 文件，CQ 码拼接）。

        Args:
            group_id: 目标群号
            text: 纯文本
            images: 图片 URL 或本地路径列表（远程直发；prefer_local_images=True 时本地化）
            videos: 视频 URL 或本地路径列表（必须本地化；超大降级为文件）
            files: 额外文件路径列表（[CQ:file,file=file:///...]）
            media_dir: 媒体本地落地目录
            max_video_mb: 视频超过该大小（MB）时降级
            fallback_video_to_file: True=超大视频降级为发文件；False=仅发文字+链接
            prefer_local_images: True=图片下载到本地再发（更稳）；False=优先远程直发
            max_images_per_msg: 单条消息最多图片数（超出分多条）

        Returns:
            全部发送成功返回 True；任一失败返回 False。
        """
        images = images or []
        videos = videos or []
        files = files or []

        # 1) 媒体本地化（图片按需、视频强制）
        local_images: list[str] = []
        for img in images:
            local = await self._ensure_local_media(img, media_dir, "image")
            if local is None:
                logger.warning(f"图片本地化失败，跳过: {str(img)[:80]}")
                continue
            local_images.append(local if prefer_local_images else img)

        local_videos: list[str] = []
        for vid in videos:
            local = await self._ensure_local_media(vid, media_dir, "video")
            if local is None:
                logger.warning(f"视频本地化失败，跳过: {str(vid)[:80]}")
                continue
            local_videos.append(local)

        # 2) 构造 CQ 码（纯函数）
        segments = _build_media_segments(
            text=text,
            images=local_images,
            videos=local_videos,
            files=files,
            max_video_mb=max_video_mb,
            fallback_video_to_file=fallback_video_to_file,
        )
        if not segments:
            return False

        # 3) 发送（P34：QQ 不允许视频与文本/图片混发，需分开发送）
        #    - 文本 + 图片 合并为消息组（按 max_images_per_msg 拆分多条）
        #    - 每个视频/文件段单独成一条消息（CQ:video / CQ:file 独立发送，
        #      避免与文本/图片混在一起被 QQ 拒收/静默丢弃）
        ok = True
        text_image_segs = [
            s for s in segments
            if not (s.startswith("[CQ:video,") or s.startswith("[CQ:file,"))
        ]
        if text_image_segs:
            chunks = _split_image_segments(text_image_segs, max_images_per_msg)
            for chunk in chunks:
                message = "\n".join(chunk)
                if message.strip() and not await self.send_group_msg(group_id, message):
                    ok = False
        for s in segments:
            if s.startswith("[CQ:video,") or s.startswith("[CQ:file,"):
                if not await self.send_group_msg(group_id, s):
                    ok = False
        return ok

    async def _ensure_local_media(
        self, url: str, media_dir: str, kind: str
    ) -> Optional[str]:
        """把媒体（URL 或本地路径）落盘到 media_dir，返回本地绝对路径。

        - 已是本地文件路径 → 直接返回绝对路径。
        - 是 URL（http/https/file）→ 下载到 media_dir/<tweetid-序号>.<ext>。
        - 失败返回 None（不抛出，由调用方决定跳过）。
        """
        src = (str(url) or "").strip()
        if not src:
            return None

        # 本地路径（不含协议前缀）
        if src.startswith(("file:///", "file://")):
            p = src.replace("file:///", "").replace("file://", "")
            if Path(p).exists():
                return str(Path(p).resolve())
            return None
        if "://" not in src:
            p = Path(src)
            if p.exists():
                return str(p.resolve())
            return None

        # 远程 URL → 下载
        return await download_media(
            url=src, media_dir=media_dir, kind=kind, filename_hint=src
        )

    async def _send(self, payload: dict, wait_ack: bool = True) -> bool:
        """底层发送方法。

        通过 OneBot 的 ``echo`` 机制携带唯一标识发送，并等待 NapCat 返回回执，
        检查 ``status``/``retcode`` 判定真实成败（避免超长消息被 QQ 侧静默拒收时
        误判为成功——见症状2诊断）。

        Args:
            payload: OneBot API 请求体。
            wait_ack: 是否等待回执（默认 True）。媒体等内部发送建议保持 True；
                      纯探测场景可传 False 以快速返回。

        Returns:
            发送且 NapCat 回执确认成功返回 True；未连接/回执失败/超时返回 False。
        """
        if not self.ws or not self._connected:
            logger.warning("WebSocket 未连接，无法发送消息")
            return False

        # 附加 echo 唯一标识（若调用方未显式指定）
        self._echo_counter += 1
        echo = str(self._echo_counter)
        payload.setdefault("echo", echo)

        future: Optional[asyncio.Future] = None
        if wait_ack:
            future = asyncio.get_event_loop().create_future()
            self._pending_acks[echo] = future

        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            if future is not None:
                self._pending_acks.pop(echo, None)
            logger.error(f"发送消息失败: {e}")
            return False

        if not wait_ack:
            logger.debug(f"已发送消息 (echo={echo})：{str(payload.get('action'))}")
            return True

        # 等待回执（带超时）
        try:
            resp = await asyncio.wait_for(
                asyncio.shield(future), timeout=self._ack_timeout
            )
        except asyncio.TimeoutError:
            self._pending_acks.pop(echo, None)
            logger.warning(
                f"发送消息回执超时 (echo={echo}, action={payload.get('action')})，"
                f"可能被 NapCat/QQ 侧拒收（如超长消息）"
            )
            return False
        except Exception as e:
            self._pending_acks.pop(echo, None)
            logger.error(f"发送消息回执异常: {e}")
            return False

        # 解析回执
        retcode = resp.get("retcode")
        status = resp.get("status")
        ok = retcode in (0, None) or status in ("ok", "success")
        if ok:
            logger.debug(f"发送消息成功 (echo={echo}, action={payload.get('action')})")
            return True
        msg = resp.get("msg") or resp.get("message") or ""
        logger.error(
            f"发送消息被拒 (echo={echo}, action={payload.get('action')}, "
            f"retcode={retcode}, status={status}, msg={msg[:120]})"
        )
        return False

    async def disconnect(self):
        """断开连接（安全关闭，容忍多次调用）"""
        self._running = False
        self._connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    @property
    def connected(self) -> bool:
        return self._connected
