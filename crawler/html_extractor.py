"""
HTML DOM 结构化提取器：从灰机 Wiki 渲染后的 HTML 中提取人格/EGO 结构化数据。

使用 BeautifulSoup 解析 DOM，不依赖 WikiText 模板语法。
所有 I-IV 阶段数据均在 HTML 的 tab-pane 中，一次解析即可全部获取。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup, Tag
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    logger.warning("beautifulsoup4 未安装，HTML 结构化提取不可用")

# ── 恐慌类型收敛（修复①）：罪孽属性名与通用 buff（非恐慌）过滤 ──
_SIN_AFFINITY_NAMES = {
    "暴怒", "色欲", "怠惰", "暴食", "忧郁", "傲慢", "嫉妒", "愤怒",
}
_NON_PANIC_BUFF_NAMES = {
    "伤害强化", "伤害弱化", "易损", "守护",
    "攻击等级提升", "攻击等级降低", "防御等级提升", "防御等级降低",
    "迅捷", "束缚", "强壮", "拼点威力提升", "拼点威力降低", "挑衅值",
}


def _collect_panic_types(row) -> list[str]:
    """从「恐慌类型」行收敛收集真正的恐慌效果。

    修复①：仅取 tooltip/title 链接文本，追加前判重保序，
    过滤罪孽属性名（愤怒/色欲/嫉妒 等）与通用 buff（伤害强化/易损 等非恐慌项）。
    """
    result: list[str] = []
    for a in row.select("a.huiji-tt, a[title]"):
        text = a.get_text(strip=True)
        if not text:
            continue
        if text in _SIN_AFFINITY_NAMES or text in _NON_PANIC_BUFF_NAMES:
            continue
        if text in result:
            continue
        result.append(text)
    return result


def _parse_importance(value) -> int:
    """将重要性值解析为 int；无法解析返回 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _strip_tooltip_preload(soup):
    """移除站点 JS 生成的 tooltip 预加载容器（.huiji-tt-preload）。

    JS gadget 渲染 buffPro/状态效果后会在 span 内附带 ``huiji-tt-preload``
    （含 ``{0}/{1}`` 占位符的效果描述）。get_text() 会把它拼入文本，
    污染技能效果/被动（如"烧伤 回合结束时：受到{0}点固定伤害…"）。
    在解析前统一移除，只保留状态效果名称本身。
    """
    if soup is None:
        return soup
    for node in soup.select(".huiji-tt-preload"):
        node.decompose()
    return soup


def _resolve_buffpro_in_text(text: str, extra_map: Optional[dict] = None) -> str:
    """将文本中的 ``{{BuffPro|Code}}`` 替换为中文名（修复④）。

    优先级（从高到低）：
        1. extra_map（页面级配对映射，见 buffs_data.build_buff_code_map_from_html）
        2. ``data/structured/buffs.json`` 索引（由 Data:Buffchoose.tabx 抓取生成；
           注意：该数据页实测为布尔表，通常不包含可用映射，见 buffs_data 模块头）
        3. ``buffs_data.DEFAULT_BUFF_CODES`` 静态表
    仍未命中则保留原代码（不标注，避免污染技能描述）。
    惰性导入 buffs_data，避免 html_extractor 被单独引入时强依赖数据文件。
    """
    def _rep(m: re.Match) -> str:
        code = m.group(1).strip()
        if extra_map and code in extra_map:
            return extra_map[code]
        try:
            from crawler.buffs_data import resolve_buff_code
            return resolve_buff_code(code)
        except Exception:
            return code

    return re.sub(r'\{\{BuffPro\|([^}|]+)\}\}', _rep, text)


# ── 技能图标文件名 → 罪孽类型 + 伤害类型映射 ──
_SKILL_ICON_MAP: dict[str, tuple[str, str]] = {
    "技能-斩击-傲慢.png": ("傲慢", "斩击"),
    "技能-斩击-暴怒.png": ("暴怒", "斩击"),
    "技能-斩击-暴食.png": ("暴食", "斩击"),
    "技能-斩击-怠惰.png": ("怠惰", "斩击"),
    "技能-斩击-忧郁.png": ("忧郁", "斩击"),
    "技能-斩击-色欲.png": ("色欲", "斩击"),
    "技能-突刺-傲慢.png": ("傲慢", "突刺"),
    "技能-突刺-暴怒.png": ("暴怒", "突刺"),
    "技能-突刺-暴食.png": ("暴食", "突刺"),
    "技能-突刺-怠惰.png": ("怠惰", "突刺"),
    "技能-突刺-忧郁.png": ("忧郁", "突刺"),
    "技能-突刺-色欲.png": ("色欲", "突刺"),
    "技能-打击-傲慢.png": ("傲慢", "打击"),
    "技能-打击-暴怒.png": ("暴怒", "打击"),
    "技能-打击-暴食.png": ("暴食", "打击"),
    "技能-打击-怠惰.png": ("怠惰", "打击"),
    "技能-打击-忧郁.png": ("忧郁", "打击"),
    "技能-打击-色欲.png": ("色欲", "打击"),
}

# ── 罪孽图标文件名 → 罪孽类型映射 ──
_SIN_ICON_MAP: dict[str, str] = {
    "罪孽-傲慢.png": "傲慢",
    "罪孽-暴怒.png": "暴怒",
    "罪孽-暴食.png": "暴食",
    "罪孽-怠惰.png": "怠惰",
    "罪孽-忧郁.png": "忧郁",
    "罪孽-色欲.png": "色欲",
    "罪孽-嫉妒.png": "嫉妒",
}

# ── 守备技能图标识别 ──
_GUARD_ICON_PATTERNS: dict[str, str] = {
    "技能-闪避": "闪避",
    "技能-防御": "防御",
    "技能-反击": "反击",
}

# ── 伤害类型（物理抗性）关键词 ──
_PHYSICAL_TYPES = ["斩击", "突刺", "打击"]

# ── 罪人名称映射（人格标题前缀 → 罪人英文ID） ──
_SINNER_PREFIX_MAP: list[tuple[str, str]] = [
    ("李箱", "yisang"),
    ("浮士德", "faust"),
    ("堂吉诃德", "donquixote"),
    ("良秀", "ryoshu"),
    ("默尔索", "meursault"),
    ("鸿璐", "honglu"),
    ("希斯克利夫", "heathcliff"),
    ("以实玛利", "ishmael"),
    ("罗佳", "rodion"),
    ("辛克莱", "sinclair"),
    ("奥提斯", "outis"),
    ("格里高尔", "gregor"),
    ("但丁", "dante"),
]


def _extract_sinner_from_title(title: str) -> str:
    """从人格/EGO 标题中提取罪人名称。"""
    for sinner_name, sinner_id in _SINNER_PREFIX_MAP:
        if title.startswith(sinner_name):
            return sinner_name
    # E.G.O 格式："永恒-浮士德" → 罪人 = "浮士德"
    parts = title.rsplit("-", 1)
    if len(parts) == 2:
        candidate = parts[1]
        for sinner_name, _ in _SINNER_PREFIX_MAP:
            if candidate == sinner_name:
                return sinner_name
    return ""


def _extract_sinner_id(title: str) -> str:
    """从标题中提取罪人英文ID。"""
    sinner_name = _extract_sinner_from_title(title)
    for name, sid in _SINNER_PREFIX_MAP:
        if name == sinner_name:
            return sid
    return ""


def _parse_skill_icon_filename(img_tag) -> Optional[tuple[str, str]]:
    """从技能图标 img 的 alt 属性解析 (罪孽类型, 伤害类型)。"""
    if not img_tag:
        return None
    alt = img_tag.get("alt", "")
    if alt in _SKILL_ICON_MAP:
        return _SKILL_ICON_MAP[alt]
    for key, value in _SKILL_ICON_MAP.items():
        if key in alt:
            return value
    return None


def _parse_sin_icon_filename(img_tag) -> Optional[str]:
    """从罪孽图标 img 的 alt 属性解析罪孽类型。"""
    if not img_tag:
        return None
    alt = img_tag.get("alt", "")
    if alt in _SIN_ICON_MAP:
        return _SIN_ICON_MAP[alt]
    for key, value in _SIN_ICON_MAP.items():
        if key in alt:
            return value
    return None


def _is_guard_icon(img_tag) -> Optional[str]:
    """判断是否为守备技能图标，返回守备类型（闪避/防御/反击）。"""
    if not img_tag:
        return None
    alt = img_tag.get("alt", "")
    src = img_tag.get("src", "")
    combined = f"{alt} {src}"
    for pattern, guard_type in _GUARD_ICON_PATTERNS.items():
        if pattern in combined:
            return guard_type
    return None


def _extract_buff_text(span_tag) -> str:
    """从 huiji-tt / buffPro span 中提取 buff/状态效果名称。"""
    if not span_tag:
        return ""
    a_tags = span_tag.select("a")
    if a_tags:
        return a_tags[-1].get_text(strip=True)
    img_alts = [img.get("alt", "") for img in span_tag.select("img")]
    text = span_tag.get_text(strip=True)
    for alt in img_alts:
        text = text.replace(alt, "")
    return text.strip()


