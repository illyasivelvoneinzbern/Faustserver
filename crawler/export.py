"""
数据导出模块：将爬取结果转换为可入库的 JSONL 格式。
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def load_jsonl(filepath: str) -> list[dict]:
    """加载 JSONL 文件"""
    path = Path(filepath)
    results = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    logger.info(f"从 {filepath} 加载了 {len(results)} 条记录")
    return results


def save_jsonl(data: list[dict], filepath: str):
    """保存为 JSONL"""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logger.info(f"已保存 {len(data)} 条记录到 {filepath}")


def merge_data(
    wiki_file: str = "./data/raw/wiki_pages.jsonl",
    x_file: str = "./data/raw/x_posts.jsonl",
    accessories_file: str = "./data/raw/wiki_accessories.jsonl",
    output_file: str = "./data/processed/all_data.jsonl",
) -> list[dict]:
    """合并 Wiki、Tabx 饰品和 X/Twitter 数据"""
    all_data = []
    if Path(wiki_file).exists():
        all_data.extend(load_jsonl(wiki_file))
    if Path(accessories_file).exists():
        all_data.extend(load_jsonl(accessories_file))
    if Path(x_file).exists():
        all_data.extend(load_jsonl(x_file))
    if all_data:
        save_jsonl(all_data, output_file)
    return all_data
