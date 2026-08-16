"""
结构化分块构建器：将 HTML 提取的结构化数据构建为 LangChain Document 列表。

不依赖 RecursiveCharacterTextSplitter 的盲切，而是按逻辑单元精确分块：
- 人格：基本信息 + 每个技能×每个阶段 + 被动
- E.G.O：基本信息 + 觉醒每阶段 + 侵蚀每阶段 + 被动
- E.G.O饰品：每条饰品记录一个 chunk（已按版本拆分）
- 状态效果：每个状态效果一个 chunk，包含名称、类型、描述
- 故事剧情：按章节分组，[角色名]\\n对话内容 格式
- 但丁笔记：每条设定记录一个 chunk，分类路径 + 记录内容

每个 chunk 携带丰富的 metadata 供 ChromaDB 过滤和检索加权。
"""

import logging
import re
from typing import Any, Optional

from langchain_core.documents import Document

from crawler.passives_data import load_passives_index
# 恐慌收敛集合（与 html_extractor 同一数据源；bs4 为可选导入，不构成硬依赖）
from crawler.html_extractor import _NON_PANIC_BUFF_NAMES, _SIN_AFFINITY_NAMES

logger = logging.getLogger(__name__)

# 人格被动占位符正则：``人格被动{ID}``（MULTILINE 使 sub 对多行字符串逐行替换）
_PASSIVE_ID_RE = re.compile(r"^人格被动\s*(\d+)\s*$", re.MULTILINE)
# 被动映射表懒加载缓存（与 rag.persona_direct 同源）
_PASSIVE_MAP_CACHE: Optional[dict] = None


def _lookup_passive(pid: str) -> Optional[dict]:
    """按被动 ID 查映射表，返回 {name, desc} 精简信息；未命中返回 None。"""
    global _PASSIVE_MAP_CACHE
    if _PASSIVE_MAP_CACHE is None:
        _PASSIVE_MAP_CACHE = load_passives_index()
    info = _PASSIVE_MAP_CACHE.get(pid)
    if not info or not isinstance(info, dict):
        return None
    name = (info.get("name") or "").strip()
    desc = (info.get("desc1") or "").strip()
    if not name and not desc:
        return None
    return {"name": name, "desc": desc}


def _render_passive_text(value) -> str:
    """将 ``人格被动{ID}`` 占位组合为 ``名称：描述``；非占位内容原样保留。

    用于 RAG 分块（crawler.chunk_builder）侧，使检索到的被动块不含占位符。
    映射缺失时保留原占位符（不虚构内容）。
    """
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return ""

    def _sub(m: re.Match) -> str:
        info = _lookup_passive(m.group(1))
        if not info:
            return m.group(0)
        if info["name"] and info["desc"]:
            return f"{info['name']}：{info['desc']}"
        if info["name"]:
            return info["name"]
        if info["desc"]:
            return info["desc"]
        return m.group(0)

    # 多行字符串逐行替换（battle_passive 每行一个占位符）
    return _PASSIVE_ID_RE.sub(_sub, text)


def _format_resistance_table(resistances: dict[str, float]) -> str:
    """将抗性字典格式化为可读文本。"""
    if not resistances:
        return ""
    mapping = {0.5: "抵抗", 0.75: "抵抗", 1.0: "普通", 1.5: "脆弱", 2.0: "脆弱"}
    parts = []
    for name, val in resistances.items():
        label = mapping.get(val, f"×{val}")
        parts.append(f"{name}：{label}")
    return " / ".join(parts)


def _format_resource_costs(costs: dict[str, int]) -> str:
    """格式化资源消耗。"""
    if not costs:
        return ""
    return " / ".join(f"{name}×{count}" for name, count in costs.items())


