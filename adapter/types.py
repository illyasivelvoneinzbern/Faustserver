"""
消息类型定义：标准化 QQ 消息结构。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QQMessage:
    """标准化 QQ 消息"""
    message_id: str
    user_id: str
    user_name: str = ""
    group_id: str | None = None      # 群消息时有值，私聊为 None
    group_name: str = ""
    text: str = ""                    # 纯文本内容
    raw_text: str = ""                # 原始文本（含 CQ 码）
    is_group: bool = False
    is_at_bot: bool = False           # 是否 @了机器人
    command: str = ""                 # 指令（如 "/人格切换"）
    command_args: str = ""            # 指令参数
    timestamp: float = 0.0


def _message_to_raw_text(message) -> str:
    """将 NapCat ``message`` 字段（字符串或 segment 数组）重建为 CQ 码格式文本。

    NapCat 新版事件的 ``message`` 为 segment 数组（例如
    ``[{"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": "你好"}}]``），
    此时 ``raw_message`` 可能为空字符串，需从此数组重建（保留 CQ 码供 at 检测）。
    """
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            data = seg.get("data") or {}
            if seg_type == "text":
                parts.append(str(data.get("text", "")))
            elif seg_type == "at":
                parts.append(f"[CQ:at,qq={data.get('qq', '')}]")
            elif seg_type == "reply":
                parts.append(f"[CQ:reply,id={data.get('id', '')}]")
            elif seg_type == "image":
                parts.append(f"[CQ:image,file={data.get('file', '')}]")
            elif seg_type == "face":
                parts.append(f"[CQ:face,id={data.get('id', '')}]")
            else:
                txt = data.get("text")
                if txt:
                    parts.append(str(txt))
                else:
                    parts.append(f"[CQ:{seg_type}]")
        return "".join(parts)
    return ""


def _is_at_bot(message, raw_message: str, bot_qq: str) -> bool:
    """检测消息是否 @了机器人（兼容 segment 数组与字符串两种形态）。"""
    if not bot_qq:
        return False
    bot_qq = str(bot_qq)
    if isinstance(message, list):
        # 优先遍历 segment 数组找 at 段（P21-C：NapCat 新版 message 为数组）
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) == bot_qq:
                return True
        # 数组未命中再兜底字符串（部分实现同时下发 raw_message）
        return f"[CQ:at,qq={bot_qq}]" in raw_message
    return f"[CQ:at,qq={bot_qq}]" in raw_message


def parse_napcat_event(event: dict, bot_qq: str = "") -> Optional[QQMessage]:
    """解析 NapCatQQ WebSocket 事件为 QQMessage"""

    post_type = event.get("post_type", "")
    if post_type != "message":
        return None

    message_type = event.get("message_type", "")
    is_group = message_type == "group"

    # 优先用 raw_message；为空时（NapCat 新版仅下发 message 数组）从 message 重建（P21-C）
    raw_message = event.get("raw_message") or _message_to_raw_text(event.get("message", ""))

    # 提取纯文本（去除 CQ 码）
    import re
    text = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()

    # 检查是否 @了机器人（兼容 message 数组与字符串两种形态，P21-C）
    is_at_bot = _is_at_bot(event.get("message"), raw_message, bot_qq) if (is_group and bot_qq) else False

    # 指令解析
    command = ""
    command_args = ""
    if text.startswith("/"):
        parts = text[1:].split(None, 1)
        command = parts[0] if parts else ""
        command_args = parts[1] if len(parts) > 1 else ""

    sender = event.get("sender", {})

    return QQMessage(
        message_id=str(event.get("message_id", "")),
        user_id=str(sender.get("user_id", "")),
        user_name=sender.get("nickname", ""),
        group_id=str(event.get("group_id", "")) if is_group else None,
        text=text,
        raw_text=raw_message,
        is_group=is_group,
        is_at_bot=is_at_bot,
        command=command,
        command_args=command_args,
        timestamp=event.get("time", 0),
    )
