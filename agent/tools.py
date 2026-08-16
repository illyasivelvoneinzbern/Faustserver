"""
Agent 工具定义：可被 Tool-calling Agent 调用的工具函数。
"""

import json
import logging
import re
from typing import Any, Callable, Optional

try:
    from langchain.tools import StructuredTool
except ImportError:  # langchain >= 1.x：StructuredTool 迁移至 langchain_core.tools
    from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def create_wiki_search_tool(retriever: Any) -> StructuredTool:
    """创建 Wiki 搜索工具"""
    def search_wiki(query: str) -> str:
        """搜索边狱巴士 Wiki 知识库"""
        docs = retriever.retrieve(query, filter_dict={"source": "wiki"})
        if not docs:
            return "未找到相关的边狱巴士知识。"
        return retriever.format_context(docs)

    return StructuredTool.from_function(
        func=search_wiki,
        name="search_limbus_wiki",
        description="搜索边狱巴士（Limbus Company）Wiki知识库，获取角色、人格、E.G.O、剧情等设定信息。",
    )


def create_news_search_tool(retriever: Any) -> StructuredTool:
    """创建官方资讯搜索工具"""
    def fetch_news(query: str = "") -> str:
        """拉取边狱巴士官方最新推文/公告"""
        filter_dict = {"source": "x_twitter"} if query else {}
        docs = retriever.retrieve(query or "最新公告", filter_dict=filter_dict)
        if not docs:
            return "暂无最新的官方资讯。"
        return retriever.format_context(docs)

    return StructuredTool.from_function(
        func=fetch_news,
        name="fetch_limbus_news",
        description="获取边狱巴士官方最新推文、公告和更新资讯。",
    )


def create_roll_dice_tool() -> StructuredTool:
    """创建掷骰子工具（边狱巴士风格判定）"""
    import random

    def roll_dice(sides: int = 6, count: int = 1) -> str:
        """掷骰子，支持多面骰和多次投掷"""
        if not (1 <= count <= 10):
            return "投掷次数应在 1~10 之间。"
        if sides not in (4, 6, 8, 10, 12, 20, 100):
            return "骰子面数应为 4/6/8/10/12/20/100。"

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        if count == 1:
            return f"掷出了 {total} 点。"
        return f"掷出了 {' + '.join(map(str, rolls))} = {total} 点。"

    return StructuredTool.from_function(
        func=roll_dice,
        name="roll_dice",
        description="掷骰子进行边狱巴士风格判定。参数：sides(面数,默认6)，count(次数,默认1)。",
    )


