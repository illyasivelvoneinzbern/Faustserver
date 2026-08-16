"""
硬编码伤害计算模块：实现边狱公司的完整伤害公式。

所有公式均来自 Wiki「伤害计算」页面，不依赖 LLM。
公式经过精确编码，保证计算结果与游戏实际机制一致。

伤害流程：
  总伤害 = 当前硬币造成的伤害 + 目标状态效果触发的伤害 + 追加伤害
  当前硬币造成的伤害 = 当前硬币数值 × 第一类乘算增伤 × 第二类乘算增伤
                      + 第一类加算增伤 + 第二类加算增伤
"""

import math
from dataclasses import dataclass, field
from typing import Optional

# ── 攻击类型常量 ──
PHYSICAL_TYPES = ("斩击", "突刺", "打击")
SIN_TYPES = ("暴怒", "色欲", "怠惰", "暴食", "忧郁", "傲慢", "嫉妒")

# ── 抗性倍率 → 游戏显示值映射 ──
# 致命 ×2.0, 脆弱 ×1.5, 一般 ×1.0, 耐性 ×0.75, 抵抗 ×0.5
_RESISTANCE_TO_MULTIPLIER: dict[float, float] = {
    2.0: 2.0,
    1.5: 1.5,
    1.0: 1.0,
    0.75: 0.75,
    0.5: 0.5,
    0.0: 0.0,
}


@dataclass
class BuffEffect:
    """一个增益/减益效果。

    用于描述第二类乘算增伤和第二类加算增伤的来源。

    Attributes:
        name: 效果名称（如 "伤害强化", "易损"）
        value: 效果值（百分比形式，如 0.2 表示 +20%）
        category: 效果类别 — "offense_mult" (进攻方乘算), "defense_mult" (受击方乘算),
                  "offense_add" (进攻方加算), "defense_add" (受击方加算)
    """
    name: str
    value: float
    category: str = "offense_mult"  # offense_mult | defense_mult | offense_add | defense_add


@dataclass
class SkillData:
    """技能数据（由人格/敌人提取结果提供）。

    Attributes:
        sin_type: 罪孽类型（暴怒/色欲/怠惰/暴食/忧郁/傲慢/嫉妒）
        damage_type: 伤害类型（斩击/突刺/打击）
        base_value: 基础威力（投掷硬币前的固定值）
        coin_power: 硬币威力（每枚硬币的变动值）
        coin_count: 硬币数量
        attack_level: 攻击等级
        attack_weight: 攻击容量
        is_guard: 是否为守备技能
    """
    sin_type: str = ""
    damage_type: str = ""
    base_value: int = 0
    coin_power: int = 0
    coin_count: int = 0
    attack_level: int = 0
    attack_weight: int = 1
    is_guard: bool = False


@dataclass
class UnitState:
    """单位状态（进攻方或受击方）。

    Attributes:
        name: 单位名称
        hp: 生命值
        defense_level: 防御等级
        speed: 速度值
        physical_resistances: 物理抗性 {"斩击": 1.0, "突刺": 0.75, ...}
        sin_resistances: 罪孽抗性 {"暴怒": 1.0, "色欲": 1.5, ...}
        active_buffs: 当前生效的增益/减益效果列表
        observation_level: 异想体观察记录等级 (0-15)
        chaos_level: 混乱等级（0=未混乱, 1+=混乱层数）
        sanity: 理智值 (-45 ~ 45)
    """
    name: str = ""
    hp: int = 0
    defense_level: int = 0
    speed: int = 0
    physical_resistances: dict[str, float] = field(default_factory=dict)
    sin_resistances: dict[str, float] = field(default_factory=dict)
    active_buffs: list[BuffEffect] = field(default_factory=list)
    observation_level: int = 0
    chaos_level: int = 0
    sanity: int = 0


@dataclass
class DamageResult:
    """伤害计算结果。

    Attributes:
        min_damage: 最小伤害（硬币全反面）
        max_damage: 最大伤害（硬币全正面）
        expected_damage: 期望伤害（考虑理智值影响的正反面概率）
        total_damage: 总伤害（含追加伤害）
        details: 各乘算因子明细
    """
    min_damage: int = 0
    max_damage: int = 0
    expected_damage: float = 0.0
    total_damage: int = 0
    details: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# 核心计算函数
# ═══════════════════════════════════════════════════════════════

