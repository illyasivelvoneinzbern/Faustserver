# -*- coding: utf-8 -*-
"""拟真人格生成引擎（Persona Engine）：检索式人格增强 + 角色心理建模 + 一致性自检。

为什么比"角色卡"更拟真（详见 plans/persona_realism_training.md）：
- 角色卡（YAML → System Prompt）只告诉模型"角色是谁、性格如何"，
  模型仍然用自己惯用的"助手腔"组织语言 → 破格、套话、不自然；
- 本引擎在生成时注入该角色**真实台词**（剧情/语音）作为【说话样本】，
  并让模型先以角色视角写下【内心反应】再出口成句（Thinking in Character），
  使回复在语气、句式、用词、情绪反应上都"像这个角色"。

模式（persona_training.mode）：
- "off"      不启用（默认，行为与现状完全一致，零开销）
- "samples"  仅注入角色真实台词样本（RAP，单次调用，延迟几乎不变）
- "thinking" samples + 内心反应建模（单次调用内完成，内心反应段被剥离后发送）

一致性自检（persona_training.consistency）：
- "off"    不检查
- "rules"  规则检测（破格句式/括号神态/AI 口吻/第三人称自称偏差等）+ 轻量修复
- "llm"    rules 之上追加一次 LLM 判定，必要时重写一次（更准，多一次调用）
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 破格检测规则 ────────────────────────────────────────────────────────
# 1) 括号神态/动作舞台指示（P32 已有程序清洗，这里双保险）
_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,30}[)）]")
# 2) 助手腔 / 越界声明
_AI_PATTERNS = (
    "作为AI", "作为人工智能", "作为语言模型", "作为一个AI", "我是AI", "我是人工智能",
    "我无法", "我不能回答", "我不能提供", "根据我的知识库", "知识截止",
    "请注意", "希望这能帮到你", "如果你有任何问题", "很高兴为你服务",
    "在边狱巴士的世界观中", "从设定上看", "在游戏设定中", "根据设定",
)
# 3) 通用口头禅破格：万能回复腔
_GENERIC_FILLER = (
    "有什么可以帮你的吗", "请问还有什么问题", "需要我帮忙吗",
    "这就是我的回答", "总之", "综上所述",
)
# 4) 超长回复（设定回复上限，超出视为失控）
_MAX_LEN = 500


def strip_inner_reaction(text: str) -> str:
    """剥离【内心反应】段（Thinking in Character 的推理痕迹），只留正式台词。

    兼容多种输出格式：
    - 【内心反应】xxx\n正式台词
    - 内心反应：xxx\n正式台词
    - （内心）xxx\n正式台词
    若未命中格式或剥离后为空，原样返回。
    """
    t = (text or "").strip()
    if not t:
        return t
    # 分行处理：首行若为内心反应标记 → 删除该行
    lines = t.split("\n")
    first = lines[0].strip()
    reacted = (
        first.startswith("【内心反应】")
        or first.startswith("内心反应")
        or first.startswith("【内心独白】")
        or first.startswith("（内心）")
        or first.startswith("（内心戏）")
    )
    if reacted and len(lines) > 1:
        rest = "\n".join(lines[1:]).strip()
        if rest:
            return rest
    return t


def rules_consistency_check(persona: dict, reply: str) -> list[str]:
    """规则化破格检测：返回问题列表（空 = 未破格）。

    覆盖：
    1. 括号神态/动作舞台指示
    2. AI/助手腔（"作为AI""我无法回答"等）
    3. 万能客服腔（"有什么可以帮你的吗"）
    4. 超长失控
    5. 第三人称自称角色用"我"（如浮士德自称"浮士德"）
    """
    issues: list[str] = []
    t = (reply or "").strip()
    if not t:
        return issues

    # 1) 括号神态
    if _STAGE_DIRECTION_RE.search(t):
        issues.append("含括号神态/动作舞台指示")

    # 2) AI 腔
    for pat in _AI_PATTERNS:
        if pat in t:
            issues.append(f"AI/助手腔：{pat}")
            break

    # 3) 客服腔
    for pat in _GENERIC_FILLER:
        if pat in t:
            issues.append(f"万能客服腔：{pat}")
            break

    # 4) 超长
    if len(t) > _MAX_LEN:
        issues.append(f"回复超长（{len(t)}字）")

    # 5) 第三人称自称角色（speech_style 含"第三人称"）误用"我"作为自称
    styles = " ".join(persona.get("speech_style", []) or [])
    name = persona.get("name", "")
    if name and ("第三人称" in styles or f"以「{name}」自称" in styles or "以{name}自称" in styles):
        # 粗略检测：以"我"开头或频繁使用"我觉得/我认为"（排除引用他人的"我"）
        if re.match(r"^我[觉得认为想希望]", t) or t.count("我觉得") + t.count("我认为") >= 2:
            issues.append(f"{name} 应以第三人称自称，出现第一人称自我表达")
    return issues


def quick_repair(reply: str, issues: list[str]) -> str:
    """对规则检测出的问题做确定性轻量修复（不新增 LLM 调用）。

    仅处理可安全机器修正的项：括号神态删除、客服腔句删除。
    AI 腔/自称问题交给 LLM 判定（rules 模式仅记录日志）。
    """
    t = reply
    if any("括号神态" in i for i in issues):
        t = _STAGE_DIRECTION_RE.sub("", t).strip()
    if any("万能客服腔" in i for i in issues):
        for pat in _GENERIC_FILLER:
            # 删除"总之""综上所述"等开头连接词
            t = re.sub(rf"^({pat})[，。！？\s]*", "", t)
    return t


class PersonaEngine:
    """拟真生成引擎：为 RAG Chain 提供台词样本注入 + 内心反应 + 一致性后处理。

    Chain 集成（见 rag/chain.py）：
    - `samples_block()` 生成【说话样本】注入 Human Prompt；
    - `thinking_instruction()` 生成内心反应指令注入 System Prompt；
    - `postprocess()` 在 run_rag_query 之后调用（剥离内心反应 + 一致性自检）。
    """

    def __init__(
        self,
        corpus: Any,
        persona_manager: Any = None,
        llm: Any = None,
        mode: str = "off",
        max_samples: int = 3,
        consistency: str = "off",
        intent_fn=None,
    ):
        self.corpus = corpus
        self.persona_manager = persona_manager
        self.llm = llm
        self.mode = mode if mode in ("off", "samples", "thinking") else "off"
        self.max_samples = max(0, min(int(max_samples or 0), 6))
        self.consistency = consistency if consistency in ("off", "rules", "llm") else "off"
        # 意图分类函数（默认用 rag.intent_gate.classify_user_intent，可注入替身便于测试）
        self._intent_fn = intent_fn

    # ── 状态 ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and self.corpus is not None

    @property
    def thinking(self) -> bool:
        return self.mode == "thinking"

    # ── 注入物构建（供 Chain 使用，纯同步零网络） ─────────────────────

    def _classify_intent(self, question: str) -> str:
        try:
            fn = self._intent_fn
            if fn is None:
                from rag.intent_gate import classify_user_intent
                fn = classify_user_intent
            return fn(question) if fn else "other"
        except Exception:
            return "other"

    def samples_block(self, persona_name: str, question: str, chat_history: str = "") -> str:
        """检索该角色真实台词，格式化为【说话样本】Prompt 区块。"""
        if not self.enabled or not persona_name:
            return ""
        if chat_history and "无对话历史" not in chat_history:
            query = f"{chat_history}\n{question}"
        else:
            query = question
        intent = self._classify_intent(question)
        lines = self.corpus.retrieve_lines(
            persona_name, query=query, top_k=self.max_samples, intent=intent
        )
        if not lines:
            return ""
        parts = [ln.sample_fmt() for ln in lines]
        return "【说话样本（以下均为" + persona_name + "在原著中的真实台词，模仿其语气与用词，但不要照抄）】\n" + "\n".join(parts)

    def thinking_instruction(self, display_name: str) -> str:
        """内心反应建模指令（Thinking in Character，单次调用实现）。"""
        if not self.thinking:
            return ""
        return (
            "【先想后说】输出正式台词之前，先输出一行【内心反应】："
            f"以{display_name}的视角写出此刻的感受、态度与想说的话（仅内部参考，不发送给用户）。"
            "随后另起一行输出正式台词。正式台词必须以角色口吻直接说出，不写括号神态。"
        )

    # ── 后处理（run_rag_query 之后调用） ───────────────────────────────

    async def postprocess_by_id(
        self, persona_id: str, reply: str, question: str = ""
    ) -> str:
        """按人格 ID 解析 persona 后执行后处理（Chain 集成入口）。"""
        persona = None
        if self.persona_manager and persona_id:
            try:
                persona = self.persona_manager.get(persona_id)
            except Exception:
                persona = None
        return await self.postprocess(persona, reply, question)

    def postprocess_sync(
        self, persona: Optional[dict], reply: str, question: str = ""
    ) -> str:
        """同步后处理（剥离内心反应 + 规则自检；LLM 判定仅在异步路径）。"""
        if not self.enabled:
            return reply
        t = strip_inner_reaction(reply) if self.thinking else (reply or "")
        if self.consistency == "off":
            return t
        persona = persona or {}
        issues = rules_consistency_check(persona, t)
        if not issues:
            return t
        repaired = quick_repair(t, issues)
        if repaired != t:
            logger.info(f"[拟真自检] 规则修复: {issues}")
            t = repaired
        return t

    async def postprocess(
        self, persona: Optional[dict], reply: str, question: str = ""
    ) -> str:
        """剥离内心反应 → 规则自检 →（可选）LLM 判定/重写。"""
        if not self.enabled:
            return reply
        t = strip_inner_reaction(reply) if self.thinking else (reply or "")

        if self.consistency == "off":
            return t

        persona = persona or {}
        issues = rules_consistency_check(persona, t)
        if not issues:
            return t

        # 规则修复（仅安全项）
        repaired = quick_repair(t, issues)
        if repaired != t:
            logger.info(f"[拟真自检] 规则修复: {issues} → {repaired[:40]}…")
            t = repaired

        # LLM 判定：仅在 rules 修复后仍存疑且配置允许时调用
        if self.consistency == "llm" and self.llm is not None:
            name = persona.get("display_name") or persona.get("name") or "角色"
            fixed = await self._llm_repair(name, persona, t, question)
            if fixed and fixed != t:
                logger.info("[拟真自检] LLM 修正破格回复")
                t = fixed
        return t

    async def _llm_repair(
        self, name: str, persona: dict, reply: str, question: str
    ) -> str:
        """LLM 判定 + 必要时重写（一次额外调用）。"""
        traits = "，".join(persona.get("traits", []) or [])
        styles = "，".join(persona.get("speech_style", []) or [])
        prompt = (
            f"你是{name}，{persona.get('identity', '')}。性格：{traits}。口吻：{styles}。\n"
            f"面对用户的话「{question[:120]}」，下面的回复出现了破格：\n"
            f"【回复】{reply[:400]}\n"
            "请判断：若回复不符合角色性格/口吻，直接给出符合该角色的一版重写（只说台词本身）；"
            "若基本符合，仅原样输出回复内容，不要加任何解释。"
        )
        try:
            resp = await self.llm.ainvoke(prompt)
            out = (resp.content or "").strip()
            return out if out else reply
        except Exception as e:
            logger.warning(f"[拟真自检] LLM 判定失败: {e}")
            return reply

    # ── 直连生成（供 scripts/eval_persona.py 评测用，不依赖 RAG Chain） ──

    def build_enhanced_prompt(
        self,
        persona: dict,
        question: str,
        chat_history: str = "",
        context: str = "",
    ) -> str:
        """构造带说话样本 + 内心反应指令的完整 Prompt（评测/独立使用）。"""
        from personas.manager import PersonaManager  # 仅用编译逻辑

        # 复用 PersonaManager 的 System Prompt 编译（不重复实现）
        pm = self.persona_manager or PersonaManager()
        if self.persona_manager is None:
            pm.load_all()
        system = pm.build_system_prompt(persona.get("id", ""))
        if self.thinking:
            system += "\n" + self.thinking_instruction(persona.get("display_name", persona.get("name", "")))

        samples = self.samples_block(
            persona.get("name", ""), question, chat_history
        )
        samples_block = f"{samples}\n\n" if samples else ""

        ctx = f"【参考资料】\n{context}\n\n" if context else ""
        history = f"【对话历史】\n{chat_history}\n\n" if chat_history else ""
        return (
            f"{system}\n\n"
            f"{samples_block}"
            f"{ctx}"
            f"{history}"
            f"用户：{question}\n"
            f"{persona.get('display_name', persona.get('name', ''))}："
        )