def _parse_resistance_value(text: str) -> Optional[float]:
    """解析抗性倍率值，返回 float 或 None。"""
    match = re.search(r"×\s*([\d.]+)", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def _parse_stage_number(stage_text: str) -> str:
    """将阶段文本统一为 I/II/III/IV。"""
    stage_map = {
        "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV",
        "I": "I", "II": "II", "III": "III", "IV": "IV",
        "1": "I", "2": "II", "3": "III", "4": "IV",
    }
    for key, val in stage_map.items():
        if key in stage_text:
            return val
    return stage_text.strip()


def _extract_coin_effects_from_cell(td_tag) -> list[dict]:
    """从表格单元格中提取硬币效果列表。"""
    if not td_tag:
        return []

    from crawler.buffs_data import resolve_buff_codes_in_text

    effects = []
    coin_imgs = td_tag.select('img[alt*="硬币"]')
    if not coin_imgs:
        text = td_tag.get_text(" ", strip=True)
        if text:
            text = resolve_buff_codes_in_text(text)
            effects.append({"coin_index": 0, "description": text})
        return effects

    # ── 前置文本（第一枚硬币图之前）→ 作为 coin_index=0 效果 ──
    # 部分技能在硬币图之前有前置说明（如 折射轨道 罗生-回归：
    # "…会影响即将到来的过去 - 对关卡2的敌方单位施加 灼热的鳞粉（蛹）"），
    # 旧实现从第一枚硬币图开始向后收集，导致该段丢失（效果为空）。
    pre_parts: list[str] = []
    sib = coin_imgs[0].previous_sibling
    safety = 0
    while sib is not None and safety < 50:
        safety += 1
        if isinstance(sib, Tag):
            if sib.name == "img" and "硬币" in (sib.get("alt", "") or ""):
                break
            text = sib.get_text(" ", strip=True)
            if text:
                pre_parts.append(text)
        elif hasattr(sib, "strip") and callable(sib.strip):
            text = sib.strip()
            if text:
                pre_parts.append(text)
        sib = sib.previous_sibling
    pre_text = " ".join(reversed(pre_parts)).strip()
    if pre_text:
        effects.append({
            "coin_index": 0,
            "description": resolve_buff_codes_in_text(pre_text),
        })

    for coin_img in coin_imgs:
        alt = coin_img.get("alt", "")
        coin_match = re.search(r"硬币(\d+)", alt)
        coin_index = int(coin_match.group(1)) if coin_match else 0

        effect_parts = []
        timing = ""

        sibling = coin_img.next_sibling
        safety = 0
        while sibling and safety < 50:
            safety += 1
            if isinstance(sibling, Tag):
                if sibling.name == "img" and "硬币" in sibling.get("alt", ""):
                    break
                timing_match = re.search(r"\[([^\]]+)\]", sibling.get_text(strip=True))
                if timing_match and not timing:
                    timing = timing_match.group(1)
                buff_spans = sibling.select(".huiji-tt, .buffPro")
                for bs in buff_spans:
                    buff_name = _extract_buff_text(bs)
                    if buff_name:
                        effect_parts.append(buff_name)
                text = sibling.get_text(strip=True)
                if text:
                    effect_parts.append(text)
            elif hasattr(sibling, "strip") and callable(sibling.strip):
                text = sibling.strip()
                if text:
                    timing_match = re.search(r"\[([^\]]+)\]", text)
                    if timing_match and not timing:
                        timing = timing_match.group(1)
                    effect_parts.append(text)
            sibling = sibling.next_sibling

        description = " ".join(effect_parts).strip()
        if description or timing:
            effects.append({
                "coin_index": coin_index,
                "timing": timing,
                "description": resolve_buff_codes_in_text(description),
            })

    return effects


def _find_all_tabpanes(tab_content_div) -> list:
    """获取 tab-content 下所有 tab-pane（包括非 active 的）。"""
    if not tab_content_div:
        return []
    return tab_content_div.select('div[role="tabpanel"].tab-pane')


@dataclass
class PersonalityData:
    """人格结构化数据"""
    page_type: str = "personality"
    title: str = ""
    sinner: str = ""
    sinner_id: str = ""
    personality_name: str = ""
    release_date: str = ""
    acquisition: str = ""
    sin_affinities: dict[str, int] = field(default_factory=dict)  # {傲慢: 3, ...}
    physical_resistances: dict[str, float] = field(default_factory=dict)  # {斩击: 0.5, ...}
    ego_resources: dict[str, int] = field(default_factory=dict)  # {色欲: 2, ...}
    skills: list[dict] = field(default_factory=list)
    battle_passive: str = ""
    support_passive: str = ""
    notes: str = ""
    voice_lines: list[dict] = field(default_factory=list)   # 语音台词: [{"file","title","text"}]
    skill_voice: list[dict] = field(default_factory=list)   # 技能语音: [{"file","skill_index","skill_label","skill_name"}]


@dataclass
class EgoData:
    """E.G.O 结构化数据"""
    page_type: str = "ego"
    title: str = ""
    sinner: str = ""
    sinner_id: str = ""
    ego_name: str = ""
    release_date: str = ""
    acquisition: str = ""
    resource_costs: dict[str, int] = field(default_factory=dict)  # {色欲: 2, ...}
    sin_resistances: dict[str, float] = field(default_factory=dict)  # {暴怒: 0.75, ...}
    awakening_stages: list[dict] = field(default_factory=list)
    erosion_stages: list[dict] = field(default_factory=list)
    passive_name: str = ""
    passive_description: str = ""


@dataclass
class EnemySkillData:
    """敌方单位技能数据"""
    skill_name: str = ""                # 技能名称
    icon_id: str = ""                   # 技能图标编号（wikitext 归属重排匹配键）
    sin_type: str = ""                  # 罪孽类型
    damage_type: str = ""               # 伤害类型（斩击/突刺/打击）
    base_value: int = 0                 # 基础值
    coin_power: int = 0                 # 硬币威力
    coin_count: int = 0                 # 硬币数量
    attack_level: int = 0               # 攻击等级
    attack_weight: int = 1              # 攻击容量
    is_guard: bool = False              # 是否为守备技能
    guard_type: str = ""                # 闪避/防御/反击
    importance: int = 0                 # 重要性（修复③：0=普通，1-3 为特殊技能）
    coin_effects: list[str] = field(default_factory=list)  # 硬币效果描述列表


@dataclass
class EnemyData:
    """敌方单位结构化数据"""
    page_type: str = "enemy"
    title: str = ""                     # 页面标题（如 主线战斗1-10）
    enemy_name: str = ""                # 敌方名称（如 暴躁的残兵）
    battle_stage: str = ""              # 所属关卡
    body_part: str = ""                 # 部 分（如 手 部）
    hp: int = 0
    defense_level: int = 0
    speed_min: int = 0
    speed_max: int = 0
    chaos_threshold: str = ""           # 混乱阈值（如 "42" 或 "120/51"）
    physical_resistances: dict[str, float] = field(default_factory=dict)  # {"斩击": 2.0, ...}
    sin_resistances: dict[str, float] = field(default_factory=dict)       # {"暴怒": 1.0, ...}
    panic_types: list[str] = field(default_factory=list)                   # ["无措", "无措"]
    passives: list[str] = field(default_factory=list)                      # 被动能力文本
    skills: list[dict] = field(default_factory=list)                       # 技能列表
    is_ally: bool = False               # 是否为援助/友方单位


@dataclass
class EventOption:
    """探索事件选项"""
    choice_text: str = ""                           # "我喜欢。"
    check_type: str = ""                            # "有利判定" / "不利判定"
    check_sin: str = ""                             # 判定罪孽（如 "色欲"）
    check_threshold: int = 0                        # 判定阈值
    success_outcomes: list[str] = field(default_factory=list)   # 成功结果
    failure_outcomes: list[str] = field(default_factory=list)   # 失败结果


@dataclass
class EventData:
    """探索事件结构化数据"""
    page_type: str = "event"
    title: str = ""                     # 事件-膏血
    event_name: str = ""                # 膏血
    narration: str = ""                 # 事件描述文本
    options: list[EventOption] = field(default_factory=list)
    ego_gifts: list[dict] = field(default_factory=list)  # [{"name": "XXX", "effect": "..."}]
    related_abnormalities: list[str] = field(default_factory=list)
    trigger_location: str = ""


class PersonalityExtractor:
    """人格页面 HTML 提取器"""

    def __init__(self, html: str, title: str, categories: list[str], wikitext: str = ""):
        if not HAS_BS4:
            raise ImportError("beautifulsoup4 未安装")
        self.soup = BeautifulSoup(html, "lxml")
        _strip_tooltip_preload(self.soup)
        self.title = title
        self.categories = categories
        self.wikitext = wikitext
        # 预解析 wikitext 中的技能名称映射（技能序号 1-based -> 技能名称）
        self._persona_skill_name_map: dict[int, str] = {}
        self._parse_skill_names_from_wikitext()
        self.content_div = self.soup.select_one("#mw-content-text")
        if not self.content_div:
            self.content_div = self.soup
        self.mw_output = self.content_div.select_one(".mw-parser-output") if self.content_div else None
        if not self.mw_output:
            self.mw_output = self.content_div

    def extract(self) -> PersonalityData:
        data = PersonalityData(
            title=self.title,
            sinner=_extract_sinner_from_title(self.title),
            sinner_id=_extract_sinner_id(self.title),
            personality_name=self.title,
        )

        if not self.mw_output:
            return data

        self._extract_basic_info(data)
        self._extract_skills(data)
        self._extract_passives(data)
        self._extract_voice(data)

        return data

    def _extract_basic_info(self, data: PersonalityData):
        """提取人格基本信息表格（第一个 wikitable）。"""
        tables = self.mw_output.select("table.wikitable")
        if not tables:
            return

        info_table = tables[0]
        rows = info_table.select("tr")

        # ── 1) 实装日期：登场时间在 Row 0，"2026.07.09" 在 Row 1 ──
        # 遍历所有行搜索日期模式
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", row_text)
            if date_match:
                data.release_date = date_match.group(1)
                break

        # ── 2) 获取方式 ──
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            if "获取方式" in row_text:
                # 取下一行的非空文本作为获取方式
                idx = list(rows).index(row)
                if idx + 1 < len(rows):
                    next_text = rows[idx + 1].get_text(" ", strip=True)
                    # 过滤掉纯数字/日期/观看剧情等
                    if next_text and not re.match(r"^\d{4}\.\d{2}\.\d{2}$", next_text) and "观看剧情" not in next_text:
                        data.acquisition = next_text

        # ── 3) 罪孽亲和（sin_affinities） ──
        # 在 info table 中搜索 ×N 模式和罪孽图标
        for row in rows:
            sin_imgs = row.select('img[alt*="罪孽-"]')
            row_text = row.get_text(strip=True)
            if sin_imgs:
                # 从行文本中提取所有 ×N
                counts = re.findall(r"×\s*(\d+)", row_text)
                if len(counts) == len(sin_imgs):
                    # 一一配对
                    for i, img in enumerate(sin_imgs):
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            data.sin_affinities[sin_type] = int(counts[i])
                else:
                    # 退化处理
                    for img in sin_imgs:
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            parent_text = img.parent.get_text(strip=True) if img.parent else ""
                            count_match = re.search(r"×\s*(\d+)", parent_text)
                            count = int(count_match.group(1)) if count_match else 1
                            data.sin_affinities[sin_type] = count

        # ── 4) E.G.O 资源 ──
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            if "E.G.O" in row_text and "资源" in row_text:
                idx = list(rows).index(row)
                # 查看下一行是否有 ×N 和图标
                if idx + 1 < len(rows):
                    next_row = rows[idx + 1]
                    next_text = next_row.get_text(" ", strip=True)
                    sin_imgs = next_row.select('img[alt*="罪孽-"]')
                    if sin_imgs:
                        # 从文本中提取所有 ×N，按顺序与图标配对
                        counts = re.findall(r"×\s*(\d+)", next_text)
                        if len(counts) == len(sin_imgs):
                            for i, img in enumerate(sin_imgs):
                                sin_type = _parse_sin_icon_filename(img)
                                if sin_type:
                                    data.ego_resources[sin_type] = int(counts[i])
                        else:
                            for img in sin_imgs:
                                sin_type = _parse_sin_icon_filename(img)
                                if sin_type:
                                    parent_text = img.parent.get_text(strip=True) if img.parent else ""
                                    count_match = re.search(r"×\s*(\d+)", parent_text)
                                    count = int(count_match.group(1)) if count_match else 1
                                    data.ego_resources[sin_type] = count

        # ── 5) 物理抗性 ──
        # 找包含物理伤害图标的行，值可能在下一行
        for i, row in enumerate(rows):
            phys_imgs = row.select('img')
            # 检测是否包含斩击/突刺/打击图标
            phys_types_found = []
            for img in phys_imgs:
                alt = img.get("alt", "")
                for phys in _PHYSICAL_TYPES:
                    if phys in alt and ".png" in alt and "技能-" not in alt and "罪孽-" not in alt:
                        if phys not in phys_types_found:
                            phys_types_found.append(phys)

            if phys_types_found:
                # 值可能在当前行或下一行
                search_rows = [row]
                if i + 1 < len(rows):
                    search_rows.append(rows[i + 1])

                all_values = []
                for sr in search_rows:
                    values = re.findall(r"\[×(\d+\.?\d*)\]", sr.get_text(" ", strip=True))
                    all_values.extend(values)

                if len(all_values) == len(phys_types_found):
                    for j, phys in enumerate(phys_types_found):
                        try:
                            data.physical_resistances[phys] = float(all_values[j])
                        except ValueError:
                            pass

        # ── 6) 从技能推断 sin_affinities（如有需要）──
        # 用户说明：sin_affinities = 3×技能1 的罪孽 + 2×技能2 的罪孽 + 1×技能3 的罪孽
        # 这会在 _extract_skills 之后处理

    def _parse_skill_names_from_wikitext(self):
        """从 wikitext 中解析 {{技能链接}} 系列模板的 N技能-名称 参数（技能序号 1-based -> 名称）。

        人格页 wikitext 技能链接模板格式示例：
        {{技能链接|...|1技能-名称=纵斩|1技能-类型=打击|1技能-罪孽=傲慢|...|
          2技能-名称=上挑|...|3技能-名称=剜刺|...|4技能-名称=闪避|类型=守备|...}}

        模板起始名存在多种变体（部分新人格页使用）：
        - {{技能链接|...}}          （标准）
        - {{鸿璐式技能链接|...}}     （鸿璐式特殊人格）
        - {{桑丘派技能链接|...}}     （桑丘派特殊人格）
        - {{技能4|...}}             （四位技能编号模板）

        命名参数也存在变体（顺序技能编号 + 段位编号）：
        - N技能-名称=XXX          （标准，N=1/2/3）
        - 强化N技能-名称=XXX      （强化人格，N=1/2/3）
        - N技能M-名称=XXX         （带段位 M，如 1技能2-名称）
        - 5技能-名称=XXX          （五位技能，如星之/罗佳）

        使用大括号计数法定位每个模板的完整内容，再以命名参数提取技能名。
        """
        if not self.wikitext:
            return
        # 模板起始名变体
        pattern_start = re.compile(
            r'\{\{\s*(?:技能链接|鸿璐式技能链接|桑丘派技能链接|技能4)\s*\|'
        )
        # 命名参数变体：可选 "强化" 前缀 + 顺序编号 + 可选 段位编号 + "技能-名称"
        name_param_pattern = re.compile(
            r'(?:强化)?\s*(\d+)\s*技能(?:\d+)?-名称\s*=\s*([^|]+)'
        )
        for match in pattern_start.finditer(self.wikitext):
            # 从模板起始开始计数大括号深度
            depth = 0
            i = match.start()
            while i < len(self.wikitext):
                if self.wikitext[i:i + 2] == '{{':
                    depth += 1
                    i += 2
                elif self.wikitext[i:i + 2] == '}}':
                    depth -= 1
                    if depth == 0:
                        template_content = self.wikitext[match.start() + 2:i]
                        # 提取所有 技能-名称 命名参数（含变体）
                        for name_match in name_param_pattern.finditer(template_content):
                            try:
                                skill_no = int(name_match.group(1))
                            except ValueError:
                                continue
                            skill_name = name_match.group(2).strip()
                            if skill_name:
                                self._persona_skill_name_map[skill_no] = skill_name
                                logger.debug(
                                    f"Personality wikitext 技能名称映射: 序号 {skill_no} -> {skill_name}"
                                )
                        break
                    i += 2
                else:
                    i += 1

    def _extract_skills(self, data: PersonalityData):
        """提取技能区域。
        
        技能在 collapsible div 中，一个 collapsible 内可能有多张 table.wikitable，
        每张 table 对应一个技能（技能一、技能二、技能三、守备技能）。
        每个 table 内可能有多个 tab-content 组（普通I-IV + 强化I-IV 等）。
        """
        if not self.mw_output:
            return

        # 找到 "技能" 区域的 h2 标题
        skill_heading = None
        for h in self.mw_output.select("h2"):
            span = h.select_one(".mw-headline")
            text = span.get_text(strip=True) if span else h.get_text(strip=True)
            if "技能" in text:
                skill_heading = h
                break

        if not skill_heading:
            return

        # 在 "技能" h2 之后收集 collapsible div，直到下一个 h2
        collapsibles = []
        sibling = skill_heading.next_sibling
        while sibling:
            if hasattr(sibling, "name") and sibling.name == "h2":
                break
            if hasattr(sibling, "name") and sibling.name == "div":
                classes = sibling.get("class", [])
                if any("collapsible" in c or "collapse" in c for c in classes):
                    collapsibles.append(sibling)
            sibling = sibling.next_sibling

        skill_index = 0
        for collapsible in collapsibles:
            # 检查是否为技能 collapsible（包含技能图标或 tab-pane）
            collapsible_text = collapsible.get_text(" ", strip=True)[:200]
            tab_panes = collapsible.select('div[role="tabpanel"].tab-pane')
            has_stages = bool(tab_panes)

            if not has_stages:
                # 跳过非技能 collapsible（如：属于XX的人格）
                continue

            # 在 collapsible 内遍历每张 table.wikitable 作为独立技能
            skill_tables = collapsible.select("table.wikitable")
            for table in skill_tables:
                table_full_text = table.get_text(" ", strip=True)
                table_text = table_full_text[:200]
                # Also check first row (header) for "守备技能"
                first_row = table.select_one("tr")
                header_text = first_row.get_text(" ", strip=True) if first_row else ""

                # 判断这张 table 是否为技能 table（有技能图标或守备关键词）
                skill_imgs = table.select('img[alt*="技能-"]')
                has_skill_icon = bool(skill_imgs)
                has_guard = (any(g in table_full_text for g in ["闪避", "防御", "反击", "守备技能"])
                            or "守备技能" in header_text)

                # 识别技能类型
                sin_type = ""
                damage_type = ""
                guard_type = ""

                for img in skill_imgs:
                    result = _parse_skill_icon_filename(img)
                    if result:
                        sin_type, damage_type = result
                        break
                    guard = _is_guard_icon(img)
                    if guard:
                        guard_type = guard
                        break

                if not sin_type and not guard_type:
                    # 再从 table 文本中推断
                    if "闪避" in table_full_text:
                        guard_type = "闪避"
                    elif "反击" in table_full_text:
                        guard_type = "反击"
                    elif "防御" in table_full_text:
                        guard_type = "防御"

                # 如果是守备技能但 guard_type 仍为空，从 pane 内容推断
                if "守备技能" in header_text and not guard_type:
                    for guard_pane in table.select('div[role="tabpanel"].tab-pane'):
                        pt = guard_pane.get_text(" ", strip=True)
                        if "反击" in pt:
                            guard_type = "反击"
                            break
                        elif "闪避" in pt:
                            guard_type = "闪避"
                            break
                        elif "防御" in pt:
                            guard_type = "防御"
                            break

                skill_name = f"技能{skill_index + 1}"
                if guard_type:
                    skill_name = f"守备技能{skill_index + 1}"

                # 优先使用 wikitext {{技能链接}} 模板解析出的真实技能名（序号 1-based），
                # 解析失败时回退为占位符（技能N / 守备技能N）
                real_name = self._persona_skill_name_map.get(skill_index + 1)
                if real_name:
                    skill_name = real_name

                # 按 tab-content 分组提取阶段数据
                tab_contents = table.select('div.tab-content')
                all_stage_groups = []

                for tc in tab_contents:
                    panes = tc.select('div[role="tabpanel"].tab-pane')
                    if not panes:
                        continue
                    stages = []
                    stage_labels = ["I", "II", "III", "IV"]
                    for pi, pane in enumerate(panes):
                        stage = stage_labels[pi] if pi < len(stage_labels) else str(pi + 1)
                        stage_data = self._extract_stage_from_pane(pane, stage)
                        if stage_data:
                            stages.append(stage_data)
                    if stages:
                        all_stage_groups.append(stages)

                # 提取硬币数量
                coin_count = 0
                for stages in all_stage_groups:
                    for s in stages:
                        coin_imgs_count = len(re.findall(r"硬币\d+\.png", s.get("raw_text", "")))
                        coin_count = max(coin_count, coin_imgs_count)
                if coin_count == 0:
                    coin_imgs = table.select('img[alt*="硬币"]')
                    coin_count = len(set(img.get("alt", "") for img in coin_imgs))

                # 提取攻击容量
                attack_capacity = 1
                # 从 whole collapsible 文本中搜（因为攻击容量可能在 table 标题行）
                capacity_match = re.search(r"攻击容量[：:]\s*(\d+)", collapsible_text)
                if not capacity_match:
                    capacity_match = re.search(r"攻击容量[：:]\s*(\d+)", table_text)
                if capacity_match:
                    attack_capacity = int(capacity_match.group(1))

                # 提取硬币效果
                coin_effects = []
                for td in table.select("td"):
                    effects = _extract_coin_effects_from_cell(td)
                    if effects:
                        coin_effects.extend(effects)

                skill = {
                    "skill_index": skill_index,
                    "skill_name": skill_name,
                    "sin_type": sin_type,
                    "damage_type": damage_type,
                    "guard_type": guard_type,
                    "attack_capacity": attack_capacity,
                    "coin_count": coin_count,
                    "stage_groups": all_stage_groups,
                    "coin_effects": coin_effects,
                }
                data.skills.append(skill)
                skill_index += 1

    def _extract_stage_from_pane(self, pane, stage: str) -> Optional[dict]:
        """从单个 tab-pane 提取阶段数据。"""
        if not pane:
            return None

        from crawler.buffs_data import resolve_buff_codes_in_text

        text = pane.get_text(" ", strip=True)

        base_value = None
        coin_power = None

        bv_match = re.search(r"基础值[：:]\s*(\d+)", text)
        if bv_match:
            base_value = int(bv_match.group(1))

        cp_match = re.search(r"变动值[：:]\s*([+-]?\d+)", text)
        if cp_match:
            coin_power = int(cp_match.group(1))

        if base_value is None:
            cp_match2 = re.search(r"硬币威力[：:]\s*([+-]?\d+)", text)
            if cp_match2:
                coin_power = int(cp_match2.group(1))

        effects = []
        timing_pattern = re.findall(r"\[([^\]]+)\]([^\[]*)", text)
        for timing, desc in timing_pattern:
            desc = desc.strip()
            if desc:
                desc = resolve_buff_codes_in_text(desc)
                effects.append({"timing": timing, "description": desc})

        return {
            "stage": stage,
            "base_value": base_value,
            "coin_power": coin_power,
            "effects": effects,
            "raw_text": resolve_buff_codes_in_text(text),
        }

    def _parse_passive_refs_from_wikitext(self) -> list[str]:
        """从 wikitext 提取被动引用（{{人格被动链接|ID}}），保序保留。

        人格页 wikitext 被动段结构（实测，见 plans/persona_direct_answer.md）：
            ===被动===
            {{人格被动链接|1061302}}
            {{人格被动链接|1061301}}
            {{人格被动链接|1061321}}
        {{人格被动链接|ID}} 仅含 ID，本地无被动名称映射表时至少保留 ID 引用
        （如 "人格被动1061302"），避免被动全空。
        """
        refs: list[str] = []
        if not self.wikitext:
            return refs

        # 定位含"被动"的 === 小节（===被动=== / ===被动技能=== 等）
        section_text = self.wikitext
        for m in _SECTION_RE.finditer(self.wikitext):
            sec_name = m.group(1).strip()
            if "被动" in sec_name:
                start = m.end()
                nxt = _SECTION_RE.search(self.wikitext, start)
                end = nxt.start() if nxt else len(self.wikitext)
                section_text = self.wikitext[start:end]
                break

        # 提取 {{人格被动链接|ID}}（兼容 |名称= 等附加参数）
        for m in re.finditer(r'\{\{\s*人格被动链接\s*\|\s*([^|}]+)', section_text):
            pid = m.group(1).strip()
            if pid:
                refs.append(f"人格被动{pid}")
        return refs

    def _extract_passives(self, data: PersonalityData):
        """提取战斗被动和支援被动。

        主路径：从 wikitext 的 ===被动=== 段提取全部 {{人格被动链接|ID}} 引用
        （旧实现依赖渲染 HTML 的 h3"被动" 后 div 文本以 战斗/支援 开头，已被证实
        对该站点结构完全失效——被动以多个模板引用存在，见 plans 3.2）。
        回退路径：渲染 HTML 的非 collapsible div 文本（兼容旧页面/旧格式）。
        """
        # ── 主路径：wikitext {{人格被动链接|ID}} ──
        if self.wikitext:
            passive_refs = self._parse_passive_refs_from_wikitext()
            if passive_refs:
                # 无法离线可靠区分 战斗/支援，先统一写入 battle_passive（保序保 ID）
                data.battle_passive = "\n".join(passive_refs)
                logger.info(
                    f"Personality 被动（wikitext）：{self.title} 提取 {len(passive_refs)} 条: "
                    f"{passive_refs}"
                )
                return

        if not self.mw_output:
            return

        # 查找 "技能" h2 区域内的 h3 "被动"
        skill_heading = None
        for h in self.mw_output.select("h2"):
            if "技能" in h.get_text(strip=True):
                skill_heading = h
                break

        if not skill_heading:
            return

        passive_heading = None
        sibling = skill_heading.next_sibling
        while sibling:
            if hasattr(sibling, "name"):
                if sibling.name == "h2":
                    break
                if sibling.name == "h3":
                    text = sibling.get_text(strip=True)
                    if "被动" in text:
                        passive_heading = sibling
                        break
            sibling = sibling.next_sibling

        if not passive_heading:
            return

        # 收集 h3 "被动" 之后、下一个 h2 之前的所有 div（非 collapsible）
        battle_parts = []
        support_parts = []
        sibling = passive_heading.next_sibling

        while sibling:
            if hasattr(sibling, "name"):
                if sibling.name == "h2":
                    break
                if sibling.name == "div":
                    classes = sibling.get("class", [])
                    # 跳过 collapsible div（"属于XX的人格" 等）
                    if any("collapsible" in c for c in classes):
                        sibling = sibling.next_sibling
                        continue
                    div_text = sibling.get_text(" ", strip=True)
                    if div_text:
                        # 判断是战斗还是支援被动
                        if div_text.startswith("战斗"):
                            battle_parts.append(div_text)
                        elif div_text.startswith("支援"):
                            support_parts.append(div_text)
            sibling = sibling.next_sibling

        if battle_parts:
            data.battle_passive = "\n".join(battle_parts)
        if support_parts:
            data.support_passive = "\n".join(support_parts)

    def _extract_voice(self, data: PersonalityData):
        """从 wikitext 解析语音台词（===语音台词===）与技能语音（===技能语音===）。

        用户需求：将语音文件与对应人格关联（类似人格剧情与人格的关系）。
        语音台词 wikitable 行格式（用户提供）：
            |width="55px"|[[文件:浮士德LCB语音1.ogg|50px]]||获得时：
            |-
            |style="color:#E5CAA5"|
            || ||
            浮士德。你人生中绝无仅有的天才。
        技能语音 wikitable 行格式：
            |width="135px"|技能二-语气词||width="75px"|[[文件:Battle_s2_10201_1.ogg|50px]]||技能二-台词
        """
        if not self.wikitext:
            return

        # 1) 定位各 === 小节范围（复用 _SECTION_RE）
        secs = []
        for m in _SECTION_RE.finditer(self.wikitext):
            secs.append((m.start(), m.end(), m.group(1).strip()))
        secs.sort(key=lambda t: t[0])

        def _range(name: str) -> Optional[tuple[int, int]]:
            for i, (start, end, sec_name) in enumerate(secs):
                if sec_name == name:
                    s = end
                    e = secs[i + 1][0] if i + 1 < len(secs) else len(self.wikitext)
                    return s, e
            return None

        # 2) 语音台词
        vr = _range("语音台词")
        if vr:
            data.voice_lines = self._parse_voice_lines(self.wikitext[vr[0]:vr[1]])

        # 3) 技能语音
        sr = _range("技能语音")
        if sr:
            data.skill_voice = self._parse_skill_voice(self.wikitext[sr[0]:sr[1]])

    def _parse_voice_lines(self, section_text: str) -> list[dict]:
        """解析语音台词小节为 [{file, title, text}]。

        以 [[文件:xxx.ogg 行为锚点：从 ogg 之后截取标题（如 获得时：）与台词文本，
        过滤 wikitable 结构行/属性行，仅保留含中文的台词内容。
        """
        ogg_re = re.compile(r'\[\[\s*文件\s*:\s*([^|\]]+\.ogg)')
        ogg_positions = list(ogg_re.finditer(section_text))
        if not ogg_positions:
            return []

        voice_lines: list[dict] = []
        for idx, m in enumerate(ogg_positions):
            file_name = m.group(1).strip()
            seg_start = m.end()
            seg_end = ogg_positions[idx + 1].start() if idx + 1 < len(ogg_positions) else len(section_text)
            seg = section_text[seg_start:seg_end]

            # 标题：ogg 之后第一个 ||标题：
            title = ""
            inline_text = ""
            title_m = re.search(r'\|\|\s*([^|\n]+?)\s*：\s*([^|\n]*)', seg)
            if title_m:
                title = title_m.group(1).strip()
                inline_text = title_m.group(2).strip()

            # 台词：跳过 ogg 所在行与 wikitable 结构行，保留含中文的内容
            text_parts: list[str] = []
            if inline_text:
                text_parts.append(inline_text)
            for line in seg.splitlines()[1:]:
                cleaned = line.strip().lstrip("|").strip()
                if not cleaned:
                    continue
                if cleaned.startswith("width=") or cleaned.startswith("style="):
                    continue
                if re.search(r'[\u4e00-\u9fff]', cleaned):
                    # 排除与标题重复的片段（标题已在 title 中）
                    if title and cleaned == f"{title}：":
                        continue
                    text_parts.append(cleaned)

            text = "".join(text_parts).strip()
            if file_name and (title or text):
                voice_lines.append({"file": file_name, "title": title, "text": text})
        return voice_lines

    def _parse_skill_voice(self, section_text: str) -> list[dict]:
        """解析技能语音小节为 [{file, skill_index, skill_label, skill_name}]。

        每行格式：|width="135px"|技能二-语气词||width="75px"|[[文件:Battle_s2_10201_1.ogg|50px]]||技能二-台词
        Battle_s<技能序号>_<人格ID>_<编号>.ogg 中的 s<技能序号> 对应技能序号（1-based）。
        """
        results: list[dict] = []
        line_ogg_re = re.compile(r'\[\[\s*文件\s*:\s*(Battle_s(\d+)_[^|\]]+\.ogg)')
        line_name_re = re.compile(r'(技能\s*[一二三四五六七八九十]+)')
        for line in section_text.splitlines():
            m = line_ogg_re.search(line)
            if not m:
                continue
            file_name = m.group(1).strip()
            skill_index = int(m.group(2))
            label = ""
            nm = line_name_re.search(line)
            if nm:
                label = nm.group(1).strip()
            # 关联真实技能名（若有 {{技能链接}} 映射，如 2 -> 上挑）
            real_name = self._persona_skill_name_map.get(skill_index, "")
            results.append({
                "file": file_name,
                "skill_index": skill_index,
                "skill_label": label,
                "skill_name": real_name,
            })
        return results


class EgoExtractor:
    """E.G.O 页面 HTML 提取器"""

    def __init__(self, html: str, title: str, categories: list[str]):
        if not HAS_BS4:
            raise ImportError("beautifulsoup4 未安装")
        self.soup = BeautifulSoup(html, "lxml")
        _strip_tooltip_preload(self.soup)
        self.title = title
        self.categories = categories
        self.content_div = self.soup.select_one("#mw-content-text")
        if not self.content_div:
            self.content_div = self.soup
        self.mw_output = self.content_div.select_one(".mw-parser-output") if self.content_div else None
        if not self.mw_output:
            self.mw_output = self.content_div

    def extract(self) -> EgoData:
        short_name = self.title
        if "-" in short_name:
            parts = short_name.rsplit("-", 1)
            if len(parts) == 2:
                short_name = parts[0]

        data = EgoData(
            title=self.title,
            sinner=_extract_sinner_from_title(self.title),
            sinner_id=_extract_sinner_id(self.title),
            ego_name=short_name,
        )

        if not self.mw_output:
            return data

        self._extract_basic_info(data)
        self._extract_ego_skills(data)
        self._extract_passive(data)

        return data

    def _extract_basic_info(self, data: EgoData):
        """提取 E.G.O 基本信息。"""
        tables = self.mw_output.select("table.wikitable")
        if not tables:
            return

        info_table = tables[0]
        rows = info_table.select("tr")

        # ── 1) 实装日期 ──
        all_text = info_table.get_text(" ", strip=True)
        date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", all_text)
        if date_match:
            data.release_date = date_match.group(1)

        # ── 2) 获取方式 ──
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            if "获取方式" in row_text:
                idx = list(rows).index(row)
                if idx + 1 < len(rows):
                    next_text = rows[idx + 1].get_text(" ", strip=True)
                    if "提取" in next_text or "赛季" in next_text or "活动" in next_text:
                        data.acquisition = next_text

        # ── 3) 资源消耗：Row with ×N values + sin icons, sequential pairing ──
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            sin_imgs = row.select('img[alt*="罪孽-"]')
            # Skip rows that are not resource rows (e.g. 罪孽抗性)
            if sin_imgs and "资源消耗" not in row_text and "罪孽抗性" not in row_text:
                # Extract all ×N values from row_text
                counts = re.findall(r"×\s*(\d+)", row_text)
                if len(counts) == len(sin_imgs):
                    for i, img in enumerate(sin_imgs):
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            data.resource_costs[sin_type] = int(counts[i])
                else:
                    for img in sin_imgs:
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            parent_text = img.parent.get_text(strip=True) if img.parent else ""
                            count_match = re.search(r"×\s*(\d+)", parent_text)
                            count = int(count_match.group(1)) if count_match else 1
                            data.resource_costs[sin_type] = count

        # ── 4) 罪孽抗性：Row with [×N] values + sin icons, sequential pairing ──
        for row in rows:
            row_text = row.get_text(" ", strip=True)
            sin_imgs = row.select('img[alt*="罪孽-"]')
            if sin_imgs and "罪孽抗性" in row_text:
                # The row_text contains both the label and the values
                # Extract [×N] patterns
                values = re.findall(r"\[×(\d+\.?\d*)\]", row_text)
                if len(values) == len(sin_imgs):
                    for i, img in enumerate(sin_imgs):
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            try:
                                data.sin_resistances[sin_type] = float(values[i])
                            except ValueError:
                                pass
                else:
                    for img in sin_imgs:
                        sin_type = _parse_sin_icon_filename(img)
                        if sin_type:
                            parent_text = img.parent.get_text(strip=True) if img.parent else ""
                            val = _parse_resistance_value(parent_text)
                            if val is not None:
                                data.sin_resistances[sin_type] = val

    def _extract_ego_skills(self, data: EgoData):
        """提取觉醒和侵蚀的技能数据。
        
        E.G.O 页面结构: info table, 觉醒table, 侵蚀table, 被动table
        觉醒/侵蚀 table 特征: 第一行有 "E.G.O觉醒" 或 "E.G.O侵蚀"
        """
        if not self.mw_output:
            return

        tables = self.mw_output.select("table.wikitable")
        if len(tables) < 2:
            return

        current_mode = None  # "awakening" or "erosion"

        for table in tables[1:]:
            first_row = table.select_one("tr")
            first_row_text = first_row.get_text(" ", strip=True) if first_row else ""

            # Detect mode from first row text
            if "E.G.O觉醒" in first_row_text:
                current_mode = "awakening"
                stage_data = self._extract_ego_stage_from_table(table)
                stage_data["mode"] = "awakening"
                data.awakening_stages.append(stage_data)
            elif "E.G.O侵蚀" in first_row_text:
                current_mode = "erosion"
                stage_data = self._extract_ego_stage_from_table(table)
                stage_data["mode"] = "erosion"
                data.erosion_stages.append(stage_data)
            # Otherwise it's a passive or other table, skip (current_mode unchanged but not used)

    def _extract_ego_stage_from_table(self, table) -> dict:
        """从 E.G.O 技能表格提取阶段数据。"""
        table_text = table.get_text(" ", strip=True)

        from crawler.buffs_data import resolve_buff_codes_in_text

        # 硬币数量
        coin_count = 0
        coin_match = re.search(r"硬币\s*[×xX]\s*(\d+)", table_text)
        if coin_match:
            coin_count = int(coin_match.group(1))

        # 理智消耗
        sanity_cost = 0
        sanity_match = re.search(r"理智消耗[：:]\s*(\d+)", table_text)
        if sanity_match:
            sanity_cost = int(sanity_match.group(1))

        # 攻击加权（" +0" 格式）
        attack_bonus = 0
        attack_match = re.search(r"\+(\d+)", table_text)
        if attack_match:
            attack_bonus = int(attack_match.group(1))

        # 提取 tab-pane 中的 I-IV 阶数据
        tab_panes = table.select('div[role="tabpanel"].tab-pane')
        stage_data_list = []

        stage_labels = ["I", "II", "III", "IV"]
        for i, pane in enumerate(tab_panes):
            stage = stage_labels[i] if i < len(stage_labels) else str(i + 1)
            pane_text = pane.get_text(" ", strip=True)

            base_value = None
            coin_power = None
            attack_capacity = 1

            bv_match = re.search(r"基础值[：:]\s*(\d+)", pane_text)
            if bv_match:
                base_value = int(bv_match.group(1))

            cp_match = re.search(r"硬币威力[：:]\s*([+-]?\d+)", pane_text)
            if cp_match:
                coin_power = int(cp_match.group(1))

            ac_match = re.search(r"攻击容量[：:]\s*(\d+)", pane_text)
            if ac_match:
                attack_capacity = int(ac_match.group(1))

            effects = []
            timing_pattern = re.findall(r"\[([^\]]+)\]([^\[]*)", pane_text)
            for timing, desc in timing_pattern:
                desc = desc.strip()
                if desc:
                    desc = resolve_buff_codes_in_text(desc)
                    effects.append({"timing": timing, "description": desc})

            stage_data_list.append({
                "stage": stage,
                "base_value": base_value,
                "coin_power": coin_power,
                "attack_capacity": attack_capacity,
                "effects": effects,
                "raw_text": resolve_buff_codes_in_text(pane_text),
            })

        return {
            "coin_count": coin_count,
            "sanity_cost": sanity_cost,
            "attack_bonus": attack_bonus,
            "stages": stage_data_list,
        }

    def _extract_passive(self, data: EgoData):
        """提取 E.G.O 被动。
        
        E.G.O 被动在最后一个 wikitable 中（非觉醒/侵蚀 table）。
        特征：含 "回合" 且不含 "E.G.O觉醒"/"E.G.O侵蚀"。
        """
        if not self.mw_output:
            return

        tables = self.mw_output.select("table.wikitable")
        for table in tables:
            first_row = table.select_one("tr")
            first_row_text = first_row.get_text(" ", strip=True) if first_row else ""

            # 跳过觉醒/侵蚀表
            if "E.G.O觉醒" in first_row_text or "E.G.O侵蚀" in first_row_text:
                continue
            # 跳过 info 表（含登场时间/获取方式/资源消耗/罪孽抗性）
            if any(kw in first_row_text for kw in ["登场时间", "获取方式", "资源消耗", "罪孽抗性"]):
                continue
            # 跳过空表
            if not first_row_text:
                continue

            # 这应该是被动表
            rows = table.select("tr")
            for row in rows:
                row_text = row.get_text(" ", strip=True)
                if not row_text:
                    continue
                # 第一行通常是被动名称，第二行是描述
                if not data.passive_name and len(row_text) < 40:
                    data.passive_name = row_text
                elif len(row_text) > 10:
                    if not data.passive_description:
                        data.passive_description = row_text
                    else:
                        data.passive_description += " " + row_text


# ── 敌方单位提取器 ──

# 物理抗性图标 alt 文本 → 类型映射
_PHYSICAL_ICON_PATTERNS: dict[str, str] = {
    "斩击": "斩击",
    "突刺": "突刺",
    "打击": "打击",
}

# 罪孽图标 alt 文本 → 类型映射（扩展版，覆盖敌人页面中的罪孽图标格式）
_SIN_ICON_PATTERNS_ENEMY: dict[str, str] = {
    "罪孽-暴怒": "暴怒",
    "罪孽-色欲": "色欲",
    "罪孽-怠惰": "怠惰",
    "罪孽-暴食": "暴食",
    "罪孽-忧郁": "忧郁",
    "罪孽-傲慢": "傲慢",
    "罪孽-嫉妒": "嫉妒",
}


class EnemyExtractor:
    """敌方单位页面 HTML 提取器。

    支持两种 HTML 结构：
    1. 主线战斗格式：每个敌人以 <h4> 开头，独立 table[width:500px]，技能在 mw-collapsible 中
    2. 异想体数据格式（如 1-11-1-情报部 连接通道）：<h4> 后跟 <center> 包裹的
       单一 table[width:700px]，rowspan 立绘，多部位内嵌在同一张表中，
       被动以 <li> 嵌入，技能在 <h3>技能</h3> 后的 mw-collapsible 中
    """

    def __init__(self, html: str, title: str, categories: list[str], wikitext: str = ""):
        if not HAS_BS4:
            raise ImportError("beautifulsoup4 未安装")
        self.soup = BeautifulSoup(html, "lxml")
        _strip_tooltip_preload(self.soup)
        self.title = title
        self.categories = categories
        self.wikitext = wikitext
        # ── BuffPro 页面级配对映射（修复"状态效果显示英文"）──
        # 渲染 HTML 中 buffPro span 的中文名 与 wikitext {{BuffPro|Code}} 顺序配对，
        # 覆盖映射表缺失的专属 code（ChoiSwordsmanship 等）。仅当 HTML 为
        # JS 渲染后（含中文名链接）时有效；action=parse 服务端 HTML 返回空 dict。
        self._buff_code_map: dict[str, str] = {}
        try:
            from crawler.buffs_data import build_buff_code_map_from_html
            self._buff_code_map = build_buff_code_map_from_html(html, wikitext)
        except Exception as e:
            logger.debug(f"BuffPro 页面级配对映射构建失败，回落静态表: {e}")
        # 预解析 wikitext 中的技能名称映射（图标编号 -> 技能名称）
        self._skill_name_map: dict[str, str] = {}
        # 始终初始化 wikitext 技能归属/完整技能容器，保证 wikitext='' 时
        # _apply_wikitext_skill_attribution 也能安全访问（不触发 AttributeError）
        self._enemy_skill_icons: dict[str, list[str]] = {}
        self._wikitext_enemy_skills: dict[str, list[dict]] = {}
        if wikitext:
            self._parse_skill_names_from_wikitext()
            self._parse_enemy_skill_icons_from_wikitext()
            self._parse_enemy_skills_from_wikitext()
        self.content_div = self.soup.select_one("#mw-content-text")
        if not self.content_div:
            self.content_div = self.soup
        self.mw_output = self.content_div.select_one(".mw-parser-output") if self.content_div else None

    def _parse_skill_names_from_wikitext(self):
        """从 wikitext 中解析技能图标编号与技能名称的映射。

        异想体技能的 wikitext 格式：
        {{敌方技能|技能名称=怀疑猜忌|技能类型=突刺|...|技能功率=2|技能硬币数=2|技能攻击等级=7|技能攻击容量=1|技能基础值=2|技能硬币加成=+1|技能描述={{正面|下回合对目标施加1层{{Status|易损}}}}|技能图标=8001001}}

        使用大括号计数法正确处理嵌套的 {{...}} 模板。
        """
        pattern_start = re.compile(r'\{\{敌方技能\s*\|')
        for match in pattern_start.finditer(self.wikitext):
            # 从 {{敌方技能| 开始计数大括号深度
            depth = 0
            i = match.start()
            while i < len(self.wikitext):
                if self.wikitext[i:i+2] == '{{':
                    depth += 1
                    i += 2
                elif self.wikitext[i:i+2] == '}}':
                    depth -= 1
                    if depth == 0:
                        # 找到匹配的 }}，提取模板内容
                        template_content = self.wikitext[match.start()+2:i]
                        # 兼容长参数（技能名称/技能图标）与短参数（名称/图标）两种形式
                        name_match = re.search(
                            r'(?:技能名称|名称)\s*=\s*([^|]+)', template_content
                        )
                        icon_match = re.search(
                            r'(?:技能图标|图标)\s*=\s*(\d+)', template_content
                        )
                        if name_match and icon_match:
                            skill_name = name_match.group(1).strip()
                            icon_id = icon_match.group(1).strip()
                            if icon_id in self._skill_name_map and \
                                    self._skill_name_map[icon_id] != skill_name:
                                # 同一图标对应不同技能名（如 9-38 摩西 40001003 两个技能）：
                                # 删除映射，回退到渲染文本技能名
                                del self._skill_name_map[icon_id]
                            else:
                                self._skill_name_map[icon_id] = skill_name
                            logger.debug(
                                f"Wikitext 技能名称映射: 图标 {icon_id} -> {skill_name}"
                            )
                        break
                    i += 2
                else:
                    i += 1

    def _parse_enemy_skill_icons_from_wikitext(self):
        """从 wikitext 按 ===敌人名===（h3）小节划分敌方技能图标归属。

        9-38 中马蒂亚斯/绮罗/摩西/以斯拉的 <div class="mw-collapsible"> 未闭合
        （直到 874 行才 </div></div>）、韦斯帕技能为裸 {{敌方技能}}（无 div 包裹），
        导致 DOM 收集时技能逐单位向后越界（马蒂亚斯收到绮罗+摩西+以斯拉+韦斯帕
        的技能）、韦斯帕技能完全缺失。以 wikitext 为权威数据源，按 h3 小节记录
        每个敌人（含援助单位）的技能图标顺序列表，供 extract() 末尾统一重排。

        图标 ID 可能为 6 位（132701/114501）、7 位（9021704）或 8 位（40001001）。
        """
        self._enemy_skill_icons: dict[str, list[str]] = {}
        if not self.wikitext:
            return

        # 按 h2/h3 标题切分：h3 设置当前敌人归属，h2 清空归属（如 ==援助单位==）。
        # P38：统一 h2~h5 标题归属切分。
        # P25：区段标题（技能/被动/基本信息等）不切换归属，继承最近的敌人名；
        # 非区段标题视为敌人名（折射轨道 h2 敌人 + h3"技能"、经验采光 h4 敌人
        # 如 ====苍白之物-棍==== 等），与 extract() 中 h2/h3/h4/h5 均视为
        # 敌人标题的处理保持一致。
        _SECTION_KW = (
            "技能", "被动", "基本信息", "战斗信息", "主线导航", "导航",
            "援助单位", "友方单位", "事件", "器物", "波次", "理智值", "特殊语音",
        )
        current_enemy: Optional[str] = None
        sections: list[tuple[Optional[str], str]] = []
        buf_lines: list[str] = []
        _HEADING_RE = re.compile(r'^(={2,5})([^=]+)\1\s*$')
        for line in self.wikitext.splitlines():
            stripped = line.strip()
            hm = _HEADING_RE.match(stripped)
            if hm:
                sections.append((current_enemy, "\n".join(buf_lines)))
                buf_lines = []
                level = len(hm.group(1))
                name = hm.group(2).strip()
                if level == 2:
                    # h2：非区段 → 敌人名；区段 → 清空归属
                    if not any(k in name for k in _SECTION_KW):
                        current_enemy = name
                        self._enemy_skill_icons.setdefault(current_enemy, [])
                    else:
                        current_enemy = None
                else:
                    # h3/h4/h5：非区段 → 敌人名；区段 → 继承当前归属
                    if not any(k in name for k in _SECTION_KW):
                        current_enemy = name
                        self._enemy_skill_icons.setdefault(current_enemy, [])
            else:
                buf_lines.append(line)
        sections.append((current_enemy, "\n".join(buf_lines)))

        # 对每个有归属的小节，用大括号计数法提取 {{敌方技能 模板的 图标= 参数
        pattern_start = re.compile(r'\{\{敌方技能\s*\|')
        for enemy_name, text in sections:
            if enemy_name is None:
                continue
            for match in pattern_start.finditer(text):
                depth = 0
                i = match.start()
                while i < len(text):
                    if text[i:i + 2] == '{{':
                        depth += 1
                        i += 2
                    elif text[i:i + 2] == '}}':
                        depth -= 1
                        if depth == 0:
                            template_content = text[match.start() + 2:i]
                            icon_match = re.search(
                                # P38：兼容中文图标名（如 黑檀皇后的苹果技能图标2），
                                # 不再要求纯数字——数字图标用于 DOM 配对，中文图标
                                # 在 _apply_wikitext_skill_attribution 走技能名兜底
                                r'(?:技能图标|图标)\s*=\s*([^\n|]+)', template_content
                            )
                            if icon_match:
                                self._enemy_skill_icons[enemy_name].append(
                                    icon_match.group(1).strip()
                                )
                            break
                        i += 2
                    else:
                        i += 1

    def _parse_enemy_skills_from_wikitext(self) -> None:
        """从 wikitext 按 h3 小节解析敌方技能完整字段，构建技能 dict 列表。

        与 _parse_enemy_skill_icons_from_wikitext 同一套 h3 归属划分，但额外解析
        完整字段（名称/图标/等级/硬币数/修正值/守备/进攻/罪孽/类型/攻击容量/
        基础值/变动值/效果）。结果存入 self._wikitext_enemy_skills，供
        _apply_wikitext_skill_attribution 在 DOM 收集缺失技能（如重要性=3 的
        强力攻击）时完整重建。

        注意：9-38 中摩西 40001003 出现两次且技能名不同（DOM pool 无法区分），
        但 wikitext 重建按顺序逐条展开，天然保留两个技能。
        """
        self._wikitext_enemy_skills: dict[str, list[dict]] = {}
        if not self.wikitext:
            return

        # 与图标归属同一套 h2~h5 切分逻辑（P38：含 h4 敌人标题，如 经验采光-5
        # ====苍白之物-棍====；P25：h3+ 区段标题继承归属，非区段标题视为敌人名）
        _SECTION_KW = (
            "技能", "被动", "基本信息", "战斗信息", "主线导航", "导航",
            "援助单位", "友方单位", "事件", "器物", "波次", "理智值", "特殊语音",
        )
        current_enemy: Optional[str] = None
        sections: list[tuple[Optional[str], str]] = []
        buf_lines: list[str] = []
        _HEADING_RE = re.compile(r'^(={2,5})([^=]+)\1\s*$')
        for line in self.wikitext.splitlines():
            stripped = line.strip()
            hm = _HEADING_RE.match(stripped)
            if hm:
                sections.append((current_enemy, "\n".join(buf_lines)))
                buf_lines = []
                level = len(hm.group(1))
                name = hm.group(2).strip()
                if level == 2:
                    if not any(k in name for k in _SECTION_KW):
                        current_enemy = name
                        self._wikitext_enemy_skills.setdefault(current_enemy, [])
                    else:
                        current_enemy = None
                else:
                    if not any(k in name for k in _SECTION_KW):
                        current_enemy = name
                        self._wikitext_enemy_skills.setdefault(current_enemy, [])
            else:
                buf_lines.append(line)
        sections.append((current_enemy, "\n".join(buf_lines)))

        pattern_start = re.compile(r'\{\{敌方技能\s*\|')
        for enemy_name, text in sections:
            if enemy_name is None:
                continue
            for match in pattern_start.finditer(text):
                depth = 0
                i = match.start()
                while i < len(text):
                    if text[i:i + 2] == '{{':
                        depth += 1
                        i += 2
                    elif text[i:i + 2] == '}}':
                        depth -= 1
                        if depth == 0:
                            template_content = text[match.start() + 2:i]
                            skill = self._build_skill_dict_from_wikitext(template_content)
                            if skill:
                                self._wikitext_enemy_skills[enemy_name].append(skill)
                            break
                        i += 2
                    else:
                        i += 1

    def _build_skill_dict_from_wikitext(self, template_content: str) -> Optional[dict]:
        """从单个 {{敌方技能}} 模板内容构建技能 dict（与 DOM 提取同 schema）。

        字段值以 `|参数=值` 形式（首行可能紧跟模板名，如 8-30 的
        `{{敌方技能|` 后直接是 `|名称=...`，9-38 为 `{{敌方技能` + 换行）。
        用正则逐字段提取，兼容 6-8 位图标与 修正值=??? 等占位值。

        兼容两套字段命名（实测并存）：
        - 主线战斗系：名称/图标/等级/硬币数/修正值/罪孽/类型/攻击容量/基础值/变动值/效果
        - 异想体系：  技能名称/技能图标/技能类型/技能硬币数/技能攻击等级/技能属性/
                      技能攻击容量/技能基础值/技能硬币加成/技能描述
        注意：`等级`/`技能等级` 是技能阶数（1/2/3），不是攻击等级；
        攻击等级来自 `修正值`/`技能攻击等级`。

        P38：数值字段以 wikitext 为权威（DOM 侧 base64 污染/图标计数/冒号缺失
        均会导致劣化），并在返回 dict 中记录 `_wt_present` 字段存在集合，
        供 _apply_wikitext_skill_attribution 合并时判断「模板显式给出该字段」。
        """
        def _field(*names: str, to_line_end: bool = False) -> str:
            """提取参数值。

            P38：改为扫描至下一个顶层 `|`（跳过 {{...}}/[[...]] 嵌套），
            支持值内嵌模板（如 `|修正值=?({{#html:Hide|{"content":"50"} }})`、
            `|效果=...{{硬币|1}}...`），不再被值内的 `|` 截断。
            to_line_end=True 时取到行尾（效果字段，兼容超长行）。
            """
            for n in names:
                m = re.search(
                    r'(?:^|\n)\s*\|\s*' + re.escape(n) + r'\s*=\s*',
                    template_content,
                )
                if not m:
                    continue
                start = m.end()
                if to_line_end:
                    end = template_content.find("\n", start)
                    if end == -1:
                        end = len(template_content)
                    return template_content[start:end].strip()
                depth = 0
                i = start
                while i < len(template_content):
                    if template_content[i:i + 2] in ("{{", "[["):
                        depth += 1
                        i += 2
                        continue
                    if template_content[i:i + 2] in ("}}", "]]"):
                        depth = max(0, depth - 1)
                        i += 2
                        continue
                    if template_content[i] == "|" and depth == 0:
                        break
                    i += 1
                return template_content[start:i].strip()
            return ""

        present: set[str] = set()  # 模板显式给出的字段名（供合并覆盖判断）

        # P38：部分敌方页面（如 主线战斗7-26 白月骑士）将真实数值隐藏在
        # {{#html:Hide|{"content":"50"} }} 内（页面显示 "?"），此处提取隐藏值。
        _HIDDEN_NUM_RE = re.compile(r'"content"\s*:\s*"?(-?\d+)"?')

        def _unhide(text: str) -> str:
            m = _HIDDEN_NUM_RE.search(text)
            return m.group(1) if m else text

        name = _field("技能名称", "名称")
        icon = _field("技能图标", "图标")
        if not name or not icon:
            return None

        # 基础值：可为负（-3）或占位（???）
        base_text = _unhide(_field("基础值", "技能基础值"))
        base_value = 0
        if base_text:
            present.add("base_value")
            try:
                base_value = int(base_text)
            except (ValueError, TypeError):
                base_value = 0

        # 硬币威力：变动值形如 +2 / -1（保留符号，减算硬币为负）
        coin_power = 0
        change_text = _unhide(_field("变动值", "技能硬币加成", "硬币加成"))
        if change_text:
            present.add("coin_power")
            pm = re.match(r'[+-]?\d+', change_text.strip())
            if pm:
                try:
                    coin_power = int(pm.group(0))
                except ValueError:
                    coin_power = 0

        # 硬币数量
        coin_count = 0
        cc_text = _unhide(_field("硬币数", "技能硬币数"))
        if cc_text:
            present.add("coin_count")
            try:
                coin_count = int(cc_text)
            except (ValueError, TypeError):
                coin_count = 0

        # 攻击等级（修正值/技能攻击等级，非 等级）；攻击容量
        attack_level = 0
        al_text = _unhide(_field("修正值", "技能攻击等级", "攻击等级"))
        if al_text:
            present.add("attack_level")
            try:
                attack_level = int(al_text)
            except (ValueError, TypeError):
                attack_level = 0
        attack_weight = 1
        aw_text = _unhide(_field("攻击容量", "技能攻击容量"))
        if aw_text:
            present.add("attack_weight")
            try:
                attack_weight = int(aw_text)
            except (ValueError, TypeError):
                attack_weight = 1

        # 罪孽 / 伤害类型
        sin_type = _field("罪孽", "技能属性")
        damage_type = _field("类型", "技能类型")
        # 效果字段含内嵌 {{...|...}} 模板，需取到行尾（to_line_end=True）；
        # 必须先于重要性解析定义，因为重要性可内嵌于效果字段（修复③）
        effect_text = _field("效果", "技能描述", to_line_end=True)
        # 重要性（修复③）：|重要性=N 直接字段，或效果字段内嵌 {{重要性|N|名称}}
        importance = 0
        imp_text = _field("重要性")
        if not imp_text:
            imp_match = re.search(r'\{\{重要性\|(\d+)', effect_text)
            if imp_match:
                imp_text = imp_match.group(1)
        importance = _parse_importance(imp_text)
        # 守备类型：类型为"守备/闪避/反击"，或效果以"可拼点反击/闪避/防御"标记
        # （P38：仅精确匹配可拼点前缀，避免"本技能不会触发目标的守备技能"等
        # 效果文本含"守备"字样的攻击技能被误判为守备技能）
        is_guard = False
        guard_type = ""
        if damage_type in ("守备", "闪避", "反击") or "守备" in damage_type:
            is_guard = True
            guard_type = damage_type
        else:
            gp = re.search(r"可拼点(反击|闪避|防御)", effect_text)
            if gp:
                is_guard = True
                guard_type = gp.group(1)

        from crawler.buffs_data import resolve_buff_codes_in_text

        # 硬币效果：从效果字段拆分 {{硬币|N}} 后的 {{颜色|...}} 段落
        coin_effects: list[str] = []
        if effect_text:
            # 效果以 <br> 分隔，逐段收集含效果关键词或 [时机标签] 的段落
            for seg in effect_text.split("<br>"):
                seg_clean = seg.strip()
                if not seg_clean:
                    continue
                seg_clean = re.sub(r'^[★\s]+', '', seg_clean)
                # P38 格式归一化（修复与 DOM 链路的排版差异）：
                # {{硬币|N}} → "硬币N："（保留硬币编号），{{颜色|X}} → "[X]"（方括号时机标签）
                seg_readable = re.sub(r'\{\{硬币\|(\d+)\}\}', r'硬币\1：', seg_clean)
                seg_readable = re.sub(r'\{\{颜色\|([^}|]+)\}\}', r'[\1]', seg_readable)
                # 修复④：先解析 {{BuffPro|Code}} -> 中文名（页面级配对映射优先，
                # 回落 buffs_data 静态表），再做通用模板清洗
                seg_readable = _resolve_buffpro_in_text(seg_readable, self._buff_code_map)
                # {{状态2|A|...}} / {{状态|A|...}} → A（状态名；P38 补：白月骑士等
                # 页面用 {{状态2|不可摧毁的硬币|4=特殊}} 而非 BuffPro）
                seg_readable = re.sub(r'\{\{状态2?\|([^|}]+)(?:\|[^}]*)?\}\}', r'\1', seg_readable)
                # 通用多参数模板 → 取第 2 参数（显示名）
                seg_readable = re.sub(r'\{\{[^{}|]+\|([^}|]+)\|[^}|]+\}\}', r'\1', seg_readable)
                # 通用两参数模板 {{X|Y}} → Y
                seg_readable = re.sub(r'\{\{[^|}]+\|([^}|]+)\}\}', r'\1', seg_readable)
                # 剥离 wikitext 效果中的内联 HTML 样式残留（如
                # <span style="color:#ff6000;...">攻击类型与罪孽属性</span>）
                seg_readable = re.sub(r'<[^>]+>', '', seg_readable)
                # 修复 P24-3：与 DOM 链路一致，补充通用纯英文 buff code 中文替换
                seg_readable = resolve_buff_codes_in_text(
                    seg_readable, extra_map=self._buff_code_map
                )
                seg_readable = seg_readable.strip()
                if not seg_readable:
                    continue
                # 修复 P24-1：放宽过滤——含效果/时机关键词 或 [方括号时机标签] 即收集，
                # 避免 [使用时]/[攻击后] 等无"命中时"字样的效果段被整体丢弃。
                # P25：补充无时机标签的效果段关键词（折射轨道"影响即将到来的过去 /
                # 对关卡N的敌方单位施加XX"等），避免此类技能效果整段丢失。
                _effect_kw = (
                    "命中时", "未摧毁", "使用时", "攻击后", "使用前", "若命中",
                    "若击杀", "若敌方", "目标", "本回合", "当回合", "下回合",
                    "每回合", "充能", "呼吸法", "流血", "烧伤", "斩击", "突刺",
                    "打击", "施加", "获得", "失去", "影响", "拼点", "本技能",
                    "不可摧毁", "硬币", "鳞粉", "过去", "现在", "未来",
                    "使目标", "使自身", "自身", "回合结束时", "优先",
                )
                if any(k in seg_readable for k in _effect_kw) or re.search(r'\[[^\]]+\]', seg_readable):
                    coin_effects.append(seg_readable)
                if len(coin_effects) >= 12:  # 防止效果过长撑爆 chunk
                    break

        return {
            "skill_name": name,
            "icon_id": icon,
            "sin_type": sin_type,
            "damage_type": damage_type,
            "base_value": base_value,
            "coin_power": coin_power,
            "coin_count": coin_count,
            "attack_level": attack_level,
            "attack_weight": attack_weight,
            "is_guard": is_guard,
            "guard_type": guard_type,
            "importance": importance,
            "coin_effects": coin_effects,
            "_wt_present": sorted(present),
        }

    def _apply_wikitext_skill_attribution(self, enemies: list[EnemyData]) -> None:
        """以 wikitext 为权威，按每个敌人的图标顺序列表重排技能归属。

        在 extract() 末尾调用。解决未闭合 div 导致的 DOM 技能越界收集与
        裸 {{敌方技能}} 导致的技能缺失（如 9-38 韦斯帕）。无 wikitext 的页面
        （_enemy_skill_icons 为空）跳过，不影响回归测试。

        当某敌人 wikitext 技能数 > DOM 收集数时（说明 DOM 收集遗漏了部分技能，
        如重要性=3 的强力攻击被 _extract_enemy_skills_from_collapsible 的过滤
        条件丢弃），直接用 wikitext 完整重建该敌人技能列表，缺失部分由
        _build_skill_dict_from_wikitext 补齐。
        """
        if not self._enemy_skill_icons:
            return
        # 构建全局技能池：图标ID -> 技能 dict 列表（保序，含重复图标如 40001003）
        pool: dict[str, list[dict]] = {}
        for e in enemies:
            for s in e.skills:
                icon = s.get("icon_id") or ""
                if icon:
                    pool.setdefault(icon, []).append(s)
        # 按每个敌人的 wikitext 图标顺序 pop(0) 分配
        for e in enemies:
            icons = self._enemy_skill_icons.get(e.enemy_name)
            if not icons:
                continue  # wikitext 无此敌人小节：保留 DOM 收集结果
            # wikitext 为权威：只要该敌人有完整 wikitext 技能列表就始终重建。
            # 不能依赖 len(icons) > len(e.skills) 判定缺失——未闭合 div 会导致 DOM
            # 越界收集，使 e.skills 数量 >= 图标数（如 9-38 马蒂亚斯/绮罗），
            # 从而使该条件不成立、重建被跳过，重要性=3 技能仍缺失。
            wt_skills = self._wikitext_enemy_skills.get(e.enemy_name, [])
            if wt_skills and len(wt_skills) >= len(icons):
                # 复用 DOM 技能（图标匹配），缺失的用 wikitext 构建
                rebuilt: list[dict] = []
                used_dom: set[int] = set()
                for wt in wt_skills:
                    wt_icon = wt.get("icon_id") or ""
                    wt_name = (wt.get("skill_name") or "").strip()
                    dom_match = None
                    for idx, s in enumerate(e.skills):
                        if idx in used_dom:
                            continue
                        if (s.get("icon_id") or "") == wt_icon:
                            dom_match = (idx, s)
                            break
                    # P38：图标未配对成功时用技能名兜底——部分页面图标为中文名
                    # （如 黑檀皇后的苹果技能图标2），或 DOM 图标 id 缺失
                    if dom_match is None and wt_name:
                        for idx, s in enumerate(e.skills):
                            if idx in used_dom:
                                continue
                            if (s.get("skill_name") or "").strip() == wt_name:
                                dom_match = (idx, s)
                                break
                    if dom_match:
                        used_dom.add(dom_match[0])
                        # P38 合并策略：wikitext 为权威。
                        # 此前（P21-A）仅当 DOM 值为默认/缺失时才回填 wikitext 值，
                        # 导致 DOM 侧劣化值（coin_power 命中 base64 数字 854/987/9703、
                        # coin_count 误计 不可摧毁的硬币/硬币.png 图标、attack_weight
                        # 因渲染文本无冒号恒为 1、base_value 选择器命中 0）无法被纠正。
                        # 现改为：凡 wikitext 模板显式给出某数值字段（_wt_present），
                        # 一律以 wikitext 值覆盖 DOM（数值/类型/守备字段）。
                        s = dict(dom_match[1])
                        wt_present = set(wt.get("_wt_present") or [])
                        if "base_value" in wt_present:
                            s["base_value"] = wt["base_value"]
                        if "coin_power" in wt_present:
                            s["coin_power"] = wt["coin_power"]
                        if "coin_count" in wt_present:
                            s["coin_count"] = wt["coin_count"]
                        if "attack_level" in wt_present:
                            s["attack_level"] = wt["attack_level"]
                        if "attack_weight" in wt_present:
                            s["attack_weight"] = wt["attack_weight"]
                        if wt.get("damage_type") and not s.get("damage_type"):
                            s["damage_type"] = wt["damage_type"]
                        if wt.get("sin_type") and not s.get("sin_type"):
                            s["sin_type"] = wt["sin_type"]
                        # P38：守备/反击 以 wikitext 侧检测为准（DOM 侧"本技能不会
                        # 触发目标的守备技能"等效果文本会误判攻击技能为守备技能）。
                        if wt.get("is_guard"):
                            s["is_guard"] = True
                            if wt.get("guard_type"):
                                s["guard_type"] = wt["guard_type"]
                        elif "守备" in (wt.get("damage_type") or ""):
                            s["is_guard"] = True
                            s["guard_type"] = wt.get("guard_type") or "守备"
                        else:
                            # wikitext 明确为非守备技能（类型为攻击属性）时，
                            # 纠正 DOM 的守备误判（如"本技能不会触发目标的守备技能"）
                            if wt.get("damage_type"):
                                s["is_guard"] = False
                                s["guard_type"] = ""
                        # P38 效果来源：wikitext 侧为权威（逐 <br> 分段 + [时机] 归一化 +
                        # 硬币N：前缀），结构比 DOM 更完整（DOM 会把硬币图标 alt 文本
                        # "不可摧毁的硬币" 粘连进效果行、丢失 拼点胜利/拼点失败 分段）。
                        # 仅当 wikitext 无效果字段时才保留 DOM 效果。
                        if wt.get("coin_effects"):
                            s["coin_effects"] = wt["coin_effects"]
                        rebuilt.append(s)
                    else:
                        rebuilt.append({k: v for k, v in wt.items() if k != "_wt_present"})
                if rebuilt:
                    e.skills = rebuilt
                continue
            reassigned: list[dict] = []
            for icon in icons:
                bucket = pool.get(icon)
                if not bucket:
                    continue
                reassigned.append(bucket.pop(0))
            if reassigned:
                e.skills = reassigned

    def extract(self) -> list[EnemyData]:
        """提取页面中所有敌方单位数据。

        不使用 width 样式定位表格，改为基于内容特征判断格式：
        - 如果表格包含 td[colspan="7"][style*="background:#000000"]（黑色部位标题行），
          则为多部位异想体格式，使用 _extract_abno_parts()
        - 否则使用 _extract_stats()，兼容主线战斗格式和单部位异想体格式

        去重：基于 (hp, physical_resistances, sin_resistances) 指纹去重。
        名字不同但 HP 与抗性完全相同视为同一单位（复用），只保留第一个。
        """
        if not self.mw_output:
            return []

        enemies = []
        # 查找所有标题（每个敌人一个）。
        # 不同页面敌人标题层级不同：
        # - 主线战斗1-10 / 1-11-2：敌人为 <h4>，技能为 <h3>技能</h3>
        # - 主线战斗9-02 / 9-49 / 事件-使用红色通行证：敌人为 <h3>，技能为 <h4>技能</h4>
        # - 折射轨道6号线-第一区段 / 主线战斗9-51 / 困难活动战斗7.5-01 / 7.5-10：
        #   敌人为 <h2>（如"折射的罗生蝶::蛹"），技能为 <h3>技能</h3>
        # 因此同时收集 h2/h3/h4/h5。BeautifulSoup 的 select() 返回顺序即为 DOM 文档顺序，
        # 无需依赖 sourceline（某些解析器/构建器可能不提供该属性）。
        headings = self.mw_output.select("h2, h3, h4, h5")

        # 区段标题关键词（技能、被动、基本信息、导航等），不作为敌人处理
        # 补充"理智值"/"特殊语音"：这些是页面内的非敌人小节标题（如 8-30 的
        # ====理智值====、9-38 的 ===E.G.O特殊语音===），若被当作敌人会产生假单位
        _SECTION_TITLE_KEYWORDS = (
            "基本信息", "战斗信息", "技能", "被动", "主线导航", "导航",
            "援助单位", "友方单位", "事件", "器物", "波次",
            "理智值", "特殊语音",
        )
        # 记录 h2 中已被当作敌人处理的标题文本，避免同一 h2 被下级
        # 查找逻辑或 h2 标题本身重复提取（详见下）。
        h2_seen_names: set[str] = set()

        for heading in headings:
            is_h2 = heading.name == "h2"
            headline = heading.select_one(".mw-headline")
            if not headline:
                continue
            enemy_name = headline.get_text(strip=True)
            if not enemy_name:
                continue

            # 跳过区段标题（技能/被动/基本信息/援助单位等），避免误当敌人
            if any(kw in enemy_name for kw in _SECTION_TITLE_KEYWORDS):
                continue

            # 援助单位下的 <h3> 由 _extract_ally_units() 专门处理，
            # 此处跳过，避免重复提取（h2 援助单位标题本身也会被上面关键词过滤）
            if self._is_inside_ally_section(heading):
                continue

            # 去重：h2 与 h3/h4/h5 可能同名（如页面同时有 h2 敌人总标题与
            # h3 子标题），只保留第一个，避免重复提取同一敌人
            if enemy_name in h2_seen_names:
                continue
            if is_h2:
                h2_seen_names.add(enemy_name)

            # 基于内容判断：查找 heading 后最近的 wikitable。
            # require_stat_icon=True：跳过无「数值图标-生命」的对话卡表格（P23b 修复，
            # 主线战斗 9-50 等页面第二阶段 h3 后先出现多张对话卡表，再出现真实属性表；
            # 若取第一张表会把对话卡当属性表解析出 hp=0，导致第二阶段被丢弃）。
            table = self._find_next_table(heading, "", require_stat_icon=True)
            if not table:
                continue

            # 检查是否为多部位异想体格式（有黑色背景 colspan=7 行）
            abno_part_header = table.select_one(
                'td[colspan="7"][style*="background:#000000"]'
            )
            if abno_part_header:
                parts = self._extract_abno_parts(table, enemy_name)
                if parts:
                    # 共享技能：所有部位共享同一套技能
                    collapsibles = self._find_following_collapsibles(heading)
                    for collapsible in collapsibles:
                        for part in parts:
                            self._extract_enemy_skills_from_collapsible(collapsible, part)
                    # 分布被动：如果只有第一个部位有被动，复制给其他部位
                    self._distribute_passives(parts)
                    enemies.extend(parts)
                continue

            # 单部位格式（主线战斗 / 单部位异想体 如 1-11-2）
            data = EnemyData(
                title=self.title,
                enemy_name=enemy_name,
                battle_stage=self.title,
            )
            self._extract_stats(table, data)
            # 查找技能
            collapsibles = self._find_following_collapsibles(heading)
            for collapsible in collapsibles:
                self._extract_enemy_skills_from_collapsible(collapsible, data)
            if data.hp > 0:  # 只保留成功提取的
                enemies.append(data)

        # 提取援助/友方单位（<h2>援助单位</h2> 下的 <h3> 单位）
        ally_units = self._extract_ally_units()
        enemies.extend(ally_units)

        # 修复②：以 wikitext 为权威，先按各敌人图标顺序列表重排技能归属
        # （解决未闭合 div 导致的 DOM 技能越界收集与裸 {{敌方技能}} 导致的技能缺失）。
        # 必须先于去重执行：若先去重，指纹基于未归属（可能缺失/越界）的技能列表，
        # 同技能单位会被误判为不同单位，重要性=3 技能也可能在归属前被指纹判定。
        self._apply_wikitext_skill_attribution(enemies)

        # 去重：指纹 (physical_resistances, sin_resistances, body_part, skill_names)
        # 修复②：去掉 hp —— 同一模型不同关卡 HP 可能不同但技能/抗性相同，
        # 应视为同一单位（跨关卡合并时由 exporter 聚合 appear_stages）。
        # 技能名不同视为不同单位；部位不同（body_part）视为不同单位。
        seen_fingerprints: set[tuple] = set()
        deduped: list[EnemyData] = []
        dedup_count = 0
        for e in enemies:
            # 技能 dict 的 key 在敌方/援助路径为 "skill_name"（_extract_enemy_skills_from_collapsible），
            # 人格路径为 "skill_name"（_extract_skills），此处同时兼容两种 key，
            # 避免技能名对比因 key 不匹配而永远返回空字符串（去重失效）
            skill_names = tuple(sorted(
                (s.get("skill_name") or s.get("name") or "") for s in e.skills
            ))
            # 修复 P22：技能为空/缺失的单位（如技能表尚未抓全）不参与去重，
            # 直接保留。否则两个技能同样缺失的同名单位会因空签名 () 相同而被
            # 误判为重复丢弃；或与技能完整的同名单位在 exporter 聚合时被误并。
            # 与 exporter 的 _unit_skill_signature() 空技能哨兵保持同一策略。
            if not skill_names:
                deduped.append(e)
                continue
            fp = (
                tuple(sorted(e.physical_resistances.items())),
                tuple(sorted(e.sin_resistances.items())),
                e.body_part,
                skill_names,
            )
            if fp not in seen_fingerprints:
                seen_fingerprints.add(fp)
                deduped.append(e)
            else:
                dedup_count += 1

        if dedup_count > 0:
            logger.info(
                f"去重：{self.title} 丢弃 {dedup_count} 个复用单位 "
                f"（抗性+部位+技能名完全相同），保留 {len(deduped)} 个"
            )

        return deduped

    def _extract_abno_parts(self, table, enemy_name: str) -> list[EnemyData]:
        """从异想体数据表格中提取多个部位数据。

        异想体表格结构（width:700px）：
        - 行 0：标题行（black bg，enemy name）
        - 行 2：总 HP（数值图标-生命 + <big>值</big>）
        - 行 3：部位标题（td[colspan=7][bg=black] + img[alt*="异想体-"] + <b>部位名</b>）
        - 行 4：图标标签行（生命/防御/速度图标 + 物理图标 + 混乱阈值文字，<big>为空）
        - 行 5：数值行（<big>HP/DEF/SPD</big> + span[color] 物理抗性）
        - 行 6：罪孽图标行
        - 行 7：罪孽数值行
        - 行 23：被动能力标题
        - 行 24：被动 <li> 内容（含 img[alt*="异想体-"] 参考图，需跳过）

        返回多个 EnemyData（每个部位一个）。
        """
        parts: list[EnemyData] = []
        current: Optional[EnemyData] = None
        rows = table.select("tr")
        collected_passives: list[str] = []

        expect_values = False
        sin_icons_seen = False

        def _save_current():
            nonlocal current, expect_values, sin_icons_seen
            if current is not None and current.hp > 0:
                parts.append(current)
            current = None
            expect_values = False
            sin_icons_seen = False

        for row in rows:
            row_text = row.get_text(" ", strip=True)

            # ── 部位标题行：td[colspan=7][bg=black] + img[alt*="异想体-"] ──
            # 用 td 特征区分真正的部位标题和被动 <li> 中的内嵌图片
            part_header = row.select_one(
                'td[colspan="7"][style*="background:#000000"]'
            )
            if part_header and part_header.select_one('img[alt*="异想体-"]'):
                _save_current()
                current = EnemyData(
                    title=self.title,
                    enemy_name=enemy_name,
                    battle_stage=self.title,
                )
                b_tag = row.select_one("b")
                if b_tag:
                    current.body_part = b_tag.get_text(strip=True)
                continue

            if current is None:
                continue

            # ── 图标标签行：同时有生命+速度图标（异想体标志性双图标行） ──
            has_life = row.select_one('img[alt*="数值图标-生命"]') is not None
            has_spd = row.select_one('img[alt*="数值图标-速度"]') is not None
            if has_life and has_spd:
                expect_values = True
                sin_icons_seen = False
                continue

            # ── 数值行（紧随图标标签行） ──
            if expect_values:
                big_tags = row.select("big")
                big_vals = [b.get_text(strip=True) for b in big_tags]
                if len(big_vals) >= 3:
                    try:
                        current.hp = int(big_vals[0])
                    except ValueError:
                        hp_match = re.search(r"(\d+)", big_vals[0])
                        if hp_match:
                            current.hp = int(hp_match.group(1))
                    try:
                        current.defense_level = int(big_vals[1])
                    except ValueError:
                        def_match = re.search(r"(\d+)", big_vals[1])
                        if def_match:
                            current.defense_level = int(def_match.group(1))
                    self._parse_speed(big_vals[2], current)
                    if len(big_vals) >= 4 and big_vals[3]:
                        current.chaos_threshold = big_vals[3]

                self._extract_physical_resistances_row(row, current)
                expect_values = False
                continue

            # ── 罪孽图标行 ──
            sin_imgs = row.select('img[alt*="罪孽-"]')
            if sin_imgs:
                sin_icons_seen = True
                if row.select('span[style*="color"]'):
                    self._extract_sin_resistances_row(row, current)
                    sin_icons_seen = False
                continue

            # ── 罪孽数值行（紧随图标行） ──
            if sin_icons_seen and not row.select_one("img"):
                if "\xd7" in row_text or "[" in row_text:
                    self._extract_sin_resistances_row(row, current)
                sin_icons_seen = False
                continue

            # ── 恐慌类型（修复①：收敛选择器 + 判重 + 过滤罪孽/非恐慌 buff）──
            if "恐慌类型" in row_text:
                current.panic_types.extend(_collect_panic_types(row))
                continue

            # ── 被动能力行（仅标题，实际内容在下一行 li 中） ──
            if "被动能力" in row_text or row.select_one(
                "td > big", string=re.compile(r"被动")
            ):
                continue

        _save_current()

        # 收集被动：mw-collapsible-content 内 <li>（名称）后紧跟 <p>（描述）
        # HTML 结构：<ul><li><img><b>名称</b></li></ul><p>描述文本</p><hr>
        for collapsible in table.select(".mw-collapsible-content"):
            li_tags = collapsible.select("li")
            for li in li_tags:
                b_tag = li.select_one("b")
                passive_name = b_tag.get_text(strip=True) if b_tag else ""
                # 描述在 <li> 所在的 <ul> 后面的 <p> 标签中
                passive_desc = ""
                ul_parent = li.parent  # <ul>
                if ul_parent and hasattr(ul_parent, "find_next"):
                    next_p = ul_parent.find_next("p")
                    if next_p:
                        # 确保这个 <p> 在下一个 <ul>/<hr> 之前
                        # 提取纯文本描述（去除 HTML 标签但保留文本）
                        passive_desc = next_p.get_text(" ", strip=True)
                if passive_name:
                    if passive_desc:
                        collected_passives.append(f"{passive_name}: {passive_desc}")
                    else:
                        collected_passives.append(passive_name)

        if collected_passives:
            # 被动保序去重：AbnormalityData 模板（如 9-38 马蒂亚斯/绮罗）的被动可能经
            # 多个 .mw-collapsible-content 或 li/p 路径重复收集（表现为 7×2、6×2），
            # 与 _extract_stats（P5）保持一致，去重后按原顺序保留。
            seen = set()
            deduped_passives = []
            for p in collected_passives:
                if p not in seen:
                    seen.add(p)
                    deduped_passives.append(p)
            collected_passives = deduped_passives
            for part in parts:
                if not part.passives:
                    part.passives = list(collected_passives)

        return parts

    def _distribute_passives(self, parts: list[EnemyData]):
        """将被动从有被动数据的部位复制到所有缺失部位。"""
        source_passives: list[str] = []
        for part in parts:
            if part.passives:
                source_passives = list(part.passives)
                break
        if not source_passives:
            return
        for part in parts:
            if not part.passives:
                part.passives = list(source_passives)

    def _find_next_table(self, start_elem, style_hint: str = "", require_stat_icon: bool = False):
        """查找 start_elem 之后最近的一个匹配 table。

        支持两种模式：
        1. 直接兄弟是 table
        2. 直接兄弟是包裹元素（如 <center>, <div>）内含 table

        边界：遇到下一个 h2/h3/h4/h5 标题（新的敌人/区段）即停止，不跨小节。
        修复跨小节越界：此前"理智值"/"E.G.O特殊语音"等非敌人标题会向后越界
        找到后续敌人的表格，产生假单位。h2 为 P22 新增的敌人标题层级
        （折射轨道6号线等），同样作为边界。

        require_stat_icon（P23b 修复）：为 True 时跳过不含「数值图标-生命」图片的
        表格。主线战斗 9-50 等页面中，敌人第二阶段 h3 后先出现多张对话卡表格
        （含 `[食指 父辈] 里恩 …` 对白，无生命图标），真实属性表在对话卡之后；
        若取第一张表会把对话卡当属性表解析出 hp=0，导致第二阶段被丢弃。
        """
        sibling = start_elem.next_sibling
        safety = 0
        while sibling and safety < 50:
            safety += 1
            if hasattr(sibling, "name") and sibling.name:
                if sibling.name in ("h2", "h3", "h4", "h5"):
                    # 遇到下一个标题（新的敌人/区段），停止，避免跨小节越界
                    break
                if sibling.name == "table":
                    style = sibling.get("style", "")
                    if (not style_hint or style_hint in style) and self._table_has_stat_icon(sibling, require_stat_icon):
                        return sibling
                elif hasattr(sibling, "select_one"):
                    # 搜索包裹元素内的 table（如 <center> 内的 table）
                    if style_hint:
                        table = sibling.select_one(f"table[style*='{style_hint}']")
                        if table and self._table_has_stat_icon(table, require_stat_icon):
                            return table
                    else:
                        table = sibling.find("table")
                        if table and self._table_has_stat_icon(table, require_stat_icon):
                            return table
            sibling = sibling.next_sibling
        return None

    @staticmethod
    def _table_has_stat_icon(table, require_stat_icon: bool) -> bool:
        """当 require_stat_icon 为 True 时，仅当 table 内含「数值图标-生命」图片才返回 True。

        对话卡表格（纯文本对白，无属性图标）不满足条件，从而被跳过。
        """
        if not require_stat_icon:
            return True
        if table is None:
            return False
        return table.select_one('img[alt*="数值图标-生命"]') is not None

    def _find_following_collapsibles(self, start_elem):
        """查找 start_elem 之后的所有 mw-collapsible div。

        兼容两种技能标题层级：
        - 主线战斗1-10 / 1-11-2：技能标题为 <h3>技能</h3>
        - 主线战斗8-30 / 9-38 / 9-49：技能标题为 <h4>技能</h4>
        - 折射轨道6号线：敌人标题为 <h2>，技能标题为 <h3>技能</h3>
        遇到标题时：若文本含"技能"则继续向后收集（技能区 collapsible）；
        否则视为新的敌人/区段标题而停止（不跨小节越界收集他人技能）。
        h2 为 P22 新增的敌人标题层级，非"技能"的 h2 即新的敌人，须停止。
        """
        collapsibles = []
        sibling = start_elem.next_sibling
        safety = 0
        while sibling and safety < 200:
            safety += 1
            if hasattr(sibling, "name") and sibling.name:
                if sibling.name in ("h2", "h3", "h4", "h5"):
                    text = sibling.get_text(strip=True)
                    if "技能" in text:
                        # 技能标题：继续向后收集其后的 collapsible
                        sibling = sibling.next_sibling
                        continue
                    # 其他标题（新的敌人/区段）：停止
                    break
                if sibling.name == "div":
                    classes = sibling.get("class", [])
                    if "mw-collapsible" in str(classes):
                        collapsibles.append(sibling)
            sibling = sibling.next_sibling
        return collapsibles

    def _extract_stats(self, table, data: EnemyData):
        """从基本信息表中提取 HP、防御、速度、抗性、恐慌类型、被动。

        支持：
        - 主线战斗格式（独立行对应独立属性）
        - 异想体格式（多部位内嵌在同一张表，rowspan 立绘，
          <img alt="异想体-头部"> 等标识部位标题行）
        """
        rows = table.select("tr")

        for row in rows:
            cells = row.select("td, th")
            row_text = row.get_text(" ", strip=True)

            # ── 异想体部位标题：img[alt*="异想体-"] ──
            # 对于 width:700px 多部位格式，部位标题行是独立的黑色背景行。
            # 对于单部位异想体格式（如 1-11-2），异想体图与 HP/DEF/SPD 在同一行，
            # 因此不能使用 continue 阻断后续检测。
            abno_img = row.select_one('img[alt*="异想体-"]')
            if abno_img:
                alt = abno_img.get("alt", "")
                # 从 alt 提取部位名："异想体-头部.png" -> "头部"
                # （不能用 row.select_one("b")，单部位格式中第一个 <b> 是 HP 值）
                part_match = re.search(r'异想体-(.+)\.(?:png|PNG)', alt)
                if part_match:
                    data.body_part = part_match.group(1)
                # 不 continue：单部位异想体格式中 HP/DEF/SPD 也在同一行

            # HP: img[alt*="数值图标-生命"] 后的数字
            life_img = row.select_one('img[alt*="数值图标-生命"]')
            if life_img:
                # 优先 img 后面最近的 <b>（单部位格式：img 和 <b> 是兄弟节点）
                b_tag = life_img.find_next("b")
                if b_tag:
                    try:
                        hp_val = int(b_tag.get_text(strip=True))
                        if hp_val > data.hp:
                            data.hp = hp_val
                    except ValueError:
                        pass
                # 回退：row 内的 <b>（主线格式）或 <big>（width:700px 格式）
                if data.hp == 0:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        try:
                            hp_val2 = int(b_tag2.get_text(strip=True))
                            if hp_val2 > data.hp:
                                data.hp = hp_val2
                        except ValueError:
                            pass
                if data.hp == 0:
                    big_tag = row.select_one("big")
                    if big_tag:
                        hp_match = re.search(r"(\d+)", big_tag.get_text(strip=True))
                        if hp_match:
                            data.hp = int(hp_match.group(1))
                # 不 continue：单部位异想体格式中 DEF/SPD 也在同一行

            # 防御等级
            def_img = row.select_one('img[alt*="数值图标-防御"]')
            if def_img:
                b_tag = def_img.find_next("b")
                if b_tag:
                    try:
                        def_val = int(b_tag.get_text(strip=True))
                        if def_val > data.defense_level:
                            data.defense_level = def_val
                    except ValueError:
                        pass
                if data.defense_level == 0:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        try:
                            def_val2 = int(b_tag2.get_text(strip=True))
                            if def_val2 > data.defense_level:
                                data.defense_level = def_val2
                        except ValueError:
                            pass
                if data.defense_level == 0:
                    big_tag = row.select_one("big")
                    if big_tag:
                        def_match = re.search(r"(\d+)", big_tag.get_text(strip=True))
                        if def_match:
                            data.defense_level = int(def_match.group(1))
                # 不 continue

            # 速度
            spd_img = row.select_one('img[alt*="数值图标-速度"]')
            if spd_img:
                b_tag = spd_img.find_next("b")
                if b_tag:
                    speed_text = b_tag.get_text(strip=True)
                    self._parse_speed(speed_text, data)
                else:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        speed_text = b_tag2.get_text(strip=True)
                        self._parse_speed(speed_text, data)
                    else:
                        big_tag = row.select_one("big")
                        if big_tag:
                            speed_text = big_tag.get_text(strip=True)
                            self._parse_speed(speed_text, data)
                # 不 continue

            # 混乱阈值（异想体格式：td 中有 "混乱阈值" 文字）
            if "混乱阈值" in row_text:
                # 查找同行中的 <big> 标签获取阈值数值
                big_tags = row.select("big")
                if big_tags:
                    # 最后一个 big 通常是混乱阈值
                    ct_text = big_tags[-1].get_text(strip=True)
                    if ct_text:
                        data.chaos_threshold = ct_text
                else:
                    # 回退：直接从文本提取数字
                    ct_match = re.search(r"(\d+(?:/\d+)?)", row_text.split("混乱阈值")[-1])
                    if ct_match:
                        data.chaos_threshold = ct_match.group(1)
                continue

            # 物理抗性：包含物理图标（不 continue，同行可能有罪孽抗性）
            phys_imgs = row.select('img[alt*="物理-" i], img[alt*="攻击-" i]')
            if phys_imgs:
                self._extract_physical_resistances_row(row, data)

            # 异想体格式的物理图标（直接 .png 引用："斩击.png" 等）
            phys_img_alts = row.select('img[alt="斩击.png"], img[alt="突刺.png"], img[alt="打击.png"]')
            if phys_img_alts:
                self._extract_physical_resistances_row(row, data)

            # 罪孽抗性：包含罪孽图标（与物理抗性不再互斥）
            sin_imgs = row.select('img[alt*="罪孽-"]')
            if sin_imgs:
                self._extract_sin_resistances_row(row, data)
                continue

            # 恐慌类型（修复①：收敛选择器 + 判重 + 过滤罪孽/非恐慌 buff）
            if "恐慌类型" in row_text:
                data.panic_types.extend(_collect_panic_types(row))
                continue

            # 部 分（身体部位）- 主线战斗格式
            # 仅当 body_part 未被异想体图片行设置过时才尝试提取
            if not data.body_part and ("部 " in row_text or "部分" in row_text):
                # 排除异想体图片行（"异想体-头部" 等已在上方处理）
                if not row.select_one('img[alt*="异想体-"]'):
                    tds = row.select("td")
                    for td in tds:
                        part_text = td.get_text(strip=True)
                        if part_text and part_text != "部 分" and "部" in part_text:
                            data.body_part = part_text
                            break
                continue

            # 被动能力（主线格式 + 异想体格式）
            if "被动能力" in row_text or "被动" in row_text:
                from crawler.buffs_data import resolve_buff_codes_in_text
                tds = row.select("td")
                for td in tds:
                    text = resolve_buff_codes_in_text(
                        td.get_text(strip=True), extra_map=self._buff_code_map
                    )
                    if text and text not in ("被动能力", "被动") and "该敌人无被动能力" not in text:
                        data.passives.append(text)
                    elif "该敌人无被动能力" in text:
                        data.passives.append("无")
                # 异想体格式：被动以 <li> 形式嵌在 mw-collapsible-content 中
                li_elems = row.parent.select("li") if row.parent else []
                if not data.passives and li_elems:
                    for li in li_elems:
                        b_tag = li.select_one("b")
                        passive_name = b_tag.get_text(strip=True) if b_tag else ""
                        passive_desc = resolve_buff_codes_in_text(
                            li.get_text(" ", strip=True), extra_map=self._buff_code_map
                        )
                        if passive_name:
                            # 去掉名称，只保留描述
                            desc = passive_desc.replace(passive_name, "", 1).strip()
                            data.passives.append(f"{passive_name}: {desc}" if desc else passive_name)
                        elif passive_desc:
                            data.passives.append(passive_desc)
                continue

        # 被动保序去重：AbnormalityData 模板（如 9-38 马蒂亚斯/绮罗）的被动可能经
        # td 文本路径与 li 兜底路径重复收集（表现为 7×2、6×2），去重后按原顺序保留。
        if data.passives:
            seen = set()
            deduped = []
            for p in data.passives:
                if p not in seen:
                    seen.add(p)
                    deduped.append(p)
            data.passives = deduped

    def _parse_speed(self, speed_text: str, data: EnemyData):
        """解析速度文本，如 "1-2"、"1~2" 或 "3"。"""
        speed_text = speed_text.strip()
        range_match = re.match(r"(\d+)\s*[-–—～~]\s*(\d+)", speed_text)
        if range_match:
            data.speed_min = int(range_match.group(1))
            data.speed_max = int(range_match.group(2))
        else:
            try:
                val = int(speed_text)
                data.speed_min = val
                data.speed_max = val
            except ValueError:
                pass

    def _extract_physical_resistances_row(self, row, data: EnemyData):
        """从物理抗性行提取三种抗性值。

        物理图标与罪孽图标可能位于同一行（如 9-02 卡片式表格中物理/罪孽
        各占一个 rowspan td）。因此只收集物理图标所在 td 内的 span，
        排除罪孽图标所在 td，避免把罪孽值误当物理值。
        """
        # 找出罪孽图标所在的 td（这些 td 内的 span 不属于物理抗性）
        sin_tds = []
        for sin_img in row.select('img[alt*="罪孽-"]'):
            td = sin_img.find_parent("td")
            if td is not None and td not in sin_tds:
                sin_tds.append(td)

        span_values = []
        for span in row.select('span[style*="color"]'):
            if any(td in span.parents for td in sin_tds):
                continue
            span_values.append(span)

        phys_order = ["斩击", "突刺", "打击"]
        for i, span in enumerate(span_values):
            b_tag = span.select_one("b")
            if b_tag:
                val = _parse_resistance_value(b_tag.get_text(strip=True))
                if val is not None and i < len(phys_order):
                    data.physical_resistances[phys_order[i]] = val

        # 回退：从整行文本提取
        if not data.physical_resistances:
            full_text = row.get_text(" ", strip=True)
            values = re.findall(r"×\s*([\d.]+)", full_text)
            for i, v in enumerate(values):
                if i < len(phys_order):
                    try:
                        data.physical_resistances[phys_order[i]] = float(v)
                    except ValueError:
                        pass

    def _extract_sin_resistances_row(self, row, data: EnemyData):
        """从罪孽抗性行提取七种罪孽抗性值。

        只收集罪孽图标所在 td 内的 span，避免把同行物理抗性的 span
        误当成罪孽抗性（9-02 卡片式表格中物理/罪孽图标同行的场景）。
        """
        # 收集罪孽图标所在的 td
        sin_tds = []
        for sin_img in row.select('img[alt*="罪孽-"]'):
            td = sin_img.find_parent("td")
            if td is not None and td not in sin_tds:
                sin_tds.append(td)

        span_values = []
        for td in sin_tds:
            span_values.extend(td.select('span[style*="color"]'))

        sin_order = ["暴怒", "色欲", "怠惰", "暴食", "忧郁", "傲慢", "嫉妒"]
        for i, span in enumerate(span_values):
            b_tag = span.select_one("b")
            if b_tag:
                val = _parse_resistance_value(b_tag.get_text(strip=True))
                if val is not None and i < len(sin_order):
                    data.sin_resistances[sin_order[i]] = val

        # 回退：如果图标与数值分属不同行（如异想体格式中图标行与数值行分离），
        # 本行无罪孽图标，改用整行文本按位置提取
        if not data.sin_resistances:
            full_text = row.get_text(" ", strip=True)
            values = re.findall(r"×\s*([\d.]+)", full_text)
            # 若该行同时包含物理与罪孽值（1-10/9-02 布局），罪孽值在第 4~10 位
            start = 3 if row.select_one(
                'img[alt="斩击.png"], img[alt="突刺.png"], img[alt="打击.png"]'
            ) else 0
            for i, v in enumerate(values[start:start + len(sin_order)]):
                if i < len(sin_order):
                    try:
                        data.sin_resistances[sin_order[i]] = float(v)
                    except ValueError:
                        pass

    def _extract_enemy_skills_from_collapsible(self, collapsible, data: EnemyData):
        """从 mw-collapsible 区域提取敌方技能。

        敌方技能卡片使用游戏内 CSS 渲染，关键提取点：
        - 技能名称：data-text 属性或 div.textskill-container
        - 罪孽类型：img[alt*="-等级"] 的 alt（如 "色欲-等级"）
        - 伤害类型：img[alt*="技能-"] 的 alt
        - 基础值：font-size:1.8em 的 span
        - 硬币威力：+N 模式
        - 硬币数量：img[alt*="硬币"] × N
        - 攻击等级：img[alt*="数值图标-攻击"] + 数值
        - 攻击容量：文本 "攻击容量" 后的数字
        """
        # 在 collapsible 内部查找所有技能 table
        skill_tables = collapsible.select('table.wikitable[style*="width:100%"]')
        if not skill_tables:
            # 尝试直接找 table.wikitable
            skill_tables = collapsible.select("table.wikitable")

        for table in skill_tables:
            skill = self._extract_single_enemy_skill(table)
            # 修复③：保留重要性>0 的技能（DOM 中可能无 sin_type 且 base_value=0，
            # 如重要性=3 的强力攻击，原过滤条件会将其丢弃）
            if skill and (skill.sin_type or skill.base_value > 0 or skill.importance > 0):
                data.skills.append({
                    "skill_name": skill.skill_name,
                    "icon_id": skill.icon_id,
                    "sin_type": skill.sin_type,
                    "damage_type": skill.damage_type,
                    "base_value": skill.base_value,
                    "coin_power": skill.coin_power,
                    "coin_count": skill.coin_count,
                    "attack_level": skill.attack_level,
                    "attack_weight": skill.attack_weight,
                    "is_guard": skill.is_guard,
                    "guard_type": skill.guard_type,
                    "importance": skill.importance,
                    "coin_effects": skill.coin_effects,
                })

    def _is_inside_ally_section(self, heading) -> bool:
        """判断 heading 是否位于"援助单位/友方单位"<h2> 区段内。

        援助单位下的 <h3> 由 _extract_ally_units() 专门处理（含 width:850px 表格
        专用解析），主循环中应跳过这些标题，避免重复提取。
        """
        # 向前查找最近的 <h2>，判断其标题是否为援助/友方
        h2 = heading.find_previous("h2")
        while h2 is not None:
            headline = h2.select_one(".mw-headline")
            text = headline.get_text(strip=True) if headline else h2.get_text(strip=True)
            if "援助" in text or "友方" in text:
                return True
            # 若遇到"战斗信息"等区段标题，说明不在援助区段内
            if text in ("基本信息", "战斗信息", "主线导航", "导航"):
                return False
            h2 = h2.find_previous("h2")
        return False

    def _extract_ally_units(self) -> list[EnemyData]:
        """提取援助/友方单位（<h2>援助单位</h2> → <h3>单位名 → width:850px 表格）。

        援助单位表格结构（width:850px）：
        - 行1: 战斗立绘 (rowspan=4) | 单位信息 (colspan=3)
        - 行2: HP + 速度 + 防御 数值
        - 行3: 混乱阈值
        - 行4: 物理抗性 (rowspan=4) | 罪孽抗性 (rowspan=4) | 其他信息
        - 技能区在 <h4>技能</h4> 后的 mw-collapsible 中
        """
        if not self.mw_output:
            return []

        # 查找 "援助单位" 的 <h2> 标题
        ally_h2 = None
        for h2 in self.mw_output.select("h2"):
            headline = h2.select_one(".mw-headline")
            text = headline.get_text(strip=True) if headline else h2.get_text(strip=True)
            if "援助" in text or "友方" in text:
                ally_h2 = h2
                break

        if not ally_h2:
            return []

        ally_units = []

        # 用 find_all_next 做 DOM 全扫描：部分页面（如 9-38 摩西）的 <div class="mw-collapsible">
        # 未闭合，导致后续以斯拉/韦斯帕的 <h3> 嵌套在 div 内，next_sibling 线性遍历无法到达。
        # 改用 find_all_next("h3") 收集援助区段内全部 h3，并通过 find_previous("h2") 确认
        # 其仍属于当前援助 <h2> 区段（遇到离开该区段的 h3 即停止）。
        for h3 in ally_h2.find_all_next("h3"):
            prev_h2 = h3.find_previous("h2")
            if prev_h2 is not ally_h2:
                break  # 已离开"援助单位"区段
            headline = h3.select_one(".mw-headline")
            ally_name = headline.get_text(strip=True) if headline else h3.get_text(strip=True)
            if not ally_name:
                continue
            ally_data = self._extract_single_ally(h3, ally_name)
            if ally_data and ally_data.hp > 0:
                ally_units.append(ally_data)

        if ally_units:
            logger.info(
                f"援助单位：{self.title} 提取到 {len(ally_units)} 个友方单位: "
                f"{[a.enemy_name for a in ally_units]}"
            )

        return ally_units

    def _extract_single_ally(self, h3_heading, ally_name: str) -> Optional[EnemyData]:
        """从单个 <h3> 援助单位标题提取数据。"""
        data = EnemyData(
            title=self.title,
            enemy_name=ally_name,
            battle_stage=self.title,
            is_ally=True,
        )

        # 表格宽度不固定（不同单位可能不同），不依赖固定宽度定位。
        # h3_heading 已由 _extract_ally_units() 确认位于"援助单位"<h2> 区段内，
        # 直接取其后最近的 wikitable 即为援助单位表格。
        table = self._find_next_table(h3_heading, "")
        if not table:
            return None

        # 解析援助单位表格
        self._extract_ally_stats(table, data)

        # 提取技能：部分援助单位（如 9-38 以斯拉/韦斯帕）的 <h3> 后直接跟技能
        # mw-collapsible（无 <h4>技能</h4> 标题），因此不依赖 find_next("h4")，
        # 直接从 h3 起收集后续 collapsible（_find_following_collapsibles 遇到含"技能"
        # 的标题会继续、遇到其他标题则停止，兼容有/无标题两种结构）。
        collapsibles = self._find_following_collapsibles(h3_heading)
        for collapsible in collapsibles:
            self._extract_enemy_skills_from_collapsible(collapsible, data)

        return data

    def _extract_ally_stats(self, table, data: EnemyData):
        """从援助单位表格（width:850px）中提取 HP、速度、防御、抗性、被动等。

        与敌方单位表格不同，援助单位表格的行结构为：
        - 行: 战斗立绘(rowspan=4) | colspan=3 单位信息
        - 行: HP图标 + 数值 + 速度图标 + 数值 + 防御图标 + 数值
        - 行: 混乱阈值
        - 行: 物理抗性(rowspan) | 罪孽抗性(rowspan) | 其他信息(被动等)
        """
        rows = table.select("tr")

        for row in rows:
            cells = row.select("td, th")
            row_text = row.get_text(" ", strip=True)

            # HP: img[alt*="数值图标-生命"]
            life_img = row.select_one('img[alt*="数值图标-生命"]')
            if life_img:
                b_tag = life_img.find_next("b")
                if b_tag:
                    try:
                        data.hp = int(b_tag.get_text(strip=True))
                    except ValueError:
                        pass
                if data.hp == 0:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        try:
                            data.hp = int(b_tag2.get_text(strip=True))
                        except ValueError:
                            pass

            # 防御等级
            def_img = row.select_one('img[alt*="数值图标-防御"]')
            if def_img:
                b_tag = def_img.find_next("b")
                if b_tag:
                    try:
                        data.defense_level = int(b_tag.get_text(strip=True))
                    except ValueError:
                        pass
                if data.defense_level == 0:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        try:
                            data.defense_level = int(b_tag2.get_text(strip=True))
                        except ValueError:
                            pass

            # 速度
            spd_img = row.select_one('img[alt*="数值图标-速度"]')
            if spd_img:
                b_tag = spd_img.find_next("b")
                if b_tag:
                    self._parse_speed(b_tag.get_text(strip=True), data)
                else:
                    b_tag2 = row.select_one("b")
                    if b_tag2:
                        self._parse_speed(b_tag2.get_text(strip=True), data)

            # 混乱阈值
            if "混乱阈值" in row_text:
                b_tag = row.select_one("b")
                if b_tag:
                    data.chaos_threshold = b_tag.get_text(strip=True)
                else:
                    ct_match = re.search(r"混乱阈值[：:]\s*(.+)", row_text)
                    if ct_match:
                        data.chaos_threshold = ct_match.group(1).strip()

            # 物理抗性 + 罪孽抗性（可能在同行或相邻列）
            # 援助单位中物理图标和罪孽图标各占一列（rowspan td），
            # 必须限定在各图标所在 td 内提取，否则会把罪孽抗性误当物理抗性
            phys_img = row.select_one(
                'img[alt="斩击.png"], img[alt="突刺.png"], img[alt="打击.png"]'
            )
            if phys_img:
                phys_td = phys_img
                while phys_td is not None and getattr(phys_td, "name", "") != "td":
                    phys_td = phys_td.parent
                if phys_td is not None:
                    self._extract_physical_resistances_row(phys_td, data)

            sin_img = row.select_one('img[alt*="罪孽-"]')
            if sin_img:
                sin_td = sin_img
                while sin_td is not None and getattr(sin_td, "name", "") != "td":
                    sin_td = sin_td.parent
                if sin_td is not None:
                    self._extract_sin_resistances_row(sin_td, data)

            # 被动能力（在 mw-collapsible-content 中）
            if "被动能力" in row_text or "被动" in row_text:
                from crawler.buffs_data import resolve_buff_codes_in_text
                # 被动通常在 mw-collapsible-content 的 <li> 中
                collapsible_content = row.select_one(".mw-collapsible-content")
                if collapsible_content:
                    li_tags = collapsible_content.select("li")
                    for li in li_tags:
                        b_tag = li.select_one("b")
                        passive_name = b_tag.get_text(strip=True) if b_tag else ""
                        # 描述在 li 后面的 <p> 标签中
                        next_p = li.find_next("p")
                        desc = resolve_buff_codes_in_text(
                            next_p.get_text(" ", strip=True) if next_p else "",
                            extra_map=self._buff_code_map,
                        )
                        if passive_name:
                            full = f"{passive_name}: {desc}" if desc else passive_name
                            data.passives.append(full)
                        elif desc:
                            data.passives.append(desc)
                    # 也检查 hr 后的 li
                    if not data.passives:
                        all_li = collapsible_content.select("li")
                        for li in all_li:
                            b_tag = li.select_one("b")
                            name = b_tag.get_text(strip=True) if b_tag else ""
                            if name and name not in [p.split(":")[0] for p in data.passives]:
                                full_text = resolve_buff_codes_in_text(
                                    li.get_text(" ", strip=True), extra_map=self._buff_code_map
                                )
                                data.passives.append(full_text)
                # 如果 collapsible 在 row 的父级中
                if not data.passives:
                    parent_collapsible = row.parent.select_one(".mw-collapsible-content") if row.parent else None
                    if parent_collapsible:
                        for li in parent_collapsible.select("li"):
                            full_text = resolve_buff_codes_in_text(
                                li.get_text(" ", strip=True), extra_map=self._buff_code_map
                            )
                            if full_text and full_text not in data.passives:
                                data.passives.append(full_text)

    def _extract_single_enemy_skill(self, table) -> Optional[EnemySkillData]:
        """从单个 table.wikitable 提取技能数据。"""
        skill = EnemySkillData()
        table_html = str(table)
        table_text = table.get_text(" ", strip=True)

        # ── 技能名称：优先从 wikitext 技能名称映射获取（图标编号匹配） ──
        # 渲染后技能图标 img 的 alt 为纯数字文件名（如 114501.png / 40001001.png），
        # 图标编号为 6-8 位（132701/114501/9021704/40001001），不能用 \d{7} 精确匹配。
        skill_name_from_wikitext = False
        skill_icon_imgs = table.select('img[alt*=".png"]')
        for img in skill_icon_imgs:
            alt = img.get("alt", "")
            # 从 alt 或 src 中提取纯数字图标编号（6-8 位）
            icon_match = re.search(r"^(\d{6,8})\.(?:png|PNG)", alt)
            if not icon_match:
                src = img.get("src", "")
                icon_match = re.search(r"/(\d{6,8})\.(?:png|PNG)", src)
            if icon_match:
                icon_id = icon_match.group(1)
                skill.icon_id = icon_id
                if icon_id in self._skill_name_map:
                    skill.skill_name = self._skill_name_map[icon_id]
                    skill_name_from_wikitext = True
                    break

        if not skill_name_from_wikitext:
            # 回退：从 data-text 属性获取
            data_text_elems = table.select('[data-text]')
            for elem in data_text_elems:
                dt = elem.get("data-text", "")
                if dt and dt != "是什么":
                    skill.skill_name = dt.strip()
                    break
        if not skill.skill_name:
            # 最终回退：textskill-container
            tc = table.select_one(".textskill-container")
            if tc:
                skill.skill_name = tc.get_text(strip=True)

        # ── 硬币效果：img[alt*="硬币1.png"] 后的 <span> 文本 ──
        coin_imgs_found = table.select('img[alt*="硬币1.png"]')
        for coin_img in coin_imgs_found:
            # 找到硬币 img 所在的 td 或 div，提取后续文本
            parent = coin_img.parent
            if parent:
                # 取 parent 之后的所有兄弟节点的文本
                effect_parts = []
                current = parent
                while current:
                    # 检查当前节点的 tail 文本（BeautifulSoup 中标签后的文本）
                    if hasattr(current, 'next_sibling') and current.next_sibling:
                        ns = current.next_sibling
                        if hasattr(ns, 'get_text'):
                            txt = ns.get_text(strip=True)
                            if txt:
                                effect_parts.append(txt)
                        elif isinstance(ns, str):
                            txt = ns.strip()
                            if txt:
                                effect_parts.append(txt)
                    current = current.parent if hasattr(current, 'parent') else None
                    # 只向上走一层（到 td）
                    if current and current.name in ('td', 'div'):
                        break
                    if current is None:
                        break

                # 更稳健的方式：在 coin img 的父级 div 中搜索文本
                container = coin_img
                for _ in range(4):  # 最多向上4层找容器
                    container = container.parent if container else None
                    if container and container.name in ('td', 'div'):
                        # 获取该容器内所有文本，过滤出含有命中/施加等关键词的
                        full_text = container.get_text(" ", strip=True)
                        # 从硬币图片后提取效果文本
                        coin_alt = coin_img.get("alt", "硬币1.png")
                        effect_patterns = re.findall(
                            r'\[(?:正面命中时|反面命中时|命中时|使用时|攻击后|使用前|若命中)\][^\[]+',
                            full_text
                        )
                        for ep in effect_patterns:
                            ep_clean = ep.strip()
                            if ep_clean and ep_clean not in skill.coin_effects:
                                skill.coin_effects.append(ep_clean)
                        break

        # ── 罪孽类型：img[alt*="等级"] 如 "色欲-等级" ──
        sin_img = table.select_one('img[alt*="-等级"]')
        if sin_img:
            alt = sin_img.get("alt", "")
            sin_match = re.match(r"([^-]+)-等级", alt)
            if sin_match:
                skill.sin_type = sin_match.group(1)

        # ── 伤害类型 + 罪孽类型（从技能图标） ──
        if not skill.sin_type:
            skill_imgs = table.select('img[alt*="技能-"]')
            for img in skill_imgs:
                result = _parse_skill_icon_filename(img)
                if result:
                    skill.sin_type, skill.damage_type = result
                    break

        # ── 基础值：font-size: 1.8em 的 span / div（修复 P21-A）──
        # 实测渲染 HTML 中该样式既可能渲染在 <span> 也可能在 <div>
        # （如 <div style="...font-size: 1.8em;..."><b><span style="color:#ECCCA3;">4</span></b></div>），
        # 旧实现只匹配 span 导致大量技能 base_value 恒为 0。
        large_els = table.select('span[style*="font-size: 1.8em"]')
        if not large_els:
            large_els = table.select('span[style*="font-size:1.8em"]')
        if not large_els:
            large_els = table.select('div[style*="font-size: 1.8em"]')
        if not large_els:
            large_els = table.select('div[style*="font-size:1.8em"]')
        if not large_els:
            large_els = table.select('[style*="font-size:1.8em"]')
        for el in large_els:
            text = el.get_text(strip=True)
            try:
                skill.base_value = int(text)
                break
            except ValueError:
                continue

        # ── 硬币威力：技能数值区中带符号的数值（+N / -N）──
        # P38 修复：旧实现 `re.findall(r"\+(\d+)", table_html)` 取整张表第一个 +N，
        # 会命中技能名内联 data:image base64 里的数字（854/987/9703），产生垃圾值。
        # 正确来源：技能卡片数值区 `<span style="color:#ECCCA3;">+2</span>`（游戏 UI 数值色），
        # 该区域内数值按顺序为 [攻击等级, 基础值, 硬币威力(带符号), 硬币数量]，
        # 因此取第一个形如 [+-]N 的 span 即为硬币威力。
        for span in table.select("span[style*='ECCCA3']"):
            txt = span.get_text(strip=True)
            if re.fullmatch(r"[+-]\d+", txt):
                try:
                    skill.coin_power = int(txt)
                except ValueError:
                    pass
                break
        else:
            # 兜底：定位 margin-top:-105px 的硬币威力显示块
            for div in table.select('div[style*="margin-top:-105px"]'):
                txt = div.get_text(strip=True)
                m = re.search(r"([+-]\d+)", txt)
                if m:
                    try:
                        skill.coin_power = int(m.group(1))
                    except ValueError:
                        pass
                    break

        # ── 硬币数量：`硬币图标 ×N` 显示块（P38 修复）──
        # 旧实现取整表第一个 "×N"（会命中"概率×5%"等）或按 img[alt*="硬币"] 计数
        # （会把 硬币.png 图标与 不可摧毁的硬币.png 都数进去，如 Furioso 5 枚误计为 2）。
        # 正确来源：`<big><b><img alt="硬币.png"><img alt="乘号.png"><span>N</span></b></big>`
        for big in table.select("big"):
            if big.select_one('img[alt*="乘号"]'):
                span = big.select_one("span[style*='ECCCA3']")
                if span:
                    try:
                        skill.coin_count = int(span.get_text(strip=True))
                    except ValueError:
                        pass
                break
        else:
            # 兜底：数带编号的硬币效果图标（硬币1.png/硬币2.png...，不含 不可摧毁的硬币.png）
            numbered = [
                img for img in table.select('img[alt*="硬币"]')
                if re.search(r"硬币\d+\.png", img.get("alt", ""))
            ]
            if numbered:
                skill.coin_count = len(numbered)

        # ── 攻击等级：img[alt*="数值图标-攻击"] ──
        atk_img = table.select_one('img[alt*="数值图标-攻击"]')
        if atk_img:
            parent = atk_img.parent
            if parent:
                atk_text = parent.get_text(strip=True)
                atk_match = re.search(r"(\d+)", atk_text)
                if atk_match:
                    skill.attack_level = int(atk_match.group(1))

        # ── 攻击容量（渲染文本为 "攻击容量 7"，冒号可选）──
        cap_match = re.search(r"攻击容量\s*[：:]?\s*(\d+)", table_text)
        if cap_match:
            skill.attack_weight = int(cap_match.group(1))

        # ── 守备技能检测（P38：仅匹配"可拼点反击/闪避/防御"前缀）──
        # 旧实现匹配裸词"守备/防御/反击"，会把效果文本含"本技能不会触发目标的
        # 守备技能"的攻击技能（如 Furioso-Replica）误判为守备技能。
        guard_match = re.search(r"可拼点(反击|闪避|防御)", table_text)
        if guard_match:
            skill.is_guard = True
            skill.guard_type = guard_match.group(1)

        # ── 重要性（修复③）：从表格文本提取「重要性：N」值 ──
        imp_match = re.search(r'重要性[：:]\s*(\d+)', table_text)
        if imp_match:
            skill.importance = _parse_importance(imp_match.group(1))

        return skill


# ── 事件提取器 ──

class EventExtractor:
    """探索事件页面 HTML 提取器。

    事件页面结构（基于事件-膏血等）：
    - nav-pills 选项卡表示不同选项
    - 每个 tab-pane 包含：
      - 选择文本（span.label 样式）
      - 判定需求（有利判定/不利判定）
      - 判定成功/失败结果
    - 可选 E.G.O 饰品区域
    """

    def __init__(self, html: str, title: str, categories: list[str]):
        if not HAS_BS4:
            raise ImportError("beautifulsoup4 未安装")
        self.soup = BeautifulSoup(html, "lxml")
        _strip_tooltip_preload(self.soup)
        self.title = title
        self.categories = categories
        self.content_div = self.soup.select_one("#mw-content-text")
        if not self.content_div:
            self.content_div = self.soup
        self.mw_output = self.content_div.select_one(".mw-parser-output") if self.content_div else None

    def extract(self) -> Optional[EventData]:
        """提取事件数据。"""
        if not self.mw_output:
            return None

        event_name = self.title
        if event_name.startswith("事件-"):
            event_name = event_name[3:]

        data = EventData(
            title=self.title,
            event_name=event_name,
        )

        # 1) 事件描述：div[style*="background:#000000"] 中的文本段落
        narr_divs = self.mw_output.select('div[style*="background:#000000"]')
        if narr_divs:
            narration_parts = []
            for div in narr_divs:
                text = div.get_text("\n", strip=True)
                if text and len(text) > 10:
                    narration_parts.append(text)
            if narration_parts:
                data.narration = "\n\n".join(narration_parts)

        # 2) 如果没找到专用叙事 div，尝试从第一个非表格段落提取
        if not data.narration:
            first_p = self.mw_output.select_one("p")
            if first_p:
                text = first_p.get_text(strip=True)
                if text and len(text) > 10:
                    data.narration = text

        # 3) 提取选项：nav-pills → tab-panes
        nav_pills = self.mw_output.select("ul.nav.nav-pills, ul.nav-pills")
        tab_contents = self.mw_output.select("div.tab-content")

        if nav_pills and tab_contents:
            # 获取所有 tab-pane
            all_panes = []
            for tc in tab_contents:
                panes = tc.select('div[role="tabpanel"].tab-pane')
                all_panes.extend(panes)

            for pane in all_panes:
                option = self._extract_option_from_pane(pane)
                if option:
                    data.options.append(option)

        # 4) 提取 E.G.O 饰品
        ego_gift_heading = None
        for h in self.mw_output.select("h2, h3"):
            span = h.select_one(".mw-headline")
            text = span.get_text(strip=True) if span else h.get_text(strip=True)
            if "E.G.O饰品" in text or "饰品" in text:
                ego_gift_heading = h
                break

        if ego_gift_heading:
            self._extract_ego_gifts(ego_gift_heading, data)

        # 5) 提取相关异想体
        for h in self.mw_output.select("h2, h3"):
            span = h.select_one(".mw-headline")
            text = span.get_text(strip=True) if span else h.get_text(strip=True)
            if "异想体" in text:
                sibling = h.next_sibling
                safety = 0
                while sibling and safety < 20:
                    safety += 1
                    if hasattr(sibling, "get_text"):
                        st = sibling.get_text(strip=True)
                        if st and len(st) < 50:
                            data.related_abnormalities.append(st)
                    sibling = sibling.next_sibling
                break

        return data

    def _extract_option_from_pane(self, pane) -> Optional[EventOption]:
        """从单个 tab-pane 提取事件选项。"""
        option = EventOption()

        # 选择文本：span.label[style*="background: #9A6433"]
        label = pane.select_one('span.label[style*="background"]')
        if label:
            option.choice_text = label.get_text(strip=True)

        pane_text = pane.get_text(" ", strip=True)
        pane_html = str(pane)

        # 判定条件
        if "有利判定" in pane_text:
            option.check_type = "有利判定"
        elif "不利判定" in pane_text:
            option.check_type = "不利判定"

        if option.check_type:
            # 提取判定罪孽和阈值
            sin_imgs = pane.select('img[alt*="罪孽-"]')
            if sin_imgs:
                alt = sin_imgs[0].get("alt", "")
                sin_match = re.match(r"罪孽-(.+)", alt)
                if sin_match:
                    option.check_sin = sin_match.group(1)

            # 阈值：紧跟图标后的数字
            threshold_match = re.search(r"(\d+)\s*[点分]?", pane_text.split(option.check_type)[-1][:100] if option.check_type in pane_text else "")
            if not threshold_match:
                threshold_match = re.search(r"判定[：:]\s*(\d+)", pane_text)
            if threshold_match:
                try:
                    option.check_threshold = int(threshold_match.group(1))
                except ValueError:
                    pass

        # 判定成功结果
        success_label = pane.select_one('span.label[style*="background: #67AD69"]')
        if success_label:
            success_parent = success_label.parent
            if success_parent:
                # 收集判定成功后所有 li 或文本
                lis = success_parent.select("li")
                for li in lis:
                    text = li.get_text(strip=True)
                    if text:
                        option.success_outcomes.append(text)
                # 也收集直接文本
                if not option.success_outcomes:
                    sibling = success_label.next_sibling
                    if sibling:
                        text = sibling.strip() if hasattr(sibling, "strip") else str(sibling).strip()
                        if text:
                            option.success_outcomes.append(text)

        # 回退：从文本匹配
        if not option.success_outcomes and "判定成功" in pane_text:
            success_parts = pane_text.split("判定成功")
            if len(success_parts) > 1:
                after_success = success_parts[1][:300]
                # 匹配常见奖励模式
                rewards = re.findall(r"(?:获得|失去)\s*[^\n，,]+", after_success)
                for r in rewards:
                    option.success_outcomes.append(r.strip())

        # 判定失败结果
        failure_label = pane.select_one('span.label[style*="background: #CD3532"]')
        if failure_label:
            failure_parent = failure_label.parent
            if failure_parent:
                lis = failure_parent.select("li")
                for li in lis:
                    text = li.get_text(strip=True)
                    if text:
                        option.failure_outcomes.append(text)
                if not option.failure_outcomes:
                    sibling = failure_label.next_sibling
                    if sibling:
                        text = sibling.strip() if hasattr(sibling, "strip") else str(sibling).strip()
                        if text:
                            option.failure_outcomes.append(text)

        if not option.failure_outcomes and "判定失败" in pane_text:
            failure_parts = pane_text.split("判定失败")
            if len(failure_parts) > 1:
                after_failure = failure_parts[1][:300]
                rewards = re.findall(r"(?:获得|失去)\s*[^\n，,]+", after_failure)
                for r in rewards:
                    option.failure_outcomes.append(r.strip())

        return option if option.choice_text or option.check_type else None

    def _extract_ego_gifts(self, heading, data: EventData):
        """提取 E.G.O 饰品信息。"""
        sibling = heading.next_sibling
        safety = 0
        while sibling and safety < 30:
            safety += 1
            if hasattr(sibling, "name") and sibling.name in ("h2", "h3"):
                break
            if hasattr(sibling, "select"):
                # 查找包含饰品名称和效果的表格
                tables = sibling.select("table.wikitable, table.navbox")
                for table in tables:
                    rows = table.select("tr")
                    for row in rows:
                        cells = row.select("td, th")
                        if len(cells) >= 2:
                            name_cell = cells[0].get_text(strip=True)
                            effect_cell = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                            if name_cell and len(name_cell) > 1:
                                data.ego_gifts.append({
                                    "name": name_cell,
                                    "effect": effect_cell,
                                })
            sibling = sibling.next_sibling


# ── 知识页面提取 ──

def extract_knowledge_from_html(html: str, title: str, categories: list[str]) -> Optional[dict]:
    """提取机制知识页面为纯文本 dict。

    用于「基础数值」「攻击抗性与类型」「技能与拼点」「伤害计算」等页面。
    这些页面没有固定的数据提取结构，直接保存为全文 knowledge 类型。
    """
    if not HAS_BS4:
        return None

    soup = BeautifulSoup(html, "lxml")
    content_div = soup.select_one("#mw-content-text .mw-parser-output")
    if not content_div:
        return None

    # 移除不需要的元素
    for tag in content_div.select("script, style, .mw-references-wrap, .category-links, .navbox, .infobox"):
        tag.decompose()

    text = content_div.get_text("\n", strip=True)
    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    if not text.strip():
        return None

    return {
        "page_type": "knowledge",
        "title": title,
        "text": text,
        "categories": categories,
        "_structured": True,
    }


# ── 序列化辅助 ──

def _enemy_to_dict(data: EnemyData) -> dict:
    """将 EnemyData 转为可 JSON 序列化的字典。"""
    return {
        "page_type": data.page_type,
        "title": data.title,
        "enemy_name": data.enemy_name,
        "battle_stage": data.battle_stage,
        "body_part": data.body_part,
        "hp": data.hp,
        "defense_level": data.defense_level,
        "speed_min": data.speed_min,
        "speed_max": data.speed_max,
        "chaos_threshold": data.chaos_threshold,
        "physical_resistances": data.physical_resistances,
        "sin_resistances": data.sin_resistances,
        "panic_types": data.panic_types,
        "passives": data.passives,
        "skills": data.skills,
        "is_ally": data.is_ally,
        "_structured": True,
    }


def _enemy_list_to_dict(enemies: list[EnemyData]) -> Optional[dict]:
    """将敌方单位列表转为可分块的 dict。

    返回 None 仅当输入为 None（不应发生），空列表返回含 _empty 标记的 dict，
    以便上层区分「提取失败」和「全部被去重丢弃」。
    """
    if enemies is None:
        return None
    if not enemies:
        return {
            "page_type": "enemy",
            "title": "",
            "enemies": [],
            "_structured": True,
            "_empty": True,
        }
    return {
        "page_type": "enemy",
        "title": enemies[0].title,
        "battle_stage": enemies[0].battle_stage,
        "enemies": [_enemy_to_dict(e) for e in enemies],
        "_structured": True,
    }


def _event_to_dict(data: EventData) -> dict:
    """将 EventData 转为可 JSON 序列化的字典。"""
    return {
        "page_type": data.page_type,
        "title": data.title,
        "event_name": data.event_name,
        "narration": data.narration,
        "options": [
            {
                "choice_text": opt.choice_text,
                "check_type": opt.check_type,
                "check_sin": opt.check_sin,
                "check_threshold": opt.check_threshold,
                "success_outcomes": opt.success_outcomes,
                "failure_outcomes": opt.failure_outcomes,
            }
            for opt in data.options
        ],
        "ego_gifts": data.ego_gifts,
        "related_abnormalities": data.related_abnormalities,
        "trigger_location": data.trigger_location,
        "_structured": True,
    }


# ── 公共入口 ──

def classify_page_type_from_categories(categories: list[str], wikitext: str = "", title: str = "") -> Optional[str]:
    """从 categories + WikiText 内容判断页面类型。

    剧情分类需要进一步区分但丁笔记和故事对话。
    状态效果页面无分类标签，需通过 WikiText 模板模式检测。
    """
    if "人格" in categories:
        return "personality"
    if "E.G.O" in categories:
        return "ego"

    # 探索事件页面：标题以 "事件-" 开头。必须在敌方判定之前检查，
    # 因为事件页面也属于"战斗数据"分类，但应归类为 event。
    if title.startswith("事件-"):
        return "event"

    # 敌方单位页面（修复 P21-A 里恩/9-50 缺失）：
    # 部分战斗关卡页面同时含"剧情"与"主线战斗/战斗数据"分类，原实现中"剧情"
    # 检查优先，导致 EnemyExtractor 未运行。此处将敌方判定提前到"剧情"之前：
    #   - 含敌方模板 → 强制 enemy（战斗数据分类缺失时兜底）
    #   - 分类含"主线战斗/战斗数据" → enemy
    if wikitext and ("{{敌方技能|" in wikitext or "{{敌方单位|" in wikitext):
        return "enemy"
    if any(c in categories for c in ("主线战斗", "战斗数据")):
        return "enemy"

    # 剧情分类 → 根据 WikiText 内容区分
    if "剧情" in categories:
        if wikitext:
            if "{{#html:Dantenote}}" in wikitext or title == "但丁笔记":
                return "story_note"
            # 主线剧情用 {{Dialog|，人格剧情用 {{Dialog-人格|，两者均为故事对话
            if "{{Dialog|" in wikitext or "{{Dialog-人格|" in wikitext:
                return "story_dialogue"
        # 回退：按标题判断
        if title == "但丁笔记":
            return "story_note"
        return "story_dialogue"

    # 状态效果页面：无分类标签，通过 WikiText 模板 {{状态页面|...}} 检测
    if wikitext and ("{{状态页面|" in wikitext or "{{状态页面" in wikitext):
        return "status_effect"

    # 机制知识页面：通过标题精确匹配
    _KNOWLEDGE_TITLES = {
        "基础数值", "攻击抗性与类型", "技能与拼点", "伤害计算",
        # 基础机制子页面
        "体力", "理智值", "速度值", "防御等级", "攻击等级",
        "攻击容量", "光之树苗能力", "脑啡肽",
    }
    if title in _KNOWLEDGE_TITLES:
        return "knowledge"

    return None


def extract_from_html(
    html: str,
    title: str,
    categories: list[str],
    wikitext: str = "",
    page_type: Optional[str] = None,
) -> Optional[dict]:
    """公共入口：根据页面类型选择提取器。

    Args:
        html: 渲染后的 HTML 文本
        title: 页面标题
        categories: 分类列表
        wikitext: 原始 WikiText（用于 story_dialogue 等类型）
        page_type: 预分类结果（可选，避免重复分类）

    Returns:
        成功时返回 dict，失败或非目标类型返回 None。
    """
    if page_type is None:
        page_type = classify_page_type_from_categories(categories, wikitext, title)
    if not page_type:
        return None

    try:
        if page_type == "personality":
            if not HAS_BS4:
                return None
            extractor = PersonalityExtractor(html, title, categories, wikitext)
            data = extractor.extract()
            return _personality_to_dict(data)

        elif page_type == "ego":
            if not HAS_BS4:
                return None
            extractor = EgoExtractor(html, title, categories)
            data = extractor.extract()
            return _ego_to_dict(data)

        elif page_type == "story_dialogue":
            # 从 WikiText 解析 Dialog 模板
            return extract_story_dialogue_from_wikitext(wikitext, title, categories)

        elif page_type == "story_note":
            # 但丁笔记：从 Playwright 渲染的 HTML 提取
            return extract_story_note_from_html(html, title, categories)

        elif page_type == "status_effect":
            # 状态效果页面：从 action=parse 渲染的 HTML 提取
            return extract_status_effect_from_html(html, title, categories)

        elif page_type == "knowledge":
            # 机制说明页面：纯文本提取
            return extract_knowledge_from_html(html, title, categories)

        elif page_type == "enemy":
            # 敌方单位页面（战斗数据/主线战斗）：结构化提取
            if not HAS_BS4:
                return None
            extractor = EnemyExtractor(html, title, categories, wikitext)
            enemies = extractor.extract()
            result = _enemy_list_to_dict(enemies)
            if result and result.get("_empty"):
                logger.info(f"敌方单位全部被去重丢弃，跳过: {title}")
                # 去重丢弃不应视为提取失败，返回带有 _empty 标记的结果
                return result
            return result

        elif page_type == "event":
            # 探索事件页面：结构化提取
            if not HAS_BS4:
                return None
            extractor = EventExtractor(html, title, categories)
            data = extractor.extract()
            return _event_to_dict(data) if data else None

    except Exception as e:
        logger.error(f"HTML 结构化提取失败 [{title}]: {e}", exc_info=True)
        return None

    return None


def _personality_to_dict(data: PersonalityData) -> dict:
    """将 PersonalityData 转为可 JSON 序列化的字典。"""
    return {
        "page_type": data.page_type,
        "title": data.title,
        "sinner": data.sinner,
        "sinner_id": data.sinner_id,
        "personality_name": data.personality_name,
        "release_date": data.release_date,
        "acquisition": data.acquisition,
        "sin_affinities": data.sin_affinities,
        "physical_resistances": data.physical_resistances,
        "ego_resources": data.ego_resources,
        "skills": data.skills,
        "battle_passive": data.battle_passive,
        "support_passive": data.support_passive,
        "notes": data.notes,
        "voice_lines": data.voice_lines,
        "skill_voice": data.skill_voice,
        "_structured": True,
    }


def _ego_to_dict(data: EgoData) -> dict:
    """将 EgoData 转为可 JSON 序列化的字典。"""
    return {
        "page_type": data.page_type,
        "title": data.title,
        "sinner": data.sinner,
        "sinner_id": data.sinner_id,
        "ego_name": data.ego_name,
        "release_date": data.release_date,
        "acquisition": data.acquisition,
        "resource_costs": data.resource_costs,
        "sin_resistances": data.sin_resistances,
        "awakening_stages": data.awakening_stages,
        "erosion_stages": data.erosion_stages,
        "passive_name": data.passive_name,
        "passive_description": data.passive_description,
        "_structured": True,
    }


# ── 状态效果提取器 ──

def extract_status_effect_from_html(html: str, title: str, categories: list[str]) -> Optional[dict]:
    """从 action=parse 渲染的状态效果页面 HTML 中提取结构化数据。

    状态效果页面结构（基于 {{状态页面}} 模板渲染）：
    - table.infobox：名称、类型（正面/负面/特殊）、罪孽、关键词、属性
    - h2 "描述" 段落：效果描述（含 {0}/{1} 占位符）
    - h2 "预览" 段落：游戏内 tooltip 预览（可选）

    占位符替换：
    - {0} → 强度（动态值）
    - {1} → 层数（持续回合）

    Args:
        html: action=parse 渲染的 HTML
        title: 页面标题（状态效果名）
        categories: 分类列表

    Returns:
        {"page_type": "status_effect", "title": ..., "effect_type": ..., ...}
    """
    if not HAS_BS4:
        return None

    soup = BeautifulSoup(html, "lxml")
    mw_output = soup.select_one(".mw-parser-output")
    if not mw_output:
        return None

    result = {
        "page_type": "status_effect",
        "title": title,
        "name": title,
        "effect_type": "",       # 正面 / 负面 / 特殊
        "sin_affinity": "",      # 罪孽亲和（如暴怒）
        "keywords": [],          # 关键词列表
        "properties": [],        # 属性列表（如可解除）
        "description": "",       # 描述文本（已替换占位符）
        "_structured": True,
    }

    # ── 1) 解析 infobox 表格 ──
    infobox = mw_output.select_one("table.infobox")
    if infobox:
        for row in infobox.select("tr"):
            cells = row.select("th, td")
            if len(cells) < 2:
                continue
            key = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)

            if not key or not value:
                continue

            if key == "类型":
                result["effect_type"] = value
            elif key == "罪孽":
                result["sin_affinity"] = value
            elif key == "关键词":
                # 关键词以顿号或逗号分隔
                result["keywords"] = [k.strip() for k in re.split(r"[、,，]", value) if k.strip()]
            elif key == "属性":
                result["properties"] = [p.strip() for p in re.split(r"[、,，]", value) if p.strip()]

    # ── 2) 解析描述段落 ──
    desc_heading = None
    for h in mw_output.select("h2"):
        span = h.select_one(".mw-headline")
        text = span.get_text(strip=True) if span else h.get_text(strip=True)
        if "描述" in text:
            desc_heading = h
            break

    if desc_heading:
        # 收集 h2 "描述" 之后、下一个 h2 之前的 <p> 文本
        sibling = desc_heading.next_sibling
        desc_parts = []
        while sibling:
            if hasattr(sibling, "name") and sibling.name == "h2":
                break
            if hasattr(sibling, "name") and sibling.name in ("p", "div"):
                text = sibling.get_text(" ", strip=True)
                if text:
                    desc_parts.append(text)
            elif hasattr(sibling, "strip") and callable(sibling.strip):
                text = sibling.strip()
                if text:
                    desc_parts.append(text)
            sibling = sibling.next_sibling

        description = " ".join(desc_parts).strip()

        # 替换占位符：{0}→强度, {1}→层数（持续回合）
        description = description.replace("{0}", "强度").replace("{1}", "层数（持续回合）")
        result["description"] = description

    # 至少要有描述才返回有效结果
    if not result["description"]:
        logger.warning(f"状态效果页面 {title} 未找到描述文本")
        return None

    logger.debug(f"状态效果 {title}: 提取完成，类型={result['effect_type']}")
    return result


