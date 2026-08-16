# -*- coding: utf-8 -*-
"""
结构化数据导出器（Persona / Gift Direct Answer 数据层）。

爬取人格页面时，将爬虫产出的结构化 dict 额外写为独立的
``data/structured/persona_<title>.json``；爬取 E.G.O 饰品（Data:Giftchoose.tabx）时，
额外写为 ``data/structured/gift_<id>.json``。运行时分别由 ``rag.persona_direct`` /
``rag.gift_direct`` 直读该目录，绕开向量检索，直接精确取数。

职责（人格）：
- ``ensure_structured_dir``   确保输出目录存在
- ``build_filename``         title -> 稳定安全文件名（转义特殊字符）
- ``export_persona_record``  单个人格记录导出（按 title 覆盖）
- ``export_persona_records`` 批量导出（过滤非人格类型）
- ``rebuild_all``            从 wiki_pages.jsonl 重建全部人格 JSON（重爬后兜底）
- ``load_persona_index``     扫描目录 -> {title: record} 索引（供运行时复用）

职责（饰品）：
- ``clean_gift_content``     清洗饰品 content（去首行标签/镜牢经费.png/版本标注）
- ``export_gift_record``     单条饰品记录导出（按 id 覆盖，规避 title 不唯一）
- ``export_gift_records``    批量导出（过滤非 ego_gift 类型）
- ``rebuild_gifts``          从 wiki_accessories.jsonl 重建全部饰品 JSON
- ``load_gift_index``        扫描目录 -> {id: record} + {title: [id...]} 双索引

schema 说明：
- 每个技能补充 ``wikitext_key``（原始模板键名，如 ``1技能`` / ``强化1技能`` /
  ``3技能2`` / ``4技能2`` / ``5技能``）。当前爬虫尚未产出该字段时，先按
  ``skill_index`` 推断基础键名（1技能/2技能/...）；待 item 22 重爬阶段解析器
  扩展后，该字段将被真实键名覆盖。
- 保留 ``_structured: True`` 标记，新增 ``_schema_version`` 便于版本兼容。
- 本目录为派生产物，不纳入 git；运行时若目录为空，直答自动失效并回落 RAG。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = "data/structured"
PERSONA_PAGE_TYPE = "personality"
GIFT_PAGE_TYPE = "ego_gift"
EVENT_PAGE_TYPE = "event"
ENEMY_PAGE_TYPE = "enemy"
SCHEMA_VERSION = 1

# 目录分离：人格 / 饰品 / 事件 / 敌方单位分文件夹存放（方案 A）
# 保持根目录 data/structured 不变，各类型写入对应子目录。
DIR_PERSONAS = "personas"
DIR_GIFTS = "gifts"
DIR_EVENTS = "events"
DIR_ENEMIES = "enemies"

# Windows 不允许出现在文件名中的字符（在 wiki 标题中可能出现的）
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def ensure_structured_dir(out_dir: str = DEFAULT_OUT_DIR) -> Path:
    """确保结构化输出目录存在并返回 Path。"""
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_type_dir(out_dir: str, sub: str) -> Path:
    """确保类型子目录 ``out_dir/<sub>`` 存在并返回其 Path。"""
    path = ensure_structured_dir(out_dir) / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_filename(title: str) -> str:
    """title -> 稳定唯一安全文件名。

    - 保留中文与字母数字
    - 特殊字符（``/ : * ? " < > |`` 空格 等）统一替换为 ``_``
    - 结果形如 ``persona_<safe>.json``
    """
    safe = _UNSAFE_CHARS.sub("_", title.strip())
    safe = safe.strip("_")
    if not safe:
        safe = "unnamed"
    return f"persona_{safe}.json"


def _infer_wikitext_key(skill: dict, idx: int) -> str:
    """当 skill 缺少 wikitext_key 时，按 skill_index 推断基础键名。

    注意：这是临时方案——强化形态（强化N技能）、子变体（N技能M）、跳号（5技能）
    需依赖 item 22 重爬阶段的真实解析结果。若 skill 已带 wikitext_key 则原样返回。
    """
    existing = skill.get("wikitext_key")
    if existing:
        return str(existing)
    guard_type = skill.get("guard_type") or ""
    if guard_type:
        return f"守备技能{idx + 1}"
    return f"{idx + 1}技能"


def _enrich_skills(record: dict) -> list[dict]:
    """为 skills 列表补充 wikitext_key（缺失时推断）。"""
    skills = record.get("skills") or []
    enriched = []
    for idx, skill in enumerate(skills):
        if not isinstance(skill, dict):
            continue
        copy = dict(skill)
        copy.setdefault("wikitext_key", _infer_wikitext_key(copy, idx))
        enriched.append(copy)
    return enriched


def export_persona_record(record: dict, out_dir: str = DEFAULT_OUT_DIR) -> Optional[Path]:
    """将单个人格记录写为 ``data/structured/persona_<title>.json``。

    仅处理 ``page_type == 'personality'`` 的记录；其他类型返回 None。
    按 title 覆盖写入（同 title 最新数据生效），使用临时文件原子替换避免半截 JSON。
    """
    if not isinstance(record, dict):
        return None
    if record.get("page_type") != PERSONA_PAGE_TYPE:
        return None
    title = record.get("title") or record.get("personality_name") or ""
    if not title:
        logger.warning("人格记录缺少 title/personality_name，跳过导出")
        return None

    out_path = _ensure_type_dir(out_dir, DIR_PERSONAS) / build_filename(title)

    enriched = dict(record)
    enriched["skills"] = _enrich_skills(record)
    enriched.setdefault("_structured", True)
    enriched["_schema_version"] = SCHEMA_VERSION

    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=2)
        tmp_path.replace(out_path)
    except OSError as e:
        logger.error(f"导出人格 JSON 失败 {title}: {e}")
        return None
    logger.debug(f"已导出结构化人格数据: {out_path}")
    return out_path


def export_persona_records(records: list[dict], out_dir: str = DEFAULT_OUT_DIR) -> int:
    """批量导出列表中的全部人格记录（自动过滤非人格类型），返回导出条数。"""
    count = 0
    for record in records or []:
        if export_persona_record(record, out_dir):
            count += 1
    if count:
        logger.info(f"已增量导出 {count} 个人格结构化数据")
    return count


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """逐行读取 JSONL 文件，跳过损坏行。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def rebuild_all(input_jsonl: str = "data/raw/wiki_pages.jsonl",
                out_dir: str = DEFAULT_OUT_DIR) -> int:
    """从 wiki_pages.jsonl 重建全部人格结构化 JSON。

    遍历 jsonl 中所有 ``page_type == 'personality'`` 的记录，按 title 去重后逐个导出。
    返回导出的记录数。用于重爬后兜底，保证 data/structured 与 jsonl 全量一致。
    """
    src = Path(input_jsonl)
    if not src.exists():
        logger.warning(f"重建结构化数据失败：源文件不存在 {src}")
        return 0

    exported = 0
    seen: set[str] = set()
    for line in _iter_jsonl(src):
        if not isinstance(line, dict):
            continue
        if line.get("page_type") != PERSONA_PAGE_TYPE:
            continue
        title = line.get("title") or line.get("personality_name") or ""
        if not title or title in seen:
            continue
        seen.add(title)
        if export_persona_record(line, out_dir):
            exported += 1
    logger.info(f"结构化数据重建完成：导出 {exported} 个人格到 {out_dir}")
    return exported


