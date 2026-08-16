"""
BM25 关键词索引模块：为混合检索提供精确关键词匹配能力。

使用 jieba 分词 + rank_bm25 实现，纯 Python 零 C 依赖。
与 ChromaDB 向量库配合使用，解决 BGE-M3 对中文短查询召回不足的问题。
"""

import logging
from typing import Any, Optional

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# 延迟导入，避免未安装依赖时模块加载失败
_BM25_AVAILABLE = False
_JIEBA_AVAILABLE = False


def _ensure_deps():
    """确保 rank_bm25 和 jieba 已安装。"""
    global _BM25_AVAILABLE, _JIEBA_AVAILABLE
    if not _BM25_AVAILABLE:
        try:
            import rank_bm25  # noqa: F401
            _BM25_AVAILABLE = True
        except ImportError:
            raise ImportError(
                "rank_bm25 未安装，请运行: pip install rank-bm25"
            )
    if not _JIEBA_AVAILABLE:
        try:
            import jieba  # noqa: F401
            _JIEBA_AVAILABLE = True
        except ImportError:
            raise ImportError(
                "jieba 未安装，请运行: pip install jieba"
            )


class BM25Index:
    """基于 BM25Okapi 的中文关键词索引。

    用法:
        index = BM25Index(documents)
        results = index.search("浮士德 身高", k=30)
        # → [(bm25_score, Document), ...]
    """

    def __init__(self, documents: Optional[list[Document]] = None):
        """
        Args:
            documents: LangChain Document 列表。传入后立即构建索引。
                       也可以先创建空索引，后续用 build() 构建。
        """
        self._bm25 = None
        self._docs: list[Document] = []
        self._tokenized: list[list[str]] = []

        if documents:
            self.build(documents)

    def build(self, documents: list[Document]):
        """从 Document 列表构建 BM25 索引。

        对每个文档的 page_content 做 jieba 分词后加入索引。
        """
        _ensure_deps()
        import jieba
        from rank_bm25 import BM25Okapi

        self._docs = list(documents)
        self._tokenized = [
            list(jieba.cut(doc.page_content))
            for doc in self._docs
        ]
        self._bm25 = BM25Okapi(self._tokenized)
        logger.info(
            f"BM25 索引构建完成: {len(self._docs)} 个文档, "
            f"平均 token 数: {sum(len(t) for t in self._tokenized) // max(len(self._tokenized), 1)}"
        )

    @property
    def is_built(self) -> bool:
        """索引是否已构建。"""
        return self._bm25 is not None

    @property
    def document_count(self) -> int:
        """索引中的文档数。"""
        return len(self._docs)

    def search(
        self,
        query: str,
        k: int = 30,
    ) -> list[tuple[float, Document]]:
        """检索与查询最相关的前 k 个文档。

        Args:
            query: 中文查询字符串
            k: 返回的文档数量

        Returns:
            [(bm25_score, Document), ...] 按分数降序排列
        """
        if not self.is_built:
            logger.warning("BM25 索引未构建，返回空结果")
            return []

        _ensure_deps()
        import jieba

        tokenized_query = list(jieba.cut(query))
        scores = self._bm25.get_scores(tokenized_query)

        # 按分数排序取 top-k
        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: x[1], reverse=True)
        top_k = indexed[:min(k, len(indexed))]

        return [(score, self._docs[idx]) for idx, score in top_k if score > 0]

    def search_all_scores(
        self,
        query: str,
        doc_ids: Optional[list[int]] = None,
    ) -> list[tuple[float, int]]:
        """返回所有文档（或指定文档）的 BM25 分数。

        Args:
            query: 中文查询字符串
            doc_ids: 可选，仅查询指定文档 ID 的分数

        Returns:
            [(score, doc_index), ...] 按分数降序排列
        """
        if not self.is_built:
            return []

        _ensure_deps()
        import jieba

        tokenized_query = list(jieba.cut(query))
        scores = self._bm25.get_scores(tokenized_query)

        if doc_ids is not None:
            result = [(scores[i], i) for i in doc_ids if i < len(scores)]
        else:
            result = list(enumerate(scores))

        result.sort(key=lambda x: x[0], reverse=True)
        return result


