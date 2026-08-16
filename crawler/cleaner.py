"""
文本清洗模块：去除 WikiText 解析后的残留噪音和模板参数样式标记。
"""

import re

# ── Wiki 模板/表格的参数样式残留正则 ──
CLEAN_PATTERNS = [
    # 表格单元格参数样式: |600px|center, |45px|link=页面名|显示文本, |居中| 等
    (re.compile(r"\|\s*\d+px\s*\|[^|\n]*(?:\|[^|\n]*)*"), ""),
    # 表格布局 token（孤立出现）: |center, |居中, |left, |right, |link=..., |class=...
    (re.compile(r"\|\s*(?:center|居中|left|right|link\s*=\s*\S*|class\s*=\s*\S*)\b", re.IGNORECASE), ""),
    # 像素规格（含紧邻中文/Unicode 的情况）: 600px, 45px, 50px
    (re.compile(r"\d+px"), ""),
    # 文件引用残留: file:xxx.ogg, File:xxx.png (大小写不敏感)
    (re.compile(r"[Ff]ile:[A-Za-z0-9_./\-]+(?:\.\w+)?(?:\s*\|\s*[^|\n]*)*"), ""),
    # 语音/音频文件引用残留: 语音：a/b/S001B-01.ogg, 语音：S001B-01.ogg 等
    (re.compile(r"语音：[A-Za-z0-9_./\-]+\.(?:ogg|mp3|wav|flac)(?:\s*\|\s*[^|\n]*)*"), ""),
    # 图片/媒体文件引用残留: xxx.png, xxx.jpg, xxx.gif, xxx.webp (含路径)
    (re.compile(r"(?:图片|立绘|图标|头像|背景)[：:]\s*[A-Za-z0-9_./\-]+\.(?:png|jpg|jpeg|gif|webp|svg)"), ""),
    # 图片文件名+描述列表: 李箱-face_sad3_R.png：悲伤-R 等（单行多个）
    # 支持中文/日文/英文前缀的文件名
    (re.compile(r"[\u4e00-\u9fffA-Za-z0-9_\-]+-(?:face|default|portrait|battle|story|sprite)"
                r"[\u4e00-\u9fffA-Za-z0-9_\-]*\.(?:png|jpg|jpeg|gif|webp|svg)[：:]"
                r"[^\n，,。.；;]{0,20}"),
                ""),
    # organize/name/picture 三元组残留: organize：xxx name：xxx picture：xxx
    (re.compile(r"\b(?:organize|name|picture|page|image)\s*[：:]\s*\S+", re.IGNORECASE), ""),
    # 残留 css class 样式 token: bg-danger, mw-collapsible, etc
    (re.compile(r"\b(?:bg-\w+|mw-\w+|title-class|body-style|text-align|display)\b[\s=]*[^;\n]*;?"), ""),
    # 内联 CSS style 属性残留: style="..." 或 style='...'
    (re.compile(r"""style\s*=\s*["'][^"']*["']""", re.IGNORECASE), ""),
    # 管道+空内容残留: | |, ||
    (re.compile(r"\|\s*\|"), " "),
    # 纯管道符号残留（孤立的行）
    (re.compile(r"^\s*\|\s*$", re.MULTILINE), ""),
    # 以管道符开头的行（模板参数截断残留）
    (re.compile(r"^\|[^\n]*$", re.MULTILINE), ""),
    # JSON/模板 尾随残留符: ！"}  "}  }"  等
    (re.compile(r"""[！!]\s*["'}）]\s*}"""), ""),
    # 孤立的 JSON 花括号/引号残余（纯符号组合）
    (re.compile(r"""^[\s!！"'，,。.\[\]{}（）(){}:：;；\-–—]+$""", re.MULTILINE), ""),
    # 大量连续的空格
    (re.compile(r" {3,}"), " "),
    # 残留 HTML 标签
    (re.compile(r"<[^>]+>"), ""),
    # wiki 表格残留 {| ... |}
    (re.compile(r"\{\|.*?\|\}", re.DOTALL), ""),
    # 模板变量引用 {{{1}}}
    (re.compile(r"\{\{\{[0-9]+\}\}\}"), ""),
    # 模板残留 {{...}}（经过 parser 后残留的简单模板）
    (re.compile(r"\{\{[^}{]*?\}\}"), ""),
    # >3 个连续空行 → 合并为 2 个
    (re.compile(r"\n{4,}"), "\n\n"),
    # 纯数字开头的编号残留（如 "1、" 开头的行，如果整行只有编号则移除）
    (re.compile(r"^\d+[、.]\s*$", re.MULTILINE), ""),
    # 纯 CSS 属性行残留（flex-direction, align-items 等）
    (re.compile(r"^\s*(?:justify-content|align-items|flex-direction|flex-wrap|"
                r"grid-template|position|display|overflow|z-index)\s*:[^;]*;?\s*$",
                re.MULTILINE | re.IGNORECASE), ""),
]