def build_personality_chunks(data: dict) -> list[Document]:
    """将结构化人格数据构建为 Document 列表。"""
    documents = []
    base_metadata = {
        "page_type": "personality",
        "page_title": data.get("title", ""),
        "sinner": data.get("sinner", ""),
        "sinner_id": data.get("sinner_id", ""),
        "personality_name": data.get("personality_name", ""),
        "source": "wiki_structured",
    }

    title = data.get("title", "")
    sinner = data.get("sinner", "")

    # ── 1) 基本信息块 ──
    info_lines = [f"人格：{title}"]
    if data.get("release_date"):
        info_lines.append(f"实装日期：{data['release_date']}")
    if data.get("acquisition"):
        info_lines.append(f"获取方式：{data['acquisition']}")

    sin_aff = data.get("sin_affinities", {})
    if sin_aff:
        aff_parts = [f"{k}×{v}" for k, v in sin_aff.items()]
        info_lines.append(f"罪孽亲和：{' / '.join(aff_parts)}")

    phys_res = data.get("physical_resistances", {})
    if phys_res:
        info_lines.append(f"物理抗性：{_format_resistance_table(phys_res)}")

    ego_res = data.get("ego_resources", {})
    if ego_res:
        info_lines.append(f"E.G.O资源：{_format_resource_costs(ego_res)}")

    info_meta = dict(base_metadata, section="info")
    info_doc = Document(
        page_content="\n".join(info_lines),
        metadata=info_meta,
    )
    documents.append(info_doc)

    # ── 2) 技能块（每个技能 × 每个阶段） ──
    skills = data.get("skills", [])
    for skill in skills:
        skill_idx = skill.get("skill_index", 0)
        skill_name = skill.get("skill_name", f"技能{skill_idx + 1}")
        sin_type = skill.get("sin_type", "")
        damage_type = skill.get("damage_type", "")
        guard_type = skill.get("guard_type", "")
        attack_capacity = skill.get("attack_capacity", 1)
        coin_count = skill.get("coin_count", 0)

        # 确定 skill_type
        if guard_type:
            skill_type = "guard"
        elif sin_type == "" and damage_type == "":
            skill_type = "conditional"
        else:
            skill_type = "attack"

        # 每个阶段一个 chunk（stage_groups 是列表的列表：[[I-IV], [强化I-IV], ...]）
        stage_groups = skill.get("stage_groups", [])
        for stages in stage_groups:
            for stage_data in stages:
                stage = stage_data.get("stage", "")
                base_value = stage_data.get("base_value")
                coin_power = stage_data.get("coin_power")

                stage_lines = [f"人格：{title}"]
                stage_lines.append(f"技能：{skill_name}（{stage}阶）")

                if sin_type:
                    stage_lines.append(f"罪孽类型：{sin_type}")
                if damage_type:
                    stage_lines.append(f"伤害类型：{damage_type}")
                if guard_type:
                    stage_lines.append(f"守备类型：{guard_type}")
                if coin_count > 0:
                    stage_lines.append(f"硬币数量：{coin_count}")
                if attack_capacity > 1:
                    stage_lines.append(f"攻击容量：{attack_capacity}")
                if base_value is not None:
                    stage_lines.append(f"基础值：{base_value}")
                if coin_power is not None:
                    sign = "+" if coin_power > 0 else ""
                    stage_lines.append(f"变动值：{sign}{coin_power}")

                # 阶段特有效果
                effects = stage_data.get("effects", [])
                for eff in effects:
                    timing = eff.get("timing", "")
                    desc = eff.get("description", "")
                    stage_lines.append(f"[{timing}] {desc}")

                # 硬币效果（来自技能级别）
                coin_effects = skill.get("coin_effects", [])
                for ce in coin_effects:
                    timing = ce.get("timing", "")
                    desc = ce.get("description", "")
                    coin_idx = ce.get("coin_index", 0)
                    prefix = f"硬币{coin_idx}" if coin_idx > 0 else ""
                    stage_lines.append(f"{prefix}[{timing}] {desc}")

                stage_meta = dict(base_metadata, **{
                    "section": "skill",
                    "skill_index": skill_idx,
                    "skill_name": skill_name,
                    "skill_type": skill_type,
                    "stage": stage,
                    "sin_type": sin_type,
                    "damage_type": damage_type,
                    "guard_type": guard_type,
                })

                stage_doc = Document(
                    page_content="\n".join(stage_lines),
                    metadata=stage_meta,
                )
                documents.append(stage_doc)

    # ── 3) 被动块 ──
    passive_parts = []
    if data.get("battle_passive"):
        passive_parts.append(f"战斗被动：{_render_passive_text(data['battle_passive'])}")
    if data.get("support_passive"):
        passive_parts.append(f"支援被动：{_render_passive_text(data['support_passive'])}")

    if passive_parts:
        passive_meta = dict(base_metadata, section="passive")
        passive_doc = Document(
            page_content=f"人格：{title}\n" + "\n".join(passive_parts),
            metadata=passive_meta,
        )
        documents.append(passive_doc)

    # ── 4) 语音台词块（关联人格）──
    voice_lines = data.get("voice_lines", [])
    for v in voice_lines:
        text = v.get("text", "")
        if not text:
            continue
        v_title = v.get("title", "")
        v_file = v.get("file", "")
        voice_lines_text = [f"人格：{title}"]
        if v_title:
            voice_lines_text.append(f"语音标题：{v_title}")
        voice_lines_text.append(f"台词：{text}")
        if v_file:
            voice_lines_text.append(f"语音文件：{v_file}")
        voice_meta = dict(base_metadata, section="voice", voice_title=v_title, voice_file=v_file)
        voice_doc = Document(
            page_content="\n".join(voice_lines_text),
            metadata=voice_meta,
        )
        documents.append(voice_doc)

    # ── 5) 技能语音块（关联人格/技能）──
    skill_voice = data.get("skill_voice", [])
    for sv in skill_voice:
        sv_file = sv.get("file", "")
        if not sv_file:
            continue
        sv_index = sv.get("skill_index", 0)
        sv_label = sv.get("skill_label", "")
        sv_name = sv.get("skill_name", "") or (f"技能{sv_index}" if sv_index else sv_label)
        skill_voice_text = [f"人格：{title}"]
        if sv_name:
            skill_voice_text.append(f"技能：{sv_name}")
        skill_voice_text.append(f"技能语音文件：{sv_file}")
        sv_meta = dict(base_metadata, section="skill_voice", skill_index=sv_index,
                       skill_name=sv_name, voice_file=sv_file)
        sv_doc = Document(
            page_content="\n".join(skill_voice_text),
            metadata=sv_meta,
        )
        documents.append(sv_doc)

    logger.debug(f"人格 {title}: 构建了 {len(documents)} 个 chunk")
    return documents


