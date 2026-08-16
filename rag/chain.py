"""
RAG Chain 组装模块：使用 LangChain LCEL 构建检索增强生成链。
支持运行时动态人格切换、分层 System Prompt、结构化输出模板。
"""
import logging
import re
from typing import Any, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

logger = logging.getLogger(__name__)


def build_rag_chain(
    llm: Any,
    retriever: Any,
    persona_manager: Any = None,
    default_persona_id: str = "",
):
    """
    构建 RAG Chain（支持运行时动态人格切换）。

    Args:
        llm: LangChain LLM 实例
        retriever: LimBusRetriever 实例
        persona_manager: PersonaManager 实例（可选）
        default_persona_id: 默认人格 ID（当请求未指定时使用）
    """

    # ── 辅助：根据 persona_id 获取角色名用于检索过滤 ──
    def _get_persona_name(persona_id: str) -> Optional[str]:
        if persona_manager and persona_id:
            persona = persona_manager.get(persona_id)
            if persona:
                return persona.get("name", "")
        return None

    # 防编造充分性门控：当检索上下文不足（空/过短）且问题涉及游戏数据时，
    # 注入明确的「未收录」指令，强制 LLM 拒绝编造（通用机制，不针对具体查询）。
    # P21-E：扩充词表覆盖更多数据意图词 + 已知敌方/人格名（卡利斯托/瓦伦希娜/马蒂亚斯/里恩/无我良秀等），
    # 确保「问具体数据但检索未命中」时走硬短路而非让 LLM 自由发挥编造。
    _DATA_INTENT_RE = re.compile(
        r"(技能|数据|数值|抗性|被动|效果|硬币|基础值|变动值|攻击容量|"
        r"资源|血量|hp|防御|速度|关卡|敌人|BOSS|人格|EGO|饰品|事件|属性|"
        r"斩杀|威力|拼点|命中|容量|等级|奖励|掉落|"
        r"守备|攻击等级|消耗理智|混乱阈值|恐慌|濒死|罪孽|伤害|抗性表|"
        r"卡利斯托|瓦伦希娜|马蒂亚斯|里恩|无我良秀|雷横)",
        re.IGNORECASE,
    )

    def _retrieve_and_format(inputs: dict) -> str:
        persona_id = inputs.get("persona_id", default_persona_id)
        persona_name = _get_persona_name(persona_id)
        retriever.persona_name = persona_name
        # P29c：opinion（观点）意图只检索剧情来源，避免把敌方单位数据
        # （HP/被动/抗性）拉进上下文淹没剧情事实（日志实证："你怎么看霍恩海姆"
        # 混入被动/战术数据）。明确游戏意图（怎么打/弱点）不走此路径。
        story_only = bool(inputs.get("story_only", False))
        filter_dict = {"page_type": "story_dialogue"} if story_only else None
        docs = retriever.retrieve(
            inputs["question"],
            persona_name=persona_name,
            filter_dict=filter_dict,
        )
        context = retriever.format_context(docs)

        question = inputs.get("question", "") or ""
        if _DATA_INTENT_RE.search(question):
            # 估算上下文信息量：非空文本 + 至少包含一个明确标题块
            est = len(context.strip())
            has_block = context.strip().startswith("[") or "[" in context[:200]
            if est < 40 or not has_block:
                return (
                    "（检索未命中：知识库中没有与该查询相关的数据。）\n\n"
                    "【强制规则】参考资料为空或不足以回答问题。"
                    "若用户询问的是《边狱巴士》中的具体数据（技能/数值/抗性/被动/效果等），"
                    "你必须直接如实回答「该数据未收录在资料中」，"
                    "绝不允许编造任何数值、技能名、效果或抗性。"
                )
        return context

    # ── 动态 Prompt 构建（运行时按 persona_id 编译 system template） ──
    def _build_messages(inputs: dict) -> list:
        persona_id = inputs.get("persona_id", default_persona_id)
        system_tmpl = _get_system_template(persona_manager, persona_id)

        # P29：背景事实底座（opinion/compare 意图注入，修复"观点编造身份"）
        # 观点类问题（如"你怎么看里恩"）绕过直答后，LLM 若拿不到实体事实，
        # 会凭印象编造身份（曾把食指父辈里恩说成 N 公司异端审判官）。
        # 此处把命中的敌方/人格事实摘要注入【背景事实】区块，LLM 基于事实发表看法。
        fact_base = (inputs.get("fact_base") or "").strip()
        fact_block = f"【背景事实（关于被问对象的已知数据，引用时不得歪曲）】\n{fact_base}\n\n" if fact_base else ""

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_tmpl),
            ("human",
             "【对话历史】\n{chat_history}\n\n"
             + fact_block +
             "【参考资料】\n{context}\n\n"
             "用户：{question}"),
        ])
        return prompt.format_prompt(
            chat_history=inputs.get("chat_history", "（无对话历史）"),
            context=inputs.get("context", ""),
            question=inputs.get("question", ""),
        ).to_messages()

    # ── LCEL Chain ──
    chain = (
        {
            "context": _retrieve_and_format,
            "question": lambda x: x["question"],
            "chat_history": lambda x: x.get("chat_history", "（无对话历史）"),
            "persona_id": lambda x: x.get("persona_id", default_persona_id),
            "fact_base": lambda x: x.get("fact_base", ""),
            "story_only": lambda x: x.get("story_only", False),
        }
        | RunnableLambda(_build_messages)
        | llm
        | StrOutputParser()
    )

    return chain


