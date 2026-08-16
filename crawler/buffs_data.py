# -*- coding: utf-8 -*-
"""BuffPro 代码 → 中文名 映射（静态兜底 + 页面级配对）。

背景
----
敌方技能 wikitext 的 ``{{BuffPro|Code}}`` 模板（如 ``{{BuffPro|SuperCoin}}``）仅含
buff 代码，本地数据（data/structured/enemies/*.json）原先只能硬编码识别
``SuperCoin`` 一个代码，其余代码（如 ``Sinking``/``Vibration``/``Bleed``）原样保留，
导致技能效果中 buff 名缺失（修复④）。

重要勘误（2026-08-15）：
- 此前依赖 ``Data:Buffchoose.tabx`` 抓取 code→中文名 映射（fetch_buffs /
  parse_buffs_tabx），但实测该数据页是「人格 → 拥有哪些 buff 类型」的布尔表
  （schema.fields = name/belong/origin/Combustion/Laceration/... 布尔列），
  **不是 BuffPro code → 中文名 映射**，解析结果无意义，buffs.json 从未正确生成。
  因此 crawl_wiki 不再调用 fetch_buffs（见 spider.py 注释），parse_buffs_tabx
  仅保留供历史兼容，不再作为翻译来源。
- 正确的中文名来源是**页面渲染 HTML**：站点 JS gadget 把 ``{{BuffPro|Code}}``
  渲染为 ``<span class="buffPro buff-pro-processed">`` + 中文名链接。
  通过 ``build_buff_code_map_from_html`` 将 HTML 中文名与 wikitext code 按
  顺序配对，得到页面级 code→中文名 映射，覆盖官方表/静态表缺失的专属 code。

本模块提供三层 BuffPro 代码解析：
    1. 页面级配对映射（build_buff_code_map_from_html，主路径）
    2. 静态兜底映射 DEFAULT_BUFF_CODES（覆盖常见 buff 代码）
    3. 兜底标签：无法解析时标注 ``未解析buff:Code``，避免静默丢失
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 官方 buff 映射数据页（灰机 Wiki 的 Tabx 数据页；若实际页面名不同，仅需改此处）
BUFFS_DATA_PAGE = "Data:Buffchoose.tabx"
# 落盘文件名（与 data/structured/passives.json 同目录）
BUFFS_INDEX_FILE = "buffs.json"
DEFAULT_INDEX_DIR = "data/structured"

# Tabx 中 buff 代码 / 名称列名候选（不同版本字段名可能不同）
CODE_FIELD_NAMES = ("code", "id", "buffcode", "key")
NAME_FIELD_NAMES = ("name", "buffname", "名称", "buff", "buffname_zh")


def clean_wikitext(text: str) -> str:
    """将 wikitext 模板/链接清洗为纯文本（与 passives_data.clean_wikitext 一致）。"""
    if not text:
        return ""
    text = re.sub(r'\{\{状态2\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', text)
    text = re.sub(r'\{\{名词\|[^}]*\}\}', '', text)
    text = re.sub(r'\{\{[^{}|]+\}\}', '', text)
    text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _norm(value) -> str:
    """None / "None" 字符串 → ""，其余去空白。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("none", "null") else s


# ── 静态兜底映射（常见 buff 代码；官方表抓取失败/缺失时仍可解析）──
# 中文名以 data/structured/effects.json 中实际存在的中文名为准（探针确认）。
DEFAULT_BUFF_CODES: dict[str, str] = {
    "SuperCoin": "不可摧毁的硬币",
    "Sinking": "沉沦",
    "Vibration": "震颤",
    "Bleed": "流血",
    "Burn": "烧伤",
    "Rupture": "破裂",
    "Paralysis": "麻痹",
    "Charge": "充能",
    "Breath": "呼吸法",
    "Vulnerable": "易损",
    "AttackLevelUp": "攻击等级提升",
    "DefenseLevelUp": "防御等级提升",
    "DefenseLevelDown": "防御等级降低",
    "Haste": "迅捷",
    "Bind": "束缚",
    "Strength": "强壮",
    "DamageBonus": "伤害强化",
    "DamageDown": "伤害弱化",
    "Protection": "守护",
    "Aggro": "挑衅值",
    "SanityDown": "恐惧",
    "ParryingResultUp": "拼点威力提升",
    "ParryingResultDown": "拼点威力降低",
    # P21-B 补齐：人格/EGO 技能 DOM 渲染后残留的纯英文 code（频率见 P21-B 探针）
    "Laceration": "撕裂",
    "Combustion": "燃烧",
    "Agility": "迅捷",
    "Burst": "爆发",
}


