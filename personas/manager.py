"""
人格管理器：加载用户自定义 YAML 人格配置，编译为 System Prompt。
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ── 人格配置的 JSON Schema 字段说明（用于校验） ──
REQUIRED_FIELDS = ["id", "name", "display_name", "identity", "traits", "speech_style"]
OPTIONAL_FIELDS = [
    "catchphrase", "greeting_template", "knowledge_scope",
    "examples", "advanced", "likes", "dislikes", "relationships",
]


class PersonaManager:
    """人格配置管理器：扫描目录、加载、编译 Prompt"""

    def __init__(self, config_dir: str = "./personas"):
        self.config_dir = Path(config_dir)
        self.personas: dict[str, dict] = {}

    def load_all(self) -> dict[str, dict]:
        """
        扫描 personas/ 目录，加载所有合法 YAML 文件。
        返回 {persona_id: persona_dict}
        """
        if not self.config_dir.exists():
            self.config_dir.mkdir(parents=True, exist_ok=True)

        loaded = {}
        for yaml_file in list(self.config_dir.glob("*.yaml")) + list(self.config_dir.glob("*.yml")):
            try:
                raw = yaml_file.read_text(encoding="utf-8")
                persona = yaml.safe_load(raw)
                if not isinstance(persona, dict):
                    logger.warning(f"跳过非 dict 格式文件: {yaml_file}")
                    continue
                if self._validate(persona, yaml_file.name):
                    pid = persona["id"]
                    loaded[pid] = persona
                    logger.info(f"已加载人格: {pid} ({persona.get('display_name', pid)})")
                else:
                    logger.warning(f"人格校验失败，跳过: {yaml_file}")
            except yaml.YAMLError as e:
                logger.warning(f"YAML 解析失败: {yaml_file} - {e}")
            except Exception as e:
                logger.warning(f"读取人格文件异常: {yaml_file} - {e}")

        self.personas = loaded
        if not self.personas:
            logger.warning(
                f"未找到任何人格配置文件！请先在 {self.config_dir.absolute()}/ "
                "目录下创建至少一个 .yaml 文件。参考文档: plans/limbus_agent_design.md"
            )
        return self.personas

    def _validate(self, persona: dict, filename: str) -> bool:
        """校验人格配置合法性"""
        missing = [f for f in REQUIRED_FIELDS if f not in persona]
        if missing:
            logger.warning(f"{filename}: 缺少必填字段 {missing}")
            return False
        if not isinstance(persona["traits"], list) or not persona["traits"]:
            logger.warning(f"{filename}: traits 必须是非空列表")
            return False
        if not isinstance(persona["speech_style"], list) or not persona["speech_style"]:
            logger.warning(f"{filename}: speech_style 必须是非空列表")
            return False
        return True

    def get(self, persona_id: str) -> Optional[dict]:
        """获取指定人格配置"""
        return self.personas.get(persona_id)

    def list_ids(self) -> list[str]:
        """列出所有已加载的人格 ID"""
        return list(self.personas.keys())

    def build_system_prompt(self, persona_id: str) -> str:
        """
        将人格配置编译为精简版 System Prompt（控制 Token 开销）。
        P28：注入 examples（few-shot 示例）与 greeting，提升扮演一致性。
        """
        p = self.personas.get(persona_id)
        if not p:
            return "你是一个边狱巴士世界的角色。请用符合世界观的方式与用户交流。"

        lines = [
            f"你是{p['display_name']}，{p['identity']}。",
            f"性格：{'，'.join(p['traits'])}。",
            f"口吻：{'，'.join(p['speech_style'])}。",
        ]

        if p.get("catchphrase"):
            lines.append(f"口头禅：「{p['catchphrase']}」。")

        # ── 角色好恶（likes/dislikes，2026-08-15 新增）──
        likes = p.get("likes") or []
        dislikes = p.get("dislikes") or []
        if likes:
            lines.append(f"喜欢：{'；'.join(likes)}。")
        if dislikes:
            lines.append(f"讨厌：{'；'.join(dislikes)}。")

        # ── 人物关系（relationships，2026-08-16 新增）──
        # 格式：{角色名: 关系描述}。注入后 LLM 在涉及相关角色时
        # 能按原著关系互动（如浮士德与霍恩海姆互不买账）。
        rels = p.get("relationships") or {}
        if rels:
            rel_lines = [f"{k}：{v}" for k, v in rels.items()]
            lines.append(f"人物关系：{'；'.join(rel_lines)}。")

        # 进阶配置
        adv = p.get("advanced", {})
        avoid = adv.get("avoid_topics", [])
        if avoid:
            lines.append(f"绝不回答这些话题：{'、'.join(avoid)}。")

        lines.extend([
            "规则：始终以角色身份说话。参考知识用自己的话表达，不提Wiki/资料等来源。",
            # P21-E：数据类未命中必须如实回答「未收录/不知道」，绝不编造数值、技能、效果。
            "数据规则：当用户询问游戏数据（技能、数值、抗性、被动、效果、敌方单位等），"
            "而资料中未收录对应数据时，必须直接如实回答「该数据未收录」或「不知道」，"
            "绝不允许编造任何数值、技能名、效果或抗性。",
            # P28：剧情/角色看法规则（目标 3：人格化观点）
            "看法规则：当用户询问剧情事件、角色评价、动机解读或表达观点的题目时，"
            "先依据资料简述事实，再以自己人格的性格、立场与经历发表个人看法"
            "（不要复述资料原文，要用自己的话表达立场），"
            "看法与事实分开表述，观点可以主观但不可编造剧情事实。",
            # P32：严禁神态/动作描写（输出前会被程序二次清洗，双保险）
            "输出规则：严禁输出神态、动作描写或括号舞台指示，如（扫了一眼屏幕）（微微抬眸）（笑）（整理领带）等；"
            "只输出角色说的话本身，直接给出内容，不做任何动作、表情、神态修饰。",
            f"回复控制在{adv.get('max_response_length', 400)}字以内。",
        ])

        # ── P28：注入 few-shot 示例（人格 YAML 的 examples 字段）──
        # 示例让模型模仿角色的具体语气/句式，大幅提升扮演一致性。
        examples = p.get("examples") or []
        if examples:
            lines.append("\n【对话示例（模仿其中的语气与风格）】")
            for ex in examples[:3]:  # 控制 token，最多 3 条
                if not isinstance(ex, dict):
                    continue
                u = ex.get("user", "")
                r = ex.get("reply", "")
                if u and r:
                    lines.append(f"用户：{u}")
                    lines.append(f"{p['display_name']}：{r}")

        # 问候语（greeting_template）随身份一并注入
        greeting = p.get("greeting_template") or ""
        if greeting:
            lines.append(f"\n首次问候语（仅在会话开头或用户打招呼时使用）：{greeting}")

        return "\n".join(lines)

    def build_full_prompt(
        self,
        persona_id: str,
        context: str,
        chat_history: str,
        question: str,
    ) -> str:
        """构造完整的 Chat Prompt（System + Context + History + Question）"""
        p = self.personas.get(persona_id, {})
        display_name = p.get("display_name", "助手")
        system = self.build_system_prompt(persona_id)

        return (
            f"{system}\n\n"
            f"【参考资料】\n{context}\n\n"
            f"【对话历史】\n{chat_history}\n\n"
            f"用户：{question}\n"
            f"{display_name}："
        )

    def get_persona_display_info(self) -> str:
        """获取人格列表的展示信息（用于 /人格列表 指令）"""
        if not self.personas:
            return "（未加载任何人情配置，请在 personas/ 目录下创建 .yaml 文件）"
        lines = ["当前可用人格："]
        for pid, p in self.personas.items():
            name = p.get("display_name", pid)
            identity = p.get("identity", "")
            lines.append(f"  [{pid}] {name} - {identity}")
        return "\n".join(lines)