# ── 分数归一化工具 ──

def minmax_normalize(
    scored_items: list[tuple[float, Any]],
) -> list[tuple[float, Any]]:
    """将分数列表归一化到 [0, 1] 区间。

    如果所有分数相同，统一返回 0.5。
    """
    if not scored_items:
        return []

    scores = [s for s, _ in scored_items]
    min_s, max_s = min(scores), max(scores)

    if max_s == min_s:
        return [(0.5, doc) for _, doc in scored_items]

    return [
        ((s - min_s) / (max_s - min_s), doc)
        for s, doc in scored_items
    ]


def merge_and_rerank(
    vec_results: list[tuple[float, Any]],
    bm25_results: list[tuple[float, Any]],
    weight_vec: float = 0.4,
    weight_bm25: float = 0.6,
) -> list[tuple[float, Any]]:
    """合并向量检索和 BM25 检索结果，归一化后加权融合。

    使用 page_content 前 80 字符 + page_title 作为文档去重键。
    只在一种检索中出现的文档，缺失方分数视为 0。

    Args:
        vec_results: [(相似度, Document), ...]
        bm25_results: [(bm25分数, Document), ...]
        weight_vec: 向量分数权重
        weight_bm25: BM25 分数权重

    Returns:
        [(合并分数, Document), ...] 按分数降序
    """
    # 归一化各自分数
    vec_norm = minmax_normalize(vec_results)
    bm25_norm = minmax_normalize(bm25_results)

    # 构建文档键 → 分数的映射
    merged: dict[str, tuple[float, float, Any]] = {}

    for score, doc in vec_norm:
        key = _make_doc_key(doc)
        merged[key] = (score, 0.0, doc)

    for score, doc in bm25_norm:
        key = _make_doc_key(doc)
        if key in merged:
            prev_vec, _, _ = merged[key]
            merged[key] = (prev_vec, score, doc)
        else:
            merged[key] = (0.0, score, doc)

    # 加权融合
    final: list[tuple[float, Any]] = []
    for vec_s, bm25_s, doc in merged.values():
        combined = weight_vec * vec_s + weight_bm25 * bm25_s
        final.append((combined, doc))

    final.sort(key=lambda x: x[0], reverse=True)
    return final


def merge_by_rrf(
    vec_results: list[tuple[float, Any]],
    bm25_results: list[tuple[float, Any]],
    k: int = 60,
) -> list[tuple[float, Any]]:
    """Reciprocal Rank Fusion：基于排名而非分数的融合。

    对分数分布差异大的场景（如 BM25 余弦相似度与向量余弦度量的量纲不同）
    比加权融合更鲁棒。同一文档出现在两个列表中时分数累加。

    Args:
        vec_results: [(相似度, Document), ...] 已按分数降序
        bm25_results: [(bm25分数, Document), ...] 已按分数降序
        k: RRF 平滑常数，默认 60

    Returns:
        [(RRF分数, Document), ...] 按分数降序
    """
    # 按分数排序获取排名
    vec_ranked = sorted(vec_results, key=lambda x: x[0], reverse=True)
    bm25_ranked = sorted(bm25_results, key=lambda x: x[0], reverse=True)

    scores: dict[str, tuple[float, Any]] = {}

    for rank, (_, doc) in enumerate(vec_ranked):
        key = _make_doc_key(doc)
        scores[key] = (1.0 / (k + rank + 1), doc)

    for rank, (_, doc) in enumerate(bm25_ranked):
        key = _make_doc_key(doc)
        rrf = 1.0 / (k + rank + 1)
        if key in scores:
            scores[key] = (scores[key][0] + rrf, doc)
        else:
            scores[key] = (rrf, doc)

    return sorted(scores.values(), key=lambda x: x[0], reverse=True)


def _make_doc_key(doc: Any) -> str:
    """生成文档的唯一去重键。"""
    title = ""
    if hasattr(doc, "metadata") and doc.metadata:
        title = doc.metadata.get("page_title", "")
    content = doc.page_content[:80] if hasattr(doc, "page_content") else ""
    return f"{title}::{content}"
