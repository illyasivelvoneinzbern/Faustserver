# -*- coding: utf-8 -*-
"""台词库（Persona Corpus）：拟真人格方案的数据底座。

从两处**真实素材**构建「角色 → 台词」索引：

1. 剧情台词：`data/raw/wiki_pages.jsonl` 中 story_dialogue / story_note / event 页面的
   对话块（约 4.5 万条，覆盖主线/活动/间章/联动），带章节、前后句、互动对象元数据；
2. 语音台词：`data/structured/personas/persona_*.json` 的 voice_lines
   （每位罪人约 20 条，覆盖问候/对话/战斗/胜负等场景），带场景标题。

作用（检索式人格增强 RAP）：回复时用「用户消息 + 近期对话」检索该角色最相关的
**真实台词**作为【说话样本】注入，让 LLM 模仿角色的真实语气/句式/用词，
而不是仅靠角色卡（YAML）的抽象设定——这是"拟真"与"角色卡"的本质区别。

设计约束：零额外 API 成本、零网络调用、进程内懒加载缓存（与 StoryFactIndex 同款模式）。
检索打分为纯词面算法（jieba 分词 + 实体/意图/场景加权），单次检索 < 5ms。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WIKI_JSONL = Path("data/raw/wiki_pages.jsonl")
PERSONAS_DIR = Path("data/structured/personas")

# role 裸名提取：去组织前缀（" - " 分隔的末段）与括号阶段后缀
_ROLE_SPLIT_RE = re.compile(r"\s*-\s*")
_STAGE_RE = re.compile(r"[（(].*?[)）]")

# 语音标题里的场景词（用于场景匹配）
_GREETING_HINTS = ("问候", "获得", "自我介绍", "对话")
_BATTLE_HINTS = ("战斗", "攻击", "胜利", "失败", "击杀", "混乱", "出战", "判定", "阵亡")

# 观点/评价意图加权（复用剧情事实模块的态度词表）
_ATTITUDE_WORDS = (
    "喜欢", "讨厌", "认为", "觉得", "必须", "应该", "当然", "享受", "厌恶",
    "有趣", "无聊", "值得", "令人", "希望", "害怕", "高兴", "放心", "满意",
    "不满", "同意", "反对", "无论如何", "绝不", "务必", "最好", "何必",
    "感激", "遗憾", "可惜", "可悲", "愚蠢", "明智", "正确", "错误",
)


def bare_role(role: str) -> str:
    """'食指父辈 - 里恩（第一阶段）' → '里恩'；'浮士德' → '浮士德'。"""
    r = (role or "").strip()
    if not r:
        return ""
    if " - " in r:
        r = r.split(" - ")[-1].strip()
    r = _STAGE_RE.sub("", r).strip()
    return r


@dataclass
class PersonaLine:
    """一条角色真实台词及其上下文元数据。"""

    text: str
    source: str                # "voice" | "story"
    scene: str                 # 场景标题（语音）或章节名（剧情）
    role: str = ""             # 剧情完整 role（如「食指父辈 - 里恩」）
    prev_text: str = ""        # 剧情中上一句（帮助理解语境）
    next_text: str = ""        # 剧情中下一句
    weight: float = 1.0        # 来源权重（语音略高，因是官方定稿台词）

    def sample_fmt(self) -> str:
        """注入 Prompt 时的展示格式（带场景，帮助 LLM 理解使用语境）。"""
        head = f"（{self.scene}）" if self.scene else ""
        return f"{head}{self.text}"


def _tokenize(text: str) -> list[str]:
    """中文分词（jieba；缺失时降级为 2-gram 字符片）。"""
    try:
        import jieba
        return [t for t in jieba.cut(text) if len(t.strip()) > 1]
    except Exception:
        t = re.sub(r"\s+", "", text)
        return [t[i:i + 2] for i in range(0, len(t) - 1)]


class PersonaCorpus:
    """角色台词库：懒加载 + 进程内缓存。

    用法:
        corpus = PersonaCorpus()
        lines = corpus.retrieve_lines("浮士德", "你怎么看希斯克利夫？", top_k=3)
    """

    _by_name: Optional[dict[str, list[PersonaLine]]] = None
    _stats: Optional[dict] = None

    # ── 构建 ──────────────────────────────────────────────────────────

    @classmethod
    def _ensure(cls):
        if cls._by_name is not None:
            return
        by_name: dict[str, list[PersonaLine]] = {}
        cls._load_story_lines(by_name)
        cls._load_voice_lines(by_name)
        cls._by_name = by_name
        total = sum(len(v) for v in by_name.values())
        logger.info(
            f"台词库构建完成: {len(by_name)} 个角色，共 {total} 条台词"
            f"（剧情 {cls._stats.get('story', 0)} / 语音 {cls._stats.get('voice', 0)}）"
        )

    @classmethod
    def _load_story_lines(cls, by_name: dict[str, list[PersonaLine]]):
        """从 wiki_pages.jsonl 剧情对话块构建（含前后句语境）。"""
        if not WIKI_JSONL.exists():
            logger.warning(f"剧情数据不存在: {WIKI_JSONL}，台词库仅含语音")
            return
        count = 0
        for line in open(WIKI_JSONL, encoding="utf-8"):
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
            # 预处理：收集该页全部对话块（保留顺序，供前后句）
            seq: list[tuple[str, str]] = []
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "dialogue":
                    continue
                role = (b.get("role") or "").strip()
                text = (b.get("text") or "").strip()
                if not role or not text:
                    continue
                seq.append((role, text))
            for i, (role, text) in enumerate(seq):
                br = bare_role(role)
                if not br:
                    continue
                rec = PersonaLine(
                    text=text,
                    source="story",
                    scene=title,
                    role=role,
                    prev_text=seq[i - 1][1] if i > 0 else "",
                    next_text=seq[i + 1][1] if i + 1 < len(seq) else "",
                    weight=1.0,
                )
                by_name.setdefault(br, []).append(rec)
                count += 1
        cls._stats = {"story": count, "voice": 0}
        logger.info(f"剧情台词载入: {count} 条")

    @classmethod
    def _load_voice_lines(cls, by_name: dict[str, list[PersonaLine]]):
        """从结构化人格 JSON 的 voice_lines 构建（官方语音台词）。"""
        if not PERSONAS_DIR.exists():
            return
        count = 0
        for f in PERSONAS_DIR.glob("persona_*.json"):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            name = (d.get("sinner") or "").strip()
            if not name:
                continue
            for v in (d.get("voice_lines") or []):
                text = (v.get("text") or "").strip()
                # 清理提取残留（"||" 前缀等）
                text = re.sub(r"^\s*[\|｜]+\s*", "", text).strip()
                if not text:
                    continue
                rec = PersonaLine(
                    text=text,
                    source="voice",
                    scene=(v.get("title") or "").strip(),
                    weight=1.5,  # 官方定稿台词，权重更高
                )
                by_name.setdefault(name, []).append(rec)
                count += 1
        if cls._stats:
            cls._stats["voice"] = count
        logger.info(f"语音台词载入: {count} 条")

    # ── 查询 ──────────────────────────────────────────────────────────

    @classmethod
    def get_lines(cls, name: str, source: str = "") -> list[PersonaLine]:
        """返回某角色的全部台词（可按 source 过滤）。"""
        cls._ensure()
        name = (name or "").strip()
        if not name:
            return []
        # 精确名 → 包含匹配（去空格）
        lines = cls._by_name.get(name, [])
        if not lines:
            en = name.replace(" ", "").replace("·", "")
            for k, v in cls._by_name.items():
                if en and (en in k.replace(" ", "").replace("·", "") or k in en):
                    lines.extend(v)
        if not source:
            return lines
        return [ln for ln in lines if ln.source == source]

    @classmethod
    def retrieve_lines(
        cls,
        name: str,
        query: str = "",
        top_k: int = 3,
        intent: str = "other",
        min_len: int = 6,
        max_len: int = 140,
    ) -> list[PersonaLine]:
        """检索该角色与当前语境最相关的真实台词。

        Args:
            name: 角色名（裸名，如"浮士德"）
            query: 检索查询（用户消息 + 近期对话拼接）
            top_k: 最多返回条数
            intent: 意图（"opinion" 等），影响打分加权
            min_len / max_len: 台词长度过滤（过滤碎片与长段落）
        """
        lines = cls.get_lines(name)
        if not lines:
            return []
        q_tokens = _tokenize(query or "")
        scored: list[tuple[float, PersonaLine]] = []
        seen: set[str] = set()
        for ln in lines:
            t = ln.text.strip()
            if len(t) < min_len or len(t) > max_len:
                continue
            if t in seen:
                continue
            seen.add(t)
            s = cls._score_line(ln, q_tokens, query, intent)
            if s <= 0:
                continue
            scored.append((s, ln))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ln for _s, ln in scored[:top_k]]

    @staticmethod
    def _score_line(ln: PersonaLine, q_tokens: list[str], query: str, intent: str) -> float:
        """台词与语境的词面相关度打分（0 分基准，越高越相关）。"""
        t = ln.text
        score = 0.0

        # 1) 查询词命中（>1 字的词出现在台词中；越长权重越高）
        hit = 0
        for tok in q_tokens:
            if len(tok) >= 3 and tok in t:
                hit += 1.3
            elif len(tok) == 2 and tok in t:
                hit += 1.0
        if hit == 0:
            return 0.0
        score += hit

        # 2) 意图加权
        if intent == "opinion":
            att = sum(1 for w in _ATTITUDE_WORDS if w in t)
            score += min(att * 0.6, 1.2)      # 有态度/评价的台词更利于发表看法
        # 3) 场景匹配（问候/战斗类问题优先命中对应语音）
        if any(g in query for g in ("你好", "早上", "晚上", "早安", "晚安", "问候", "自我介绍", "你是谁")):
            if any(g in ln.scene for g in _GREETING_HINTS):
                score += 2.0
        if any(g in query for g in ("战斗", "打", "技能", "敌人", "胜利", "失败", "作战", "出击")):
            if any(g in ln.scene for g in _BATTLE_HINTS):
                score += 1.6

        # 4) 信息量：过短/过长的台词扣分
        if len(t) < 10:
            score -= 0.8
        elif len(t) > 100:
            score -= 0.5

        # 5) 来源权重（语音官方定稿优先）
        score *= ln.weight
        return round(score, 2)

    # ── 统计 ──────────────────────────────────────────────────────────

    @classmethod
    def stats(cls) -> dict:
        """构建统计（供 scripts/build_persona_corpus.py 使用）。"""
        cls._ensure()
        per_char = {k: len(v) for k, v in cls._by_name.items()}
        return {
            "characters": len(per_char),
            "total_lines": sum(per_char.values()),
            "story_lines": cls._stats.get("story", 0),
            "voice_lines": cls._stats.get("voice", 0),
            "per_character": dict(sorted(per_char.items(), key=lambda x: -x[1])),
        }