# ── 故事/剧情提取器 ──

# 角色名后缀去除正则：匹配数字后缀（如 浮士德1→浮士德，李箱2→李箱）
_CHAR_SUFFIX_RE = re.compile(r"(\d+)$")


def _strip_char_suffix(name: str) -> str:
    """去除角色名末尾的数字后缀（立绘变体编号）。

    例如: 浮士德1 → 浮士德, 李箱2 → 李箱, 格里高尔 → 格里高尔
    """
    return _CHAR_SUFFIX_RE.sub("", name).strip()


# Dialog 模板匹配：
#   {{Dialog|角色=NAME|对话=CONTENT|语音=FILE}}          主线剧情
#   {{Dialog-人格|角色=NAME|对话=CONTENT}}               人格剧情（模板带 -人格 后缀，
#                                                        参数名与主线一致，见人格剧情页面）
# 使用大括号计数法处理嵌套模板（如 {{Status|...}}、{{正面|...}} 等）
# 兼容 8-33-06 等页面的多行写法：{{Dialog\n|角色=...\n|对话=...\n|语音=...\n}}
# 负向断言仅排除 {{Dialog-头像|...}} 这类带连字符的同名"辅助渲染"模板
# （Dialog-头像 是无对话内容的图片模板，不应解析为对话行），
# 多行换行（\n）不在此列，故可跨行匹配
_DIALOG_START_RE = re.compile(
    r'\{\{(?![Dd]ialog-(?:头像))[Dd]ialog(?:-人格)?[\s\n]*\|',
    re.MULTILINE,
)

