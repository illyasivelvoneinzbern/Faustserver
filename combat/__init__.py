"""
战斗模块：硬编码伤害计算，供 Agent Tools 调用。

不依赖 LLM，所有公式直接写在代码中，保证精确性。
"""

from combat.damage_calculator import (
    BuffEffect,
    SkillData,
    UnitState,
    DamageResult,
    create_buff,
    calculate_coin_power_range,
    calculate_physical_resistance_value,
    calculate_type1_multiplicative,
    calculate_type2_multiplicative,
    calculate_type1_additive,
    calculate_type2_additive,
    calculate_current_coin_damage,
    calculate_max_damage,
    calculate_expected_damage_range,
    resistance_value_to_damage_bonus,
    calculate_attack_defense_diff_multiplier,
)

__all__ = [
    "BuffEffect",
    "SkillData",
    "UnitState",
    "DamageResult",
    "create_buff",
    "calculate_coin_power_range",
    "calculate_physical_resistance_value",
    "calculate_type1_multiplicative",
    "calculate_type2_multiplicative",
    "calculate_type1_additive",
    "calculate_type2_additive",
    "calculate_current_coin_damage",
    "calculate_max_damage",
    "calculate_expected_damage_range",
    "resistance_value_to_damage_bonus",
    "calculate_attack_defense_diff_multiplier",
]