def build_ego_chunks(data: dict) -> list[Document]:
    """将结构化 E.G.O 数据构建为 Document 列表。"""
    documents = []
    base_metadata = {
        "page_type": "ego",
        "page_title": data.get("title", ""),
        "sinner": data.get("sinner", ""),
        "sinner_id": data.get("sinner_id", ""),
        "ego_name": data.get("ego_name", ""),
        "source": "wiki_structured",
    }

    title = data.get("title", "")
    ego_name = data.get("ego_name", "")

    # ── 1) 基本信息块 ──
    info_lines = [f"E.G.O：{title}"]
    if data.get("release_date"):
        info_lines.append(f"实装日期：{data['release_date']}")
    if data.get("acquisition"):
        info_lines.append(f"获取方式：{data['acquisition']}")

    costs = data.get("resource_costs", {})
    if costs:
        info_lines.append(f"资源消耗：{_format_resource_costs(costs)}")

    sin_res = data.get("sin_resistances", {})
    if sin_res:
        info_lines.append(f"罪孽抗性：{_format_resistance_table(sin_res)}")

    info_meta = dict(base_metadata, section="info")
    info_doc = Document(
        page_content="\n".join(info_lines),
        metadata=info_meta,
    )
    documents.append(info_doc)

    # ── 2) 觉醒阶段块 ──
    awakening = data.get("awakening_stages", [])
    for aw_data in awakening:
        coin_count = aw_data.get("coin_count", 0)
        sanity_cost = aw_data.get("sanity_cost", 0)
        attack_bonus = aw_data.get("attack_bonus", 0)

        stages = aw_data.get("stages", [])
        for stage_data in stages:
            stage = stage_data.get("stage", "")
            base_value = stage_data.get("base_value")
            coin_power = stage_data.get("coin_power")
            attack_capacity = stage_data.get("attack_capacity", 1)

            stage_lines = [f"E.G.O：{title}"]
            stage_lines.append(f"模式：E.G.O觉醒（{stage}阶）")

            if coin_count > 0:
                stage_lines.append(f"硬币数量：{coin_count}")
            if sanity_cost:
                stage_lines.append(f"理智消耗：{sanity_cost}")
            if attack_bonus:
                stage_lines.append(f"攻击加权：+{attack_bonus}")
            if base_value is not None:
                stage_lines.append(f"基础值：{base_value}")
            if coin_power is not None:
                sign = "+" if coin_power > 0 else ""
                stage_lines.append(f"硬币威力：{sign}{coin_power}")
            if attack_capacity > 1:
                stage_lines.append(f"攻击容量：{attack_capacity}")

            effects = stage_data.get("effects", [])
            for eff in effects:
                timing = eff.get("timing", "")
                desc = eff.get("description", "")
                stage_lines.append(f"[{timing}] {desc}")

            stage_meta = dict(base_metadata, **{
                "section": "awakening",
                "mode": "awakening",
                "stage": stage,
                "coin_count": coin_count,
                "sanity_cost": sanity_cost,
            })

            stage_doc = Document(
                page_content="\n".join(stage_lines),
                metadata=stage_meta,
            )
            documents.append(stage_doc)

    # ── 3) 侵蚀阶段块 ──
    erosion = data.get("erosion_stages", [])
    for er_data in erosion:
        coin_count = er_data.get("coin_count", 0)
        sanity_cost = er_data.get("sanity_cost", 0)
        attack_bonus = er_data.get("attack_bonus", 0)

        stages = er_data.get("stages", [])
        for stage_data in stages:
            stage = stage_data.get("stage", "")
            base_value = stage_data.get("base_value")
            coin_power = stage_data.get("coin_power")
            attack_capacity = stage_data.get("attack_capacity", 1)

            stage_lines = [f"E.G.O：{title}"]
            stage_lines.append(f"模式：E.G.O侵蚀（{stage}阶）")

            if coin_count > 0:
                stage_lines.append(f"硬币数量：{coin_count}")
            if sanity_cost:
                stage_lines.append(f"理智消耗：{sanity_cost}")
            if attack_bonus:
                stage_lines.append(f"攻击加权：+{attack_bonus}")
            if base_value is not None:
                stage_lines.append(f"基础值：{base_value}")
            if coin_power is not None:
                sign = "+" if coin_power > 0 else ""
                stage_lines.append(f"硬币威力：{sign}{coin_power}")
            if attack_capacity > 1:
                stage_lines.append(f"攻击容量：{attack_capacity}")

            effects = stage_data.get("effects", [])
            for eff in effects:
                timing = eff.get("timing", "")
                desc = eff.get("description", "")
                stage_lines.append(f"[{timing}] {desc}")

            stage_meta = dict(base_metadata, **{
                "section": "erosion",
                "mode": "erosion",
                "stage": stage,
                "coin_count": coin_count,
                "sanity_cost": sanity_cost,
            })

            stage_doc = Document(
                page_content="\n".join(stage_lines),
                metadata=stage_meta,
            )
            documents.append(stage_doc)

    # ── 4) 被动块 ──
    if data.get("passive_name") or data.get("passive_description"):
        passive_lines = [f"E.G.O：{title}", "被动："]
        if data.get("passive_name"):
            passive_lines.append(f"被动名称：{data['passive_name']}")
        if data.get("passive_description"):
            passive_lines.append(data["passive_description"])

        passive_meta = dict(base_metadata, section="passive")
        passive_doc = Document(
            page_content="\n".join(passive_lines),
            metadata=passive_meta,
        )
        documents.append(passive_doc)

    logger.debug(f"E.G.O {title}: 构建了 {len(documents)} 个 chunk")
    return documents


