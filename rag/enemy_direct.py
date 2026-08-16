# -*- coding: utf-8 -*-
"""
敌方单位结构化直答模块（Enemy Direct Answer 运行时）。

绕开向量检索，直接从 ``data/structured/enemies/enemy_*.json`` 精确取数，
按确定性规范格式输出（不经过 LLM、无幻觉）。用于根治
"询问敌方单位（如雷横）不返回数据"问题——此前敌方名查询被意图规则
误路由到 character page_type 过滤，敌方 chunk（page_type=enemy）被直接排除。

组成：
- ``extract_enemy_name``  从查询中剥离噪词得到候选关键词
- ``EnemyDirectStore``    运行时索引（懒加载 data/structured/enemies 目录）
- ``format_enemy_full``   确定性格式化（关卡 / 部位 / HP / 防御 / 速度 / 混乱阈值 /
                          物理与罪孽抗性 / 恐慌类型 / 被动 / 技能与硬币效果）
- ``try_direct_answer``   查询入口：命中具体敌方名 → 直答文本；否则 None（回落 RAG）

匹配策略：以敌方名对原始查询做包含匹配（``name in query``），可命中
"雷横的技能" "雷横弱点" "穿着整齐的拇指士兵" 等多样问法；多个候选时
优先完全相等，否则回落 RAG 避免歧义。

依赖：``rag.query_processor`` 的 ``classify_intent``（is_listing 检测）。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from crawler.structured_exporter import DEFAULT_OUT_DIR, load_enemy_index
from rag.query_processor import classify_intent

logger = logging.getLogger(__name__)

# 敌方名提取：从查询中剥离的噪声词（问句词 / 意图词，不属于敌方名）
# 与 event_direct/gift_direct 的噪声词对齐。
_ENEMY_NOISE_RE = re.compile(
    "技能|弱点|属性|数据|介绍|展示|查询|说说|讲讲|看下|给我|看看|怎么打|打法"
    "|是什么|有哪些|有什么|多少|怎么样|在哪儿|在哪|哪里|吗|呢|的|？|\\?"
)


def extract_enemy_name(query: str) -> Optional[str]:
    """从查询中剥离噪声词得到候选关键词（用于精确 key 匹配辅助）。

    注意：敌方名包含匹配的主路径在 ``try_direct_answer`` 中直接对原始查询
    进行（``name in query``），本函数仅在需要时提供剥噪后的候选。

    Returns:
        剥噪后的候选关键词；无法提取返回 None。
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None
    cleaned = _ENEMY_NOISE_RE.sub("", q).strip()
    return cleaned or None


def _format_resistance(prefix: str, resistances) -> list[str]:
    """渲染抗性表（物理：斩/突/打；罪孽：七罪孽）。"""
    if not isinstance(resistances, dict) or not resistances:
        return []
    parts = [f"{k}:{v}" for k, v in resistances.items() if v is not None]
    if not parts:
        return []
    return [f"{prefix}{'  '.join(parts)}"]


def _format_skill(skill: dict, idx: int) -> list[str]:
    """渲染单个技能：名称 / 罪孽 / 伤害类型 / 硬币 / 攻击等级 / 硬币效果。"""
    if not isinstance(skill, dict):
        return []
    lines: list[str] = []
    name = (skill.get("skill_name") or "").strip()
    if not name:
        return []
    head_parts = []
    sin = (skill.get("sin_type") or "").strip()
    if sin:
        head_parts.append(sin)
    dtype = (skill.get("damage_type") or "").strip()
    if dtype:
        head_parts.append(dtype)
    guard = (skill.get("guard_type") or "").strip()
    if skill.get("is_guard"):
        head_parts.append(f"守备（{guard or '防御'}）")

    try:
        coin_power = int(skill.get("coin_power") or 0)
    except (TypeError, ValueError):
        coin_power = 0
    try:
        coin_count = int(skill.get("coin_count") or 0)
    except (TypeError, ValueError):
        coin_count = 0
    atk = skill.get("attack_level")
    tail = f"{coin_count}×{coin_power}"
    if atk:
        tail += f"（攻击等级{atk}）"
    # 修复 P21-A：显示基础值（>0 时显示，避免刷屏 0）
    try:
        base_value = int(skill.get("base_value") or 0)
    except (TypeError, ValueError):
        base_value = 0
    if base_value > 0:
        tail += f"（基础值{base_value}）"
    # 修复③：显示重要性（>0 时为特殊技能）
    try:
        importance = int(skill.get("importance") or 0)
    except (TypeError, ValueError):
        importance = 0
    if importance > 0:
        tail += f"（重要性{importance}）"
    if head_parts:
        lines.append(f"{idx}. {name}【{'/'.join(head_parts)}】{tail}")
    else:
        lines.append(f"{idx}. {name} {tail}")

    for ce in skill.get("coin_effects") or []:
        ce_text = str(ce).strip()
        if ce_text:
            lines.append(f"   · {ce_text}")
    return lines


