"""
边狱巴士 RAG Agent 主入口
=======================
启动：python main.py
首次运行前：
  1. 复制 .env.example 为 .env 并填入 API Key
  2. 在 personas/ 目录下创建至少一个人格 .yaml 文件
  3. （可选）运行 crawler 抓取 Wiki 数据 → 构建向量数据库
"""

import asyncio
import sys
import signal
import logging

from agent.core import LimbusAgent
from utils.logger import setup_logger
from utils.config import get_config


async def run_data_pipeline(config: dict, limit: int = 0, full_crawl: bool = False, skip_crawl: bool = False):
    """可选：运行数据管道（爬取 Wiki + 构建向量库）

    Args:
        config: 配置字典
        limit: 限制爬取页面数，0 表示不限制
        full_crawl: True=全量重抓，False=增量模式（默认）
        skip_crawl: True=跳过抓取，直接从已有 JSONL 分块+向量化
    """
    print("=" * 50)
    if skip_crawl:
        print(f"  数据管道: 跳过抓取 → 分块 → 向量入库")
    else:
        mode_str = "全量" if full_crawl else "增量"
        print(f"  数据管道（{mode_str}）: Wiki 爬取 → 分块 → 向量入库")
    print("=" * 50)

    if skip_crawl:
        # 跳过抓取，直接从已有 JSONL 加载
        print(f"\n[1/2] 跳过抓取，使用已有数据...")
        from crawler.export import load_jsonl
        wiki_path = "./data/raw/wiki_pages.jsonl"
        results = load_jsonl(wiki_path)
        if not results:
            print(f"错误：{wiki_path} 不存在或为空，请先运行抓取。")
            return
        # 同时加载 Cargo 饰品数据
        acc_path = "./data/raw/wiki_accessories.jsonl"
        accessories = load_jsonl(acc_path)
        results.extend(accessories)
        print(f"  已加载 {len(results)} 条已有数据（含 {len(accessories)} 条饰品）")
    else:
        # Step 1: 爬取 Wiki
        limit_msg = f"（限制 {limit} 页）" if limit > 0 else "（不限制）"
        print(f"\n[1/3] 爬取边狱巴士 Wiki... {limit_msg}")

        from crawler.spider import crawl_wiki
        results = await crawl_wiki(output_dir="./data/raw", limit=limit, full_crawl=full_crawl)
        if not results:
            print("警告：未爬取到任何数据。请检查网络连接。")
            return

    # Step 2: 合并数据（skip 模式是 1/2）
    step2_label = "2/2" if skip_crawl else "2/3"
    print(f"\n[{step2_label}] 数据处理: {len(results)} 条 Wiki 记录")
    from crawler.export import merge_data
    all_data = merge_data()
    print(f"  合并完成：共 {len(all_data)} 条记录")

    # Step 3: 分块 + 向量入库（skip 模式是 2/2）
    step3_label = "2/2" if skip_crawl else "3/3"
    print(f"\n[{step3_label}] 文本分块 & 向量入库...")
    from rag.chunker import chunk_documents, create_splitter
    from rag.embedder import create_embedder
    from rag.vector_store import build_from_documents

    chunk_cfg = config.get("chunking", {})
    splitter = create_splitter(
        chunk_size=chunk_cfg.get("chunk_size", 512),
        chunk_overlap=chunk_cfg.get("chunk_overlap", 64),
    )

    docs = chunk_documents(all_data, splitter)
    print(f"  分块完成: {len(all_data)} 条原始数据 → {len(docs)} 个块")

    print("  正在加载 Embedding 模型...")
    embedder = create_embedder(config["embedding"])
    vs_cfg = config["vector_store"]

    # 带进度回调的向量入库
    import time as _time
    t0 = _time.time()

    def chunk_progress(current, total):
        """每 500 个 chunk 或完成时报告一次进度（API 模式批次大，不必太频繁）"""
        if current % 500 == 0 or current == total:
            elapsed = _time.time() - t0
            speed = current / elapsed if elapsed > 0 else 0
            pct = current * 100 // total if total > 0 else 0
            print(f"  向量化: [{current}/{total}] {pct}% | 速度: {speed:.1f} chunk/秒")

    _, bm25_index = build_from_documents(
        documents=docs,
        embedder=embedder,
        persist_directory=vs_cfg["persist_directory"],
        collection_name=vs_cfg["collection_name"],
        progress_callback=chunk_progress if len(docs) > 100 else None,
        force_rebuild=True,
    )

    elapsed = _time.time() - t0
    print(f"  向量化完成！耗时 {elapsed:.1f} 秒")

    if bm25_index is not None:
        print(f"  BM25 索引已构建: {bm25_index.document_count} 个文档")
    else:
        print("  BM25 索引未构建（跳过或失败）")

    print("\n数据管道完成！向量数据库已构建。")
    print(f"  位置: {vs_cfg['persist_directory']}/{vs_cfg['collection_name']}")