def build_gift_chunks(data: dict) -> list[Document]:
    """将结构化 E.G.O 饰品数据构建为 Document 列表。

    每条饰品记录已经是单个版本（base/upgraded_2/upgraded_3），
    直接构建一个 chunk。
    """
    documents = []
    gift_name = data.get("gift_name", "") or data.get("title", "")
    stage = data.get("stage", "base")

    base_metadata = {
        "page_type": "ego_gift",
        "page_title": gift_name,
        "gift_name": gift_name,
        "rarity": data.get("rarity", -1),
        "cost": data.get("cost", ""),
        "effect_types": data.get("effect_types", ""),
        "attack_type": data.get("attack_type", ""),
        "location": data.get("location", ""),
        "stage": stage,
        "source": "tabx",
    }

    # 特殊条件/事件来源
    if data.get("special"):
        base_metadata["special"] = data["special"]
    if data.get("event"):
        base_metadata["event"] = data["event"]

    doc = Document(
        page_content=data.get("content", ""),
        metadata=base_metadata,
    )
    documents.append(doc)

    logger.debug(f"E.G.O饰品 {gift_name}[{stage}]: 构建了 1 个 chunk")
    return documents


def build_story_dialogue_chunks(data: dict) -> list[Document]:
    """将故事剧情对话数据构建为 Document 列表。

    策略：按章节分组，同一章节内的对话合并在一个 chunk 中。
    每个章节块内，对话格式为 [角色名]\\n对话内容。

    块大小控制：如果单个章节对话过多（>2000 字符），
    按每 40 行对话进行二次切分。

    Args:
        data: extract_story_dialogue_from_wikitext() 返回的 dict，
              包含 "blocks": [{type, role, text}, ...]
    """
    documents = []
    title = data.get("title", "")
    chapter = data.get("chapter", "") or title

    blocks = data.get("blocks", [])
    if not blocks:
        return documents

    base_metadata = {
        "page_type": "story_dialogue",
        "page_title": title,
        "chapter": chapter,
        "source": "wiki_parsed",
    }

    # 人格剧情关联：透传 extract_story_dialogue_from_wikitext 派生出的人格名，
    # 使 RAG 可按人格名检索其对应剧情（仅人格剧情页面携带该字段）。
    if data.get("personality_name"):
        base_metadata["personality_name"] = data["personality_name"]

    # 按章节分组
    current_section = chapter
    current_lines: list[str] = []
    MAX_CHARS = 2000  # 单块最大字符数
    MAX_LINES = 40    # 单块最大对话行数

    def _flush_section(section_name: str, lines: list[str]):
        """将当前累积的行输出为一个 chunk。"""
        if not lines:
            return
        content = "\n".join(lines)
        meta = dict(base_metadata, section=section_name)
        doc = Document(page_content=content, metadata=meta)
        documents.append(doc)
        lines.clear()

    for block in blocks:
        btype = block.get("type", "")
        role = block.get("role", "")
        text = block.get("text", "")

        if btype == "section":
            # 新章节开始 → 先输出上一章节的累积内容
            _flush_section(current_section, current_lines)
            current_section = block.get("text", current_section)
            continue

        if btype == "dialogue":
            current_lines.append(f"[{role}]\n{text}")
        elif btype == "narration":
            current_lines.append(f"[旁白]\n{text}")
        else:
            continue

        # 检查是否需要二次切分
        if len(current_lines) >= MAX_LINES:
            _flush_section(current_section, current_lines)

    # 输出最后一个章节
    _flush_section(current_section, current_lines)

    if documents:
        logger.debug(f"故事剧情 {title}: 构建了 {len(documents)} 个 chunk（{len(blocks)} 个块）")
    else:
        logger.debug(f"故事剧情 {title}: 无有效对话块")
    return documents