# 章节标题：=== 标题 ===（允许标题前后有空白）
# 必须带 MULTILINE，使 ^/$ 匹配行首/行尾（8-33-06 用 <big> 分节，但旧格式仍用 === 章节 ===）
_SECTION_RE = re.compile(r'^\s*===\s*(.+?)\s*===\s*$', re.MULTILINE)


def _parse_dialog_params(template_content: str) -> dict:
    """从 Dialog 模板内容中解析参数（角色、对话、语音）。

    支持两种格式：
    1. 主线 {{Dialog|章节=..|角色=..|语音=..|对话=..}}：命名参数
       （章节/角色/语音/对话，语音可能缺失即旁白）
    2. 人格剧情 {{Dialog-人格|头像|标签|角色名|对话}}：位置参数
       其中：第1=头像名（无/以实玛利/空），第2=标签（LCCB 等），
       第3=角色名，第4=对话内容。
       旁白形式为 {{Dialog-人格|空|4=旁白内容}}（"4=" 是第4个位置参数的命名写法）。

    使用大括号计数法分割顶级管道符，正确处理嵌套模板。
    例如: 角色=李箱|对话=我想...{{Status|流血}}...|语音=xxx.ogg
    """
    result = {"role": "", "dialog": "", "voice": ""}
    # 按顶级 | 分割（不计嵌套 {{...}} 内的 |）
    parts = []
    depth = 0
    current = []
    for ch in template_content:
        if ch == '{' and len(current) > 0 and current[-1] == '{':
            depth += 1
            current.append(ch)
        elif ch == '}' and len(current) > 0 and current[-1] == '}':
            depth -= 1
            current.append(ch)
        elif ch == '|' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))

    # 第一个 part 是模板名（Dialog / Dialog-人格），跳过
    parts = parts[1:] if parts else []
    # 位置参数计数（仅用于人格剧情 Dialog-人格 的无名参数）
    pos = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 主线 Dialog 的命名参数
        if part.startswith("角色=") or part.startswith("角色 ="):
            result["role"] = part.split("=", 1)[1].strip()
        elif part.startswith("对话=") or part.startswith("对话 ="):
            result["dialog"] = part.split("=", 1)[1].strip()
        elif part.startswith("语音=") or part.startswith("语音 ="):
            result["voice"] = part.split("=", 1)[1].strip()
        elif part.startswith("4=") and not result["dialog"]:
            # 人格剧情旁白：{{Dialog-人格|空|4=旁白内容}}
            result["dialog"] = part.split("=", 1)[1].strip()
        else:
            # 人格剧情无名位置参数：第3个=角色名，第4个=对话内容
            pos += 1
            if pos == 3:
                result["role"] = part
            elif pos == 4:
                result["dialog"] = part

    return result


