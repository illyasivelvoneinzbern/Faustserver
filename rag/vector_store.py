"""
向量数据库管理模块：ChromaDB 封装，支持新建、重建和增量追加。
"""

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


def _retry_on_rate_limit(func, max_retries: int = 5, base_delay: float = 30.0):
    """执行 func，并在遭遇 API 限流（429 TPM limit）时按指数退避重试。

    langchain_openai 在 SiliconFlow 云端 embedding 被限流时会抛出 openai.RateLimitError，
    其 status_code 为 429。重试整个批次是安全的：embedding 在写入 Chroma 之前调用，
    失败时不会产生部分写入。
    """
    import time

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:  # noqa: BLE001 - 需要捕获任意限流异常
            status = getattr(e, "status_code", None)
            is_rate_limit = (
                status == 429
                or "429" in str(getattr(e, "message", ""))
                or "RateLimit" in type(e).__name__
                or "rate limit" in str(e).lower()
            )
            if not is_rate_limit or attempt >= max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                f"Embedding 请求触发限流(429)，{delay:.0f}s 后重试 "
                f"({attempt + 1}/{max_retries}) ..."
            )
            time.sleep(delay)
    raise RuntimeError("_retry_on_rate_limit 不应到达此处")


def delete_collection(
    persist_directory: str = "./data/vector_db",
    collection_name: str = "limbus_wiki",
) -> bool:
    """显式删除指定 ChromaDB collection。

    删除策略（按优先级）：
    1. 通过 ChromaDB HTTP 客户端删除（适用于 client/server 模式）
    2. 直接删除持久化目录中的 collection 子目录（适用于本地模式）

    Returns:
        True 表示成功删除或本来就不存在
    """
    import chromadb

    collection_path = Path(persist_directory)
    collection_dir = collection_path / collection_name

    deleted = False

    # 方法 1: 通过 ChromaDB PersistentClient 删除
    if collection_path.exists():
        try:
            client = chromadb.PersistentClient(path=str(collection_path))
            try:
                client.delete_collection(collection_name)
                logger.info(f"通过 PersistentClient 删除 collection: {collection_name}")
                deleted = True
            except (ValueError, Exception):
                logger.debug(f"PersistentClient 未找到 collection '{collection_name}'，尝试目录删除")
        except Exception as e:
            logger.debug(f"PersistentClient 连接失败: {e}，尝试目录删除")

    # 方法 2: 直接删除 collection 子目录（兜底）
    if not deleted and collection_dir.exists():
        shutil.rmtree(collection_dir, ignore_errors=True)
        logger.info(f"通过目录删除 collection: {collection_dir}")
        deleted = True

    # 同时清理 Chroma 的 SQLite 索引中相关行（如果 sqlite3 文件存在）
    sqlite_path = collection_path / "chroma.sqlite3"
    if sqlite_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(sqlite_path))
            conn.execute("DELETE FROM collections WHERE name = ?", (collection_name,))
            conn.commit()
            conn.close()
            logger.debug(f"已清理 chroma.sqlite3 中 '{collection_name}' 的索引记录")
        except Exception as e:
            logger.debug(f"清理 sqlite3 索引失败（可能无害）: {e}")

    if deleted:
        logger.info(f"Collection '{collection_name}' 已删除，准备重建")
    else:
        logger.info(f"Collection '{collection_name}' 不存在，无需删除")

    return True


def create_vector_store(
    embedder: Any,
    persist_directory: str = "./data/vector_db",
    collection_name: str = "limbus_wiki",
) -> Chroma:
    """创建或加载 ChromaDB 向量数据库"""
    path = Path(persist_directory)
    path.mkdir(parents=True, exist_ok=True)

    return Chroma(
        embedding_function=embedder,
        persist_directory=str(path),
        collection_name=collection_name,
    )