def format_enemy_full(records: list[dict]) -> str:
    """确定性格式化：将一个敌方单位（可含多个部位记录）输出为规范纯文本。

    输出字段：敌方名 / 来源关卡 / 部位 / HP / 防御 / 速度 / 混乱阈值 /
    物理抗性 / 罪孽抗性 / 恐慌类型（去重）/ 被动 / 技能与硬币效果。

    Args:
        records: 同一 enemy_name 的 1..n 条记录（多部位时逐部位输出）。

    Returns:
        规范化纯文本；输入为空返回空串。
    """
    valid = [r for r in records if isinstance(r, dict)]
    if not valid:
        return ""
    first = valid[0]
    name = first.get("enemy_name") or first.get("title") or "敌方单位"

    lines: list[str] = []
    for rec in valid:
        body = (rec.get("body_part") or "").strip()
        if len(valid) > 1:
            lines.append(f"【{name}·{body}】" if body else f"【{name}】")
        else:
            lines.append(f"【{name}】")

        meta_parts = []
        stage = (rec.get("battle_stage") or "").strip()
        if stage:
            meta_parts.append(f"关卡：{stage}")
        # 修复②：跨关卡合并单位展示全部出现关卡
        appear_stages = rec.get("appear_stages") or []
        if len(appear_stages) > 1:
            meta_parts.append(f"出现关卡：{'、'.join(str(s) for s in appear_stages)}")
        if body and len(valid) == 1:
            meta_parts.append(f"部位：{body}")
        hp = rec.get("hp")
        if hp not in (None, "", 0):
            meta_parts.append(f"HP：{hp}")
        defense = rec.get("defense_level") or rec.get("defense")
        if defense not in (None, ""):
            meta_parts.append(f"防御等级：{defense}")
        speed = rec.get("speed") or (
            f"{rec.get('speed_min')}~{rec.get('speed_max')}"
            if rec.get("speed_min") is not None and rec.get("speed_max") is not None
            else ""
        )
        if speed:
            meta_parts.append(f"速度：{speed}")
        chaos = (rec.get("chaos_threshold") or "").strip()
        if chaos and chaos != "0":
            meta_parts.append(f"混乱阈值：{chaos}")
        if meta_parts:
            lines.append("　" + "　".join(meta_parts))

        lines.extend(_format_resistance("　物理抗性：", rec.get("physical_resistances")))
        lines.extend(_format_resistance("　罪孽抗性：", rec.get("sin_resistances")))

        panics = rec.get("panic_types") or []
        seen = []
        for p in panics:
            p_text = str(p).strip()
            if p_text and p_text not in seen:
                seen.append(p_text)
        if seen:
            lines.append(f"　恐慌：{'、'.join(seen)}")

    # 被动（合并全部部位，去重）
    passive_lines: list[str] = []
    for rec in valid:
        for p in rec.get("passives") or []:
            p_text = str(p).strip()
            if p_text and p_text not in passive_lines:
                passive_lines.append(p_text)
    if passive_lines:
        lines.append("【被动】")
        for p in passive_lines:
            lines.append(f"　· {p}")

    # 技能（合并全部部位，去重；编号全局递增）
    skill_lines: list[str] = []
    skill_idx = 0
    for rec in valid:
        for sk in rec.get("skills") or []:
            skill_idx += 1
            rendered = _format_skill(sk, skill_idx)
            if rendered and rendered[0] not in skill_lines:
                skill_lines.append(rendered[0])
                skill_lines.extend(rendered[1:])
    if skill_lines:
        lines.append("【技能】")
        lines.extend(skill_lines)

    return "\n".join(lines)