def parse_story_dialogue(wikitext: str) -> list[dict]:
    """从剧情对话 WikiText 解析为结构化对话块。

    支持的内容：
    - === 章节名 === → 章节标题
    - {{Dialog|角色=NAME|对话=CONTENT|语音=FILE}} → 对话行（单行）
    - {{Dialog\n|角色=...\n|对话=...\n|语音=...\n}} → 对话行（多行，8-33-06 等页面）
    - {{Dialog|对话=CONTENT|语音=FILE}} → 旁白（无角色）
    - 其他 → 跳过（图片、标注等）

    使用大括号计数法处理嵌套模板（如 {{Status|...}}），并对整篇做
    字符流扫描，因此模板跨行也能正确闭合（这是 8-33-06 回退问题的根因）。

    Returns:
        [{type: "section"|"dialogue"|"narration", role: str, text: str}, ...]
    """
    blocks = []
    text_len = len(wikitext)
    pos = 0

    while pos < text_len:
        # 1) 查找下一个可能作为起点的位置：章节标题或 Dialog 模板
        sec_match = _SECTION_RE.search(wikitext, pos)
        dia_match = _DIALOG_START_RE.search(wikitext, pos)
        candidates = [(m.start(), "section", m) for m in [sec_match] if m]
        candidates += [(m.start(), "dialog", m) for m in [dia_match] if m]
        if not candidates:
            break
        candidates.sort(key=lambda t: t[0])
        start, kind, match = candidates[0]

        if kind == "section":
            blocks.append({
                "type": "section",
                "role": "",
                "text": match.group(1).strip(),
            })
            pos = match.end()
            continue

        # 2) Dialog 模板：从 {{Dialog 起点做跨行大括号计数
        depth = 1                      # 已经进入一层 {{
        i = match.start() + 2          # 跳过 {{
        content_end = -1
        while i < text_len and depth > 0:
            if wikitext[i:i+2] == '{{':
                depth += 1
                i += 2
            elif wikitext[i:i+2] == '}}':
                depth -= 1
                if depth == 0:
                    content_end = i
                    break
                i += 2
            else:
                i += 1

        if content_end < 0:
            # 模板未闭合，跳过起点，避免死循环
            pos = match.start() + 2
            continue

        template_content = wikitext[match.start() + 2:content_end]
        params = _parse_dialog_params(template_content)
        role = params["role"]
        text = params["dialog"]
        if text:
            # HTML 实体解码（注意顺序：& 最后替换，避免破坏 </>）
            text = text.replace("<", "<").replace(">", ">").replace("&", "&")
            # 剥离剧情原文中的 HTML 强调标签（如 <font style="color:...">、<b>）
            # 及 wiki 粗体标记 '''，避免残留标签被带入向量库/回答。
            text = re.sub(r"<[^>]+>", "", text)
            text = text.replace("'''", "")
            if role:
                role = _strip_char_suffix(role)
                blocks.append({
                    "type": "dialogue",
                    "role": role,
                    "text": text,
                })
            else:
                blocks.append({
                    "type": "narration",
                    "role": "",
                    "text": text,
                })

        pos = content_end + 2

    return blocks


