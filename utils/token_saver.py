"""
Token 节省模块：上下文压缩 + 语义缓存 + 双模型路由。
"""

import time
import hashlib
from typing import Any


class ContextCompressor:
    """当对话历史超过 memory_window 时，将早期对话压缩为一段摘要。"""

    def __init__(self, keep_recent: int = 6):
        self.keep_recent = keep_recent

    def need_compress(self, messages: list, max_window: int) -> bool:
        """判断是否需要压缩"""
        return len(messages) > max_window

    def compress(self, llm, messages: list) -> str:
        """调用 LLM 生成摘要"""
        old_messages = messages[: -self.keep_recent]
        if len(old_messages) < 2:
            return ""

        summary_prompt = (
            "请用一句话总结以下对话的核心内容（仅总结内容，不要添加额外说明）：\n"
            f"{old_messages}"
        )
        try:
            return llm.invoke(summary_prompt)
        except Exception:
            return ""


class SemanticCache:
    """基于向量相似度的回答缓存，避免重复调用 LLM。
    支持两种查找模式：
    - hash_lookup：轻量字符集 Jaccard 相似度（无需 embedder，fallback）
    - lookup：embedding 余弦相似度（精度更高，需要 embedder 支持）
    """

    def __init__(self, threshold: float = 0.92, max_size: int = 200):
        self.threshold = threshold
        self.max_size = max_size
        # hash -> (question, answer, embedding, ts)
        self._cache: dict[str, tuple[str, str, list[float] | None, float]] = {}

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """基于字符集的快速文本相似度（Jaccard，无需 embedding）"""
        if a == b:
            return 1.0
        set_a = set(a)
        set_b = set(b)
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """计算两个 embedding 向量的余弦相似度。"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def hash_lookup(self, query: str) -> str | None:
        """基于字符集 Jaccard 相似度的缓存查找（轻量，无需 embedder）"""
        best_sim = 0.0
        best_answer = None

        for key, (cached_q, cached_a, cached_emb, ts) in self._cache.items():
            sim = self._text_similarity(query, cached_q)
            if sim > best_sim:
                best_sim = sim
                best_answer = cached_a

        return best_answer if best_sim >= self.threshold else None

    async def lookup(self, query: str, embedder: Any) -> str | None:
        """基于 embedding 余弦相似度的精确缓存查找。

        优先使用 embedding 比较，若 embedder 不可用或缓存中无 embedding
        则降级为 Jaccard 文本相似度。
        """
        if embedder is None:
            return self.hash_lookup(query)

        try:
            q_emb = await embedder.aembed_query(query)
        except Exception:
            return self.hash_lookup(query)

        if q_emb is None:
            return self.hash_lookup(query)

        best_sim = 0.0
        best_answer = None
        for key, (cached_q, cached_a, cached_emb, ts) in self._cache.items():
            if cached_emb is not None and len(cached_emb) > 0:
                # ── embedding 余弦相似度 ──
                sim = self._cosine_sim(q_emb, cached_emb)
            else:
                # ── 降级：文本 Jaccard 相似度 ──
                sim = self._text_similarity(query, cached_q)

            if sim > best_sim:
                best_sim = sim
                best_answer = cached_a
            if best_sim >= self.threshold:
                break

        return best_answer if best_sim >= self.threshold else None

    def store(self, query: str, answer: str, embedding: list[float] | None = None):
        """存入缓存（可选附带 embedding 向量以支持余弦相似度查找）"""
        key = hashlib.md5(query.encode()).hexdigest()
        self._cache[key] = (query, answer, embedding, time.time())

        # LRU 淘汰
        if len(self._cache) > self.max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][3])
            del self._cache[oldest_key]


class DualModelRouter:
    """寒暄/简单指令走轻量模型，复杂问题走主模型。"""

    GREETING_PATTERNS = [
        "你好", "在吗", "早", "晚安", "再见", "谢谢", "嗯",
        "哈哈", "好", "ok", "OK", "👋", "嗨",
    ]

    @classmethod
    def route(cls, message: str) -> str:
        """判断消息复杂度，返回 'cheap' 或 'main'。"""
        msg = message.strip()
        if len(msg) <= 10 and any(p in msg for p in cls.GREETING_PATTERNS):
            return "cheap"
        return "main"


def truncate_context(docs: list, max_chars: int = 600) -> str:
    """截断检索结果到指定字符数，按句子边界切割。"""
    result_parts = []
    total = 0
    for doc in docs:
        content = doc.page_content if hasattr(doc, "page_content") else str(doc)
        title = doc.metadata.get("page_title", "未知来源") if hasattr(doc, "metadata") else ""
        prefix = f"[{title}] "

        available = max_chars - total - len(prefix)
        if available <= 0:
            break

        if len(content) > available:
            # 按句子截断
            truncated = ""
            for ch in content:
                truncated += ch
                if len(prefix + truncated) >= available:
                    break
            content = truncated + "…"

        result_parts.append(prefix + content)
        total += len(prefix + content)

    return "\n\n".join(result_parts)