def build_from_documents(
    documents: list[Document],
    embedder: Any,
    persist_directory: str = "./data/vector_db",
    collection_name: str = "limbus_wiki",
    batch_size: int = 200,
    progress_callback=None,
    batch_delay: float = 1.0,
    force_rebuild: bool = False,
    build_bm25: bool = True,
) -> tuple[Chroma, Optional[Any]]:
    """
    从文档列表构建向量数据库，并可选构建 BM25 索引。

    Args:
        progress_callback: 可选，签名 callback(current, total)，每批次调用一次
        batch_delay: 批次间延迟（秒），避免触发 API 限流
        force_rebuild: 如果为 True，先删除已有 collection 再重建（确保完全覆盖）
        build_bm25: 是否同时构建 BM25 关键词索引

    Returns:
        (vector_store, bm25_index) — bm25_index 在 build_bm25=False 或失败时为 None
    """
    import time

    if force_rebuild:
        delete_collection(persist_directory, collection_name)

    total = len(documents)
    logger.info(f"开始构建向量数据库: {total} 个文档块")

    # ── 构建向量库 ──
    if progress_callback and total > batch_size:
        vector_store = None
        for i in range(0, total, batch_size):
            batch = documents[i:i + batch_size]
            if vector_store is None:
                vector_store = _retry_on_rate_limit(
                    lambda: Chroma.from_documents(
                        documents=batch,
                        embedding=embedder,
                        persist_directory=persist_directory,
                        collection_name=collection_name,
                    )
                )
            else:
                _retry_on_rate_limit(
                    lambda: vector_store.add_documents(batch)
                )
            progress_callback(min(i + batch_size, total), total)
            if i + batch_size < total:
                time.sleep(batch_delay)
    else:
        vector_store = _retry_on_rate_limit(
            lambda: Chroma.from_documents(
                documents=documents,
                embedding=embedder,
                persist_directory=persist_directory,
                collection_name=collection_name,
            )
        )
        if progress_callback:
            progress_callback(total, total)

    logger.info(f"向量数据库构建完成: {persist_directory}/{collection_name}")

    # ── 构建 BM25 索引 ──
    bm25_index = None
    if build_bm25:
        try:
            from rag.bm25_index import BM25Index
            bm25_index = BM25Index(documents)
            logger.info(f"BM25 索引构建完成: {bm25_index.document_count} 个文档")
        except ImportError as e:
            logger.warning(f"BM25 依赖未安装，跳过 BM25 索引构建: {e}")
        except Exception as e:
            logger.warning(f"BM25 索引构建失败，回退到纯向量模式: {e}")

    return vector_store, bm25_index


def add_documents(
    vector_store: Chroma,
    documents: list[Document],
    batch_size: int = 50,
) -> int:
    """增量追加文档到已有向量数据库"""
    if not documents:
        return 0

    for i in range(0, len(documents), batch_size):
        batch = documents[i:i + batch_size]
        vector_store.add_documents(batch)
        logger.debug(f"已写入 {min(i + batch_size, len(documents))}/{len(documents)} 个块")

    logger.info(f"增量追加完成: {len(documents)} 个文档块")
    return len(documents)


def get_or_create(
    embedder: Any,
    persist_directory: str = "./data/vector_db",
    collection_name: str = "limbus_wiki",
    documents: Optional[list[Document]] = None,
) -> Chroma:
    """
    获取已有向量数据库，不存在则从 documents 新建。

    这是最常用的入口函数。
    """
    path = Path(persist_directory) / collection_name
    if path.exists() and any(path.iterdir()):
        logger.info(f"加载已有向量数据库: {persist_directory}/{collection_name}")
        return create_vector_store(embedder, persist_directory, collection_name)

    if documents:
        logger.info("向量数据库不存在，从文档新建")
        vs, _ = build_from_documents(documents, embedder, persist_directory, collection_name)
        return vs

    logger.warning("向量数据库不存在且未提供文档，返回空数据库")
    return create_vector_store(embedder, persist_directory, collection_name)


def build_bm25_index(
    vector_store: Chroma,
) -> Optional[Any]:
    """从已有向量数据库加载全部文档并构建 BM25 关键词索引。

    Fix E：生产环境（agent/core.py）此前用 get_or_create() 只拿到 Chroma，
    BM25 索引从未接线，导致 config 的 hybrid_search.enabled=true 形同虚设、
    检索实际是纯向量模式。此函数从向量库元数据重建 Document 列表，
    供 LimBusRetriever(bm25_index=...) 使用。

    Args:
        vector_store: 已加载的 Chroma 向量库

    Returns:
        BM25Index 或 None（构建失败时回退纯向量模式）
    """
    try:
        from rag.bm25_index import BM25Index

        raw = vector_store._collection.get(include=["documents", "metadatas"])
        texts = raw.get("documents", [])
        metas = raw.get("metadatas", [])
        if not texts:
            logger.warning("向量库为空，无法构建 BM25 索引")
            return None

        docs = [
            Document(page_content=t, metadata=m)
            for t, m in zip(texts, metas)
        ]
        bm25 = BM25Index(docs)
        logger.info(
            f"BM25 索引从向量库构建完成: {bm25.document_count} 个文档"
        )
        return bm25
    except ImportError as e:
        logger.warning(f"BM25 依赖未安装，跳过 BM25 索引构建: {e}")
        return None
    except Exception as e:
        logger.warning(f"BM25 索引构建失败，回退到纯向量模式: {e}")
        return None
