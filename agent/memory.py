"""
对话记忆管理模块：基于 langchain_core 的 InMemoryChatMessageHistory 实现。
langchain_community 中的 ConversationBufferWindowMemory 已在新版废弃。
"""

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage


def create_memory(window_size: int = 10) -> InMemoryChatMessageHistory:
    """创建对话记忆实例（使用 langchain_core 内建类，无额外依赖）"""
    return InMemoryChatMessageHistory()


def get_chat_history_text(memory: InMemoryChatMessageHistory, window_size: int = 10) -> str:
    """从 Memory 中提取最近 N 轮对话历史文本"""
    try:
        messages = memory.messages
        if not messages:
            return "（无对话历史）"

        # 只保留最近 window_size * 2 条消息（每轮一问一答）
        recent = messages[-(window_size * 2):]
        lines = []
        for msg in recent:
            if isinstance(msg, HumanMessage):
                lines.append(f"用户：{msg.content}")
            elif isinstance(msg, AIMessage):
                lines.append(f"助手：{msg.content}")
        return "\n".join(lines)
    except Exception:
        return "（对话历史加载失败）"