def build_status_effect_chunks(data: dict) -> list[Document]:
    """将状态效果数据构建为单个 Document。

    每个状态效果页面生成一个 chunk，包含：
    - 名称、类型（正面/负面/特殊）
    - 罪孽亲和、关键词、属性
    - 描述（{0}/{1} 已替换为"强度"/"层数（持续回合）"）

    元数据中包含 effect_type、sin_affinity、keywords 等过滤字段。
    """
    documents = []
    name = data.get("name", "") or data.get("title", "")
    effect_type = data.get("effect_type", "")
    sin_affinity = data.get("sin_affinity", "")
    keywords = data.get("keywords", [])
    properties = data.get("properties", [])
    description = data.get("description", "")

    if not description:
        logger.debug(f"状态效果 {name}: 无描述，跳过")
        return documents

    # 构建 content
    lines = [f"状态效果：{name}"]
    if effect_type:
        lines.append(f"类型：{effect_type}")
    if sin_affinity:
        lines.append(f"罪孽亲和：{sin_affinity}")
    if keywords:
        lines.append(f"关键词：{'、'.join(keywords)}")
    if properties:
        lines.append(f"属性：{'、'.join(properties)}")
    lines.append("")
    lines.append(description)

    meta = {
        "page_type": "status_effect",
        "page_title": name,
        "effect_type": effect_type,
        "sin_affinity": sin_affinity,
        "keywords": ",".join(keywords) if keywords else "",
        "source": "wiki_structured",
    }
    if properties:
        meta["properties"] = ",".join(properties)

    doc = Document(
        page_content="\n".join(lines),
        metadata=meta,
    )
    documents.append(doc)

    logger.debug(f"状态效果 {name}: 构建了 1 个 chunk")
    return documents