def create_damage_calc_tool() -> StructuredTool:
    """创建伤害计算工具：硬编码公式，不依赖 LLM 推算。

    当用户询问「某人格/某技能能打多少伤害」「某 buff 下最大伤害」
    「某技能对某单位能造成多少伤害」等问题时调用此工具。
    """
    from combat.damage_calculator import (
        SkillData, UnitState, BuffEffect,
        calculate_max_damage, calculate_expected_damage_range,
    )

    def calc_damage(skill_json: str, attacker_json: str, defender_json: str) -> str:
        """计算边狱巴士伤害。

        Args:
            skill_json: 技能数据 JSON。格式：
                {"sin_type":"暴怒","damage_type":"斩击","base_value":4,
                 "coin_power":4,"coin_count":4,"attack_level":40,"attack_weight":1}
            attacker_json: 进攻方状态 JSON。格式：
                {"name":"浮士德","defense_level":35,"sanity":0,
                 "buffs":[{"name":"伤害强化","value":0.2,"category":"offense_mult"}]}
            defender_json: 受击方状态 JSON。格式：
                {"name":"暴躁的残兵","defense_level":35,
                 "physical_resistances":{"斩击":2.0,"突刺":1.0,"打击":1.0},
                 "sin_resistances":{"暴怒":1.0,"色欲":1.0,"怠惰":1.0,"暴食":1.0,"忧郁":1.0,"傲慢":1.0,"嫉妒":1.0},
                 "chaos_level":0,"buffs":[]}

        Returns:
            格式化的伤害计算结果（含明细）
        """
        try:
            # 解析 JSON 参数
            skill_data = json.loads(skill_json)
            attacker_data = json.loads(attacker_json)
            defender_data = json.loads(defender_json)

            skill = SkillData(
                sin_type=skill_data.get("sin_type", ""),
                damage_type=skill_data.get("damage_type", ""),
                base_value=skill_data.get("base_value", 0),
                coin_power=skill_data.get("coin_power", 0),
                coin_count=skill_data.get("coin_count", 0),
                attack_level=skill_data.get("attack_level", 0),
                attack_weight=skill_data.get("attack_weight", 1),
                is_guard=skill_data.get("is_guard", False),
            )

            attacker = UnitState(
                name=attacker_data.get("name", "进攻方"),
                defense_level=attacker_data.get("defense_level", 0),
                sanity=attacker_data.get("sanity", 0),
                active_buffs=[
                    BuffEffect(name=b["name"], value=b["value"], category=b.get("category", "offense_mult"))
                    for b in attacker_data.get("buffs", [])
                ],
            )

            defender = UnitState(
                name=defender_data.get("name", "受击方"),
                defense_level=defender_data.get("defense_level", 0),
                physical_resistances=defender_data.get("physical_resistances", {}),
                sin_resistances=defender_data.get("sin_resistances", {}),
                chaos_level=defender_data.get("chaos_level", 0),
                active_buffs=[
                    BuffEffect(name=b["name"], value=b["value"], category=b.get("category", "defense_mult"))
                    for b in defender_data.get("buffs", [])
                ],
            )

            # 计算最大伤害
            result = calculate_max_damage(skill, attacker, defender)
            d = result.details

            # 格式化输出
            lines = [
                f"【伤害计算】{attacker.name} 的 {skill.sin_type}-{skill.damage_type} 技能 → {defender.name}",
                f"",
                f"▸ 技能数据：基础值 {skill.base_value}，硬币威力 +{skill.coin_power}，硬币数 {skill.coin_count}",
                f"  硬币威力区间：{d['coin_power_range'][0]} ~ {d['coin_power_range'][1]}",
                f"",
                f"▸ 抗性：物理 {skill.damage_type}={d['physical_resistance_value']}，罪孽 {skill.sin_type}={d['sin_resistance_value']}",
                f"",
                f"▸ 第一类乘算增伤：{d['type1_multiplicative']}",
                f"  第二类乘算增伤：{d['type2_multiplicative']}",
                f"  第一类加算增伤：{d['type1_additive']}",
                f"  第二类加算增伤：{d['type2_additive']}",
                f"",
                f"▸ 伤害结果：",
                f"  最小伤害（全反面）：{result.min_damage}",
                f"  最大伤害（全正面）：{result.max_damage}",
                f"  期望伤害（coin_prob={d['coin_probability']}）：{result.expected_damage}",
            ]

            if d.get("status_damage", 0) > 0:
                lines.append(f"  状态效果触发伤害：{d['status_damage']}")

            if d.get("is_crit"):
                lines.append(f"  （含暴击，暴击倍率已计入第一类乘算）")

            return "\n".join(lines)

        except json.JSONDecodeError as e:
            return f"参数 JSON 解析失败：{e}。请确保参数是合法的 JSON 字符串。"
        except Exception as e:
            logger.error(f"伤害计算异常: {e}")
            return f"伤害计算失败：{e}"

    return StructuredTool.from_function(
        func=calc_damage,
        name="calculate_damage",
        description=(
            "硬编码边狱巴士伤害计算公式。当用户询问特定技能能造成多少伤害、"
            "某 buff 组合下的最大伤害、某技能对特定单位的伤害期望时调用。"
            "参数说明：skill_json 是技能 JSON（sin_type/damage_type/base_value/coin_power/coin_count/attack_level），"
            "attacker_json 是进攻方状态（name/defense_level/sanity/buffs），"
            "defender_json 是受击方状态（name/defense_level/physical_resistances/sin_resistances/chaos_level/buffs）。"
            "buffs 格式：[{name, value, category}]，category 为 offense_mult/defense_mult/offense_add/defense_add/status_damage。"
        ),
    )


def create_gacha_tool() -> StructuredTool:
    """创建抽奖（Gacha）工具：三灯3% / 二灯13% / 一灯81% / EGO 3%。

    用户表达抽卡/抽奖意图（如「抽卡」「十连」「抽一下」）时调用。
    数据源：data/gacha/rarity.json + ego_pool.json（scripts/build_gacha_data.py 生成）。
    """
    def gacha_pull(times: int = 1) -> str:
        """进行边狱巴士人格/EGO 抽奖。

        Args:
            times: 抽取次数，1=单抽，10=十连（1~100）
        """
        from tools.gacha import gacha_pull as _pull
        return _pull(times)

    return StructuredTool.from_function(
        func=gacha_pull,
        name="gacha_pull",
        description=(
            "边狱巴士（Limbus Company）人格/EGO 抽奖。"
            "概率：三灯人格3%、二灯人格13%、一灯人格81%、EGO 3%。"
            "参数 times：抽取次数（1=单抽，10=十连，1~100）。"
            "返回每抽的灯级与名称（如『三灯人格 · 浮士德黑兽-卯魁首』）。"
            "当用户说『抽卡/抽奖/十连/单抽/抽一次』等时调用。"
        ),
    )


