"""
WikiText → 语义化文本解析器。
使用 mwparserfromhell 递归遍历节点树，从模板/链接/标题中提取结构化内容。
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── 尝试导入 mwparserfromhell ──
try:
    import mwparserfromhell
    HAS_MWPARSER = True
except ImportError:
    HAS_MWPARSER = False
    logger.warning("mwparserfromhell 未安装，将使用基础正则解析")

# ── 可选导入：节点类型（用于 isinstance 判断） ──
if HAS_MWPARSER:
    try:
        from mwparserfromhell.nodes import (
            Text, Template, Wikilink, ExternalLink,
            Heading, Tag, Comment, HTMLEntity, Argument,
        )
    except ImportError:
        HAS_MWPARSER = False


# ── WikiText 残留的正则清理规则（节点遍历后的安全网） ──
REPLACEMENTS = [
    # 标题（残留）
    (r"=====?\s*(.+?)\s*=====?", r"### \1"),
    (r"====\s*(.+?)\s*====", r"### \1"),
    (r"===\s*(.+?)\s*===", r"## \1"),
    (r"==\s*(.+?)\s*==", r"# \1"),

    # 粗体 / 斜体（残留）
    (r"'''''(.+?)'''''", r"***\1***"),
    (r"'''(.+?)'''", r"**\1**"),
    (r"''(.+?)''", r"*\1*"),

    # 内部链接 [[页面名|显示文本]]（残留）
    (r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2"),
    (r"\[\[([^\]]+)\]\]", r"\1"),

    # 外部链接 [http://... 文本]（残留）
    (r"\[(https?://[^\s\]]+)\s+([^\]]+)\]", r"\2 (\1)"),

    # 列表
    (r"^\*\s+", r"- ", re.MULTILINE),
    (r"^#\s+", r"1. ", re.MULTILINE),

    # 模板引用（残留，含嵌套）
    (r"\{\{[^}]+\}\}", ""),

    # HTML 注释（残留）
    (r"<!--.*?-->", "", re.DOTALL),

    # 引用 <ref>（残留）
    (r"<ref[^>]*>.*?</ref>", "", re.DOTALL),
    (r"<ref[^>]*/>", ""),

    # 多余空行
    (r"\n{3,}", "\n\n"),
]


def _extract_text_from_wikicode(wikicode) -> str:
    """
    递归遍历 mwparserfromhell 节点树，逐节点提取语义化文本。

    与 strip_code() 的粗暴去除不同，此函数：
    - Template: 展开参数名和参数值（如「波次：1」）
    - Wikilink: 提取显示文本
    - Heading: 保留标题文本和换行
    - Tag/Comment/Argument: 跳过
    """
    parts = []

    for node in wikicode.nodes:
        if isinstance(node, Text):
            parts.append(str(node))

        elif isinstance(node, Wikilink):
            # [[页面|显示文本]] 或 [[页面]]
            text = str(node.text) if node.text else str(node.title)
            parts.append(text)

        elif isinstance(node, ExternalLink):
            # [http://... 文本]
            text = str(node.text) if node.text else str(node.url)
            parts.append(text)

        elif isinstance(node, Template):
            # {{模板名|参数1=值1|值2}}
            params_text = []
            for param in node.params:
                name = str(param.name).strip() if param.name else ""
                value = _extract_text_from_wikicode(param.value).strip()
                if name and value:
                    params_text.append(f"{name}：{value}")
                elif value:
                    params_text.append(value)
            if params_text:
                parts.append("\n".join(params_text))

        elif isinstance(node, Heading):
            title = _extract_text_from_wikicode(node.title)
            parts.append(f"\n{title}\n")

        elif isinstance(node, Tag):
            # 跳过 <ref>、<gallery>、<br> 等 HTML 标签
            # 但 <br /> 可以转为换行
            tag_str = str(node)
            if tag_str.strip().startswith("<br"):
                parts.append("\n")
            # 其他标签（ref, gallery, div, span 等）全部跳过

        elif isinstance(node, Comment):
            # 跳过 <!-- 注释 -->
            continue

        elif isinstance(node, HTMLEntity):
            parts.append(str(node))

        elif isinstance(node, Argument):
            # 跳过 {{{1}}} 这类模板参数占位符
            continue

        else:
            # 未知节点类型，fallback 到字符串（安全网）
            try:
                s = str(node)
                if s and not s.isspace():
                    parts.append(s)
            except Exception:
                pass

    return "".join(parts)


def wikitext_to_markdown(wikitext: str, title: str = "") -> str:
    """将 MediaWiki WikiText 转换为语义化纯文本"""

    if not wikitext or not wikitext.strip():
        return ""

    text = wikitext

    # ── Stage 1: 递归节点遍历（提取模板参数、链接文本等） ──
    if HAS_MWPARSER:
        try:
            parsed = mwparserfromhell.parse(text)
            text = _extract_text_from_wikicode(parsed)
        except Exception:
            pass

    # ── Stage 2: 正则安全网（清理节点遍历未能处理的残留语法） ──
    for pattern in REPLACEMENTS:
        if len(pattern) == 3:
            regex, repl, flags = pattern
            text = re.sub(regex, repl, text, flags=flags)
        else:
            regex, repl = pattern
            text = re.sub(regex, repl, text, flags=re.DOTALL)

    # 移除残留的开头/结尾空白
    text = text.strip()

    return text
