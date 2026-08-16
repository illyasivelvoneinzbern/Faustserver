"""
真人打字延迟模拟器：让 Bot 的回复看起来更像真人打字。
"""

import asyncio
import random


class TypingDelaySimulator:
    """
    模拟真人打字节奏：
    - base_delay: 基础思考时间（秒）
    - 每字符随机延迟 char_delay_min ~ char_delay_max
    - max_delay: 硬上限
    """

    def __init__(
        self,
        base_delay: float = 1.0,
        char_delay_min: float = 0.05,
        char_delay_max: float = 0.15,
        max_delay: float = 8.0,
    ):
        self.base_delay = base_delay
        self.char_delay_min = char_delay_min
        self.char_delay_max = char_delay_max
        self.max_delay = max_delay

    def calc_delay(self, text: str) -> float:
        """计算延迟秒数（不执行等待）"""
        char_count = len(text)
        typing_time = sum(
            random.uniform(self.char_delay_min, self.char_delay_max)
            for _ in range(char_count)
        )
        return min(self.base_delay + typing_time, self.max_delay)

    async def delay(self, text: str) -> float:
        """计算并执行延迟，返回实际等待秒数"""
        total = self.calc_delay(text)
        await asyncio.sleep(total)
        return total

    def delay_sync(self, text: str) -> float:
        """同步版本（仅计算延迟，不做实际等待）"""
        return self.calc_delay(text)
