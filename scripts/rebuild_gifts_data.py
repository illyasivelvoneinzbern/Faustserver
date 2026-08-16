# -*- coding: utf-8 -*-
"""P37：重新生成饰品数据（拉取 Data:Giftchoose.tabx → 修复后拆分 → 覆盖导出）。

仅刷饰品数据（tabx 数据页），不重爬 wiki 页面。
"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from crawler.spider import WikiSpider
    from crawler.export import save_jsonl
    from crawler.structured_exporter import export_gift_records

    print("正在拉取 Data:Giftchoose.tabx ...")
    async with WikiSpider(output_dir="./data/raw") as spider:
        accessories = await spider.fetch_cargo_accessories()

    if not accessories:
        print("✗ 饰品数据拉取失败")
        return

    print(f"✓ 拉到 {len(accessories)} 条饰品记录（修复后拆分）")

    # 覆盖 wiki_accessories.jsonl
    save_jsonl(accessories, "./data/raw/wiki_accessories.jsonl")

    # P37：先清理旧 gift 文件（增量导出不删除旧 upgraded 残留）
    gifts_dir = Path("data/structured/gifts")
    if gifts_dir.exists():
        removed = 0
        for f in gifts_dir.glob("gift_*.json"):
            f.unlink()
            removed += 1
        print(f"✓ 已清理旧饰品文件: {removed} 个")

    # 重新导出结构化 gift JSON
    n = export_gift_records(accessories)
    print(f"✓ 结构化饰品导出: {n} 条 -> data/structured/gifts/")


if __name__ == "__main__":
    asyncio.run(main())