def build_story_note_chunks(data: dict) -> list[Document]:
    """将但丁笔记数据构建为 Document 列表。

    每条记录一个 chunk，格式：
    [分类路径：A > B > C]
    记录 #N YYYY.MM.DD
    记录内容

    Args:
        data: extract_story_note_from_html() 返回的 dict，
              包含 "entries": [{title, text, path}, ...]
    """
    documents = []
    title = data.get("title", "")
    entries = data.get("entries", [])
    if not entries:
        return documents

    base_metadata = {
        "page_type": "story_note",
        "page_title": title,
        "source": "wiki_js_rendered",
    }

    for entry in entries:
        entry_title = entry.get("title", "")
        entry_text = entry.get("text", "")
        path = entry.get("path", [])

        if not entry_text:
            continue

        # 构建分类路径行
        path_str = " > ".join(path) if path else entry_title
        content_lines = [f"[{path_str}]"]
        content_lines.append(entry_title)
        content_lines.append("")
        content_lines.append(entry_text)

        # metadata：提取记录编号
        meta = dict(base_metadata)
        if path:
            meta["category_path"] = path_str
        # 尝试提取记录编号（如 "记录 #1"）
        record_match = re.search(r'记录\s*#(\d+)', entry_title)
        if record_match:
            meta["record_number"] = int(record_match.group(1))

        doc = Document(
            page_content="\n".join(content_lines),
            metadata=meta,
        )
        documents.append(doc)

    logger.debug(f"但丁笔记 {title}: 构建了 {len(documents)} 个 chunk")
    return documents


def build_knowledge_chunks(data: dict) -> list[Document]:
    """将机制说明页面构建为 Document 列表。

    data 结构（来自 extract_knowledge_from_html）：
        {
            "page_type": "knowledge",
            "title": "基础数值",
            "categories": [...],
            "text": "纯文本内容...",
            "_structured": True,
        }
    """
    documents: list[Document] = []
    title = data.get("title", "")
    text = data.get("text", "").strip()
    categories = data.get("categories", [])

    if not text:
        return documents

    base_metadata = {
        "page_type": "knowledge",
        "page_title": title,
        "source": "wiki_mechanism",
        "title": title,
        "categories": ", ".join(categories) if categories else "",
    }

    doc = Document(
        page_content=f"# {title}\n\n{text}",
        metadata=base_metadata,
    )
    documents.append(doc)

    logger.debug(f"机制页面 {title}: 构建了 1 个 chunk（{len(text)} 字符）")
    return documents