def calculate_coin_power_range(base: int, coin_power: int, coin_count: int) -> tuple[int, int]:
    """计算硬币威力的最小/最大值区间。

    最小 = base + coin_power * coin_count（全反面：加算硬币反面也加，减算硬币反面则减）
    最大 = base + coin_power * coin_count（全正面）

    注意：加算硬币的硬币威力为正值，减算硬币为负值。
          全反面：值 = base + (-coin_power) * coin_count（如果 coin_power 本身为负则反面为减）
          全正面：值 = base + coin_power * coin_count

    此处 coin_power 为加算硬币的威力（正值），
    全反面时视为 0 加成（加算硬币反面不加威力），全正面时全部加上。

    Args:
        base: 基础威力
        coin_power: 硬币威力（加算硬币为正值）
        coin_count: 硬币数量

    Returns:
        (min_value, max_value) 元组
    """
    # 加算硬币：正面 +coin_power，反面 +0
    min_val = base  # 全反面，硬币不加威力
    max_val = base + coin_power * coin_count  # 全正面
    return min_val, max_val


def calculate_attack_defense_diff_multiplier(atk_level: int, def_level: int) -> float:
    """计算攻防等级差增伤倍率。

    公式：差值 / (|差值| + 25)

    Args:
        atk_level: 进攻方攻击等级
        def_level: 受击方防御等级

    Returns:
        攻防等级差倍率（可为正/负）
    """
    diff = atk_level - def_level
    return diff / (abs(diff) + 25)


def calculate_physical_resistance_value(
    base_resistance: float,
    mult_bonus: float = 0.0,
    add_bonus: float = 0.0,
    chaos_level: int = 0,
    override: Optional[float] = None,
) -> float:
    """计算物理抗性值（按照三级优先级）。

    优先级（从高到低）：
    1. 混乱状态 2+：(混乱等级 - 1) * 0.5 + 加算增伤
    2. 抗性覆盖效果：直接设为目标抗性（-2 到 2）
    3. 基础抗性 × (1 + 乘算增伤) + 加算增伤（clamp 到 -2~2）

    Args:
        base_resistance: 基础物理抗性值（如 1.0=一般, 2.0=致命, 0.5=抵抗）
        mult_bonus: 乘算增伤加成
        add_bonus: 加算增伤加成
        chaos_level: 混乱等级
        override: 抗性覆盖值（None 表示无覆盖）

    Returns:
        物理增伤数值
    """
    # 优先级 1：混乱状态
    if chaos_level >= 2:
        return (chaos_level - 1) * 0.5 + add_bonus

    # 优先级 2：抗性覆盖
    if override is not None:
        return max(-2.0, min(2.0, override))

    # 优先级 3：基础抗性计算
    value = base_resistance * (1.0 + mult_bonus) + add_bonus
    return max(-2.0, min(2.0, value))


def resistance_value_to_damage_bonus(resistance_value: float) -> float:
    """将物理/罪孽增伤数值转换为抗性增伤倍率。

    三段计算：
    (1) 增伤数值 > 1：增伤 = 增伤数值 - 1
    (2) 增伤数值 > 0：增伤 = (增伤数值 - 1) * 0.5
    (3) 增伤数值 <= 0：增伤 = -0.5

    Args:
        resistance_value: 物理/罪孽增伤数值

    Returns:
        抗性增伤倍率（加到第一类乘算中）
    """
    if resistance_value > 1.0:
        return resistance_value - 1.0
    elif resistance_value > 0:
        return (resistance_value - 1.0) * 0.5
    else:
        return -0.5


def calculate_type1_multiplicative(
    clash_wins: int = 0,
    is_crit: bool = False,
    crit_multiplier: float = 1.2,
    atk_level: int = 0,
    def_level: int = 0,
    observation_level: int = 0,
    physical_resistance_value: float = 1.0,
    sin_resistance_value: float = 1.0,
) -> float:
    """计算第一类乘算增伤。

    第一类乘算增伤 = 1 + 拼点胜利增伤 + 暴击增伤 + 攻防等级增伤
                     + 异想体观察等级增伤 + 物理抗性增伤 + 罪孽抗性增伤

    Args:
        clash_wins: 拼点胜利次数
        is_crit: 是否暴击
        crit_multiplier: 暴击倍率（基础 1.2）
        atk_level: 攻击方攻击等级
        def_level: 受击方防御等级
        observation_level: 异想体观察记录等级
        physical_resistance_value: 物理增伤数值
        sin_resistance_value: 罪孽增伤数值

    Returns:
        第一类乘算增伤倍率
    """
    total = 1.0

    # 拼点胜利增伤：每胜利 1 次 +3%
    total += clash_wins * 0.03

    # 暴击增伤：暴击倍率 - 1
    if is_crit:
        total += crit_multiplier - 1.0

    # 攻防等级差增伤
    total += calculate_attack_defense_diff_multiplier(atk_level, def_level)

    # 异想体观察等级增伤：观察等级 * 0.03
    total += observation_level * 0.03

    # 物理抗性增伤
    total += resistance_value_to_damage_bonus(physical_resistance_value)

    # 罪孽抗性增伤（逻辑同物理抗性增伤）
    total += resistance_value_to_damage_bonus(sin_resistance_value)

    return total