# ═══════════════════════════════════════════════════════════════════════
# System Prompt 分层构建
# ═══════════════════════════════════════════════════════════════════════

def _get_system_template(persona_manager, persona_id: str) -> str:
    """
    构建分层 System Prompt：

    Layer 1 — 角色人格（来自 PersonaManager，含 few-shot 示例 + 看法规则）
    Layer 2 — 知识检索规则
    Layer 3 — 人格/EGO 结构化输出模板
    Layer 4 — 剧情/角色看法规则（P28，目标 3：人格化观点）
    Layer 5 — 安全约束
    """
    persona_block = _build_persona_block(persona_manager, persona_id)
    rag_rules = _build_rag_rules()
    output_templates = _build_output_templates()
    opinion_rules = _build_opinion_rules()
    safety_rules = _build_safety_rules()

    return (
        f"{persona_block}\n\n{rag_rules}\n\n{output_templates}\n\n"
        f"{opinion_rules}\n\n{safety_rules}"
    )


def _build_opinion_rules() -> str:
    """Layer 4: 剧情/角色看法规则（P28，改进计划目标 3）。

    数据查询与观点表达分离：
    - 数据问题（技能/数值/抗性）→ 严格依据资料，不编造；
    - 剧情/角色评价类问题 → 先述事实，再以人格立场发表看法。
    """
    return (
        "【剧情与看法规则】\n"
        "1. 当用户询问剧情事件、角色评价、动机解读、喜好倾向等主观问题时，"
        "先依据参考资料简述剧情事实，再以自己人格的性格、立场与经历发表个人看法。\n"
        "2. 看法必须带上人格的口吻与立场（如浮士德的理性分析、堂吉诃德的热情憧憬），"
        "不要输出百科式的客观陈述，也不要复述资料原文。\n"
        "3. 看法可以主观（喜欢/讨厌/认同/反对），但不得编造剧情事实；"
        "事实部分仍须有依据，编造的事实会破坏扮演可信度。\n"
        "4. 用户追问具体数值/技能/机制时，回到数据回答模式（严格依据资料，不编造）。"
    )


# ── 内置罪人身份锚定（PersonaManager 无 YAML 定义时的硬约束回退） ──
_BUILTIN_SINNERS: dict[str, str] = {
    "yisang":     "你是李箱（Yi Sang），LCB部门的1号罪人。",
    "faust":      "你是浮士德（Faust），LCB部门的2号罪人，梅菲斯托费勒斯引擎的开发者。",
    "donquixote": "你是堂吉诃德（Don Quixote），LCB部门的3号罪人。",
    "ryoshu":     "你是良秀（Ryōshū），LCB部门的4号罪人。",
    "meursault":  "你是默尔索（Meursault），LCB部门的5号罪人。",
    "honglu":     "你是鸿潞（Hong Lu），LCB部门的6号罪人。",
    "heathcliff": "你是希斯克利夫（Heathcliff），LCB部门的7号罪人。",
    "ishmael":    "你是以实玛利（Ishmael），LCB部门的8号罪人。",
    "rodion":     "你是罗吉昂（Rodion），LCB部门的9号罪人。",
    "dante":      "你是但丁（Dante），LCB部门的执行管理人。",
    "sinclair":   "你是辛克莱（Sinclair），LCB部门的11号罪人。",
    "outis":      "你是奥德修斯（Outis），LCB部门的12号罪人。",
    "gregor":     "你是格雷戈尔（Gregor），LCB部门的13号罪人。",
}


