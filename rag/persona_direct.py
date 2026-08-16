# -*- coding: utf-8 -*-
"""
人格结构化直答模块（Persona Direct Answer 运行时）。

绕开向量检索，直接从 ``data/structured/persona_*.json`` 精确取数，
按完整规范格式化输出（确定性、不经过 LLM、无幻觉），根治
"人格技能 chunk 过多 → 检索错漏"的问题。

组成：
- ``PersonaDirectStore``  运行时索引（懒加载扫描 data/structured 目录）
- ``format_persona_full`` 完整规范格式化（基本信息 + 技能四阶段/最高阶 + 硬币效果
                          + 被动 + 语音），含特殊技能标注（强化/衍生/跳号/守备）
- ``try_direct_answer``   查询入口：命中具体人格名 → 直答文本；否则 None（回落 RAG）

依赖：``rag.query_processor`` 的 ``classify_intent`` / ``extract_personality_name``
（查询侧已能锁定精确人格标题，含 NICKNAME_MAP 昵称 / LCB 罪人 / 大小写变体）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from crawler.structured_exporter import DEFAULT_OUT_DIR, load_persona_index
from crawler.passives_data import load_passives_index
from rag.query_processor import (
    LCB_SINNERS,
    classify_intent,
    extract_personality_name,
)

logger = logging.getLogger(__name__)

# 占位符技能名（解析器正则缺陷导致，重爬后由真实技能名替代）
_PLACEHOLDER_RE = re.compile(r"^(技能|守备技能)\d+$")
# 纯数值/标签占位的硬币效果
_TAG_DESCS = {"技能一", "技能二", "技能三", "技能四", "守备技能", "守备技能4"}

_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九"]

# 人格模糊解析噪声词：查询中不属于人格标题的泛化词汇（用于 "罪人名+残词" 解析）。
# 仅剥离明确非标题词汇；不包含单字"有/哪/哪些"等，避免误删标题片段
# （若残词因此无法匹配，会自然回落 RAG，安全）。
_FUZZY_NOISE_RE = re.compile(
    r"(人格|数据|技能|介绍|是什么|信息|资料|属性|展示|查询|看看|请告诉我|"
    r"给我|现在|最新|的|？|\?)"
)


def _cn(n: int) -> str:
    """1 -> 一, 2 -> 二 ...（超出九则回退数字）"""
    return _CN_NUM[n - 1] if 1 <= n <= len(_CN_NUM) else str(n)


def extract_skill_name(skill: dict) -> str:
    """从 coin_effects 提取真实技能名（占位符 skill_name 不可用时的兜底）。

    逻辑：找到 ``技能一/技能二/...`` 标签后的第一个非空描述作为技能名。
    """
    ces = skill.get("coin_effects") or []
    for i, ce in enumerate(ces):
        d = (ce.get("description") or "").strip()
        if d not in _TAG_DESCS:
            continue
        for j in range(i + 1, min(i + 4, len(ces))):
            nxt = (ces[j].get("description") or "").strip()
            if not nxt:
                continue
            m = re.match(r"^(.*?)(?:\s+[0-9]+)?$", nxt)
            name = m.group(1).strip()
            if name and name not in _TAG_DESCS:
                return name
    return ""


def resolve_skill_name(skill: dict, idx: int) -> str:
    """优先真实 skill_name；占位符则从 coin_effects 提取；再兜底 技能N。"""
    sn = (skill.get("skill_name") or "").strip()
    if sn and not _PLACEHOLDER_RE.match(sn):
        return sn
    return extract_skill_name(skill) or (sn or f"技能{idx + 1}")


def is_guard(skill: dict) -> bool:
    """判定守备技能（guard_type 或 coin_effects 含"守备技能"标签）。"""
    if skill.get("guard_type"):
        return True
    return any(
        (c.get("description") or "").strip() == "守备技能"
        for c in (skill.get("coin_effects") or [])
    )


def _skill_header(skill: dict, idx: int) -> str:
    """生成技能标题，含特殊技能标注。

    依据 wikitext_key 识别：
    - ``强化N技能``  → 【强化技能N】（标注"强化N技能"）
    - ``N技能M``     → 【技能N衍生M】（子变体）
    - ``5技能`` 等跳号 → 【技能N】
    - 守备技能        → 【守备技能】
    - 无 key → 按 idx 推断
    """
    key = skill.get("wikitext_key") or ""
    guard = is_guard(skill)
    sname = resolve_skill_name(skill, idx)

    # 强化形态：强化N技能
    m = re.match(r"强化(\d+)技能", key)
    if m:
        return f"【强化技能{_cn(int(m.group(1)))}】{sname}（{key}）"

    # 守备技能
    if guard or key.startswith("守备"):
        return f"【守备技能】{sname}"

    # 子变体：N技能M
    m = re.match(r"(\d+)技能(\d+)", key)
    if m:
        return f"【技能{_cn(int(m.group(1)))}衍生】{sname}（{key}）"

    # 跳号/普通：N技能
    m = re.match(r"(\d+)技能", key)
    if m:
        return f"【技能{_cn(int(m.group(1)))}】{sname}"

    # 无 wikitext_key：按 idx 推断
    return f"【{'守备技能' if guard else '技能' + _cn(idx + 1)}】{sname}"


def _format_stage(line: str, st: dict) -> str:
    """格式化单个阶段（基础值 + 变动值 + 效果）。负硬币威力保留负号。"""
    stage = st.get("stage", "")
    bv = st.get("base_value")
    cp = st.get("coin_power")
    out = f"{stage}阶：基础值 {bv if bv is not None else '—'}"
    if cp is not None:
        sign = "+" if cp > 0 else ""
        out += f" | 变动值 {sign}{cp}"
    effs = st.get("effects") or []
    if effs:
        parts = []
        for e in effs:
            timing = e.get("timing", "")
            desc = e.get("description", "")
            if desc:
                parts.append(f"[{timing}] {desc}" if timing else f"{desc}")
        if parts:
            out += " | " + "；".join(parts)
    return out


def _format_skill(lines: list, skill: dict, idx: int):
    """格式化单个技能（标题 + 类型 + 容量/硬币 + 四阶段 + 硬币效果）。"""
    header = _skill_header(skill, idx)
    lines.append(header)

    sin_type = skill.get("sin_type", "") or ""
    dmg_type = skill.get("damage_type", "") or ""
    guard = is_guard(skill)
    if guard:
        lines.append("[守备技能]")
    elif sin_type or dmg_type:
        lines.append(f"[{sin_type}] [{dmg_type}]")

    coins = skill.get("coin_count", 0)
    cap = skill.get("attack_capacity", 1)
    lines.append(f"攻击容量：{cap}")
    lines.append(f"硬币：{coins}")
    lines.append("── 阶段（默认四阶段/最高阶）──")

    # 默认展示最高阶段（优先 IV 阶；无 IV 则取该组最高阶）
    groups = skill.get("stage_groups") or []
    main_group = groups[0] if groups else []
    target = None
    for st in main_group:
        if (st.get("stage") or "") == "IV":
            target = st
            break
    if target is None and main_group:
        target = main_group[-1]
    if target is not None:
        lines.append(_format_stage("", target))
    else:
        lines.append("（无阶段数据）")

    # 强化阶段（groups[1:]）默认折叠为一行提示，避免信息丢失又不混入正文
    if len(groups) > 1:
        lines.append("（含强化阶段，如需可单独询问）")

    # 硬币效果：按阶段分组标注（方案1，详见 _format_coin_effects）
    _format_coin_effects(lines, skill)


_PASSIVE_ID_RE = re.compile(r"^人格被动\s*(\d+)\s*$")

# 被动映射表懒加载缓存（{id: {name, where, desc1, desc2, ...}}），
# 由 crawler/passives_data.load_passives_index 读取 data/structured/passives.json。
# 显式初始化为 None，避免与 load_passives_index 内部缓存混淆。
_PASSIVE_MAP_CACHE: Optional[dict] = None


def _get_passive_map() -> dict:
    """懒加载被动映射表；缺失/损坏返回空 dict（不抛异常）。"""
    global _PASSIVE_MAP_CACHE
    if _PASSIVE_MAP_CACHE is None:
        _PASSIVE_MAP_CACHE = load_passives_index()
    return _PASSIVE_MAP_CACHE


def _lookup_passive(pid: str) -> Optional[dict]:
    """按被动 ID 查映射表，返回 {name, where, desc} 精简信息；未命中返回 None。"""
    info = _get_passive_map().get(pid)
    if not info or not isinstance(info, dict):
        return None
    name = (info.get("name") or "").strip()
    desc = (info.get("desc1") or "").strip()
    where = (info.get("where") or "").strip()
    if not name and not desc:
        return None
    return {"name": name, "desc": desc, "where": where}


def _format_passive_item(item) -> str:
    """单个被动项 → 展示文本。

    优先级：
    1. dict 形态（含 passive_name/description）：优先展示名称/描述
    2. ``人格被动{ID}`` 占位：查被动映射表（data/structured/passives.json）
       组合输出 ``名称：描述``；映射缺失时输出引用说明（不虚构名称/描述）
    3. 其它字符串：原样输出
    """
    if isinstance(item, dict):
        name = (item.get("passive_name") or item.get("name") or "").strip()
        desc = (item.get("passive_desc") or item.get("description") or "").strip()
        if name and desc:
            return f"{name}：{desc}"
        if name:
            return name
        if desc:
            return desc
        pid = item.get("passive_id") or item.get("id") or ""
        if pid:
            return f"人格被动 {pid}（详见人格被动页面）"
        return str(item)
    s = str(item).strip()
    if not s:
        return ""
    m = _PASSIVE_ID_RE.match(s)
    if m:
        pid = m.group(1)
        info = _lookup_passive(pid)
        if info:
            if info["name"] and info["desc"]:
                return f"{info['name']}：{info['desc']}"
            if info["name"]:
                return info["name"]
            if info["desc"]:
                return info["desc"]
        return f"人格被动 {pid}（详见人格被动页面）"
    return s


def _coin_desc_ok(ce: dict) -> bool:
    """跳过空/纯数值/标签占位的硬币描述。"""
    desc = (ce.get("description") or "").strip()
    if not desc or re.fullmatch(r"[+-]?[0-9]+", desc) or desc in _TAG_DESCS:
        return False
    return True


def _coin_desc(ce: dict) -> str:
    """硬币描述：description 已含 "[timing]" 前缀则直接用，否则补前缀。"""
    desc = (ce.get("description") or "").strip()
    timing = ce.get("timing", "")
    bracket = f"[{timing}]"
    if desc.startswith(bracket):
        return desc
    return f"{bracket} {desc}" if timing else desc


def _format_coin_effects_fallback(lines: list, skill: dict):
    """硬币效果兜底：按 coin_index 合并去重（数据源无 stage，无法精确分阶段）。"""
    ces = skill.get("coin_effects") or []
    by_coin: dict[int, list[str]] = {}
    order: list[int] = []
    for ce in ces:
        ci = ce.get("coin_index") or 0
        if ci <= 0 or not _coin_desc_ok(ce):
            continue
        d = _coin_desc(ce)
        if ci not in by_coin:
            by_coin[ci] = []
            order.append(ci)
        by_coin[ci].append(d)
    coin_lines = []
    for ci in order:
        descs = list(dict.fromkeys(by_coin[ci]))
        if len(descs) == 1:
            coin_lines.append(f"硬币{ci}：{descs[0]}")
        else:
            coin_lines.append(f"硬币{ci}：" + "；".join(descs))
    if coin_lines:
        lines.append("── 硬币效果（数据源未区分阶段，已按硬币合并）──")
        lines.extend(coin_lines)


def _format_coin_effects(lines: list, skill: dict):
    """硬币效果按阶段分组标注（方案1）。

    ``coin_effects`` 是无 stage 字段的扁平列表（数据源限制），但顺序上按
    “每阶段一组 coin1..coinN” 排列，可结合 ``stage_groups[0]`` 的阶段数切分，
    从而为每组标注所属阶段（[I]/[II]/[III]/[IV]），避免同硬币多阶段描述混列。
    切分校验失败则退化为 :func:`_format_coin_effects_fallback`（不丢信息）。
    强化形态的硬币效果不展开（随强化阶段折叠提示一并省略）。
    """
    ces = skill.get("coin_effects") or []
    groups = skill.get("stage_groups") or []
    stages = groups[0] if groups else []
    n_stages = len(stages)
    if n_stages <= 0:
        _format_coin_effects_fallback(lines, skill)
        return

    # 剥离头标签（普通形态中 coin_index==0 均为技能名/数值标签）
    records = [c for c in ces if (c.get("coin_index") or 0) > 0 and _coin_desc_ok(c)]

    # 按顺序切分阶段组：coin_index 降序/相等跳变 → 进入下一阶段
    split: list[list[dict]] = []
    cur: list[dict] = []
    prev_ci: Optional[int] = None
    for c in records:
        ci = c.get("coin_index") or 0
        if prev_ci is not None and ci <= prev_ci:
            if cur:
                split.append(cur)
            cur = []
        cur.append(c)
        prev_ci = ci
    if cur:
        split.append(cur)

    # 只取普通形态的前 n_stages 组（强化形态的硬币记录随折叠提示省略）
    stage_grps = split[:n_stages]

    # 校验：组数需与阶段数一致，且每组覆盖到合法硬币
    valid = len(stage_grps) == n_stages and all(
        any(_coin_desc_ok(c) for c in grp) for grp in stage_grps
    )
    if not valid:
        _format_coin_effects_fallback(lines, skill)
        return

    coin_lines = []
    for idx, grp in enumerate(stage_grps):
        stage_name = (stages[idx].get("stage") or "").strip()
        label = f"[{stage_name}]" if stage_name else f"[{idx + 1}]"
        by_coin: dict[int, list[str]] = {}
        for c in grp:
            ci = c.get("coin_index") or 0
            by_coin.setdefault(ci, []).append(_coin_desc(c))
        for ci in sorted(by_coin):
            descs = list(dict.fromkeys(by_coin[ci]))
            if len(descs) == 1:
                coin_lines.append(f"{label} 硬币{ci}：{descs[0]}")
            else:
                coin_lines.append(f"{label} 硬币{ci}：" + "；".join(descs))
    if coin_lines:
        lines.append("── 硬币效果（普通形态，按阶段）──")
        lines.extend(coin_lines)


def _format_passives(lines: list, record: dict):
    """格式化被动：战斗被动 / 支援被动（兼容字符串与列表两种形态）。

    数据源当前将全部被动写入 ``battle_passive``、``support_passive`` 恒空，
    因此借助被动映射表（data/structured/passives.json）的 ``where`` 字段，
    将“支援”被动从战斗组分离到“支援：”分组；映射缺失时归入战斗组兜底。
    对 ``人格被动{ID}`` 占位输出引用说明（不虚构名称/描述）；
    若数据升级为带 passive_name/description 的 dict 则直接展示。
    """
    lines.append("被动技能：")
    bp = record.get("battle_passive") or ""
    sp = record.get("support_passive") or ""

    def _iter_items(value):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and "\n" in item:
                    for sub in item.split("\n"):
                        yield sub
                else:
                    yield item
        elif isinstance(value, str):
            for sub in value.split("\n"):
                yield sub
        else:
            yield value

    def _where_of(item) -> str:
        """判断被动项归属分组：dict 优先看 where 字段，占位字符串查映射表。"""
        if isinstance(item, dict):
            return (item.get("where") or "").strip()
        m = _PASSIVE_ID_RE.match(str(item).strip())
        if m:
            info = _lookup_passive(m.group(1))
            if info:
                return info["where"]
        return ""

    battle_items: list = []
    support_items: list = []

    def _collect(value, forced_support: bool):
        for item in _iter_items(value):
            if isinstance(item, str) and not item.strip():
                continue
            if forced_support:
                support_items.append(item)
            elif "支援" in _where_of(item):
                support_items.append(item)
            else:
                battle_items.append(item)

    # 原 battle_passive 按映射表 where 分流；support_passive 语义上恒为支援
    _collect(bp, forced_support=False)
    _collect(sp, forced_support=True)

    def _dump(tag: str, items: list):
        if not items:
            return
        lines.append(tag)
        for item in items:
            rendered = _format_passive_item(item)
            if rendered:
                lines.append(f"- {rendered}")

    _dump("战斗：", battle_items)
    _dump("支援：", support_items)
    lines.append("")


def _format_voices(lines: list, record: dict):
    """格式化语音台词与技能语音。"""
    vl = record.get("voice_lines") or []
    sv = record.get("skill_voice") or []
    if vl:
        lines.append("语音台词：")
        for v in vl[:20]:
            title = v.get("title") or ""
            text = v.get("text") or ""
            f = v.get("file") or ""
            if title:
                lines.append(f"- [{title}] {text}")
            elif text:
                lines.append(f"- {text}")
            elif f:
                lines.append(f"- {f}")
        lines.append("")
    if sv:
        lines.append("技能语音：")
        for v in sv[:20]:
            sname = v.get("skill_name") or v.get("skill_label") or ""
            f = v.get("file") or ""
            if sname and f:
                lines.append(f"- {sname}（{f}）")
            elif sname:
                lines.append(f"- {sname}")
            elif f:
                lines.append(f"- {f}")
        lines.append("")


def format_persona_full(record: dict) -> str:
    """按完整规范格式化单个人格记录（确定性文本，不经过 LLM）。

    包含：基本信息（罪人/罪孽亲和/物理抗性/E.G.O资源/实装日期/获取方式）
    + 技能（标题/类型/容量/硬币/四阶段/硬币效果）+ 被动 + 语音。
    缺失字段留空（对齐既有规范 diag_output_persona_skills_final.py）。
    """
    lines: list[str] = []
    pname = record.get("personality_name") or ""
    title = record.get("title") or pname or ""
    lines.append(f"（人格名）{pname or title}")

    sinner = record.get("sinner") or ""
    if sinner:
        lines.append(f"罪人：{sinner}")

    sa = record.get("sin_affinities") or {}
    if sa:
        lines.append("罪孽亲和：" + " ".join(f"{k}{v}" for k, v in sa.items()))

    pr = record.get("physical_resistances") or {}
    parts = [f"{k}抗性：{v}" for k, v in pr.items() if k in ("斩击", "突刺", "打击")]
    if parts:
        lines.append("物理抗性：" + " ".join(parts))

    er = record.get("ego_resources") or {}
    if er:
        lines.append("E.G.O资源：" + " ".join(f"{k}{v}" for k, v in er.items()))

    rel = record.get("release_date") or ""
    acq = record.get("acquisition") or ""
    if rel:
        lines.append(f"实装日期：{rel}")
    if acq:
        lines.append(f"获取方式：{acq}")

    lines.append("（技能默认展示四阶段/最高阶；含强化阶段时另行标注）")
    lines.append("")

    skills = record.get("skills") or []
    for idx, skill in enumerate(skills):
        if isinstance(skill, dict):
            _format_skill(lines, skill, idx)
            lines.append("")

    _format_passives(lines, record)
    _format_voices(lines, record)

    return "\n".join(lines).strip()


class PersonaDirectStore:
    """运行时结构化人格索引（懒加载 data/structured 目录）。

    用法（agent/core.py）：
        self.persona_direct = PersonaDirectStore(
            data_dir=cfg.get("data_dir", "data/structured"),
            enabled=cfg.get("enabled", True),
        )
        direct = self.persona_direct.try_direct_answer(msg.text)
    """

    def __init__(self, data_dir: str = DEFAULT_OUT_DIR, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self._index: Optional[dict[str, dict]] = None

    def _ensure_index(self) -> dict[str, dict]:
        """懒加载：首次访问时扫描目录建立 {title: record} 索引。"""
        if self._index is None:
            self._index = load_persona_index(self.data_dir)
            if not self._index:
                logger.warning(
                    f"结构化人格目录为空（{self.data_dir}），直答将自动失效并回落 RAG"
                )
        return self._index

    def reload(self):
        """重载索引（爬虫重建 data/structured 后调用）。"""
        self._index = None
        self._ensure_index()

    def has_persona(self, title: str) -> bool:
        return title in self._ensure_index()

    def get_persona(self, title: str) -> Optional[dict]:
        return self._ensure_index().get(title)

    def search(self, name_like: str) -> list[str]:
        """前缀/包含模糊匹配（用于提示，非精确路径）。"""
        idx = self._ensure_index()
        hits = [t for t in idx if name_like in t or t in name_like]
        return sorted(hits)

    def _resolve_title_fuzzy(self, query: str) -> Optional[str]:
        """罪人名 + 残词 → 唯一人格标题的通用模糊解析。

        解决精确人格名提取失败、但查询明确含「罪人名 + 描述性残词」的情况，
        如：
            "希斯克利夫狐雨的数据"        → 希斯克利夫脑叶公司E.G.O::狐雨
            "希斯克利夫W公司4级清扫人员的数据" → 希斯克利夫W公司4级清扫人员-CCA

        逻辑：
        1. 找出查询中的罪人名（LCB_SINNERS 长名优先）
        2. 剥离罪人名与噪声词，得到残词片段
        3. 在索引中找「标题以罪人名开头」且「标题含残词片段」的候选
        4. 仅当唯一候选时才返回（多候选不猜，回落 RAG 防误判）
        """
        if not query:
            return None
        # 1) 定位罪人名
        sinner = None
        for s in sorted(LCB_SINNERS, key=len, reverse=True):
            if s in query:
                sinner = s
                break
        if not sinner:
            return None

        # 2) 剥离罪人名 + 噪声词 → 残词
        residue = query.replace(sinner, "", 1)
        residue = _FUZZY_NOISE_RE.sub("", residue).strip()
        if not residue:
            return None

        # 3) 候选：标题以罪人名开头，且标题含残词
        idx = self._ensure_index()
        candidates = []
        for t in idx:
            if not t.startswith(sinner):
                continue
            if residue in t:
                candidates.append(t)

        # 4) 唯一候选才返回；「残词含非汉字符号」时优先宽松匹配
        if len(candidates) == 1:
            logger.info(f"人格名模糊解析: '{query}' → '{candidates[0]}'")
            return candidates[0]
        if len(candidates) > 1:
            logger.debug(f"人格名模糊解析多候选（{candidates}），回落 RAG: {query}")
        return None

    def try_direct_answer(self, query: str) -> Optional[str]:
        """直答入口。
        1. 非启用 / 空查询 → None
        2. 穷举/列表查询（如"有哪些人格"）→ None（避免误触发）
        3. extract_personality_name 锁定具体人格标题 → 索引精确取数 → 完整格式化
        4. 精确解析失败 → 罪人名+残词 模糊解析（唯一标题）→ 完整格式化
        5. 仍未命中 → None（回落 RAG）
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        # 穷举/列表查询不直答（"有哪些人格/谁的技能最强" 等泛指）
        try:
            intent = classify_intent(q)
            if intent.get("is_listing"):
                logger.debug(f"直答跳过（列表查询）: {q[:30]}")
                return None
        except Exception as e:
            logger.warning(f"classify_intent 异常，继续直答尝试: {e}")

        title = extract_personality_name(q)
        if not title:
            title = self._resolve_title_fuzzy(q)
        if not title:
            logger.debug(f"直答未命中人格名，回落 RAG: {q[:30]}")
            return None

        record = self.get_persona(title)
        if record is None:
            # 兜底：小写归一化（索引 key 为正式标题，正常不会走到这里）
            lower_map = {k.lower(): v for k, v in self._ensure_index().items()}
            record = lower_map.get(title.lower())
        if record is None:
            logger.debug(f"直答命中标题但索引缺失，回落 RAG: {title}")
            return None

        logger.info(f"人格直答命中: {title}（len={len(record.get('skills') or [])} 技能）")
        return format_persona_full(record)

    # ── 比较直答（改进计划 P1：compare 意图）──

    def try_compare_answer(self, query: str) -> Optional[str]:
        """比较型直答：解析查询中的两个人格并并排输出关键数据。

        查询格式：含「和/与/vs/对比/比较/跟」连接的两侧各为一个人格
        （支持昵称/正式标题，如「兔浮和W浮谁更强」）。
        任一测未识别或索引缺失 → None（回落 RAG）。
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        parts = re.split(r"(?:和|与|vs|VS|对比|比较|跟|、)", q, maxsplit=1)
        if len(parts) < 2:
            return None
        left = extract_personality_name(parts[0].strip())
        right = extract_personality_name(parts[1].strip())
        if not left or not right or left == right:
            logger.debug(f"比较直答跳过（未识别双人格）: {q[:30]}")
            return None

        rec_l = self.get_persona(left)
        rec_r = self.get_persona(right)
        if rec_l is None or rec_r is None:
            logger.debug(f"比较直答跳过（索引缺失）: {left} / {right}")
            return None

        logger.info(f"人格比较直答命中: {left} ↔ {right}")
        return format_persona_compare(rec_l, rec_r)


def format_persona_compare(rec_a: dict, rec_b: dict) -> str:
    """并排输出两个人格的关键数据（抗性/资源/技能数/被动摘要）。"""
    def _brief(rec: dict) -> dict:
        pname = rec.get("personality_name") or rec.get("title") or "?"
        sinner = rec.get("sinner") or ""
        sa = rec.get("sin_affinities") or {}
        pr = rec.get("physical_resistances") or {}
        skills = rec.get("skills") or []
        bp = (rec.get("battle_passive") or "").strip()
        return {
            "name": pname, "sinner": sinner, "sa": sa, "pr": pr,
            "n_skills": len(skills),
            "passive": bp.splitlines()[0][:30] if bp else "（无战斗被动）",
            "release": rec.get("release_date") or "",
        }

    a, b = _brief(rec_a), _brief(rec_b)
    lines = [f"【人格比较】{a['name']}  vs  {b['name']}"]
    lines.append(f"罪人：{a['sinner'] or '—'}  vs  {b['sinner'] or '—'}")
    lines.append("物理抗性：")
    for k in ("斩击", "突刺", "打击"):
        av = a["pr"].get(k, "—")
        bv = b["pr"].get(k, "—")
        lines.append(f"  {k}：{av}  vs  {bv}")
    lines.append("罪孽亲和：")
    for k in sorted(set(a["sa"]) | set(b["sa"])):
        av = a["sa"].get(k, "—")
        bv = b["sa"].get(k, "—")
        lines.append(f"  {k}：{av}  vs  {bv}")
    lines.append(f"技能数：{a['n_skills']}  vs  {b['n_skills']}")
    lines.append(f"战斗被动：{a['passive']}")
    lines.append(f"          {b['passive']}")
    lines.append(f"实装日期：{a['release'] or '—'}  vs  {b['release'] or '—'}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 独立验证入口
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = PersonaDirectStore()
    q = sys.argv[1] if len(sys.argv) > 1 else "浮士德LCB罪人"
    out = store.try_direct_answer(q)
    if out:
        print(out)
    else:
        print("(未命中直答，应回落 RAG)")
