# -*- coding: utf-8 -*-
"""人格被动映射数据（Data:Personalitypassives.json）的抓取、清洗与索引。

背景
----
人格页 wikitext 的 ``{{人格被动链接|ID}}`` 模板仅含被动 ID（如 ``1021201``），
本地数据（data/structured/personas/*.json）因此只能保存 ``人格被动{ID}`` 占位符，
无名称/描述。渲染（``rag.persona_direct``）与分块（``crawler.chunk_builder``）
需要一张「被动 ID → 名称/描述」映射表才能组合展示。

本模块从灰机 Wiki 的官方数据页 ``Data:Personalitypassives.json`` 抓取该映射表
（与 ``Data:Giftchoose.tabx`` 同属 Tabx/JSON 数据页，可被 ``WikiSpider`` 定向抓取），
清洗 wikitext 后落盘为 ``data/structured/passives.json``，供运行时懒加载。

映射表字段（原始）：
    id     被动 ID（如 "1021201"）
    name   被动名（如 "天究星"）
    where  战斗 / 支援（用于渲染时分组）
    trigger 触发条件（共鸣/持有 等）
    aff1/num1 对应罪孽/数量
    level1/desc1 基础等级/基础描述
    level2/desc2 强化等级/强化描述（可能为空）

清洗规则与 ``spider._fetch_tabx_gifts`` 的 ``_clean_wikitext`` 保持一致：
``{{状态2|NAME|4=负面}}`` → NAME、``{{...}}`` 简单模板移除、``[[链接|显示]]`` → 显示、
``<br>`` → 换行。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 官方被动映射数据页（灰机 Wiki 的 JSON 数据页）
PASSIVES_DATA_PAGE = "Data:Personalitypassives.json"
# 落盘文件名（与 data/structured/personas、gifts、events 同目录）
PASSIVES_INDEX_FILE = "passives.json"
DEFAULT_INDEX_DIR = "data/structured"


def clean_wikitext(text: str) -> str:
    """将 wikitext 模板/链接清洗为纯文本。

    - ``{{状态2|NAME|4=负面}}`` → NAME
    - 其它 ``{{...}}`` 简单模板 → 移除
    - ``[[链接|显示]]`` → 显示；``[[链接]]`` → 链接
    - ``<br>`` → 换行；压缩多余空行
    """
    if not text:
        return ""
    # {{状态2|NAME|4=负面}} → NAME
    text = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', text)
    # {{名词|...}} → 移除
    text = re.sub(r'\{\{名词\|[^}]*\}\}', '', text)
    # 其他简单模板 → 移除
    text = re.sub(r'\{\{[^{}|]+\}\}', '', text)
    # [[链接|显示]] → 显示, [[链接]] → 链接
    text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    # <br> → 换行
    text = re.sub(r'<br\s*/?>', '\n', text)
    # 压缩多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _norm(value) -> str:
    """None / "None" 字符串 → ""，其余去空白。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("none", "null") else s


def parse_passives_json(raw: str) -> list[dict]:
    """解析 ``Data:Personalitypassives.json`` 原始内容 → 清洗后的被动记录列表。

    返回记录字段：
        id      被动 ID
        name    被动名（清洗后）
        where   战斗 / 支援
        trigger 触发条件
        aff     主罪孽（aff1）
        num     主罪孽数量（num1）
        level1/desc1  基础等级/描述（描述已清洗）
        level2/desc2  强化等级/描述（可能为空；desc2 已清洗）
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Data:Personalitypassives.json JSON 解析失败: {e}")
        return []

    rows = data.get("dataList") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        logger.warning("Data:Personalitypassives.json 缺少 dataList 列表")
        return []

    records = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pid = _norm(r.get("id"))
        name = clean_wikitext(_norm(r.get("name")))
        if not pid or not name:
            continue
        records.append({
            "id": pid,
            "name": name,
            "where": _norm(r.get("where")),
            "trigger": _norm(r.get("trigger")),
            "aff": _norm(r.get("aff1")),
            "num": _norm(r.get("num1")),
            "level1": _norm(r.get("level1")),
            "desc1": clean_wikitext(_norm(r.get("desc1"))),
            "level2": _norm(r.get("level2")),
            "desc2": clean_wikitext(_norm(r.get("desc2"))),
        })
    logger.info(f"Data:Personalitypassives.json 解析完成: {len(records)} 条被动记录")
    return records


def _index_records(records: list[dict]) -> dict[str, dict]:
    """记录列表 → {id: info} 索引（保序，后写覆盖）。"""
    index: dict[str, dict] = {}
    for r in records:
        index[r["id"]] = r
    return index


async def fetch_passives(spider) -> dict[str, dict]:
    """用 WikiSpider 抓取被动映射，返回 {id: info} 索引。

    ``spider`` 需提供 ``fetch_page_raw(title)`` 异步方法（即 ``WikiSpider`` 实例）。
    失败时返回空 dict（不抛异常，保证不影响主流程）。
    """
    raw = await spider.fetch_page_raw(PASSIVES_DATA_PAGE)
    if not raw:
        logger.error("Data:Personalitypassives.json 获取失败")
        return {}
    records = parse_passives_json(raw)
    return _index_records(records)


def save_passives_index(passives: dict[str, dict],
                        out_dir: str = DEFAULT_INDEX_DIR) -> Optional[Path]:
    """将被动映射索引写为 ``<out_dir>/passives.json``（原子替换）。"""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    target = out_path / PASSIVES_INDEX_FILE
    payload = {
        "_meta": {
            "source": PASSIVES_DATA_PAGE,
            "count": len(passives),
        },
        "passives": passives,
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(target)
    except OSError as e:
        logger.error(f"被动映射写盘失败 {target}: {e}")
        return None
    logger.info(f"已保存 {len(passives)} 条人格被动映射 -> {target}")
    return target


# ── 运行时懒加载缓存 ──
_INDEX_CACHE: Optional[dict[str, dict]] = None


def load_passives_index(out_dir: str = DEFAULT_INDEX_DIR,
                        use_cache: bool = True) -> dict[str, dict]:
    """懒加载 ``{id: info}`` 映射；文件缺失/损坏返回空 dict。

    ``use_cache=True`` 时同一进程只读一次文件。
    """
    global _INDEX_CACHE
    if use_cache and _INDEX_CACHE is not None:
        return _INDEX_CACHE

    path = Path(out_dir) / PASSIVES_INDEX_FILE
    index: dict[str, dict] = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            raw = payload.get("passives") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                index = raw
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"被动映射文件损坏，跳过: {path}: {e}")

    if use_cache:
        _INDEX_CACHE = index
    return index


def reload_passives_index():
    """清除懒加载缓存（测试/数据更新后调用）。"""
    global _INDEX_CACHE
    _INDEX_CACHE = None