def build_enemy_chunks(data: dict) -> list[Document]:
    """将敌方单位数据构建为 Document 列表。

    data 结构（来自 _enemy_list_to_dict）：
        {
            "page_type": "enemy",
            "title": "主线战斗1-10",
            "enemy_name": "L.C.B.罪人",
            "battle_stage": "主线战斗1-10",
            "hp": 41,
            ...
            "skills": [...],
            "_structured": True,
        }
    每个敌人构建一个独立 Document，技能以文本块列出。
    """
    documents: list[Document] = []
    title = data.get("title", "")
    enemies: list[dict] = data.get("enemies", [])

    if not enemies:
        logger.debug(f"敌人页面 {title}: 无敌人数据")
        return documents

    for enemy in enemies:
        enemy_name = enemy.get("enemy_name", "未知敌人")
        body_part = enemy.get("body_part", "")
        battle_stage = enemy.get("battle_stage", title)
        hp = enemy.get("hp", 0)
        def_level = enemy.get("defense_level", 0)
        speed_min = enemy.get("speed_min", 0)
        speed_max = enemy.get("speed_max", 0)
        chaos_threshold = enemy.get("chaos_threshold", "")
        physical_resistances = enemy.get("physical_resistances", {})
        sin_resistances = enemy.get("sin_resistances", {})
        panic_types = enemy.get("panic_types", [])
        passives = enemy.get("passives", [])
        skills: list[dict] = enemy.get("skills", [])

        content_lines = [f"# {enemy_name}"]
        if body_part:
            content_lines.append(f"部位: {body_part}")
        content_lines.append(f"来源: {battle_stage}")
        # 修复②：跨关卡合并的单位展示出现关卡列表
        appear_stages = enemy.get("appear_stages") or []
        if len(appear_stages) > 1:
            content_lines.append(f"出现关卡: {'、'.join(str(s) for s in appear_stages)}")
        content_lines.append("")

        # 基础数值
        content_lines.append("## 基础数值")
        content_lines.append(f"- 生命值 (HP): {hp}")
        content_lines.append(f"- 防御等级: {def_level}")
        content_lines.append(f"- 速度范围: {speed_min}-{speed_max}")
        if chaos_threshold:
            content_lines.append(f"- 混乱阈值: {chaos_threshold}")
        content_lines.append("")

        # 抗性
        if physical_resistances:
            content_lines.append("## 物理抗性")
            for k, v in physical_resistances.items():
                content_lines.append(f"- {k}: ×{v}")
            content_lines.append("")
        if sin_resistances:
            content_lines.append("## 罪孽抗性")
            for k, v in sin_resistances.items():
                content_lines.append(f"- {k}: ×{v}")
            content_lines.append("")

        # 恐慌类型（修复①：保序去重 + 过滤罪孽名/通用 buff，避免重复与污染项撑爆 chunk）
        if panic_types:
            content_lines.append("## 恐慌类型")
            seen_panics = []
            for pt in panic_types:
                pt_text = str(pt).strip()
                if not pt_text or pt_text in seen_panics:
                    continue
                if pt_text in _SIN_AFFINITY_NAMES or pt_text in _NON_PANIC_BUFF_NAMES:
                    continue  # 罪孽属性名/通用 buff 不是恐慌，丢弃
                seen_panics.append(pt_text)
            for pt in seen_panics:
                content_lines.append(f"- {pt}")
            content_lines.append("")

        # 被动
        if passives:
            content_lines.append("## 被动能力")
            for ps in passives:
                content_lines.append(f"- {ps}")
            content_lines.append("")

        # 技能
        if skills:
            content_lines.append("## 技能列表")
            for i, skill in enumerate(skills, 1):
                skill_name = skill.get("skill_name", f"技能{i}")
                sin_type = skill.get("sin_type", "")
                damage_type = skill.get("damage_type", "")
                base_value = skill.get("base_value", 0)
                coin_power = skill.get("coin_power", 0)
                coin_count = skill.get("coin_count", 0)
                atk_weight = skill.get("attack_weight", 1)
                coin_effects = skill.get("coin_effects", [])
                skill_lines = [
                    f"### 技能{i}: {skill_name}",
                    f"- 罪孽属性: {sin_type}",
                    f"- 伤害类型: {damage_type}",
                    f"- 基础值: {base_value}",
                    f"- 硬币威力: +{coin_power}",
                    f"- 硬币数量: {coin_count}",
                    f"- 攻击容量: {atk_weight}",
                ]
                # 修复③：显示重要性（>0 时为特殊技能）
                importance = skill.get("importance") or 0
                try:
                    importance = int(importance)
                except (TypeError, ValueError):
                    importance = 0
                if importance > 0:
                    skill_lines.append(f"- 重要性: {importance}")
                if skill.get("is_guard"):
                    skill_lines.append(f"- 类型: 守备 ({skill.get('guard_type', '')})")
                if coin_effects:
                    skill_lines.append("- 硬币效果:")
                    for ce in coin_effects:
                        skill_lines.append(f"  * {ce}")
                content_lines.extend(skill_lines)
                content_lines.append("")

        meta = {
            "page_type": "enemy",
            "page_title": title,
            "source": "wiki_battle_data",
            "title": title,
            "enemy_name": enemy_name,
            "battle_stage": battle_stage,
            "hp": hp,
            "defense_level": def_level,
        }

        doc = Document(
            page_content="\n".join(content_lines),
            metadata=meta,
        )
        documents.append(doc)

    logger.debug(f"敌人页面 {title}: 构建了 {len(documents)} 个 chunk")
    return documents


