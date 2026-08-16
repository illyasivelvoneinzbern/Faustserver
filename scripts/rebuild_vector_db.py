"""
向量库重生脚本：对已有 JSONL 数据重新清洗 → 分块 → 建库。

用法:
  python scripts/rebuild_vector_db.py              # 从 data/raw/wiki_pages.jsonl 重生（SiliconFlow 云端 BGE-M3）
  python scripts/rebuild_vector_db.py --local-embed  # 使用本地 BGE-M3（CPU，约 2GB 内存）
  python scripts/rebuild_vector_db.py --input data/processed/all_data.jsonl  # 从合并数据重生
  python scripts/rebuild_vector_db.py --dry-run    # 仅检查，不实际写入

无需重新爬取 Wiki，仅重新应用更新后的 cleaner + chunker 规则。
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import load_config
from crawler.cleaner import clean_text, is_empty_after_clean
from rag.embedder import create_embedder, _create_local_embedder
from rag.chunker import chunk_from_jsonl
from rag.vector_store import build_from_documents

logger = logging.getLogger("rebuild_vdb")


def re_clean_jsonl(input_path: str, output_path: str) -> tuple[int, int]:
    """重新清洗 JSONL 中的每条记录。

    Returns:
        (保留数, 丢弃数)
    """
    input_file = Path(input_path)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    kept = 0
    discarded = 0

    with open(input_file, encoding="utf-8") as fin, open(output_file, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                discarded += 1
                continue

            # ── 结构化记录（_structured=True）原样透传保留 ──
            # 其数据在结构化字段中（content 为 None），不能按文本清洗/丢弃
            if record.get("_structured"):
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                kept += 1
                continue

            # 重新清洗内容
            old_content = record.get("content", "")
            new_content = clean_text(old_content)

            if is_empty_after_clean(new_content):
                logger.debug(f"清洗后为空，丢弃: {record.get('title', '?')}")
                discarded += 1
                continue

            record["content"] = new_content
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1

    return kept, discarded


def main():
    parser = argparse.ArgumentParser(description="重生向量数据库")
    parser.add_argument(
        "--input", default="data/raw/wiki_pages.jsonl",
        help="输入 JSONL 路径（默认: data/raw/wiki_pages.jsonl）"
    )
    parser.add_argument(
        "--output", default="data/raw/wiki_pages_cleaned.jsonl",
        help="重新清洗后的输出 JSONL（默认: data/raw/wiki_pages_cleaned.jsonl）"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="配置文件路径"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅检查差异，不实际写入向量库"
    )
    parser.add_argument(
        "--local-embed", action="store_true",
        help="使用本地 BGE-M3 模型做 embedding（CPU，约 2GB 内存），默认使用 SiliconFlow 云端 API"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)

    # ── Step 1: 重新清洗 ──
    logger.info(f"Step 1: 重新清洗 {args.input} ...")
    kept, discarded = re_clean_jsonl(args.input, args.output)
    logger.info(f"清洗完成: 保留 {kept} 条, 丢弃 {discarded} 条")

    if kept == 0:
        logger.error("清洗后无有效数据，终止")
        return

    # ── Step 2: 重新分块 ──
    chunk_config = config.get("chunking", {})
    logger.info(
        f"Step 2: 重新分块 (chunk_size={chunk_config.get('chunk_size', 512)}, "
        f"overlap={chunk_config.get('chunk_overlap', 64)}) ..."
    )
    # 注意：chunk_from_jsonl 内部会调用 chunk_documents，后者已经走新的 _is_noise_content 和最小长度检查
    documents = chunk_from_jsonl(
        args.output,
        chunk_size=chunk_config.get("chunk_size", 512),
        chunk_overlap=chunk_config.get("chunk_overlap", 64),
    )
    logger.info(f"分块完成: {len(documents)} 个文档块")

    if args.dry_run:
        logger.info("Dry-run 模式，不写入向量库。统计数据:")
        logger.info(f"  清洗后记录: {kept}")
        logger.info(f"  丢弃记录: {discarded}")
        logger.info(f"  文档块数: {len(documents)}")

        # 抽样展示几个块
        if documents:
            logger.info("\n--- 抽样展示前 5 个块 ---")
            for i, doc in enumerate(documents[:5]):
                title = doc.metadata.get("page_title", "?")
                preview = doc.page_content[:80].replace("\n", " ")
                logger.info(f"  [{i+1}] {title} | {preview}...")
        return

    # ── Step 3: 重建向量库 ──
    if args.local_embed:
        logger.info("Step 3: 使用本地 BGE-M3 重建向量数据库（先删除旧 collection）...")
        local_config = {
            "provider": "local",
            "model": "BAAI/bge-m3",
            "device": config.get("embedding", {}).get("device", "cpu"),
        }
        embedder = _create_local_embedder(local_config)
    else:
        logger.info("Step 3: 使用 SiliconFlow 云端 BGE-M3 重建向量数据库（先删除旧 collection）...")
        embedder = create_embedder(config["embedding"])

    vs_config = config["vector_store"]

    def progress_cb(current, total):
        pct = current * 100 // total if total else 100
        logger.info(f"  向量化进度: {current}/{total} ({pct}%)")

    vector_store, bm25_index = build_from_documents(
        documents=documents,
        embedder=embedder,
        persist_directory=vs_config["persist_directory"],
        collection_name=vs_config["collection_name"],
        # batch_size 从 200 降到 100：BGE-M3 单请求 token 数约为 100×400≈4 万，
        # 落在 SiliconFlow TPM 额度内（原 200×400≈8 万会超限触发 429）。
        # batch_delay 从 1.0 提到 2.0：拉长批次间隔，避免批次间 TPM 累积超限。
        # 即便仍触发限流，build_from_documents 已内置 429 指数退避重试兜底。
        batch_size=100,
        progress_callback=progress_cb,
        batch_delay=2.0,
        force_rebuild=True,
    )

    logger.info(
        f"✅ 向量库重生完成！"
        f"  {kept} 条记录 → {len(documents)} 个文档块 → {vs_config['persist_directory']}/{vs_config['collection_name']}"
        f"  BM25: {'已构建' if bm25_index is not None else '未构建'}"
    )


if __name__ == "__main__":
    main()