def extract_story_dialogue_from_wikitext(wikitext: str, title: str, categories: list[str]) -> Optional[dict]:
    """从剧情对话 WikiText 提取结构化数据。

    Returns:
        {"page_type": "story_dialogue", "title": ..., "blocks": [...], ...}
    """
    blocks = parse_story_dialogue(wikitext)
    if not blocks:
        return None

    # 提取章节名（第一个 section）
    chapter = ""
    for b in blocks:
        if b["type"] == "section":
            chapter = b["text"]
            break

    result = {
        "page_type": "story_dialogue",
        "title": title,
        "chapter": chapter,
        "blocks": blocks,
        "_structured": True,
    }

    # 人格剧情关联：仅当页面使用 {{Dialog-人格| 模板（且标题以"剧情"结尾）时，
    # 剥离"剧情"后缀派生出对应人格名（如 以实玛利LCCB系长剧情 → 以实玛利LCCB系长），
    # 供 RAG 端按人格名检索关联剧情。主线剧情（{{Dialog|）不触发，只作用于人格剧情页面。
    if "{{Dialog-人格|" in wikitext and title.endswith("剧情"):
        personality_name = title[: -len("剧情")].strip()
        if personality_name:
            result["personality_name"] = personality_name

    return result


def extract_story_note_from_html(html: str, title: str, categories: list[str]) -> Optional[dict]:
    """从但丁笔记的 Playwright 渲染 HTML 中提取结构化数据。

    解析 .dantecontainer 中由 JS 生成的 DOM 树：
    - .dantecategory (分类导航) → 建立分类层级
    - .content-item (内容块) → .content-title + .content-text

    Args:
        html: Playwright 渲染后的完整 HTML
        title: 页面标题
        categories: 分类列表

    Returns:
        {"page_type": "story_note", "title": ..., "categories": [...], "entries": [...]}
    """
    if not HAS_BS4:
        return None

    soup = BeautifulSoup(html, "lxml")

    # 查找 .dantecontainer 中的内容
    container = soup.select_one(".dantecontainer")
    if not container:
        # 回退：直接在 mw-parser-output 中查找 .content-item
        content_area = soup.select_one(".mw-parser-output")
        if not content_area:
            return None
    else:
        content_area = container.select_one("#content-dantecontainer") or container

    # 提取导航层级（.dantecategory）
    nav_items = soup.select(".dantecategory")
    category_tree: list[dict] = []
    for item in nav_items:
        text_el = item.select_one(".dantecategory-text")
        cat_name = text_el.get_text(strip=True) if text_el else ""
        if not cat_name:
            continue

        # 判断层级
        classes = item.get("class", [])
        if "main-dantecategory" in classes or "level-1" in classes:
            level = 1
        elif "sub-dantecategory" in classes or "level-2" in classes:
            level = 2
        elif "sub-sub-dantecategory" in classes or "level-3" in classes:
            level = 3
        else:
            level = 1

        category_tree.append({"name": cat_name, "level": level})

    # 提取内容块
    content_items = content_area.select(".content-item")
    entries: list[dict] = []
    for item in content_items:
        title_el = item.select_one(".content-title")
        text_el = item.select_one(".content-text")
        if not title_el or not text_el:
            continue

        entry_title = title_el.get_text(strip=True)
        entry_text = text_el.get_text("\n", strip=True)
        entries.append({"title": entry_title, "text": entry_text})

    if not entries:
        # 回退：用纯文本方式提取
        return _extract_note_from_text(html, title)

    # 根据条目标题计算分类路径
    # 条目标题格式: "但丁笔记 > 都市 > 地区特征"
    enriched_entries = []
    for entry in entries:
        path_parts = entry["title"].split(">")
        path_parts = [p.strip() for p in path_parts]
        enriched_entries.append({
            "title": entry["title"],
            "text": entry["text"],
            "path": path_parts,
        })

    return {
        "page_type": "story_note",
        "title": title,
        "category_tree": category_tree,
        "entries": enriched_entries,
        "_structured": True,
    }


def _extract_note_from_text(html: str, title: str) -> Optional[dict]:
    """回退方案：用纯文本方式从 HTML 提取但丁笔记内容。

    使用 BeautifulSoup get_text() 获取全文，按 "记录 #N" 模式分块。
    """
    if not HAS_BS4:
        return None

    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text("\n", strip=False)

    # 按 "记录 #N YYYY.MM.DD" 分割
    record_pattern = re.compile(r'(记录\s*#\d+\s+\d{4}\.\d{1,2}\.\d{1,2})')
    parts = record_pattern.split(full_text)

    entries = []
    current_title = ""
    for part in parts:
        if not part.strip():
            continue
        if record_pattern.match(part):
            current_title = part.strip()
        elif current_title:
            text = part.strip()
            # 截取合理长度（最多 5000 字符）
            if len(text) > 5000:
                text = text[:5000] + "..."
            entries.append({"title": current_title, "text": text})
            current_title = ""

    if not entries:
        return None

    return {
        "page_type": "story_note",
        "title": title,
        "entries": entries,
        "_structured": True,
    }