def load_persona_index(out_dir: str = DEFAULT_OUT_DIR) -> dict[str, dict]:
    """扫描 ``data/structured/`` 目录，构建 {title: record} 索引。

    运行时复用：``rag.persona_direct`` 模块启动时调用，建立 title 精确匹配索引。
    """
    index: dict[str, dict] = {}
    root = Path(out_dir) / DIR_PERSONAS
    if not root.exists():
        return index
    for f in sorted(root.glob("persona_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"结构化人格文件损坏，跳过: {f}")
            continue
        if not isinstance(record, dict):
            continue
        title = record.get("title") or record.get("personality_name") or ""
        if title:
            index[title] = record
    logger.debug(f"已加载结构化人格索引：{len(index)} 条")
    return index


# ════════════════════════════════════════════════════════════════════════
# E.G.O 饰品结构化导出（Gift Direct Answer）
#
# 饰品数据源：data/raw/wiki_accessories.jsonl（爬虫从 Data:Giftchoose.tabx 解析）。
# 与人格不同，饰品存在两个数据难点：
#  1. title 不唯一（如 怀表：Type L 在 时间杀人时间 与 镜像迷宫 各一条）
#  2. 多 stage 版本（如 炎鳞 有 base / upgraded_2 / upgraded_3 三条；P38 后
#     upgraded_2/upgraded_3 仅由 tabx desc_1/desc_2 真强化阶段产生）
# → 文件名与主索引键一律用 id（gift_xxxx），title 仅作反向聚合键。
# ════════════════════════════════════════════════════════════════════════

# 内容清洗正则
_GIFT_TAGLINE_RE = re.compile(r"^[^\n]*?：\[[^\]]*\](?:\[[^\]]*\])?.*\n")  # 首行 名字：[地点][效果类型]
_GIFT_COST_RE = re.compile(r"^镜牢经费(?:\.png)?[：:].*\n?", re.MULTILINE)  # 镜牢经费.png：600
_GIFT_STAGE_LABEL_RE = re.compile(r"^[（(](?:未强化版|强化版·[ⅠⅡⅢ]+级)[）)]\s*$", re.MULTILINE)  # 版本标注行


def clean_gift_content(record: dict) -> dict:
    """清洗饰品记录，将 content 中的解析标签残留剥离为干净的效果正文。

    处理：
    - 去除首行标签行 ``名字：[地点][效果类型]``（信息已拆分为结构化字段）
    - 去除 ``镜牢经费.png：xxx``（经费由 cost 字段表达）
    - 去除尾部 ``（未强化版）/（强化版·Ⅰ级）/（强化版·Ⅱ级）`` 版本标注行
      （版本由 stage 字段表达）
    - 压缩多余空行、去除行尾空白

    返回清洗后的 record 副本（不修改入参）。
    """
    if not isinstance(record, dict):
        return record
    copy = dict(record)
    content = copy.get("content") or ""
    if not content:
        copy["content"] = ""
        return copy

    text = _GIFT_COST_RE.sub("", content)      # 整行经费前缀
    text = _GIFT_STAGE_LABEL_RE.sub("", text)  # 独立版本标注行
    # 首行标签行：仅当它是整行标签（形如 名字：[...][...]）时移除
    lines = text.splitlines()
    if lines:
        first = lines[0].strip()
        if _GIFT_TAGLINE_RE.match(first + "\n"):
            lines = lines[1:]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    copy["content"] = text
    return copy


def build_gift_filename(gift_id: str) -> str:
    """gift_id -> 稳定安全文件名（``gift_<safe>.json``）。

    用 id 而非 title，规避 title 不唯一与特殊字符问题。
    """
    safe = _UNSAFE_CHARS.sub("_", str(gift_id).strip())
    safe = safe.strip("_")
    if not safe:
        safe = "unnamed"
    return f"gift_{safe}.json"


def export_gift_record(record: dict, out_dir: str = DEFAULT_OUT_DIR) -> Optional[Path]:
    """将单条饰品记录写为 ``data/structured/gift_<id>.json``。

    仅处理 ``page_type == 'ego_gift'`` 且带 ``id`` 的记录；其他返回 None。
    按 id 覆盖写入，使用临时文件原子替换避免半截 JSON。
    """
    if not isinstance(record, dict):
        return None
    if record.get("page_type") != GIFT_PAGE_TYPE:
        return None
    gift_id = record.get("id") or ""
    if not gift_id:
        logger.warning("饰品记录缺少 id，跳过导出")
        return None

    cleaned = clean_gift_content(record)
    cleaned.setdefault("_structured", True)
    cleaned["_schema_version"] = SCHEMA_VERSION

    out_path = _ensure_type_dir(out_dir, DIR_GIFTS) / build_gift_filename(gift_id)
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        tmp_path.replace(out_path)
    except OSError as e:
        logger.error(f"导出饰品 JSON 失败 {gift_id}: {e}")
        return None
    logger.debug(f"已导出结构化饰品数据: {out_path}")
    return out_path


def export_gift_records(records: list[dict], out_dir: str = DEFAULT_OUT_DIR) -> int:
    """批量导出列表中的全部饰品记录（自动过滤非 ego_gift 类型），返回导出条数。"""
    count = 0
    for record in records or []:
        if export_gift_record(record, out_dir):
            count += 1
    if count:
        logger.info(f"已增量导出 {count} 条饰品结构化数据")
    return count


def rebuild_gifts(input_jsonl: str = "data/raw/wiki_accessories.jsonl",
                  out_dir: str = DEFAULT_OUT_DIR) -> int:
    """从 wiki_accessories.jsonl 重建全部饰品结构化 JSON。

    遍历 jsonl 中所有 ``page_type == 'ego_gift'`` 的记录，按 id 去重后逐个导出。
    返回导出的记录数。用于重爬后兜底，保证 data/structured 与 jsonl 全量一致。
    """
    src = Path(input_jsonl)
    if not src.exists():
        logger.warning(f"重建饰品结构化数据失败：源文件不存在 {src}")
        return 0

    exported = 0
    seen: set[str] = set()
    for line in _iter_jsonl(src):
        if not isinstance(line, dict):
            continue
        if line.get("page_type") != GIFT_PAGE_TYPE:
            continue
        gift_id = line.get("id") or ""
        if not gift_id or gift_id in seen:
            continue
        seen.add(gift_id)
        if export_gift_record(line, out_dir):
            exported += 1
    logger.info(f"饰品结构化数据重建完成：导出 {exported} 条饰品到 {out_dir}")
    return exported


def load_gift_index(out_dir: str = DEFAULT_OUT_DIR) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """扫描 ``data/structured/`` 目录，构建饰品双索引。

    Returns:
        (id_index, title_index)：
        - id_index:    {gift_id: record}
        - title_index: {title: [gift_id, ...]}（同 title 多版本/多地点 -> id 列表）

    运行时复用：``rag.gift_direct`` 模块启动时调用。
    """
    id_index: dict[str, dict] = {}
    title_index: dict[str, list[str]] = {}
    root = Path(out_dir) / DIR_GIFTS
    if not root.exists():
        return id_index, title_index
    for f in sorted(root.glob("gift_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"结构化饰品文件损坏，跳过: {f}")
            continue
        if not isinstance(record, dict):
            continue
        gift_id = record.get("id") or ""
        title = record.get("title") or record.get("gift_name") or ""
        if not gift_id or not title:
            continue
        id_index[gift_id] = record
        title_index.setdefault(title, []).append(gift_id)
    logger.debug(f"已加载结构化饰品索引：{len(id_index)} 条 / {len(title_index)} 个名称")
    return id_index, title_index


# ════════════════════════════════════════════════════════════════════════
# 探索事件结构化导出（Event Direct Answer）
#
# 事件数据源：data/raw/wiki_pages.jsonl（page_type == "event"）。
# 字段结构见 _event_to_dict()：
#   event_name / narration / options（choice_text/check_type/check_sin/
#   check_threshold/success_outcomes/failure_outcomes）/ ego_gifts /
#   related_abnormalities / trigger_location / id（wiki_事件-<名称>）
#
# 数据难点与清洗策略：
#  1. check_sin 带 .png 后缀（如 色欲.png）→ 导出时去后缀
#  2. ego_gifts 的 name/effect 字段错位（name 实为效果文本）→ 仅 4 事件有值，
#     导出时降级为单行效果文本展示，不构造名-效果假配对
#  3. related_abnormalities 含垃圾值（"事件触发地点[编辑]"、"无"、评级拼接串）
#     → 导出时过滤，仅保留纯异想体名称
#  4. 存在 8 个空占位事件（narration/options 全空）→ 仍导出（保留索引完整性），
#     运行时直答对空内容输出"暂无详细数据"标注
# ════════════════════════════════════════════════════════════════════════

# 关联异想体垃圾值过滤正则：评级拼接串（如 "HELCE评级HE-04O-06-20-02kqe-1j-23"）
_ABNO_JUNK_RE = re.compile(
    r"(?:ZAYIN|TETH|HE|WAW|ALEPH|HELCE|TETHLCE|WAW|ALEPH)评级"
)
_ABNO_PLACEHOLDER = {"事件触发地点[编辑]", "无", "触发地点[编辑]"}


def clean_event_record(record: dict) -> dict:
    """清洗事件记录副本，输出可直答的干净数据（不修改入参）。

    处理：
    - ``check_sin`` 去掉 ``.png`` 后缀（色欲.png → 色欲）
    - ``ego_gifts`` 字段错位降级：name/effect 实为两段效果文本，不再构造
      "名: 效果" 假配对，改为单行 ``- <name>`` 效果展示；保留原始字段
    - ``related_abnormalities`` 过滤垃圾值（评级拼接串 / 占位符 / 空串）
    - ``trigger_location`` 为空时置空串（格式化层省略）
    """
    if not isinstance(record, dict):
        return record
    copy = dict(record)

    # check_sin 去 .png 后缀
    options = []
    for opt in copy.get("options") or []:
        if not isinstance(opt, dict):
            options.append(opt)
            continue
        o = dict(opt)
        sin = o.get("check_sin") or ""
        if isinstance(sin, str):
            o["check_sin"] = re.sub(r"\.png$", "", sin, flags=re.IGNORECASE)
        options.append(o)
    copy["options"] = options

    # related_abnormalities 过滤垃圾值
    abnos = []
    for a in copy.get("related_abnormalities") or []:
        if not isinstance(a, str):
            continue
        s = a.strip()
        if not s or s in _ABNO_PLACEHOLDER or _ABNO_JUNK_RE.search(s):
            continue
        abnos.append(s)
    copy["related_abnormalities"] = abnos

    # trigger_location 归一化（空串省略）
    loc = copy.get("trigger_location") or ""
    copy["trigger_location"] = loc.strip()

    # ego_gifts 字段错位降级：仅保留文本字段以便格式化层单行展示
    gifts = copy.get("ego_gifts") or []
    if gifts:
        copy["ego_gifts"] = gifts  # 保留原始结构，运行时降级渲染
    else:
        copy["ego_gifts"] = []

    return copy


def build_event_filename(event_id: str) -> str:
    """event_id -> 稳定安全文件名（``event_<safe>.json``）。

    用 id 而非 title，与 gift 一致规避特殊字符；当前事件无重名，id 形如
    ``wiki_事件-<名称>``。
    """
    safe = _UNSAFE_CHARS.sub("_", str(event_id).strip())
    safe = safe.strip("_")
    if not safe:
        safe = "unnamed"
    return f"event_{safe}.json"


def export_event_record(record: dict, out_dir: str = DEFAULT_OUT_DIR) -> Optional[Path]:
    """将单条事件记录写为 ``data/structured/events/event_<id>.json``。

    仅处理 ``page_type == 'event'`` 且带 ``id`` 的记录；其他返回 None。
    按 id 覆盖写入，使用临时文件原子替换避免半截 JSON。
    """
    if not isinstance(record, dict):
        return None
    if record.get("page_type") != EVENT_PAGE_TYPE:
        return None
    event_id = record.get("id") or ""
    if not event_id:
        logger.warning("事件记录缺少 id，跳过导出")
        return None

    cleaned = clean_event_record(record)
    cleaned.setdefault("_structured", True)
    cleaned["_schema_version"] = SCHEMA_VERSION

    out_path = _ensure_type_dir(out_dir, DIR_EVENTS) / build_event_filename(event_id)
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        tmp_path.replace(out_path)
    except OSError as e:
        logger.error(f"导出事件 JSON 失败 {event_id}: {e}")
        return None
    logger.debug(f"已导出结构化事件数据: {out_path}")
    return out_path


def export_event_records(records: list[dict], out_dir: str = DEFAULT_OUT_DIR) -> int:
    """批量导出列表中的全部事件记录（自动过滤非 event 类型），返回导出条数。"""
    count = 0
    for record in records or []:
        if export_event_record(record, out_dir):
            count += 1
    if count:
        logger.info(f"已增量导出 {count} 条事件结构化数据")
    return count


def rebuild_events(input_jsonl: str = "data/raw/wiki_pages.jsonl",
                   out_dir: str = DEFAULT_OUT_DIR) -> int:
    """从 wiki_pages.jsonl 重建全部事件结构化 JSON。

    遍历 jsonl 中所有 ``page_type == 'event'`` 的记录，按 id 去重后逐个导出。
    返回导出的记录数。用于重爬后兜底，保证 data/structured/events 与 jsonl 全量一致。
    """
    src = Path(input_jsonl)
    if not src.exists():
        logger.warning(f"重建事件结构化数据失败：源文件不存在 {src}")
        return 0

    exported = 0
    seen: set[str] = set()
    for line in _iter_jsonl(src):
        if not isinstance(line, dict):
            continue
        if line.get("page_type") != EVENT_PAGE_TYPE:
            continue
        event_id = line.get("id") or ""
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        if export_event_record(line, out_dir):
            exported += 1
    logger.info(f"事件结构化数据重建完成：导出 {exported} 条事件到 {out_dir}")
    return exported


def clean_enemy_record(record: dict) -> dict:
    """清洗敌方单位记录副本，输出可直答的干净数据（不修改入参）。

    敌方记录（`page_type='enemy'` 的 `enemies` 数组元素）已是结构化 dict，
    此处仅补齐 `enemy_id`（`battle_stage|enemy_name|body_part`，修复② 加入
    body_part 维度避免同关卡同名不同部位单位被索引覆盖）、`defense` 别名
    （兼容 `defense_level`）与 `speed` 显示串（`speed_min~speed_max`），
    并保证 3 物理 + 7 罪孽抗性缺省值为 1.0（领域规则）。
    """
    if not isinstance(record, dict):
        return record
    copy = dict(record)

    battle_stage = str(copy.get("battle_stage") or "").strip()
    enemy_name = str(copy.get("enemy_name") or "").strip()
    body_part = str(copy.get("body_part") or "").strip()
    if not copy.get("enemy_id"):
        copy["enemy_id"] = f"{battle_stage}|{enemy_name}|{body_part}"

    # defense 别名（直答格式化层统一用 defense）
    if "defense" not in copy:
        copy["defense"] = copy.get("defense_level", 0)

    # speed 显示串
    if "speed" not in copy:
        smin = copy.get("speed_min")
        smax = copy.get("speed_max")
        if smin is not None and smax is not None:
            copy["speed"] = f"{smin}~{smax}"
        elif smin is not None:
            copy["speed"] = str(smin)

    # 抗性缺省 1.0：3 物理 + 7 罪孽
    _PHYS = ("斩击", "突刺", "打击")
    _SINS = ("暴怒", "色欲", "怠惰", "暴食", "忧郁", "傲慢", "嫉妒")
    phys = copy.get("physical_resistances") or {}
    sins = copy.get("sin_resistances") or {}
    if isinstance(phys, dict):
        for k in _PHYS:
            phys.setdefault(k, 1.0)
        copy["physical_resistances"] = phys
    if isinstance(sins, dict):
        for k in _SINS:
            sins.setdefault(k, 1.0)
        copy["sin_resistances"] = sins

    return copy


def build_enemy_filename(battle_stage: str, enemy_name: str) -> str:
    """battle_stage + enemy_name -> 稳定安全文件名（``enemy_<stage>_<name>.json``）。

    与计划 8.4 一致：同一 stage 多单位各自成文件。
    """
    stage_safe = _UNSAFE_CHARS.sub("_", str(battle_stage).strip()).strip("_")
    name_safe = _UNSAFE_CHARS.sub("_", str(enemy_name).strip()).strip("_")
    if not stage_safe:
        stage_safe = "unknown_stage"
    if not name_safe:
        name_safe = "unnamed"
    return f"enemy_{stage_safe}_{name_safe}.json"


def export_enemy_record(record: dict, out_dir: str = DEFAULT_OUT_DIR) -> Optional[Path]:
    """将单个敌方单位记录写为 ``data/structured/enemies/enemy_<stage>_<name>.json``。

    仅处理敌方单位记录（含 ``enemy_name``/``battle_stage`` 的单单位 dict）；
    其他返回 None。按 ``battle_stage|enemy_name`` 覆盖写入，使用临时文件原子替换。
    """
    if not isinstance(record, dict):
        return None
    if record.get("page_type") != ENEMY_PAGE_TYPE and "enemy_name" not in record:
        return None
    battle_stage = record.get("battle_stage") or record.get("title") or ""
    enemy_name = record.get("enemy_name") or ""
    if not enemy_name:
        logger.warning("敌方记录缺少 enemy_name，跳过导出")
        return None

    cleaned = clean_enemy_record(record)
    cleaned.setdefault("_structured", True)
    cleaned["_schema_version"] = SCHEMA_VERSION

    out_path = _ensure_type_dir(out_dir, DIR_ENEMIES) / build_enemy_filename(battle_stage, enemy_name)
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        tmp_path.replace(out_path)
    except OSError as e:
        logger.error(f"导出敌方单位 JSON 失败 {enemy_name}: {e}")
        return None
    logger.debug(f"已导出结构化敌方单位数据: {out_path}")
    return out_path


def export_enemy_records(records: list[dict], out_dir: str = DEFAULT_OUT_DIR) -> int:
    """批量导出列表中的全部敌方单位记录（自动过滤非敌方记录），返回导出条数。"""
    count = 0
    for record in records or []:
        if export_enemy_record(record, out_dir):
            count += 1
    if count:
        logger.info(f"已增量导出 {count} 条敌方单位结构化数据")
    return count


# 空技能哨兵值：技能全部缺失/为空的单位不应与正常单位（技能非空）按同一签名聚合，
# 否则会与同名同部位但技能完整的单位误并，或与另一个技能同样缺失的同名单位误判为重复。
# 该哨兵保证技能缺失单位始终单独成键（不并入正常聚合组，也不互相合并），
# 便于后续人工补充技能（如 P21-D 无我良秀、技能待补单位）。
_EMPTY_SKILL_SENTINEL = "__NO_SKILL__"


def _unit_skill_signature(unit: dict) -> tuple:
    """单位技能签名：(skill_name, importance) 有序元组。

    修复②：技能名/重要性不同 → 视为不同单位（新键），
    与 extract() 指纹（去 hp + 部位）保持一致，供 exporter 聚合去重。
    修复 P22：skills 为空/缺失时返回哨兵签名（非空元组），
    使技能缺失单位不并入正常聚合组，也不互相误并（每个技能缺失单位独立成键）。
    """
    sigs = []
    for s in unit.get("skills") or []:
        if isinstance(s, dict):
            sigs.append((s.get("skill_name") or "", s.get("importance") or 0))
    if not sigs:
        # 空技能：返回哨兵，避免与正常单位签名 () 混淆导致误并
        return (_EMPTY_SKILL_SENTINEL,)
    return tuple(sorted(sigs))


def rebuild_enemies(input_jsonl: str = "data/raw/wiki_pages.jsonl",
                    out_dir: str = DEFAULT_OUT_DIR) -> int:
    """从 wiki_pages.jsonl 重建全部敌方单位结构化 JSON。

    遍历 jsonl 中所有 ``page_type == 'enemy'`` 记录的 ``enemies`` 数组，
    按 ``enemy_name|body_part|技能签名`` 聚合去重（修复②）：
    - 技能名/重要性不同 → 不同单位（即使同关卡同名）
    - 同模型跨关卡（技能/抗性相同）→ 合并为单条记录，`appear_stages` 记录出现关卡列表
    - 保留首条完整数据（battle_stage 取首个出现关卡，作为主文件名）
    返回导出的单位数。用于重爬后兜底，保证 data/structured/enemies 与 jsonl 全量一致。
    """
    src = Path(input_jsonl)
    if not src.exists():
        logger.warning(f"重建敌方单位结构化数据失败：源文件不存在 {src}")
        return 0

    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for line in _iter_jsonl(src):
        if not isinstance(line, dict):
            continue
        if line.get("page_type") != ENEMY_PAGE_TYPE:
            continue
        for unit in line.get("enemies") or []:
            if not isinstance(unit, dict):
                continue
            unit_copy = dict(unit)
            unit_copy.setdefault("page_type", ENEMY_PAGE_TYPE)
            if not unit_copy.get("battle_stage"):
                unit_copy["battle_stage"] = line.get("battle_stage") or line.get("title") or ""
            if not unit_copy.get("title"):
                unit_copy["title"] = line.get("title") or ""
            enemy_name = unit_copy.get("enemy_name") or ""
            if not enemy_name:
                continue
            battle_stage = unit_copy.get("battle_stage") or ""
            body_part = str(unit_copy.get("body_part") or "").strip()
            key = (enemy_name, body_part, _unit_skill_signature(unit_copy))
            if key in groups:
                # 跨关卡合并：仅合并出现关卡，保留首条完整数据
                existing = groups[key]
                stages = existing.get("appear_stages") or []
                if battle_stage and battle_stage not in stages:
                    stages.append(battle_stage)
                    existing["appear_stages"] = stages
                continue
            unit_copy["appear_stages"] = [battle_stage] if battle_stage else []
            groups[key] = unit_copy
            order.append(key)

    exported = 0
    used_filenames: set[str] = set()
    for key in order:
        rec = groups[key]
        battle_stage = rec.get("battle_stage") or ""
        enemy_name = rec.get("enemy_name") or ""
        base = build_enemy_filename(battle_stage, enemy_name)
        filename = base
        counter = 1
        # 同 stage+name 多单位（不同部位/技能）→ 追加 _bN 序号避免覆盖
        while filename in used_filenames:
            filename = f"{base[:-5]}_b{counter}.json"
            counter += 1
        used_filenames.add(filename)

        cleaned = clean_enemy_record(rec)
        cleaned.setdefault("_structured", True)
        cleaned["_schema_version"] = SCHEMA_VERSION
        out_path = _ensure_type_dir(out_dir, DIR_ENEMIES) / filename
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"导出敌方单位 JSON 失败 {enemy_name}: {e}")
            continue
        exported += 1

    logger.info(
        f"敌方单位结构化数据重建完成：导出 {exported} 个单位到 {out_dir} "
        f"（聚合 {len(order)} 键，合并跨关卡同名单位）"
    )
    return exported


def load_enemy_index(out_dir: str = DEFAULT_OUT_DIR) -> dict[str, dict]:
    """扫描 ``data/structured/enemies/`` 目录，构建 {enemy_id: record} 索引。

    索引主键用 ``enemy_id``（``battle_stage|enemy_name``）；运行时可按单位名或
    关卡匹配。运行时复用（后续）：``rag.enemy_direct``。
    """
    index: dict[str, dict] = {}
    root = Path(out_dir) / DIR_ENEMIES
    if not root.exists():
        return index
    for f in sorted(root.glob("enemy_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"结构化敌方单位文件损坏，跳过: {f}")
            continue
        if not isinstance(record, dict):
            continue
        eid = record.get("enemy_id") or f"{record.get('battle_stage')}|{record.get('enemy_name')}"
        if eid:
            index[eid] = record
    logger.debug(f"已加载结构化敌方单位索引：{len(index)} 条")
    return index


def load_event_index(out_dir: str = DEFAULT_OUT_DIR) -> dict[str, dict]:
    """扫描 ``data/structured/events/`` 目录，构建 {event_name: record} 索引。

    索引主键用 ``event_name``（裸名，用户查询的是裸名）；title 存于 record 中，
    运行时可用 title（带"事件-"前缀）作辅助匹配。运行时复用：``rag.event_direct``。
    """
    index: dict[str, dict] = {}
    root = Path(out_dir) / DIR_EVENTS
    if not root.exists():
        return index
    for f in sorted(root.glob("event_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"结构化事件文件损坏，跳过: {f}")
            continue
        if not isinstance(record, dict):
            continue
        name = record.get("event_name") or record.get("title") or ""
        if name:
            index[name] = record
    logger.debug(f"已加载结构化事件索引：{len(index)} 条")
    return index


if __name__ == "__main__":
    # 独立运行入口：从 jsonl 重建全部结构化 JSON（无需爬虫环境）
    # 用法：python -m crawler.structured_exporter [persona|gift|event|enemy|all]
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    kind = sys.argv[1] if len(sys.argv) > 1 else "persona"
    if kind == "gift":
        n = rebuild_gifts()
        print(f"重建完成：{n} 条饰品")
    elif kind == "event":
        n = rebuild_events()
        print(f"重建完成：{n} 条事件")
    elif kind == "enemy":
        n = rebuild_enemies()
        print(f"重建完成：{n} 个敌方单位")
    elif kind == "all":
        np_ = rebuild_all()
        ng = rebuild_gifts()
        ne = rebuild_events()
        ne2 = rebuild_enemies()
        print(f"重建完成：{np_} 个人格 / {ng} 条饰品 / {ne} 条事件 / {ne2} 个敌方单位")
    else:
        n = rebuild_all()
        print(f"重建完成：{n} 个人格")