async def main():
    """主函数"""
    # 加载配置
    try:
        config = get_config()
    except FileNotFoundError:
        print("错误：config.yaml 未找到。请确保在项目根目录运行。")
        sys.exit(1)
    except Exception as e:
        print(f"错误：配置加载失败 - {e}")
        sys.exit(1)

    # 设置日志
    log_cfg = config.get("logging", {})
    logger = setup_logger(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file", "logs/agent.log"),
    )

    # 命令行参数解析
    # 支持: python main.py --full-crawl [--limit 10]     → 全量抓取 + 向量化
    #        python main.py --skip-crawl                   → 跳过抓取，仅向量化
    #        python main.py                                → 启动 Agent
    #        python main.py crawl [--crawl-mode full] ...  → 旧格式兼容
    do_crawl = False
    skip_crawl = False
    full_crawl = False
    limit = 0

    args = sys.argv[1:]
    # 跳过可选的 "crawl" 子命令（兼容旧格式）
    if args and args[0] == "crawl":
        do_crawl = True
        args = args[1:]

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--full-crawl":
            do_crawl = True
            full_crawl = True
        elif arg == "--skip-crawl":
            skip_crawl = True
        elif arg == "--crawl-mode" and i + 1 < len(args):
            i += 1
            full_crawl = (args[i] == "full")
            skip_crawl = False  # --crawl-mode 时默认不 skip
            do_crawl = True
        elif arg == "--limit" and i + 1 < len(args):
            i += 1
            try:
                limit = int(args[i])
                do_crawl = True
            except ValueError:
                logger.warning(f"--limit 参数值无效: {args[i]}，将不限制")
        elif arg in ("crawl",):
            do_crawl = True
        i += 1

    # 如果指定了任何数据管道相关参数，进入数据管线模式
    if do_crawl or skip_crawl:
        if skip_crawl:
            logger.info("启动数据管道（跳过抓取）...")
        else:
            mode_str = "全量" if full_crawl else "增量"
            limit_msg = f"，限制 {limit} 页" if limit > 0 else ""
            logger.info(f"启动数据管道（{mode_str}{limit_msg}）...")
        try:
            await run_data_pipeline(config, limit=limit, full_crawl=full_crawl, skip_crawl=skip_crawl)
        except Exception as e:
            logger.error(f"数据管道异常: {e}")
        return

    # ── 正常启动 Agent ──
    logger.info("=" * 50)
    logger.info("  边狱巴士 RAG Agent")
    logger.info("=" * 50)

    agent = LimbusAgent()

    # 优雅退出
    def signal_handler():
        logger.info("收到退出信号")
        asyncio.create_task(agent.shutdown())

    # 注册信号处理器（仅 Unix 平台有效，Windows 静默跳过）
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass

    try:
        await agent.start()
    except KeyboardInterrupt:
        logger.info("用户中断")
    except asyncio.CancelledError:
        logger.info("任务被取消")
    except Exception as e:
        logger.error(f"Agent 异常退出: {e}")
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    # 使用 Windows 默认 ProactorEventLoop（支持子进程，Playwright 需要）
    # 注意：不要设为 SelectorEventLoop，它不支持 create_subprocess_exec
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
