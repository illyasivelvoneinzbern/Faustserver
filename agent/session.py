"""
会话管理模块：每个 QQ 会话（群/好友）维护独立的对话状态。
"""

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    """单个会话的上下文状态"""

    session_id: str
    persona_id: str
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    # P39：模糊搜索消歧的待确认选择（{"choices": [...], "query": str}），
    # 用户回复数字编号后清空；非数字消息也会清除（防过期选择残留）。
    pending_choice: dict | None = field(default=None)

    def touch(self):
        """更新最后活跃时间"""
        self.last_active = time.monotonic()

    def is_expired(self, timeout_seconds: float) -> bool:
        """判断会话是否过期"""
        return (time.monotonic() - self.last_active) > timeout_seconds


class SessionManager:
    """会话管理器：管理所有活跃会话"""

    def __init__(self, default_persona: str = "", session_timeout: float = 3600):
        self.default_persona = default_persona
        self.session_timeout = session_timeout
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str, persona_id: str = "") -> Session:
        """获取已有会话或创建新会话"""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if not session.is_expired(self.session_timeout):
                session.touch()
                return session
            # 过期了，清理重建
            del self._sessions[session_id]

        pid = persona_id or self.default_persona
        session = Session(session_id=session_id, persona_id=pid)
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> "Session | None":
        """获取会话对象（不存在或已过期返回 None，不重建）。"""
        session = self._sessions.get(session_id)
        if session is None or session.is_expired(self.session_timeout):
            return None
        return session

    def get_persona(self, session_id: str) -> str:
        """获取会话当前人格 ID"""
        session = self._sessions.get(session_id)
        return session.persona_id if session else self.default_persona

    def set_persona(self, session_id: str, persona_id: str):
        """切换会话的人格"""
        session = self.get_or_create(session_id, persona_id)
        session.persona_id = persona_id

    def cleanup_expired(self):
        """清理过期会话"""
        expired = [
            sid for sid, s in self._sessions.items()
            if s.is_expired(self.session_timeout)
        ]
        for sid in expired:
            del self._sessions[sid]

    @property
    def active_count(self) -> int:
        """活跃会话数"""
        return len(self._sessions)
