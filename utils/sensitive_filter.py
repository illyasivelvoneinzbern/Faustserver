"""
敏感词过滤与高危行为拦截模块。
对 QQ 个人号有实时文本过滤，触发敏感词将导致封号。
必须做到输入 + 输出双重过滤。
"""

import re
import time
from pathlib import Path
from typing import Optional


# ── 边狱巴士相关话题白名单关键词 ──
ALLOWED_TOPIC_KEYWORDS = [
    "边狱", "巴士", "limbus", "罪人", "人格", "ego", "异想体",
    "公司", "都市", "但丁", "维吉里乌斯", "浮士德", "堂吉诃德",
    "良秀", "格里高尔", "希斯克利夫", "以实玛利", "罗佳",
    "辛克莱", "奥提斯", "鸿", "默尔索", "卡戎", "萨姆乔",
    "扭曲", "侦探", "收尾人", "拇指", "食指", "中指", "环指",
]


class SensitiveFilter:
    """敏感词过滤器：输入 + 输出双重保障 + 话题白名单守卫 + 连续违规熔断"""

    def __init__(
        self,
        wordlist_path: str = "./data/sensitive_words.txt",
        enable_input_filter: bool = True,
        enable_output_filter: bool = True,
        enable_topic_guard: bool = True,
        max_violations: int = 3,
        violation_block_minutes: int = 30,
    ):
        self.wordlist_path = Path(wordlist_path)
        self.enable_input_filter = enable_input_filter
        self.enable_output_filter = enable_output_filter
        self.enable_topic_guard = enable_topic_guard
        self.max_violations = max_violations
        self.violation_block_seconds = violation_block_minutes * 60

        self._patterns: list[re.Pattern] = []
        self._violation_count: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._load_wordlist()

    def _load_wordlist(self):
        """从文件加载敏感词正则（一行一个 pattern，以 # 开头为注释）"""
        if not self.wordlist_path.exists():
            return
        try:
            for line in self.wordlist_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        self._patterns.append(re.compile(line, re.IGNORECASE))
                    except re.error:
                        pass
        except Exception:
            pass

    def check_input(self, text: str) -> bool:
        """检查输入是否安全：True=通过, False=拦截"""
        if not self.enable_input_filter:
            return True
        for p in self._patterns:
            if p.search(text):
                return False
        return True

    def check_output(self, text: str, session_id: str) -> tuple[bool, Optional[str]]:
        """
        检查输出是否安全。
        返回 (是否通过, 替换文本或 None 表示静默不发)
        """
        if not self.enable_output_filter:
            return True, text

        # 检查是否在熔断中
        if self.is_session_blocked(session_id):
            return False, None

        for p in self._patterns:
            if p.search(text):
                self._record_violation(session_id)
                return False, None

        # 通过，重置违规计数
        self._violation_count.pop(session_id, None)
        return True, text

    def _record_violation(self, session_id: str):
        """记录一次违规"""
        self._violation_count[session_id] = self._violation_count.get(session_id, 0) + 1
        if self._violation_count[session_id] >= self.max_violations:
            self._blocked_until[session_id] = time.monotonic() + self.violation_block_seconds

    def is_session_blocked(self, session_id: str) -> bool:
        """检查会话是否处于熔断状态"""
        if session_id in self._blocked_until:
            if time.monotonic() < self._blocked_until[session_id]:
                return True
            # 熔断已过期
            del self._blocked_until[session_id]
            self._violation_count.pop(session_id, None)
        return False

    def is_allowed_topic(self, text: str) -> bool:
        """白名单话题守卫：检查是否在边狱巴士相关话题范围内"""
        if not self.enable_topic_guard:
            return True
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in ALLOWED_TOPIC_KEYWORDS)

    def safe_fallback_reply(self) -> str:
        """安全兜底回复（仅极端情况下使用）"""
        return "（抱歉，这个话题我不方便讨论。换个问题吧？）"

    @property
    def pattern_count(self) -> int:
        """已加载的过滤规则数量"""
        return len(self._patterns)