# ── 保留的有效行模式（匹配这些模式的行即使短也保留） ──
KEEP_LINE_PATTERN = re.compile(
    r"^(名称|编号|罪人|特质|波次|可上场人数|敌人|敌方增援|地点|角色|"
    r"介绍|性格|身份|被动|技能|语音|台词|标题|内容|效果|图标|"
    r"上一章|下一章|相关|"
    r"[A-Za-z\u4e00-\u9fff]{2,})"
)

# 图片文件名模式（单行检测用）
# 支持中文/日文/英文前缀的文件名（如 李箱-face_sad3_R.png）
_IMAGE_FILE_PATTERN = re.compile(
    r"^[\u4e00-\u9fffA-Za-z0-9_\-]+-(?:face|default|portrait|battle|story)[A-Za-z0-9_\-]*\.[a-z]{3,4}[：:]\s*\S",
    re.IGNORECASE
)


def _is_meaningful_line(line: str) -> bool:
    """判断一行文本是否有实质内容"""
    stripped = line.strip()
    if not stripped:
        return False

    # 纯数字/符号/标点
    if re.match(r"""^[\d,.\-–—|/\\\[\]{}()（）{}:：;；!！?"'＂\s]+$""", stripped):
        return False

    # 纯英文文件名/路径
    if re.match(r"^[A-Za-z0-9_./\-]+\.[a-z]{2,4}$", stripped):
        return False

    # 图片文件名+描述行: "xxx-face_sad.png：悲伤-R"
    if _IMAGE_FILE_PATTERN.match(stripped):
        return False

    # CSS 属性行（如 "justify-content: flex-end" 独立成行）
    if re.match(r"^[a-z\-]+\s*:\s*[^;]*;?\s*$", stripped, re.IGNORECASE):
        return False

    # organize/name/picture 孤行
    if re.match(r"^(?:organize|name|picture|page|image)\s*[：:]\s*\S+$", stripped, re.IGNORECASE):
        return False

    # 长度 ≥ 3 个中文字符 或 ≥ 6 个英文字母
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    ascii_chars = len(re.findall(r"[A-Za-z]", stripped))
    if cjk_chars >= 3 or ascii_chars >= 6:
        return True
    if cjk_chars + ascii_chars >= 5:
        return True
    if KEEP_LINE_PATTERN.match(stripped):
        return True

    return False


def clean_text(text: str) -> str:
    """清洗文本，去除残留的 Wiki 标记噪音"""

    if not text:
        return ""

    # Stage 1: 正则批量替换（去噪音）
    for pattern, replacement in CLEAN_PATTERNS:
        text = pattern.sub(replacement, text)

    # Stage 2: 逐行过滤无意义行
    lines = text.split("\n")
    meaningful = []
    for line in lines:
        stripped = line.strip()
        if _is_meaningful_line(stripped):
            meaningful.append(stripped)

    # Stage 3: 合并且处理多余空行
    text = "\n".join(meaningful)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def is_empty_after_clean(text: str) -> bool:
    """检查清洗后的文本是否实质为空（无有效语义内容）。

    用于在入向量库前做最后一道把关。
    """
    if not text or not text.strip():
        return True

    # 统计有效字符
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    alpha = len(re.findall(r"[A-Za-z]{3,}", text))
    total_len = len(text.strip())

    # 清洗后仍短于 30 字符 或 有效词极少 → 视为空
    if total_len < 30:
        return True
    if cjk + alpha < 3:
        return True

    return False