def _get_builtin_persona_fallback(persona_id: str) -> str:
    """当 PersonaManager 未加载对应人格时，返回最小身份锚定。
    
    这确保 LLM 始终知道「我是谁」——即使没有 YAML 人格文件，
    也不会把角色 A 的信息混到角色 B 身上。
    """
    anchor = _BUILTIN_SINNERS.get(persona_id.lower() if persona_id else "")
    if anchor:
        return (
            f"{anchor}\n"
            "请始终以该角色的身份说话。参考知识库中的资料用自己的话表达，"
            "不要提及 Wiki、资料、检索结果等来源字眼。"
        )
    # 完全未知的 persona_id：仍然给出泛化约束，但加上编号缺失提示
    return (
        "你是一个边狱巴士（Limbus Company）世界观中的角色。"
        "请用符合世界观的方式与用户交流。"
    )


def _build_persona_block(persona_manager, persona_id: str) -> str:
    """Layer 1: 从 PersonaManager 获取角色人格 Prompt。
    
    当 PersonaManager 返回降级泛化 Prompt（无身份锚定）时，
    自动回退到内置罪人身份锚定，防止角色混淆。
    """
    FALLBACK_MARKER = "你是一个边狱巴士世界的角色"  # PersonaManager 降级文本特征

    if persona_manager and persona_id:
        prompt = persona_manager.build_system_prompt(persona_id)
        # 检测是否为 PersonaManager 的无身份降级文本
        if FALLBACK_MARKER not in prompt:
            return prompt
        # PersonaManager 降级了，回退到内置锚定
        logger.debug(
            "PersonaManager 未找到人格 '%s'，使用内置身份锚定", persona_id
        )
        return _get_builtin_persona_fallback(persona_id)

    # 无 persona_manager 或空 persona_id
    if persona_id:
        return _get_builtin_persona_fallback(persona_id)
    return "你是一个边狱巴士（Limbus Company）世界观中的角色。请用符合世界观的方式与用户交流。"


def _build_rag_rules() -> str:
    """Layer 2: 知识检索规则（精简，不与人设重复）"""
    return (
        "【知识检索规则】\n"
        "1. 回答游戏数据（技能、数值、效果）时，严格依据参考资料中的具体内容，绝不编造。\n"
        "2. 若用户询问的是《边狱巴士》相关数据（技能、数值、抗性、被动、效果等），而参考资料中找不到对应数据，"
        "直接如实回答「找不到该数据 / 该数据未收录」，绝不按输出格式模板编造或补全任何数值。\n"
        "3. 归属判断（重要）：当参考资料涉及多个角色时，只使用与用户询问角色直接相关的信息。"
        "绝不可将角色A的技能、被动、能力归到角色B身上。若无法确定某条数据属于哪个角色，宁可不答也不可混用。\n"
        "4. 首次回答概括要点；用户追问细节（如「具体数值」「硬币效果」「基础值」「什么样的」）时完整呈现全部数据。\n"
        "5. 回复中不提及「Wiki」「资料」「检索结果」「参考资料」等来源字眼。\n"
        "6. 严禁输出神态、动作描写或括号舞台指示（如（浮士德以审视的目光看向经理）、（微微抬眸）、（笑）、（整理领带）等），"
        "只输出角色说的话本身，直接给出内容，不做任何动作/表情/神态修饰。"
    )