def build_event_chunks(data: dict) -> list[Document]:
    """将探索事件数据构建为 Document 列表。

    data 结构（来自 _event_to_dict）：
        {
            "page_type": "event",
            "title": "事件-膏血",
            "event_name": "膏血",
            "narration": "...",
            "options": [
                {
                    "choice_text": "...",
                    "check_type": "有利判定",
                    "check_sin": "暴食",
                    "check_threshold": 14,
                    "success_outcomes": [...],
                    "failure_outcomes": [...],
                }
            ],
            "ego_gifts": [...],
            "related_abnormalities": [...],
            "trigger_location": "...",
            "_structured": True,
        }
    """
    documents: list[Document] = []
    title = data.get("title", "")
    event_name = data.get("event_name", title)
    narration = data.get("narration", "").strip()
    options: list[dict] = data.get("options", [])
    ego_gifts: list[dict] = data.get("ego_gifts", [])
    related_abnormalities: list[str] = data.get("related_abnormalities", [])
    trigger_location = data.get("trigger_location", "")

    content_lines = [f"# {event_name}"]
    if trigger_location:
        content_lines.append(f"触发地点: {trigger_location}")
    content_lines.append("")

    if narration:
        content_lines.append("## 事件描述")
        content_lines.append(narration)
        content_lines.append("")

    if options:
        content_lines.append("## 选项与判定")
        for i, opt in enumerate(options, 1):
            choice = opt.get("choice_text", f"选项{i}")
            check_type = opt.get("check_type", "")
            check_sin = opt.get("check_sin", "")
            check_threshold = opt.get("check_threshold", 0)
            success = opt.get("success_outcomes", [])
            failure = opt.get("failure_outcomes", [])

            content_lines.append(f"### 选项{i}: {choice}")
            if check_type:
                check_desc = f"{check_type}"
                if check_sin:
                    check_desc += f" | 罪孽: {check_sin}"
                if check_threshold:
                    check_desc += f" | 阈值: {check_threshold}"
                content_lines.append(f"判定: {check_desc}")

            if success:
                content_lines.append("成功结果:")
                for s in success:
                    content_lines.append(f"  - {s}")
            if failure:
                content_lines.append("失败结果:")
                for f in failure:
                    content_lines.append(f"  - {f}")
            content_lines.append("")

    if ego_gifts:
        content_lines.append("## E.G.O饰品")
        for gift in ego_gifts:
            gift_name = gift.get("name", "未知饰品")
            gift_effect = gift.get("effect", "")
            content_lines.append(f"- **{gift_name}**: {gift_effect}")
        content_lines.append("")

    if related_abnormalities:
        content_lines.append("## 关联异想体")
        for ab in related_abnormalities:
            content_lines.append(f"- {ab}")
        content_lines.append("")

    meta = {
        "page_type": "event",
        "page_title": title,
        "source": "wiki_event",
        "title": title,
        "event_name": event_name,
        "option_count": len(options),
        "gift_count": len(ego_gifts),
    }

    doc = Document(
        page_content="\n".join(content_lines),
        metadata=meta,
    )
    documents.append(doc)

    logger.debug(f"事件页面 {title}: 构建了 1 个 chunk")
    return documents


def build_structured_chunks(data: dict) -> list[Document]:
    """公共入口：根据 page_type 选择分块构建器。

    Args:
        data: html_extractor.extract_from_html() 返回的 dict，
              必须包含 "page_type" 和 "_structured": True。

    Returns:
        LangChain Document 列表。如果 page_type 未知，返回空列表。
    """
    if not data.get("_structured"):
        logger.warning("非结构化数据传入 build_structured_chunks，将返回空列表")
        return []

    page_type = data.get("page_type", "")

    try:
        if page_type == "personality":
            return build_personality_chunks(data)
        elif page_type == "ego":
            return build_ego_chunks(data)
        elif page_type == "ego_gift":
            return build_gift_chunks(data)
        elif page_type == "story_dialogue":
            return build_story_dialogue_chunks(data)
        elif page_type == "story_note":
            return build_story_note_chunks(data)
        elif page_type == "status_effect":
            return build_status_effect_chunks(data)
        elif page_type == "knowledge":
            return build_knowledge_chunks(data)
        elif page_type == "enemy":
            return build_enemy_chunks(data)
        elif page_type == "event":
            return build_event_chunks(data)
        else:
            logger.warning(f"未知的结构化 page_type: {page_type}")
            return []
    except Exception as e:
        logger.error(f"构建结构化分块失败 [{data.get('title', '?')}]: {e}", exc_info=True)
        return []