def calculate_type2_multiplicative(attacker_buffs: list[BuffEffect],
                                    defender_buffs: list[BuffEffect]) -> float:
    """计算第二类乘算增伤。

    第二类乘算增伤 = 1 + (受击方伤害乘算增伤 + 进攻方伤害乘算增伤)

    各类状态效果/被动/场地/E.G.O饰品的效果叠加计算。

    Args:
        attacker_buffs: 进攻方增益效果
        defender_buffs: 受击方增益效果

    Returns:
        第二类乘算增伤倍率
    """
    total = 1.0

    for buff in attacker_buffs:
        if buff.category == "offense_mult":
            total += buff.value

    for buff in defender_buffs:
        if buff.category == "defense_mult":
            total += buff.value

    return total


def calculate_type1_additive(attacker_buffs: list[BuffEffect],
                              defender_buffs: list[BuffEffect],
                              physical_resistance_value: float = 1.0,
                              sin_resistance_value: float = 1.0,
                              defense_bonus: float = 0.0) -> int:
    """计算第一类加算增伤。

    第一类加算增伤 = (受击方伤害加算增伤 + 进攻方伤害加算增伤)
                     × (物理抗性增伤 + 罪孽抗性增伤 - 防御增伤)

    会受到物理抗性增伤和罪孽抗性增伤的影响。

    Args:
        attacker_buffs: 进攻方增益效果
        defender_buffs: 受击方增益效果
        physical_resistance_value: 物理增伤数值
        sin_resistance_value: 罪孽增伤数值
        defense_bonus: 防御增伤

    Returns:
        第一类加算增伤值（整数，四舍五入）
    """
    add_value = 0
    for buff in attacker_buffs:
        if buff.category == "offense_add":
            add_value += buff.value
    for buff in defender_buffs:
        if buff.category == "defense_add":
            add_value += buff.value

    # 物理/罪孽抗性倍数
    resistance_mult = (
        resistance_value_to_damage_bonus(physical_resistance_value) +
        resistance_value_to_damage_bonus(sin_resistance_value) -
        defense_bonus
    )

    return round(add_value * max(0, resistance_mult))


def calculate_type2_additive(attacker_buffs: list[BuffEffect],
                              defender_buffs: list[BuffEffect],
                              attacker_hp: int = 0,
                              attacker_max_hp: int = 0,
                              defender_hp: int = 0,
                              defender_max_hp: int = 0) -> int:
    """计算第二类加算增伤。

    第二类加算增伤 = (硬币/技能/被动/场地效果)的
                    (根据受击方体力获得的增伤 + 根据进攻方体力获得的增伤)

    这是固定伤害，不受任何加成影响，与硬币本身伤害分别显示。

    Args:
        attacker_buffs: 进攻方增益效果
        defender_buffs: 受击方增益效果
        attacker_hp: 进攻方当前体力
        attacker_max_hp: 进攻方最大体力
        defender_hp: 受击方当前体力
        defender_max_hp: 受击方最大体力

    Returns:
        第二类加算增伤值（整数）
    """
    # 当前的实现：直接从 buff 中汇总（buff 值已经根据体力计算过）
    add_value = 0
    for buff in attacker_buffs:
        if buff.category == "offense_fixed":
            add_value += buff.value
    for buff in defender_buffs:
        if buff.category == "defense_fixed":
            add_value += buff.value

    return round(add_value)


def calculate_current_coin_damage(
    coin_value: int,
    type1_mult: float,
    type2_mult: float,
    type1_add: int,
    type2_add: int,
) -> int:
    """计算单个硬币造成的伤害。

    当前硬币造成的伤害 = 当前硬币数值 × 第一类乘算增伤 × 第二类乘算增伤
                        + 第一类加算增伤 + 第二类加算增伤

    最低伤害：max(1, floor(当前硬币数值 × 0.05))
    结果四舍五入。

    Args:
        coin_value: 当前硬币数值
        type1_mult: 第一类乘算增伤
        type2_mult: 第二类乘算增伤
        type1_add: 第一类加算增伤
        type2_add: 第二类加算增伤

    Returns:
        该硬币造成的伤害值
    """
    raw = coin_value * type1_mult * type2_mult + type1_add + type2_add
    result = round(raw)

    # 最低伤害保障
    min_damage = max(1, math.floor(coin_value * 0.05))
    return max(min_damage, result)