class EnemyDirectStore:
    """运行时结构化敌方单位索引（懒加载 data/structured/enemies 目录）。

    用法（agent/core.py）：
        self.enemy_direct = EnemyDirectStore(
            data_dir=cfg.get("data_dir", "data/structured"),
            enabled=cfg.get("enabled", True),
        )
        direct = self.enemy_direct.try_direct_answer(msg.text)
    """

    def __init__(self, data_dir: str = DEFAULT_OUT_DIR, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self._enemy_index: Optional[dict[str, dict]] = None
        self._name_index: Optional[dict[str, list[dict]]] = None

    def _ensure_index(self) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        """懒加载：扫描目录建立 {enemy_id: record} 与 {enemy_name: [records]} 索引。"""
        if self._enemy_index is None:
            self._enemy_index = load_enemy_index(self.data_dir)
            name_index: dict[str, list[dict]] = {}
            for rec in self._enemy_index.values():
                n = (rec.get("enemy_name") or "").strip()
                if n:
                    name_index.setdefault(n, []).append(rec)
            self._name_index = name_index
            if not self._enemy_index:
                logger.warning(
                    f"结构化敌方单位目录为空（{self.data_dir}），直答将自动失效并回落 RAG"
                )
        return self._enemy_index, self._name_index

    def reload(self):
        """重载索引（爬虫重建 data/structured 后调用）。"""
        self._enemy_index = None
        self._name_index = None
        self._ensure_index()

    def has_enemy(self, name: str) -> bool:
        _, name_index = self._ensure_index()
        return name in name_index

    def get_enemy(self, name: str) -> list[dict]:
        _, name_index = self._ensure_index()
        return name_index.get(name, [])

    def search(self, name_like: str) -> list[str]:
        """包含模糊匹配（用于提示，非精确路径）。"""
        _, name_index = self._ensure_index()
        hits = [n for n in name_index if name_like in n or n in name_like]
        return sorted(hits)

    def resolve_enemy(self, query: str) -> Optional[str]:
        """从查询解析出真实敌方名（P29，供观点事实底座使用）。

        使用 query_processor._detect_enemy_name 做权威解析（双向包含 +
        去空格规范化 + 裸名/别名匹配），若返回裸名/别名（如"里恩"），
        再通过索引反查唯一真实名（如"食指 父辈 - 里恩（第一阶段）"）。

        Returns:
            真实敌方名；未命中或歧义返回 None。
        """
        if not query:
            return None
        try:
            from rag.query_processor import _detect_enemy_name
            name = _detect_enemy_name(query)
        except Exception as e:
            logger.debug(f"resolve_enemy 解析异常: {e}")
            return None
        if not name:
            return None
        _, name_index = self._ensure_index()
        if name in name_index:
            return name
        hits = [n for n in name_index if name in n or n in name]
        if len(hits) == 1:
            return hits[0]
        if hits:
            # 多候选消歧（P29）：优先非"折射"前缀（主版本），再优先第一阶段
            plain = [h for h in hits if not h.startswith("折射")]
            if len(plain) == 1:
                return plain[0]
            stage1 = [h for h in (plain or hits) if "（第一阶段）" in h]
            if len(stage1) == 1:
                return stage1[0]
            return (plain or hits)[0]
        return None

    # ── P23：反向匹配（q in name）泛化词黑名单 ──
    # 防止 "第一阶段"、"父辈" 等过短/泛化片段被当作敌方名命中，
    # 导致直答返回无关敌人（此前 "食指父辈 - 里恩（第一阶段）数据" 被误判为
    # "第一阶段"）。与 query_processor._detect_enemy_name 的 _GENERIC_SEGMENTS 对齐。
    _GENERIC_SEGMENTS = {
        "第一阶段", "第二阶段", "第三阶段", "第四阶段", "第五阶段",
        "父辈", "长辈", "子辈", "士兵", "工人", "清扫人员",
    }

    def _norm(self, s: str) -> str:
        """去除空格/间隔号（P23 跨空白差异匹配）。"""
        return (s or "").replace(" ", "").replace("·", "")

    def _bare(self, n: str) -> str:
        """剥离组织前缀与括号内阶段后缀，得到纯裸名。"""
        bare = n.split(" - ")[-1].strip()
        return re.sub(r"[（(].*?[)）]", "", bare).strip()

    def try_direct_answer(self, query: str) -> "str | list[str] | None":
        """直答入口。

        1. 非启用 / 空查询 → None
        2. 穷举/列表查询（如"有哪些敌人"）→ None（避免误触发，回落 RAG 列表检索）
        3. 遍历索引敌方名，命中候选集合（支持双向包含 + 去空格规范化匹配）
        4. 单候选 → 直答全部部位记录；多候选 → 取完全相等，否则返回**候选名列表**
           （P23：不再静默回落 RAG 导致"未收录"；列表由 agent/core.py 统一
           存会话待确认——用户回复数字编号即确定性作答，根治"回复数字被
           误当饰品名查询"（如"1"命中"1B型八角螺栓"））
        5. 未命中 → None（回落 RAG）
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        # 穷举/列表查询不直答（"有哪些敌人" 等泛指）
        try:
            intent = classify_intent(q)
            if intent.get("is_listing"):
                logger.debug(f"敌方直答跳过（列表查询）: {q[:30]}")
                return None
        except Exception as e:
            logger.warning(f"classify_intent 异常，继续直答尝试: {e}")

        _, name_index = self._ensure_index()
        if not name_index:
            logger.debug(f"敌方索引为空，回落 RAG: {q[:30]}")
            return None

        # ── 第一遍：双向包含匹配（保留 P21-A 原逻辑）──
        #   name in q  → "雷横的技能" 命中 "雷横"
        #   q in name  → 裸名 "卡利斯托" 命中 "环指 父辈 - 卡利斯托"
        # q 过短（<2 字符）不做反向匹配，避免 "理"/"守" 等单字误命中大量记录。
        # P23：泛化词正向命中（name in q 命中 "第一阶段" 等）放入 generic_candidates
        # 兜底而非 candidates，防止其阻塞后续更具体的去空格/裸名匹配。
        candidates: list[str] = []
        generic_candidates: list[str] = []
        for name in name_index:
            if not name:
                continue
            if name in q:
                if name in self._GENERIC_SEGMENTS:
                    generic_candidates.append(name)
                else:
                    candidates.append(name)
            elif len(q) >= 2 and q in name and q not in self._GENERIC_SEGMENTS:
                candidates.append(name)

        # ── 第二遍：去空格规范化匹配（P23）──
        # 查询去空格后与敌方名去空格做双向包含，解决
        # "食指父辈里恩数据" vs "食指 父辈 - 里恩（第一阶段）" 的空白差异。
        if not candidates:
            q_no = self._norm(q)
            if len(q_no) >= 2:
                for name in name_index:
                    if not name:
                        continue
                    n_no = self._norm(name)
                    if not n_no:
                        continue
                    if n_no in q_no:
                        if name in self._GENERIC_SEGMENTS:
                            generic_candidates.append(name)
                        else:
                            candidates.append(name)
                    elif q_no in n_no and q_no not in self._GENERIC_SEGMENTS:
                        candidates.append(name)

        # ── 第三遍：裸名匹配（P23）──
        # 剥离组织前缀/阶段后缀后，用纯裸名（如 "里恩"）匹配查询，
        # 解决 "食指父辈里恩数据"/"里恩数据" 未命中 "食指 父辈 - 里恩（第一阶段）"。
        # 同裸名多个敌人（多阶段/折射轨道）→ 交由下方多候选清单处理。
        if not candidates:
            q_no = self._norm(q)
            if len(q_no) >= 2:
                for name in name_index:
                    if not name:
                        continue
                    bare = self._bare(name)
                    bare_no = self._norm(bare)
                    if not bare_no or len(bare_no) < 2:
                        continue
                    if bare_no in self._GENERIC_SEGMENTS:
                        continue
                    if bare_no in q_no:
                        candidates.append(name)

        # ── 第四遍：剥噪关键词精确命中（如查询正好等于敌方名）──
        if not candidates:
            key = extract_enemy_name(q)
            if key and key in name_index:
                candidates = [key]

        if len(candidates) == 1:
            logger.info(f"敌方直答命中（唯一候选）: {candidates[0]}")
            return format_enemy_full(name_index[candidates[0]])

        if len(candidates) > 1:
            # 多候选：优先与剥噪 key 完全相等（避免"雷横"与"雷横·XX"并存歧义），
            exact = [c for c in candidates if c == q]
            if exact:
                logger.info(f"敌方直答命中（完全相等）: {q} → {exact[0]}")
                return format_enemy_full(name_index[exact[0]])
            # P23：多候选不再静默回落 RAG（否则会被 chain 硬短路为"未收录"），
            # 改为返回候选名列表，由 agent/core.py 统一"存会话待确认"——
            # 用户回复数字编号 → 确定性作答；根治"回复数字被误当饰品名查询"
            # （如"1"命中"1B型八角螺栓"）。
            logger.info(f"敌方直答多候选（{len(candidates)}），返回候选列表: {candidates}")
            return candidates

        logger.debug(f"敌方直答未命中，回落 RAG: {q[:30]}")
        return None

    # ── 比较直答（改进计划 P1：compare 意图）──

    def try_compare_answer(self, query: str) -> Optional[str]:
        """比较型直答：解析查询中的两个敌方单位并并排输出关键数据。

        查询格式：含「和/与/vs/对比/比较/跟」连接的两侧各为一个敌方名
        （如「雷横和拇指士兵谁更强」）。任一测未识别或索引缺失 → None。
        """
        if not self.enabled:
            return None
        q = (query or "").strip()
        if not q:
            return None

        parts = re.split(r"(?:和|与|vs|VS|对比|比较|跟|、)", q, maxsplit=1)
        if len(parts) < 2:
            return None

        _, name_index = self._ensure_index()
        if not name_index:
            return None

        def _resolve(name: str) -> Optional[str]:
            """按敌方名索引精确/双向匹配解析。"""
            n = name.strip()
            if not n:
                return None
            if n in name_index:
                return n
            hits = [k for k in name_index if n in k or (len(n) >= 2 and k in n)]
            if len(hits) == 1:
                return hits[0]
            return None

        left = _resolve(parts[0])
        right = _resolve(parts[1])
        if not left or not right or left == right:
            logger.debug(f"敌方比较直答跳过（未识别双单位）: {q[:30]}")
            return None

        logger.info(f"敌方比较直答命中: {left} ↔ {right}")
        return format_enemy_compare(name_index[left], name_index[right])


def format_enemy_compare(recs_a: list[dict], recs_b: list[dict]) -> str:
    """并排输出两个敌方单位的关键数据（HP/防御/速度/抗性）。"""
    def _brief(recs: list[dict]) -> dict:
        if not recs:
            return {}
        r = recs[0]
        return {
            "name": r.get("enemy_name") or r.get("name") or "?",
            "hp": r.get("hp"), "defense": r.get("defense_level"),
            "speed": r.get("speed"), "chaos": r.get("chaos_threshold"),
            "pr": r.get("physical_resistances") or {},
            "sr": r.get("sin_resistances") or {},
        }

    a, b = _brief(recs_a), _brief(recs_b)
    if not a or not b:
        return "（比较数据不完整）"
    lines = [f"【单位比较】{a['name']}  vs  {b['name']}"]
    lines.append(f"HP：{a.get('hp', '—')}  vs  {b.get('hp', '—')}")
    lines.append(f"防御等级：{a.get('defense', '—')}  vs  {b.get('defense', '—')}")
    lines.append(f"速度：{a.get('speed', '—')}  vs  {b.get('speed', '—')}")
    lines.append(f"混乱阈值：{a.get('chaos', '—')}  vs  {b.get('chaos', '—')}")
    lines.append("物理抗性：")
    for k in ("斩击", "突刺", "打击"):
        lines.append(f"  {k}：{a['pr'].get(k, '—')}  vs  {b['pr'].get(k, '—')}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 独立验证入口：python -m rag.enemy_direct 雷横
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = EnemyDirectStore()
    q = sys.argv[1] if len(sys.argv) > 1 else "雷横"
    out = store.try_direct_answer(q)
    if out:
        print(out)
    else:
        print("(未命中直答，应回落 RAG)")
