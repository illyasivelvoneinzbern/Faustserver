"""
频率控制模块：三层速率限制（用户冷却 + 全局每分钟 + 群聊降频）。
"""

import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """三层消息频率控制器"""

    # ── 第一层：用户级冷却 ──
    per_user_cooldown: float = 5.0

    # ── 第二层：全局每分钟上限 ──
    global_per_minute: int = 10

    # ── 第三层：群聊降频 ──
    group_per_minute: int = 3

    # ── 内部状态 ──
    _last_user_time: dict[str, float] = field(default_factory=dict)
    _global_timestamps: list[float] = field(default_factory=list)
    _group_timestamps: dict[str, list[float]] = field(default_factory=dict)

    def check(self, user_id: str, group_id: str | None = None) -> tuple[bool, str]:
        """
        检查是否允许发送消息。

        返回 (是否允许, 拒绝原因)
        """
        now = time.monotonic()

        # 1. 用户冷却
        if user_id in self._last_user_time:
            elapsed = now - self._last_user_time[user_id]
            if elapsed < self.per_user_cooldown:
                remaining = self.per_user_cooldown - elapsed
                return False, f"冷却中，请 {remaining:.1f}s 后再试"

        # 2. 全局每分钟
        self._global_timestamps = [t for t in self._global_timestamps if now - t < 60]
        if len(self._global_timestamps) >= self.global_per_minute:
            return False, "全局消息频率已达上限，请稍候"

        # 3. 群聊降频
        if group_id:
            ts = self._group_timestamps.setdefault(group_id, [])
            ts[:] = [t for t in ts if now - t < 60]
            if len(ts) >= self.group_per_minute:
                return False, "本群消息频率已达上限，请稍候"

        # 全部通过 → 记录时间戳
        self._last_user_time[user_id] = now
        self._global_timestamps.append(now)
        if group_id:
            self._group_timestamps[group_id].append(now)

        return True, "ok"

    def reset_user(self, user_id: str):
        """手动重置用户冷却（如 Bot 管理员指令）"""
        self._last_user_time.pop(user_id, None)

    @property
    def global_count_last_minute(self) -> int:
        """当前全局每分钟计数"""
        now = time.monotonic()
        return sum(1 for t in self._global_timestamps if now - t < 60)
