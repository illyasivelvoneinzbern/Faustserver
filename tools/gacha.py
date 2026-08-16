# -*- coding: utf-8 -*-
"""抽奖（Gacha）核心模块：三灯/二灯/一灯人格 + E.G.O 概率抽取。

概率（用户配置，2026-08-15）：
    三灯人格  3%
    二灯人格 13%
    一灯人格 81%
    EGO       3%

数据：
    data/gacha/rarity.json    人格稀有度分类（1灯/2灯/3灯，由 scripts/build_gacha_data.py 生成）
    data/gacha/ego_pool.json  独立 E.G.O 池（wiki page_type=ego 页面）

API：
    GachaPool.pull()         单抽 → {kind, label, name, desc?}
    GachaPool.pull_n(n)      n 连抽 → list[dict]
    顶层便捷函数 gacha_pull(times) / 与 MCP/StructuredTool 共用
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RARITY_PATH = "data/gacha/rarity.json"
DEFAULT_EGO_POOL_PATH = "data/gacha/ego_pool.json"

# 抽取权重（顺序无关，随机数落在哪个区间即哪个类别）
PULL_WEIGHTS: dict[str, float] = {
    "one_star": 0.81,    # 一灯
    "two_star": 0.13,    # 二灯
    "three_star": 0.03,  # 三灯
    "ego": 0.03,         # EGO
}

# 灯级显示标签
_LABELS = {
    "one_star": "一灯人格",
    "two_star": "二灯人格",
    "three_star": "三灯人格",
    "ego": "E.G.O",
}


class GachaPool:
    """抽奖池：加载稀有度数据 + E.G.O 池，按权重抽取。"""

    def __init__(
        self,
        rarity_path: str = DEFAULT_RARITY_PATH,
        ego_pool_path: str = DEFAULT_EGO_POOL_PATH,
        seed: Optional[int] = None,
    ):
        self._rng = random.Random(seed)
        self.rarity = self._load_json(rarity_path)
        self.ego_items = self._load_json(ego_pool_path).get("items", [])
        self._pools: dict[str, list[str]] = {
            "one_star": list(self.rarity.get("one_star") or []),
            "two_star": list(self.rarity.get("two_star") or []),
            "three_star": list(self.rarity.get("three_star") or []),
        }
        # 校验
        for k, items in self._pools.items():
            if not items:
                logger.warning(f"抽奖池 {k} 为空！请先运行 scripts/build_gacha_data.py")
        if not self.ego_items:
            logger.warning("EGO 池为空！请先运行 scripts/build_gacha_data.py")

    @staticmethod
    def _load_json(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            logger.error(f"数据文件不存在: {p}（请先运行 scripts/build_gacha_data.py）")
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"数据文件解析失败 {p}: {e}")
            return {}

    def _pick_category(self) -> str:
        """按权重抽取类别。"""
        r = self._rng.random()
        acc = 0.0
        for kind, w in PULL_WEIGHTS.items():
            acc += w
            if r < acc:
                return kind
        return "one_star"  # 兜底（浮点误差）

    def _pick_from(self, kind: str) -> dict:
        """类别内均匀随机选一个条目。"""
        if kind == "ego":
            if not self.ego_items:
                return {"kind": "ego", "label": _LABELS["ego"], "name": "（EGO 池为空）", "desc": ""}
            item = self._rng.choice(self.ego_items)
            return {
                "kind": "ego",
                "label": _LABELS["ego"],
                "name": item.get("name") or "",
                "desc": item.get("desc") or "",
            }
        pool = self._pools.get(kind) or []
        if not pool:
            return {"kind": kind, "label": _LABELS.get(kind, kind), "name": "（池为空）", "desc": ""}
        name = self._rng.choice(pool)
        return {
            "kind": kind,
            "label": _LABELS.get(kind, kind),
            "name": name,
            "desc": "",
        }

    def pull(self) -> dict:
        """单抽。"""
        kind = self._pick_category()
        return self._pick_from(kind)

    def pull_n(self, n: int) -> list[dict]:
        """n 连抽（n 次独立抽取）。

        十连保底（用户确认，2026-08-15）：n >= 10 时，若结果中没有
        「二灯或三灯人格」，则将最后一抽替换为随机二灯人格，
        保证十连至少有一个二灯人格。其余抽取概率不变。
        """
        n = max(1, min(int(n or 1), 100))  # 防滥用上限 100
        results = [self.pull() for _ in range(n)]
        if n >= 10:
            has_two_or_above = any(
                r.get("kind") in ("two_star", "three_star") for r in results
            )
            if not has_two_or_above:
                # 保底：替换最后一抽为二灯人格（EGO/一灯不满足保底）
                results[-1] = self._pick_from("two_star")
                logger.info("十连保底触发：无二灯及以上人格，末抽替换为二灯")
        return results

    # ── 展示格式（用户确认：仅名称 + 灯级）──
    @staticmethod
    def format_result(result: dict) -> str:
        """单抽结果 → 文本（如『三灯人格 · 浮士德黑兽-卯魁首』）。"""
        label = result.get("label") or ""
        name = result.get("name") or ""
        return f"{label} · {name}"

    @staticmethod
    def format_results(results: list[dict]) -> str:
        """多次抽取结果 → 多行文本（含统计摘要）。"""
        lines = []
        counts: dict[str, int] = {}
        for r in results:
            kind = r.get("kind") or ""
            counts[kind] = counts.get(kind, 0) + 1
            lines.append(GachaPool.format_result(r))
        if len(results) > 1:
            summary = "，".join(
                f"{_LABELS.get(k, k)}×{c}" for k, c in sorted(counts.items(), key=lambda x: -x[1])
            )
            lines.append(f"（共 {len(results)} 抽：{summary}）")
        return "\n".join(lines)


# ── 进程级单例（避免重复加载数据文件）──
_pool: Optional[GachaPool] = None


def get_pool() -> GachaPool:
    global _pool
    if _pool is None:
        _pool = GachaPool()
    return _pool


def gacha_pull(times: int = 1) -> str:
    """便捷函数：抽奖并返回展示文本（供 StructuredTool / MCP / 指令预拦截共用）。

    Args:
        times: 抽取次数，1=单抽，10=十连（1~100）
    """
    try:
        results = get_pool().pull_n(times)
        return GachaPool.format_results(results)
    except Exception as e:
        logger.error(f"抽奖异常: {e}")
        return "（抽奖系统暂时无法使用，请稍后再试。）"


if __name__ == "__main__":
    # 自测：python -m tools.gacha
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(gacha_pull(n))
