"""
LLM Reranker：使用 LLM 对检索结果做语义相关性重排序。

集成位置：在 retriever.retrieve() 之后、format_context() 之前调用，
过滤无关文档以提升 Precision（预期 +20~35%）。
"""
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class LLMReranker:
    """使用 LLM 对检索结果做语义相关性评分重排序。

    仅对候选中的前 top_n * candidate_multiplier 条做评分，
    控制 Token 开销。评分为 0~10 的整数。
    """

    RERANK_PROMPT = (
        "判断以下文档片段与用户问题的相关性（0=完全无关, 10=高度相关）。"
        "只输出数字，每行一个，对应下方文档顺序。\n"
        "\n"
        "用户问题：{question}\n"
        "\n"
        "文档片段：\n"
        "{documents}\n"
        "\n"
        "相关性评分（每行一个数字）："
    )

    def __init__(
        self,
        llm: Any = None,
        top_n: int = 6,
        candidate_multiplier: int = 2,
        enabled: bool = True,
    ):
        self.llm = llm
        self.top_n = top_n
        self.candidate_multiplier = candidate_multiplier
        self.enabled = enabled

    async def rerank(
        self, question: str, docs: list[Any]
    ) -> list[Any]:
        """对检索结果做 LLM 相关性评分，过滤无关文档。

        Args:
            question: 用户原始问题
            docs: 检索结果列表（按初排分数降序）

        Returns:
            重排序后的文档列表（最多 top_n 条）
        """
        if not self.enabled or self.llm is None:
            return docs[:self.top_n]

        candidates = docs[:self.top_n * self.candidate_multiplier]
        if len(candidates) <= self.top_n:
            return candidates

        scored: list[tuple[int, Any]] = []
        batch_size = 3  # 每批最多3条，控制 Token

        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            docs_text = "\n---\n".join(
                f"[{j}] {d.page_content[:200] if hasattr(d, 'page_content') else str(d)[:200]}"
                for j, d in enumerate(batch, start=i)
            )

            try:
                response = await self.llm.ainvoke(
                    self.RERANK_PROMPT.format(
                        question=question, documents=docs_text
                    )
                )
                text = response.content if hasattr(response, "content") else str(response)
                scores = self._parse_scores(text, len(batch))
                for doc, score in zip(batch, scores):
                    scored.append((score, doc))
            except Exception as e:
                logger.warning(f"LLM Reranker 评分失败，降级为中等分: {e}")
                for doc in batch:
                    scored.append((5, doc))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 日志记录评分分布
        if scored:
            avg_score = sum(s[0] for s in scored) / len(scored)
            logger.debug(
                f"LLM Reranker: {len(docs)} 条 → {self.top_n} 条 "
                f"(avg_score={avg_score:.1f}, range=[{scored[-1][0]}, {scored[0][0]}])"
            )

        return [doc for _, doc in scored[:self.top_n]]

    def rerank_sync(
        self, question: str, docs: list[Any]
    ) -> list[Any]:
        """同步版本（用于不支持 async 的场景）"""
        if not self.enabled or self.llm is None:
            return docs[:self.top_n]

        candidates = docs[:self.top_n * self.candidate_multiplier]
        if len(candidates) <= self.top_n:
            return candidates

        scored: list[tuple[int, Any]] = []
        batch_size = 3

        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            docs_text = "\n---\n".join(
                f"[{j}] {d.page_content[:200] if hasattr(d, 'page_content') else str(d)[:200]}"
                for j, d in enumerate(batch, start=i)
            )

            try:
                response = self.llm.invoke(
                    self.RERANK_PROMPT.format(
                        question=question, documents=docs_text
                    )
                )
                text = response.content if hasattr(response, "content") else str(response)
                scores = self._parse_scores(text, len(batch))
                for doc, score in zip(batch, scores):
                    scored.append((score, doc))
            except Exception as e:
                logger.warning(f"LLM Reranker 评分失败（同步），降级为中等分: {e}")
                for doc in batch:
                    scored.append((5, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:self.top_n]]

    @staticmethod
    def _parse_scores(text: str, expected: int) -> list[int]:
        """解析 LLM 输出的评分数值列表。

        容错策略：
        - 匹配所有连续数字
        - 限制范围为 0~10
        - 数量不足时用 5（中等分）补齐
        """
        nums = re.findall(r'\b(\d+)\b', text)
        scores = [min(10, max(0, int(n))) for n in nums[:expected]]
        while len(scores) < expected:
            scores.append(5)
        return scores