def create_default_tools(retriever: Any) -> list[StructuredTool]:
    """创建默认工具集"""
    return [
        create_wiki_search_tool(retriever),
        create_news_search_tool(retriever),
        create_roll_dice_tool(),
        create_damage_calc_tool(),
    ]


# ═══════════════════════════════════════════════════════════════════════
# 人格切换工具（P17）：LLM 可调用的 switch_persona + 正则预拦截兜底
# ═══════════════════════════════════════════════════════════════════════

# 名称清洗字符（引号/括号/空白）
_PERSONA_NAME_STRIP_CHARS = "「」『』\"'“”()（） \t"

# 常见异体字/谐音字归一化（用于宽松匹配，如 唐吉诃德 → 堂吉诃德）
_PERSONA_NORM_REPL = {
    "唐": "堂",   # 唐吉诃德 → 堂吉诃德
    "轲": "诃",   # 吉轲德 → 吉诃德
    "柯": "诃",   # 吉柯德 → 吉诃德
}

# 强信号正则：明确的「切换/变成X人格」句式（name 捕获目标人格名）
_PERSONA_SWITCH_PATTERNS = [
    re.compile(
        r"(?:人格|角色|身份)?"
        r"(?:切换|换成|变成|变为|改成|换为|改为|转成|换回|变回|改回|扮演|装作|装成)"
        r"(?:为|成|到)?(?:人格|角色|身份)?(?:为|成|到)?"
        r"\s*[「『\"']?(?P<name>[^。！？!?，,、\s「」『』\"'（）]{1,20})[」』\"']?"
    ),
    re.compile(
        r"(?:扮演|cos(?:play)?)\s*[「『\"']?(?P<name>[^。！？!?，,、\s「」『』\"'（）]{1,20})[」』\"']?"
    ),
]

# 提问型前缀：命中说明用户在问“怎么切换”，而非下达切换指令
_PERSONA_QUESTION_HINTS = (
    "怎么", "如何", "怎样", "啥", "什么", "干什么", "操作", "步骤",
    "方法", "指令", "命令", "是啥", "啥意思", "什么意思", "用法", "吗", "呢",
)

# 弱信号词：强正则未命中但含这些词 → 交由 LLM function-calling 判断
_PERSONA_SWITCH_WEAK_WORDS = [
    "切换", "换成", "变成", "换为", "改为", "变为",
    "转成", "改成", "扮演", "装作", "装成", "变回", "改回", "换回",
]

# 泛词目标：未给出具体人格名时（如「如何切换人格」），不视为切换指令
_PERSONA_GENERIC_TARGETS = (
    "人格", "角色", "身份", "人设", "风格", "样子", "说话", "口吻",
    "你", "我", "它", "这个", "那个", "一下",
)


def _normalize_persona_key(s: str) -> str:
    """规范化人名：统一常见异体字/谐音字，便于宽松匹配。"""
    return "".join(_PERSONA_NORM_REPL.get(ch, ch) for ch in (s or ""))


def resolve_persona_id(persona_manager: Any, raw: str) -> Optional[str]:
    """宽松解析用户说的人格 → 人格 ID（P17）。

    支持（按优先级）：
    - 精确 ID（faust / donquixote）
    - 名称 / 显示名（浮士德 / 堂吉诃德）
    - 谐音/异体字（唐吉诃德 → 堂吉诃德）
    - 去除「人格/身份」后缀（堂吉诃德人格 → 堂吉诃德）
    - 包含匹配（目标含人格名，或人格名含目标；仅唯一命中才返回）

    Returns:
        唯一命中的人格 ID；无法解析或有歧义返回 None。
    """
    if not raw:
        return None
    target = raw.strip().strip(_PERSONA_NAME_STRIP_CHARS)
    if not target:
        return None

    personas = persona_manager.personas
    if not personas:
        return None

    # 1) 精确 ID
    if target in personas:
        return target

    # 2) 规范化后精确匹配 name / display_name / id
    norm_target = _normalize_persona_key(target)
    for pid, p in personas.items():
        cands = {pid, p.get("name", ""), p.get("display_name", "")}
        if norm_target in cands:
            return pid

    # 3) 去除「人格/身份」字样后规范化精确匹配
    cleaned = norm_target.replace("人格", "").replace("身份", "").strip()
    if cleaned and cleaned != norm_target:
        for pid, p in personas.items():
            cands = {
                _normalize_persona_key(c)
                for c in (pid, p.get("name", ""), p.get("display_name", ""))
            }
            if cleaned in cands:
                return pid

    # 4) 包含匹配（唯一命中才返回，避免歧义）
    hits: list[str] = []
    for pid, p in personas.items():
        name = _normalize_persona_key(p.get("name", "") or "")
        display = _normalize_persona_key(p.get("display_name", "") or "")
        if name and (name in norm_target or norm_target in name):
            hits.append(pid)
        elif display and (display in norm_target or norm_target in display):
            hits.append(pid)
    if len(hits) == 1:
        return hits[0]
    return None


