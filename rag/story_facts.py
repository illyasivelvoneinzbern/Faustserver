# -*- coding: utf-8 -*-
"""剧情事实索引（P29b）：为 opinion 观点类问题提供剧情依据。

背景：
- 观点类问题（如「你怎么看里恩？」）应**结合剧情**发表看法，
  而不是注入单位数值（HP/抗性/技能）——那是"怎么打/弱点"类
  明确游戏意图才需要的数据（走直答/数据链路）。
- 本模块从已有剧情数据（wiki_pages.jsonl 的 story_dialogue/story_note/event
  blocks）构建「角色 → 剧情台词」索引（懒加载 + 内存缓存，不重爬），
  供 opinion 链路注入【背景事实（剧情）】，LLM 基于角色在剧情中的
  表现与言行发表看法，避免编造身份（曾把食指父辈里恩说成 N 公司异端审判官）。

角色匹配：剧情对话的 role 常带组织前缀（如「食指父辈 - 里恩」），
索引 key 同时登记完整 role 与裸名，匹配时用裸名/去空格包含。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WIKI_JSONL = "data/raw/wiki_pages.jsonl"

# role 裸名提取：去组织前缀（" - " 分隔的末段）与括号阶段后缀
_ROLE_SPLIT_RE = re.compile(r"\s*-\s*")
_STAGE_RE = re.compile(r"[（(].*?[)）]")


def _bare_role(role: str) -> str:
    """'食指父辈 - 里恩（第一阶段）' → '里恩'；'浮士德' → '浮士德'。"""
    r = (role or "").strip()
    if not r:
        return ""
    if " - " in r:
        r = r.split(" - ")[-1].strip()
    r = _STAGE_RE.sub("", r).strip()
    return r


# ═══════════════════════════════════════════════════════════════════════
# 剧情台词打分（P30）：利于观点表达的台词加分，不利的减分。
# ═══════════════════════════════════════════════════════════════════════
# 观点表达所需的台词特征：
#   + 态度/评价词（喜欢/讨厌/认为/必须/应该/当然/享受/厌恶/有趣/无聊…）
#   + 主观判断/情绪（我/我们/希望/害怕/高兴/值得/令人…）
#   + 人物关系线索（提及/称呼他人、与 focus_role 的互动）
#   + 足够的信息量（较长、有实质内容）
# 不利特征：
#   - 纯事实陈述（"是/就是/这是"且无态度词）
#   - 过短碎片、纯省略/沉默
_ATTITUDE_WORDS = (
    "喜欢", "讨厌", "认为", "觉得", "必须", "应该", "当然", "享受", "厌恶",
    "有趣", "无聊", "值得", "令人", "希望", "害怕", "高兴", "放心", "满意",
    "不满", "同意", "反对", "无论如何", "绝不", "务必", "最好", "何必",
    "感激", "遗憾", "可惜", "可悲", "愚蠢", "明智", "正确", "错误",
)
_RELATION_WORDS = ("经理", "但丁", "罪人", "同事", "助手", "部长", "父亲", "母亲", "女儿", "儿子", "兄弟", "姐妹", "朋友")
# 纯事实陈述标记（无态度词时的减分项）
_FACT_MARKERS = ("是", "就是", "这是", "那是", "指的是", "意味着")
_SILENCE_RE = re.compile(r"^[。.…\s]+$")


def score_story_line(text: str, focus_role: str = "") -> float:
    """对单条剧情台词打分（0 分基准，-2 ~ +3 区间）。

    加分：态度/评价词、人物关系线索、与 focus_role 互动、信息量。
    减分：纯事实陈述（无态度词）、过短碎片、纯省略。
    """
    t = (text or "").strip()
    if not t:
        return -2.0
    if _SILENCE_RE.match(t):
        return -2.0
    score = 0.0

    # 态度/评价词（每词 +0.5，封顶 +1.5）
    att = sum(1 for w in _ATTITUDE_WORDS if w in t)
    score += min(att * 0.5, 1.5)

    # 人物关系线索（提及他人/关系称谓，+0.4）
    rel = sum(1 for w in _RELATION_WORDS if w in t)
    score += min(rel * 0.4, 0.8)

    # 第一人称主观（我/我们/想/希望 +0.2，封顶 0.4）
    for w in ("我", "我们", "想", "希望", "不愿", "不想"):
        if w in t:
            score += 0.2
            break

    # 与 focus_role 相关（互动台词利于观点表达 +0.8）
    if focus_role and focus_role in t:
        score += 0.8

    # 信息量：较长且有实质内容
    if len(t) >= 40:
        score += 0.3
    elif len(t) >= 15:
        score += 0.15

    # 减分：纯事实陈述（无任何态度/关系/主观词时）
    if att == 0 and rel == 0 and "我" not in t and any(m in t for m in _FACT_MARKERS):
        score -= 0.3

    # 过短碎片
    if len(t) < 8:
        score -= 0.5

    return round(score, 2)


class StoryFactIndex:
    """剧情角色台词索引（懒加载，进程内缓存）。"""

    _index: Optional[dict[str, list[tuple[str, str]]]] = None  # 裸名 → [(章节, 台词)]
    _full_index: Optional[dict[str, list[tuple[str, str]]]] = None  # 完整 role → [(章节, 台词)]
    _pages: Optional[dict[str, list[tuple[str, str]]]] = None  # 章节 → [(role, 台词)]（对话序列）

    @classmethod
    def _ensure(cls):
        if cls._index is not None:
            return
        bare: dict[str, list[tuple[str, str]]] = {}
        full: dict[str, list[tuple[str, str]]] = {}
        pages: dict[str, list[tuple[str, str]]] = {}
        path = Path(WIKI_JSONL)
        if not path.exists():
            logger.error(f"剧情数据不存在: {path}")
            cls._index, cls._full_index, cls._pages = {}, {}, {}
            return
        count = 0
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("page_type") not in ("story_dialogue", "story_note", "event"):
                continue
            blocks = d.get("blocks") or []
            if not blocks:
                continue
            title = d.get("title") or ""
            seq = pages.setdefault(title, [])
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "dialogue":
                    continue
                role = (b.get("role") or "").strip()
                text = (b.get("text") or "").strip()
                if not role or not text:
                    continue
                seq.append((role, text))
                full.setdefault(role, []).append((title, text))
                br = _bare_role(role)
                if br and br != role:
                    bare.setdefault(br, []).append((title, text))
                count += 1
        cls._index, cls._full_index, cls._pages = bare, full, pages
        logger.info(f"剧情角色台词索引构建完成: 完整role {len(full)} 个，裸名 {len(bare)} 个，页面 {len(pages)} 个，共 {count} 条台词")

    @classmethod
    def _match_roles(cls, entity: str) -> list[str]:
        """返回与实体名匹配的 role（完整 role 优先，再裸名）。"""
        cls._ensure()
        entity = (entity or "").strip()
        if not entity:
            return []
        # 精确 role / 裸名
        hits = [r for r in cls._full_index if r == entity]
        bare_hits = [r for r in cls._index if r == entity]
        # 包含匹配（去空格）
        en = entity.replace(" ", "").replace("·", "")
        if not hits:
            hits = [r for r in cls._full_index if en and en in r.replace(" ", "").replace("·", "")]
        if not bare_hits:
            bare_hits = [r for r in cls._index if en and (en in r or r in en)]
        return hits or bare_hits

    @classmethod
    def get_story_lines(
        cls, entity: str, max_lines: int = 5, focus_role: str = "", min_score: float = 0.0
    ) -> list[tuple[str, str]]:
        """返回该角色的剧情台词样本（章节, 台词），最多 max_lines 条。

        P30：**打分排序**——用 score_story_line 对每句台词打分
        （利于观点表达的加分、不利的减分），按分数降序取前 max_lines 条
        （每节选分高的对话输出）。过滤无意义台词（过短/纯标点）。

        Args:
            entity: 实体名（裸名）
            max_lines: 最多返回条数
            focus_role: 关注角色（如当前扮演罪人），影响打分
            min_score: 低于该分数的台词不输出（默认 0，可放宽为负值）
        """
        roles = cls._match_roles(entity)
        if not roles:
            return []
        scored: list[tuple[float, str, str]] = []  # (score, title, text)
        seen: set[tuple] = set()
        for r in roles:
            for title, text in cls._full_index.get(r, []):
                t = text.strip()
                if len(t) < 4 or not re.search(r"[\u4e00-\u9fffA-Za-z]", t):
                    continue
                key = (title, t)
                if key in seen:
                    continue
                seen.add(key)
                score = score_story_line(t, focus_role=focus_role)
                if score < min_score:
                    continue
                scored.append((score, title, t))
        # 按分数降序（同分保持剧情顺序）
        title_order = {t: i for i, t in enumerate(cls._pages)}
        scored.sort(key=lambda x: (-x[0], title_order.get(x[1], 0)))
        return [(title, text) for _s, title, text in scored[:max_lines]]

    @classmethod
    def get_interactions(cls, entity: str, focus_role: str, max_pairs: int = 3) -> list[tuple[str, str, str, str]]:
        """提取被问角色与另一角色（如当前扮演罪人）的剧情互动。

        在页面对话序列中，找 role 匹配 entity 的对话块，收集同页相邻
        （前 2 后 2 块）中 role 匹配 focus_role 的台词，形成交锋片段。
        **每个页面最多取 1 组最佳互动**（相邻优先 + 台词去重），
        再按剧情顺序取前 max_pairs 页——保证不同章节（如战前/战后）的
        互动都能覆盖（日志实证：7.5-04 战前的"纠正自述"与战后的"助手交锋"
        都应体现）。

        Returns:
            [(章节, entity台词, focus角色台词, focus台词)]，最多 max_pairs 组。
        """
        cls._ensure()
        if not entity or not focus_role:
            return []
        ent_roles = cls._match_roles(entity)
        foc_roles = cls._match_roles(focus_role)
        if not ent_roles or not foc_roles:
            return []
        ent_set, foc_set = set(ent_roles), set(foc_roles)
        # 每页最佳互动：{title: (dist, etext, ftext, frole)}
        per_page: dict[str, tuple] = {}
        for title, seq in cls._pages.items():
            best: Optional[tuple] = None
            for i, (role, text) in enumerate(seq):
                if role not in ent_set:
                    continue
                if len(text.strip()) < 4:
                    continue
                for j in range(max(0, i - 2), min(len(seq), i + 3)):
                    if j == i:
                        continue
                    frole, ftext = seq[j]
                    if frole not in foc_set:
                        continue
                    if len(ftext.strip()) < 4:
                        continue
                    cand = (abs(i - j), text.strip(), ftext.strip(), frole)
                    if (
                        best is None
                        or cand[0] < best[0]
                        # 同距离优先取更长（信息量更大）的台词
                        or (cand[0] == best[0] and len(cand[1]) > len(best[1]))
                    ):
                        best = cand
            if best is not None:
                per_page[title] = best
        if not per_page:
            return []
        out: list[tuple[str, str, str, str]] = []
        seen: set[tuple] = set()
        for title in cls._pages:
            if title not in per_page:
                continue
            _dist, etext, ftext, frole = per_page[title]
            key = (title, etext, ftext)
            if key in seen:
                continue
            seen.add(key)
            out.append((title, etext, ftext, frole))
            if len(out) >= max_pairs:
                break
        return out

    @classmethod
    def has_entity(cls, entity: str) -> bool:
        return bool(cls._match_roles(entity))


def build_story_fact_base(
    entity_name: str,
    identity_note: str = "",
    max_lines: int = 5,
    focus_role: str = "",
) -> str:
    """构建剧情事实底座文本（P29b/P29d）。

    Args:
        entity_name: 实体名（裸名，如"里恩"）
        identity_note: 身份备注（如"食指的父辈"），可为空
        max_lines: 最多台词条数
        focus_role: 关注角色（如当前扮演罪人"浮士德"）。非空时额外注入
            【人物互动】——被问角色与 focus_role 在剧情中的对话交锋，
            让 LLM 发表看法时符合原著人物关系
            （日志实证："你怎么看霍恩海姆"忽略了浮士德与霍恩海姆互相看不上的关系）。

    Returns:
        剧情事实文本；无任何剧情台词且无身份时返回空串。
    """
    lines = StoryFactIndex.get_story_lines(
        entity_name, max_lines=max_lines, focus_role=focus_role
    )
    if not lines and not identity_note:
        return ""
    parts: list[str] = []
    if identity_note:
        parts.append(f"- 身份：{identity_note}")
    if lines:
        parts.append(f"- 剧情登场（{entity_name}）：")
        for title, text in lines:
            snippet = text if len(text) <= 70 else text[:70] + "…"
            parts.append(f"  ·（{title}）{snippet}")
    if focus_role:
        interactions = StoryFactIndex.get_interactions(entity_name, focus_role, max_pairs=3)
        if interactions:
            parts.append(f"- 与{focus_role}的剧情互动：")
            for title, etext, ftext, frole in interactions:
                parts.append(f"  ·（{title}）{entity_name}：{etext[:50]}")
                parts.append(f"    {frole}：{ftext[:50]}")
    return "\n".join(parts)


def entity_has_story(entity_name: str) -> bool:
    """实体是否在剧情中有台词（供调用方判断是否值得注入）。"""
    return StoryFactIndex.has_entity(entity_name)