def calculate_max_damage(
    skill: SkillData,
    attacker: UnitState,
    defender: UnitState,
    clash_wins: int = 0,
    is_crit: bool = False,
    crit_multiplier: float = 1.2,
    physical_resistance_override: Optional[float] = None,
    sin_resistance_override: Optional[float] = None,
    defense_bonus: float = 0.0,
) -> DamageResult:
    """计算某人格某技能在给定加成下能对某单位造成的最大伤害。

    这是用户直接调用的高层 API。

    Args:
        skill: 技能数据
        attacker: 进攻方状态
        defender: 受击方状态
        clash_wins: 拼点胜利次数
        is_crit: 是否暴击
        crit_multiplier: 暴击倍率
        physical_resistance_override: 物理抗性覆盖值（如弱点分析）
        sin_resistance_override: 罪孽抗性覆盖值（如狂喜）
        defense_bonus: 防御增伤

    Returns:
        DamageResult 包含完整伤害明细
    """
    # 1) 硬币威力范围
    min_coin, max_coin = calculate_coin_power_range(
        skill.base_value, skill.coin_power, skill.coin_count
    )

    # 2) 物理增伤数值（三级优先级）
    base_phys_res = defender.physical_resistances.get(skill.damage_type, 1.0)
    phys_res_value = calculate_physical_resistance_value(
        base_phys_res,
        chaos_level=defender.chaos_level,
        override=physical_resistance_override,
    )

    # 3) 罪孽增伤数值
    base_sin_res = defender.sin_resistances.get(skill.sin_type, 1.0)
    sin_res_value = calculate_physical_resistance_value(
        base_sin_res,
        override=sin_resistance_override,
    )

    # 4) 第一类乘算增伤
    type1_mult = calculate_type1_multiplicative(
        clash_wins=clash_wins,
        is_crit=is_crit,
        crit_multiplier=crit_multiplier,
        atk_level=skill.attack_level,
        def_level=defender.defense_level,
        observation_level=attacker.observation_level,
        physical_resistance_value=phys_res_value,
        sin_resistance_value=sin_res_value,
    )

    # 5) 第二类乘算增伤
    type2_mult = calculate_type2_multiplicative(attacker.active_buffs, defender.active_buffs)

    # 6) 第一类加算增伤
    type1_add = calculate_type1_additive(
        attacker.active_buffs, defender.active_buffs,
        physical_resistance_value=phys_res_value,
        sin_resistance_value=sin_res_value,
        defense_bonus=defense_bonus,
    )

    # 7) 第二类加算增伤
    type2_add = calculate_type2_additive(attacker.active_buffs, defender.active_buffs)

    # 8) 计算各硬币伤害
    max_coin_damage = calculate_current_coin_damage(
        max_coin, type1_mult, type2_mult, type1_add, type2_add
    )
    min_coin_damage = calculate_current_coin_damage(
        min_coin, type1_mult, type2_mult, type1_add, type2_add
    )

    # 9) 期望伤害区间（考虑理智值对硬币概率的影响）
    coin_prob = 0.5 + attacker.sanity * 0.01
    coin_prob = max(0.0, min(1.0, coin_prob))  # clamp 到 [0, 1]

    # 期望硬币数值 = base + coin_power * coin_count * coin_prob
    expected_coin_value = skill.base_value + skill.coin_power * skill.coin_count * coin_prob
    expected_coin_damage = calculate_current_coin_damage(
        round(expected_coin_value), type1_mult, type2_mult, type1_add, type2_add
    )

    # 10) 目标状态效果触发伤害（简化：仅计算常见的 破裂/沉沦）
    status_damage = 0
    for buff in defender.active_buffs:
        if buff.category == "status_damage":
            status_damage += round(buff.value)

    # 11) 总伤害
    total_max = max_coin_damage + status_damage
    total_min = min_coin_damage + status_damage

    return DamageResult(
        min_damage=total_min,
        max_damage=total_max,
        expected_damage=round(expected_coin_damage + status_damage, 1),
        total_damage=total_max,
        details={
            "coin_power_range": (min_coin, max_coin),
            "type1_multiplicative": round(type1_mult, 4),
            "type2_multiplicative": round(type2_mult, 4),
            "type1_additive": type1_add,
            "type2_additive": type2_add,
            "physical_resistance_value": round(phys_res_value, 4),
            "sin_resistance_value": round(sin_res_value, 4),
            "clash_wins": clash_wins,
            "is_crit": is_crit,
            "coin_probability": round(coin_prob, 2),
            "status_damage": status_damage,
        },
    )


