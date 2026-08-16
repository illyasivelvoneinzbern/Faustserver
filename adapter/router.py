"""
消息路由器：决定消息是否需要 Agent 响应，以及路由到哪个会话。
"""

import logging
from typing import Optional

from adapter.types import QQMessage, parse_napcat_event

logger = logging.getLogger(__name__)


class MessageRouter:
    """消息路由控制器"""

    def __init__(
        self,
        bot_qq: str = "",
        trigger_keywords: list[str] | None = None,
        command_prefix: str = "/",
    ):
        self.bot_qq = bot_qq
        self.trigger_keywords = trigger_keywords or []
        self.command_prefix = command_prefix

    def parse_event(self, event: dict) -> Optional[QQMessage]:
        """将 NapCatQQ 事件解析为 QQMessage"""
        return parse_napcat_event(event, self.bot_qq)

    def should_respond(self, msg: QQMessage) -> bool:
        """
        判断是否应该响应此消息。

        规则优先级：
        1. 指令消息 (/开头) → 总是响应
        2. 私聊消息 → 总是响应
        3. 群聊 @Bot → 总是响应
        4. 群聊含触发关键词 → 响应
        5. 其他 → 不响应
        """
        # 指令消息
        if msg.command:
            return True

        # 私聊消息
        if not msg.is_group:
            return True

        # 群聊 @机器人
        if msg.is_at_bot:
            return True

        # 群聊触发关键词
        if self.trigger_keywords:
            text_lower = msg.text.lower()
            for kw in self.trigger_keywords:
                if kw.lower() in text_lower:
                    return True

        return False

    def get_session_id(self, msg: QQMessage) -> str:
        """获取会话 ID（群聊 → group_id，私聊 → user_id）"""
        if msg.is_group and msg.group_id:
            return f"group_{msg.group_id}"
        return f"user_{msg.user_id}"

    def get_response_target(self, msg: QQMessage) -> tuple[str, bool]:
        """
        获取回复目标。
        返回 (target_id, is_group)
        """
        if msg.is_group and msg.group_id:
            return msg.group_id, True
        return msg.user_id, False