def parse_buffs_tabx(raw: str) -> list[dict]:
    """解析 ``Data:Buffchoose.tabx`` 原始内容 → 清洗后的 buff 记录列表。

    返回记录字段：
        code   buff 代码（如 "SuperCoin"）
        name   buff 中文名（清洗后，如 "超级硬币"）
        desc   描述（清洗后，可为空）
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Data:Buffchoose.tabx JSON 解析失败: {e}")
        return []

    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        logger.warning("Data:Buffchoose.tabx 缺少 data 列表")
        return []

    # 解析 schema 字段名（兼容 "schema.fields" 与 flat "fields"）
    schema = data.get("schema") if isinstance(data, dict) else None
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        fields = data.get("fields") if isinstance(data, dict) else None
    code_idx: Optional[int] = None
    name_idx: Optional[int] = None
    desc_idx: Optional[int] = None
    if isinstance(fields, list):
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                continue
            fname = _norm(f.get("name") or f.get("id") or f.get("key") or "")
            if fname in CODE_FIELD_NAMES and code_idx is None:
                code_idx = i
            elif fname in NAME_FIELD_NAMES and name_idx is None:
                name_idx = i
            elif fname in ("desc", "description", "效果", "说明") and desc_idx is None:
                desc_idx = i

    records = []
    for r in rows:
        if not isinstance(r, list):
            # 兼容 dict 行（无 schema 时）
            if isinstance(r, dict):
                code = ""
                name = ""
                for k, v in r.items():
                    if _norm(k).lower() in CODE_FIELD_NAMES and not code:
                        code = _norm(v)
                    if _norm(k) in NAME_FIELD_NAMES and not name:
                        name = clean_wikitext(_norm(v))
                if code and name:
                    records.append({"code": code, "name": name, "desc": ""})
            continue
        if code_idx is not None and code_idx < len(r):
            code = _norm(r[code_idx])
        else:
            continue
        if name_idx is not None and name_idx < len(r):
            name = clean_wikitext(_norm(r[name_idx]))
        else:
            continue
        if not code or not name:
            continue
        desc = ""
        if desc_idx is not None and desc_idx < len(r):
            desc = clean_wikitext(_norm(r[desc_idx]))
        records.append({"code": code, "name": name, "desc": desc})

    logger.info(f"Data:Buffchoose.tabx 解析完成: {len(records)} 条 buff 记录")
    return records


def _index_records(records: list[dict]) -> dict[str, dict]:
    """记录列表 → {code: info} 索引（保序，后写覆盖）。"""
    index: dict[str, dict] = {}
    for r in records:
        code = r.get("code") or ""
        if code:
            index[code] = r
    return index


async def fetch_buffs(spider) -> dict[str, dict]:
    """用 WikiSpider 抓取 buff 表，返回 {code: info} 索引。

    ``spider`` 需提供 ``fetch_page_raw(title)`` 异步方法（即 ``WikiSpider`` 实例）。
    失败时返回空 dict（不抛异常，保证不影响主流程）。
    """
    raw = await spider.fetch_page_raw(BUFFS_DATA_PAGE)
    if not raw:
        logger.error("Data:Buffchoose.tabx 获取失败")
        return {}
    records = parse_buffs_tabx(raw)
    return _index_records(records)


def save_buffs_index(buffs: dict[str, dict],
                     out_dir: str = DEFAULT_INDEX_DIR) -> Optional[Path]:
    """将 buff 映射索引写为 ``<out_dir>/buffs.json``（原子替换）。"""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    target = out_path / BUFFS_INDEX_FILE
    payload = {
        "_meta": {
            "source": BUFFS_DATA_PAGE,
            "count": len(buffs),
        },
        "buffs": buffs,
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(target)
    except OSError as e:
        logger.error(f"buff 映射写盘失败 {target}: {e}")
        return None
    logger.info(f"已保存 {len(buffs)} 条 buff 映射 -> {target}")
    return target


# ── 运行时懒加载缓存 ──
_INDEX_CACHE: Optional[dict[str, dict]] = None


def load_buffs_index(out_dir: str = DEFAULT_INDEX_DIR,
                     use_cache: bool = True) -> dict[str, dict]:
    """懒加载 ``{code: info}`` 映射；文件缺失/损坏返回空 dict。

    ``use_cache=True`` 时同一进程只读一次文件。
    """
    global _INDEX_CACHE
    if use_cache and _INDEX_CACHE is not None:
        return _INDEX_CACHE

    path = Path(out_dir) / BUFFS_INDEX_FILE
    index: dict[str, dict] = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            raw = payload.get("buffs") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                index = raw
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"buff 映射文件损坏，跳过: {path}: {e}")

    if use_cache:
        _INDEX_CACHE = index
    return index


def reload_buffs_index():
    """清除懒加载缓存（测试/数据更新后调用）。"""
    global _INDEX_CACHE
    _INDEX_CACHE = None


def resolve_buff_code(code: str,
                      out_dir: str = DEFAULT_INDEX_DIR,
                      use_cache: bool = True) -> str:
    """将 BuffPro 代码解析为中文名。

    解析顺序：
        1. 官方 buff 表索引（buffs.json）
        2. 静态兜底映射 DEFAULT_BUFF_CODES
        3. 兜底标签 ``未解析buff:Code``（不静默丢失）

    返回始终为非空字符串。
    """
    code = (code or "").strip()
    if not code:
        return ""
    index = load_buffs_index(out_dir=out_dir, use_cache=use_cache)
    if code in index:
        name = (index[code].get("name") or "").strip()
        if name:
            return name
    if code in DEFAULT_BUFF_CODES:
        return DEFAULT_BUFF_CODES[code]
    return f"未解析buff:{code}"


# ── P21-B：人格/EGO 技能描述中的纯英文 buff code → 中文名 ──
# DOM 渲染后 ``{{Status|Code}}`` 模板已变为纯英文 code 文本（如 "Breath 强度"），
# 仅处理 ``{{BuffPro|}}`` 模板的 _resolve_buffpro_in_text 无法覆盖。
# 此处在纯文本上做词边界替换（仅替换完整 code 单词，避免误伤中文/长词）。
_PLAIN_CODE_TO_CN: dict[str, str] = dict(DEFAULT_BUFF_CODES)
_PLAIN_CODE_TO_CN.update({
    # P21-B 扩充：官方 effects.json 中有独立中文条目（url 指向 wiki 页面）
    # 的专属/复合 code，以官方条目名为准（探针比对 effects.json 确认）。
    "VibrationExplosion": "震颤引爆",
    "VibrationIgnition": "震颤引爆",
    "Binding": "束缚",
    "AccelBullet": "加速弹",
    "DefenseUp": "防御等级提升",
    "DefenseDown": "防御等级降低",
    "PhotoElectricity": "光电",
    "WideAreaRampage": "广域乱射",
    # 官方 effects.json 中无法按 code 精确反查的复合效果名（保持与页面一致）
    "WindBladeIshmael": "风刃",
    "AengduNewArmIshmael": "仇甫的新臂",
})
_PLAIN_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z])(?P<code>"
    + "|".join(re.escape(k) for k in sorted(_PLAIN_CODE_TO_CN, key=len, reverse=True))
    + r")(?![A-Za-z])"
)


def resolve_buff_codes_in_text(text: str,
                               out_dir: str = DEFAULT_INDEX_DIR,
                               use_cache: bool = True,
                               extra_map: Optional[dict[str, str]] = None) -> str:
    """将文本中的纯英文 buff code 替换为中文名（修复 P21-B）。

    - 词边界匹配（code 两侧不能是英文字母），长 code 优先，避免 ``Charge``
      误吞 ``Charge`` 以外的词或中文被误伤。
    - 映射来源（优先级从高到低）：extra_map（页面级配对映射，见
      ``build_buff_code_map_from_html``）→ 官方 buffs.json 索引（若已抓取）
      → DEFAULT_BUFF_CODES 静态兜底。
    - 未命中的 code 原样保留（不标注未解析，避免污染技能描述）。
    """
    if not text:
        return text
    index = load_buffs_index(out_dir=out_dir, use_cache=use_cache)
    mapping: dict[str, str] = {}
    if extra_map:
        mapping.update(extra_map)
    mapping.update(_PLAIN_CODE_TO_CN)
    for code, info in index.items():
        if not code:
            continue
        name = (info.get("name") or "").strip()
        if name and code not in mapping:
            mapping[code] = name
    if not mapping:
        return text
    pattern = re.compile(
        r"(?<![A-Za-z])(?P<code>"
        + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
        + r")(?![A-Za-z])"
    )

    def _rep(m: re.Match) -> str:
        return mapping[m.group("code")]

    return pattern.sub(_rep, text)


# ═══════════════════════════════════════════════════════════════════════
# 页面级 BuffPro 配对映射（根治"状态效果显示英文"）
# ═══════════════════════════════════════════════════════════════════════
#
# 背景：
# - 敌方/异想体页面的 wikitext 中状态效果用 ``{{BuffPro|Code}}`` 模板引用
#   （如 ``{{BuffPro|Combustion}}``），code 为英文。
# - 该模板的中文化由站点 JS gadget（BuffPro 脚本）在浏览器中执行：
#   渲染后 ``<span class="buffPro buff-pro-processed">`` 内是中文名链接
#   （``<a title="烧伤">烧伤</a>``）。action=parse 服务端 HTML 里仍是
#   ``<span class="buffPro">Combustion</span>`` 英文占位。
# - Data:Buffchoose.tabx 是"人格→拥有的 buff 类型"布尔表，**不是**
#   code→中文名 映射，无法作为翻译来源（parse_buffs_tabx 已因此失效，
#   见模块头注释与 spider.crawl_wiki 中不再调用 fetch_buffs 的说明）。
# - 专属/复合效果 code（如 HeatedWingScalesExplain、ChoiSwordsmanship）
#   不在任何官方映射表中，但页面渲染 HTML 里有其中文名。
#
# 方案：将「渲染 HTML 中 buffPro span 的中文名」与「wikitext 中
# ``{{BuffPro|Code}}`` 的 code」按出现顺序一一配对，构建页面级
# code→中文名 映射。模板按序渲染，同一页面内顺序稳定（实测
# 折射轨道6号线-第一区段：wikitext 23 个 code ↔ HTML 23 个 span，
# 顺序完全一致）。该映射覆盖页面内全部 code，零手工维护。
_WIKITEXT_BUFFPRO_RE = re.compile(r"\{\{\s*BuffPro\s*\|\s*([^}|]+)")


def _extract_rendered_buff_names(html: str) -> list[str]:
    """从渲染后 HTML 提取 buffPro span 的中文名（按出现顺序）。

    仅收集「已由 JS 处理」的 span（class 含 ``buff-pro-processed``，
    或内部含中文 a[title] 链接）——服务端英文占位 span（仅 ``buffPro``
    且无中文链接）不计入，避免与 wikitext code 错位配对。
    用 BeautifulSoup 解析（比手写正则更稳健：buffPro span 内嵌套多层
    span/a，正则非贪婪截断会漏掉深层 a[title]）。
    """
    names: list[str] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return names
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:
        return names
    for span in soup.select("span.buffPro"):
        classes = span.get("class") or []
        processed = "buff-pro-processed" in classes
        a = span.select_one("a[title]")
        if a is not None:
            name = (a.get("title") or "").strip()
            if name and re.search(r"[\u4e00-\u9fff]", name):
                names.append(name)
                continue
        if processed:
            # JS 已处理但无链接：取 span 内纯文本（tooltip 容器仅存在于
            # 部分结构，此处不依赖它，直接取文本去重空格）
            text = span.get_text(" ", strip=True)
            if text and re.search(r"[\u4e00-\u9fff]", text):
                names.append(text)
    return names


def build_buff_code_map_from_html(html: str, wikitext: str) -> dict[str, str]:
    """构建页面级 ``{BuffPro code: 中文名}`` 配对映射。

    Args:
        html: 渲染后 HTML（Playwright 执行 JS 后的页面；action=parse
            服务端 HTML 无中文名可配对，返回空 dict）。
        wikitext: 页面原始 wikitext（从中提取 ``{{BuffPro|Code}}`` 顺序）。

    Returns:
        {code: 中文名}；任一输入为空或无法配对时返回空 dict。
    """
    if not html or not wikitext:
        return {}
    codes = [c.strip() for c in _WIKITEXT_BUFFPRO_RE.findall(wikitext)]
    names = _extract_rendered_buff_names(html)
    if not codes or not names:
        return {}
    pairs: dict[str, str] = {}
    for code, name in zip(codes, names):
        if code and name and code not in pairs:
            pairs[code] = name
    logger.info(
        f"BuffPro 页面级配对映射: wikitext {len(codes)} 个 code ↔ "
        f"HTML {len(names)} 个 span → {len(pairs)} 条映射"
    )
    return pairs