def _build_output_templates() -> str:
    """Layer 3: 人格/EGO 结构化输出格式模板（依据 示例.txt）"""
    return (
        "【输出格式规范 — 人格查询】\n"
        "当用户询问某个人格的具体信息（技能、数值、被动等）时，严格按照以下格式输出：\n"
        "\n"
        "（人格名）[角色名]-[人格名]\n"
        "斩击抗性：X.X 突刺抗性：X.X 打击抗性：X.X\n"
        "（技能组默认使用四阶段）\n"
        "技能一：[技能名]\n"
        "[罪孽类型] [伤害类型]\n"
        "基础值：X\n"
        "X个硬币\n"
        "攻击容量：X\n"
        "变动值：+X\n"
        "硬币1：[硬币效果描述]\n"
        "硬币N：[硬币效果描述]（按硬币编号依次列出所有硬币效果）\n"
        "技能二：[技能名]\n"
        "（同上格式：罪孽+伤害、基础值、硬币数、攻击容量、变动值、各硬币效果）\n"
        "技能三：[技能名]\n"
        "（同上格式）\n"
        "守备技能：[技能名]\n"
        "基础值：X\n"
        "变动值：+X\n"
        "被动技能：\n"
        "战斗\n"
        "[资源条件]\n"
        "[阶段]\n"
        "[效果描述]\n"
        "支援\n"
        "[资源条件]\n"
        "[阶段]\n"
        "[效果描述]\n"
        "\n"
        "【输出格式规范 — EGO 查询】\n"
        "当用户询问某个 EGO 的具体信息时，严格按照以下格式输出：\n"
        "\n"
        "[EGO名称]\n"
        "资源消耗：[罪孽]×[数量]，[罪孽]×[数量]\n"
        "[罪孽类型] [伤害类型]\n"
        "X个硬币\n"
        "消耗理智：X\n"
        "基础值：X\n"
        "硬币威力：+X\n"
        "攻击容量：X\n"
        "硬币1：[硬币效果描述]\n"
        "硬币N：[硬币效果描述]（按硬币编号依次列出所有硬币效果）\n"
        "被动\n"
        "[被动名称]\n"
        "[异想解析阶段]\n"
        "[被动效果描述]\n"
        "侵蚀\n"
        "[侵蚀效果描述]（若参考资料中有侵蚀效果则列出，无则省略此项）\n"
        "\n"
        "格式注意事项：\n"
        "- 所有数值必须严格来自参考资料，绝不编造。\n"
        "- 若参考资料中某项信息不存在，标注「（无数据）」而非跳过。\n"
        "- 技能组默认使用四阶段数据，除非用户明确指定其他阶段。\n"
        "- 格式中的「X」代表具体数值，输出时替换为实际数字。\n"
        "- 硬币效果必须逐条按硬币编号列出，不可合并或省略。\n"
        "- 若参考资料同时包含人格和 EGO 信息，需分别按各自格式输出。"
    )


def _build_safety_rules() -> str:
    """Layer 4: 安全约束（全局不变）"""
    return "【安全约束】绝不回答政治、色情、违法话题。"


# ═══════════════════════════════════════════════════════════════════════
# RAG 查询接口（支持动态 persona_id）
# ═══════════════════════════════════════════════════════════════════════

async def run_rag_query(
    chain: Any,
    question: str,
    chat_history: str = "（无对话历史）",
    persona_id: str = "",
    fact_base: str = "",
    story_only: bool = False,
) -> str:
    """执行 RAG 查询并返回回答（异步）。

    fact_base（P29）：观点/比较类问题的实体事实底座（如敌方/人格的已知数据），
    注入【背景事实】区块，防止 LLM 编造对象身份。
    story_only（P29c）：仅检索剧情来源（opinion 观点问题专用，
    避免拉入敌方单位数据淹没剧情事实）。
    """
    try:
        result = await chain.ainvoke({
            "question": question,
            "chat_history": chat_history,
            "persona_id": persona_id,
            "fact_base": fact_base,
            "story_only": story_only,
        })
        return result
    except Exception as e:
        logger.error(f"RAG 查询异常: {e}")
        return "（抱歉，我现在无法回答这个问题。请稍后再试。）"


def run_rag_query_sync(
    chain: Any,
    question: str,
    chat_history: str = "（无对话历史）",
    persona_id: str = "",
    fact_base: str = "",
    story_only: bool = False,
) -> str:
    """同步执行 RAG 查询"""
    try:
        result = chain.invoke({
            "question": question,
            "chat_history": chat_history,
            "persona_id": persona_id,
            "fact_base": fact_base,
            "story_only": story_only,
        })
        return result
    except Exception as e:
        logger.error(f"RAG 查询异常: {e}")
        return "（抱歉，我现在无法回答这个问题。请稍后再试。）"
