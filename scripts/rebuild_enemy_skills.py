# -*- coding: utf-8 -*-
"""P38：定向重爬敌方页面（page_type == 'enemy'），用修复后的技能提取重建数据。

背景：DOM 侧 coin_power 正则命中 base64 数字（854/987/9703）、coin_count 误计
不可摧毁的硬币/硬币.png 图标、attack_weight 因渲染文本无冒号恒为 1、attack_level
误用模板"等级"字段——约 23% 敌方技能数值劣化。修复后以 wikitext {{敌方技能}}
模板为权威（修正值=攻击等级 / 变动值=硬币威力 / 硬币数 / 攻击容量 / 基础值），
且 h4 敌人标题（====敌人名====，如经验采光/主线战斗1-10）也能正确归属 wikitext
技能，需要重爬敌方页面才能让现有数据生效（revid 增量无法感知代码变更）。

用法：
  python scripts/rebuild_enemy_skills.py                    # 重爬全部敌方页面
  python scripts/rebuild_enemy_skills.py --pages-file x.txt # 仅重爬列表中的页面

仅重爬敌方页面（不碰人格/EGO/事件/剧情），完成后重建 data/structured/enemies。
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rebuild_enemy_skills")

JSONL = "data/raw/wiki_pages.jsonl"


def load_enemy_titles(only: set[str] | None = None) -> list[str]:
    """从 wiki_pages.jsonl 收集敌方页面标题（按出现顺序去重）；only 非空时过滤。"""
    titles: list[str] = []
    seen: set[str] = set()
    for line in open(JSONL, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("page_type") != "enemy":
            continue
        t = (r.get("title") or "").strip()
        if not t or t in seen:
            continue
        if only is not None and t not in only:
            continue
        seen.add(t)
        titles.append(t)
    return titles


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-file", default=None,
                        help="每行一个页面标题的文本文件；缺省重爬全部敌方页面")
    args = parser.parse_args()

    only: set[str] | None = None
    if args.pages_file:
        p = Path(args.pages_file)
        only = {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
        logger.info(f"定向重爬模式：{len(only)} 个指定页面")

    from crawler.spider import WikiSpider

    titles = load_enemy_titles(only)
    logger.info(f"待重爬敌方页面: {len(titles)} 个")

    # 加载现有记录用于失败时保留旧数据
    existing: dict[str, dict] = {}
    for line in open(JSONL, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        existing[r.get("title", "")] = r

    updated: dict[str, dict] = {}
    failed: list[str] = []
    start = time.time()

    async with WikiSpider(output_dir="./data/raw", delay=1.0) as spider:
        for i, title in enumerate(titles, 1):
            try:
                page = await spider.crawl_page(title)
                if page:
                    updated[title] = page
                else:
                    failed.append(f"{title}（返回空）")
            except Exception as e:
                failed.append(f"{title}（{e}）")
            if i % 25 == 0 or i == len(titles):
                elapsed = time.time() - start
                speed = i / elapsed * 60 if elapsed > 0 else 0
                eta = (len(titles) - i) / speed * 60 if speed > 0 else 0
                logger.info(
                    f"[{i}/{len(titles)}] 已更新 {len(updated)} 失败 {len(failed)} "
                    f"速度 {speed:.1f}页/分 剩余约 {eta:.0f} 分钟"
                )

    logger.info(f"重爬完成：更新 {len(updated)} 页，失败 {len(failed)} 页")
    for f in failed[:20]:
        logger.warning(f"  失败: {f}")

    # 回写 wiki_pages.jsonl（敌方记录用新数据替换，其余保留）
    out_path = Path(JSONL)
    tmp = out_path.with_suffix(".jsonl.tmp")
    written = 0
    replaced = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for line in open(JSONL, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                f.write(line + "\n")
                continue
            t = r.get("title", "")
            if t in updated:
                f.write(json.dumps(updated[t], ensure_ascii=False) + "\n")
                replaced += 1
            else:
                f.write(line + "\n")
                written += 1
    tmp.replace(out_path)
    logger.info(f"jsonl 回写完成：替换 {replaced} 条敌方记录，保留 {written} 条其他记录")

    # 重建敌方结构化 JSON
    from crawler.structured_exporter import rebuild_enemies
    n = rebuild_enemies(JSONL)
    logger.info(f"敌方结构化数据重建完成：{n} 个单位")


if __name__ == "__main__":
    asyncio.run(main())