def switch_persona_impl(
    persona_manager: Any,
    session_manager: Any,
    session_id: str,
    persona_id: str,
) -> str:
    """校验人格存在并切换会话人格，返回确认/错误文本。

    与 agent/core.py 的 _cmd_persona_switch 逻辑一致（保留命令入口的同时
    复用同一套切换语义）。session_manager 为 None 时仅校验不生效（dry 模式）。
    """
    persona = persona_manager.get(persona_id)
    if not persona:
        available = "、".join(persona_manager.list_ids()) or "（无）"
        return f"未找到人格「{persona_id}」。可用人格: {available}"

    if session_manager is not None:
        session_manager.set_persona(session_id, persona_id)

    display_name = persona.get("display_name", persona_id)
    return f"已切换人格为「{display_name}」。{persona.get('identity', '')}"


def detect_persona_switch_intent(text: str) -> str:
    """从用户消息中检测「切换人格」意图，返回解析出的目标名（原样）。

    命中明确句式（切换/变成/扮演X人格）且目标非提问用词时返回目标名；
    否则返回空串（未命中 → 交由常规链路或 LLM function-calling）。
    """
    for pat in _PERSONA_SWITCH_PATTERNS:
        m = pat.search(text)
        if m:
            name = (m.group("name") or "").strip().strip(_PERSONA_NAME_STRIP_CHARS)
            if not name:
                continue
            # 未给出具体人格名（如「如何切换人格」）→ 视为提问，不拦截
            if name in _PERSONA_GENERIC_TARGETS:
                continue
            # “人格怎么切换/如何切换”是提问，不是切换指令
            if _PERSONA_QUESTION_HINTS and name.startswith(_PERSONA_QUESTION_HINTS):
                continue
            return name
    return ""


def needs_persona_switch_llm(text: str) -> bool:
    """弱信号检测：未命中强信号正则但含人格切换意图词。

    命中 → 交由 LLM 原生 function-calling 判断（DeepSeek 兼容 OpenAI tools）。
    未命中 → 常规问答链路，零额外开销。
    """
    if detect_persona_switch_intent(text):
        return False
    return any(w in text for w in _PERSONA_SWITCH_WEAK_WORDS)


def run_persona_switch_preempt(
    persona_manager: Any,
    session_manager: Any,
    session_id: str,
    text: str,
) -> Optional[str]:
    """正则强信号预拦截主逻辑（确定性、零 LLM 成本）。

    检测切换意图 → 宽松解析人格 → 校验并切换会话人格，返回确认/错误文本。
    未命中切换意图返回 None，由调用方回落常规链路。
    """
    target = detect_persona_switch_intent(text)
    if not target:
        return None

    pid = resolve_persona_id(persona_manager, target)
    if pid is None:
        return (
            f"无法识别人格「{target}」。可用人格：\n"
            f"{persona_manager.get_persona_display_info()}"
        )
    return switch_persona_impl(persona_manager, session_manager, session_id, pid)


def create_persona_switch_tool(
    persona_manager: Any,
    session_manager: Any = None,
    session_id: str = "",
) -> StructuredTool:
    """创建『切换人格』工具：供 LLM function-calling 调用，自动切换会话人格。

    Args:
        persona_manager: PersonaManager 实例（用于校验/宽松匹配人格）
        session_manager: SessionManager 实例（用于执行切换；None 时仅校验不生效）
        session_id: 目标会话 ID
    """
    def switch_persona(persona_name: str) -> str:
        """切换当前会话人格到指定角色。

        Args:
            persona_name: 目标人格名称或 ID。支持别名与模糊写法，
                如「堂吉诃德」「堂吉诃德人格」「唐吉诃德」「浮士德」。
        """
        pid = resolve_persona_id(persona_manager, persona_name)
        if pid is None:
            return (
                f"无法识别人格「{persona_name}」。可用人格：\n"
                f"{persona_manager.get_persona_display_info()}"
            )
        return switch_persona_impl(
            persona_manager, session_manager, session_id, pid
        )

    return StructuredTool.from_function(
        func=switch_persona,
        name="switch_persona",
        description=(
            "切换边狱巴士角色人格。当用户表达切换人格/角色扮演意图时调用"
            "（如『切换人格为堂吉诃德』『变成浮士德』『扮演堂吉诃德』）。"
            "参数 persona_name 为目标人格名称（支持别名与模糊写法）。"
        ),
    )