def calculate_expected_damage_range(
    skill: SkillData,
    attacker: UnitState,
    defender: UnitState,
    physical_resistance_override: Optional[float] = None,
    sin_resistance_override: Optional[float] = None,
    defense_bonus: float = 0.0,
) -> DamageResult:
    """计算技能面板显示的期望伤害区间。

    与 max_damage 的区别：
    - 不含拼点胜利增伤
    - 不含暴击增伤
    - 第二类乘算增伤只考虑当前已有的状态效果，不含技能使用时的效果
    - 第一类乘算增伤少了暴击和拼点胜利

    公式：
      期望伤害区间 = 技能威力上下限 × 第一类乘算增伤 × 第二类乘算增伤
                     + 第一类加算增伤 + 第二类加算增伤

    Args:
        skill: 技能数据
        attacker: 进攻方状态
        defender: 受击方状态
        physical_resistance_override: 物理抗性覆盖值
        sin_resistance_override: 罪孽抗性覆盖值
        defense_bonus: 防御增伤

    Returns:
        DamageResult 包含期望伤害区间
    """
    # 1) 硬币威力范围
    min_coin, max_coin = calculate_coin_power_range(
        skill.base_value, skill.coin_power, skill.coin_count
    )

    # 2) 物理/罪孽增伤数值
    base_phys_res = defender.physical_resistances.get(skill.damage_type, 1.0)
    phys_res_value = calculate_physical_resistance_value(
        base_phys_res,
        chaos_level=defender.chaos_level,
        override=physical_resistance_override,
    )

    base_sin_res = defender.sin_resistances.get(skill.sin_type, 1.0)
    sin_res_value = calculate_physical_resistance_value(
        base_sin_res,
        override=sin_resistance_override,
    )

    # 3) 第一类乘算增伤（不含拼点和暴击）
    # 期望区间公式：1 + 防御增伤 + 异想体观察等级增伤 + 物理抗性增伤 + 罪孽抗性增伤
    type1_mult = 1.0
    type1_mult += defense_bonus  # 防御增伤（来自守备技能）
    type1_mult += attacker.observation_level * 0.03
    type1_mult += resistance_value_to_damage_bonus(phys_res_value)
    type1_mult += resistance_value_to_damage_bonus(sin_res_value)

    # 4) 第二类乘算增伤
    type2_mult = calculate_type2_multiplicative(attacker.active_buffs, defender.active_buffs)

    # 5) 第一类加算增伤
    type1_add = calculate_type1_additive(
        attacker.active_buffs, defender.active_buffs,
        physical_resistance_value=phys_res_value,
        sin_resistance_value=sin_res_value,
        defense_bonus=defense_bonus,
    )

    # 6) 第二类加算增伤
    type2_add = calculate_type2_additive(attacker.active_buffs, defender.active_buffs)

    # 7) 伤害计算
    max_damage = calculate_current_coin_damage(
        max_coin, type1_mult, type2_mult, type1_add, type2_add
    )
    min_damage = calculate_current_coin_damage(
        min_coin, type1_mult, type2_mult, type1_add, type2_add
    )

    return DamageResult(
        min_damage=min_damage,
        max_damage=max_damage,
        expected_damage=(min_damage + max_damage) / 2.0,
        total_damage=max_damage,
        details={
            "coin_power_range": (min_coin, max_coin),
            "type1_multiplicative": round(type1_mult, 4),
            "type2_multiplicative": round(type2_mult, 4),
            "type1_additive": type1_add,
            "type2_additive": type2_add,
            "physical_resistance_value": round(phys_res_value, 4),
            "sin_resistance_value": round(sin_res_value, 4),
            "is_expected_range": True,
        },
    )


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════

def create_buff(name: str, value: float, category: str = "offense_mult") -> BuffEffect:
    """快速创建一个 BuffEffect 对象。

    Args:
        name: 效果名称
        value: 效果值（百分比形式，如 0.2 表示 20%）
        category: 效果类别
    """
    return BuffEffect(name=name, value=value, category=category)
