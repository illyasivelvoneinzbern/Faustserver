"""
文本分块模块：将长文本切分为适合向量嵌入的块。

对于人格/EGO 页面的结构化数据（_structured=True），使用 chunk_builder 精确分块；
对于其他页面，使用 RecursiveCharacterTextSplitter 盲切。
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

from crawler.chunk_builder import build_structured_chunks

logger = logging.getLogger(__name__)


# ── 噪声内容检测正则 ──
_NOISE_PATTERNS = [
    # CSS 属性/值残留（如 justify-content, align-items, flex-end, position: absolute 等）
    re.compile(r"(?:justify-content|align-items|flex-(?:start|end|direction|wrap)|"
               r"position\s*:\s*absolute|display\s*:\s*|border-\w+|margin-\w+|padding-\w+)",
               re.IGNORECASE),
    # HTML 实体残留
    re.compile(r"&[a-z]+;", re.IGNORECASE),
    # 纯 JSON 片段（花括号连续出现 ≥3 次）
    re.compile(r"[\{\}].*[\{\}].*[\{\}]"),
    # 图片文件名列表残留：xxx-face_xxx.png，xxx.png：xxx 等
    # 支持中文/日文/英文前缀的文件名（如 李箱-face_sad3_R.png）
    re.compile(r"[\u4e00-\u9fffA-Za-z0-9_\-]+-(?:face|default|portrait|battle|story)[A-Za-z0-9_\-]*\.(?:png|jpg|jpeg|gif|webp|svg|ogg)",
               re.IGNORECASE),
    # organize: / name: / picture: 模式（JSON/模板残留键值对块）
    re.compile(r"\b(?:organize|name|picture|page|image)\s*:\s*\S+", re.IGNORECASE),
    # Wiki 模板嵌入（如 {{#html:Personalitygif|...}}, {{#html:LCBAudio|...}}）
    re.compile(r"\{\{#html:"),
    # sortable 表格残留（如 { sortable" style="width: 100%;）
    re.compile(r"\{\s*sortable"),
    # class/class： 属性残留（如 class="huiji-tt-preload", class：itemlist dark）
    re.compile(r"class[=：]", re.IGNORECASE),
    # 预告/预览标签残留（如 *预告P）
    re.compile(r"\*预告[Pp]"),
    # 裸露长 URL（Wiki 模板残留的 B站/微博 链接等）
    re.compile(r"https?://[^\s]{25,}"),
    # colspan= / rowspan= 残留（表格属性）
    re.compile(r"(?:colspan|rowspan)\s*=", re.IGNORECASE),
    # __NOTOC__ / __TOC__ 等 Wiki 魔术字
    re.compile(r"__[A-Z]+__"),
]

# ── Wiki 模板剥离：在分块前清除所有 {{...}} 语法 ──
# {{#html:Personalitygif|{"1":"..."}}}, {{Dialog|角色=罪人们|对话=……}},
# 以及分块边界处被截断的孤立 }}，如果不先清除就会污染所有 chunk。
# 不能用单一正则（内层 { } 会干扰 [^{}]*），改用括号计数迭代清除。
def _strip_wiki_templates(text: str) -> str:
    """迭代清除所有 {{...}} Wiki 模板语法（含嵌套花括号）。

    与 _is_noise_content 配合：阈值逻辑对"大量中文+少量模板残留"无效，
    必须在分块前直接删除。孤立 }}（被切掉开头的模板尾巴）也一并清除。
    """
    # 快速路径：没有 {{ 就直接返回
    if "{{" not in text:
        return text

    # 第一轮：尽量用简单正则清除扁平模板（性能好）
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{\{[^{}]*?\}\}", "", text)

    # 第二轮：括号计数处理嵌套模板（如 {{#html:...|{"1":"..."}}}）
    if "{{" in text:
        result: list[str] = []
        i = 0
        while i < len(text):
            if i + 1 < len(text) and text[i:i + 2] == "{{":
                depth = 1
                i += 2
                while i < len(text) and depth > 0:
                    if i + 1 < len(text) and text[i:i + 2] == "{{":
                        depth += 1
                        i += 2
                    elif i + 1 < len(text) and text[i:i + 2] == "}}":
                        depth -= 1
                        i += 2
                    else:
                        i += 1
            elif i + 1 < len(text) and text[i:i + 2] == "}}":
                # 孤立关闭括号（模板被分块边界切掉开头）
                i += 2
            else:
                result.append(text[i])
                i += 1
        text = "".join(result)

    return text

# 常见 CSS/HTML/代码关键词，不计入「有效英文词」
_CODE_NOISE_WORDS = frozenset({
    "justify", "flex", "align", "items", "position", "absolute",
    "relative", "display", "style", "width", "height", "border",
    "margin", "padding", "color", "font", "size", "left", "right",
    "top", "bottom", "center", "none", "block", "auto", "solid",
    "hidden", "visible", "pointer", "cursor", "index", "overflow",
    "content", "wrap", "direction", "column", "start", "grid",
    "template", "repeat", "span", "area", "self", "text",
    "organize", "picture", "image", "name", "page", "link",
    "true", "false", "class", "data", "type", "role", "aria",
    "href", "src", "alt", "div", "span",
    "colspan", "rowspan", "notoc", "toc", "html",
})


def _is_noise_content(text: str) -> bool:
    """检测内容是否主要由无效噪声构成（CSS/HTML/JSON/Wiki模板残留）。

    如果噪声命中次数 >= 有效文本词数，则判定为噪声块。
    """
    if not text:
        return True

    # 统计有效中文字符
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

    # 统计有效英文单词（≥3 个字母），排除 CSS/HTML 代码关键词
    all_en = re.findall(r"[A-Za-z]{3,}", text)
    en_words = sum(1 for w in all_en if w.lower() not in _CODE_NOISE_WORDS)

    # 如果几乎无有效文本，直接判定为噪声
    if cn_chars + en_words < 3:
        return True

    # 检查噪声模式命中次数
    noise_hits = sum(len(p.findall(text)) for p in _NOISE_PATTERNS)

    # 噪声命中次数 >= 有效词数 → 噪声块
    if noise_hits >= (cn_chars + en_words):
        return True

    return False


# ── 对话格式检测：用于区分 character 和 plot ──
# 匹配 Wiki 对话格式："角色名：内容" 或 "角色名:内容"（行首或以"1："开头的Wiki列表）
_DIALOG_LINE_RE = re.compile(
    r"(?:^|\n)(?:\d+：|\*\*)?[\u4e00-\u9fffA-Za-z]{2,10}(?:：|:)",
)

# 非角色名称的系统/道具前缀——若行首是这些词则不是对话行
_NON_DIALOG_PREFIXES = re.compile(
    r"^(?:用途|道具描述|介绍|中文名|英文名|图像|性别|职业|爱好|"
    r"初次登场|其他称呼|CV[：:]|名称|类型|效果|等级|品质|"
    r"获得方式|售价|备注|所属|编号|属性|技能|被动|同步|"
    r"觉醒|侵蚀|阶段|定位|季节|赛季|实装|语音|台词|"
    r"角色经历|相关角色|相关人格|相关考据|基本信息|"
    r"地点|波次|可上场人数|敌人|战斗音乐|音效|"
    r"用途|描述|说明|来源|译名|原文|译者|注[：:]|"
    r"译者注|相关链接|链接|参考资料|参见|外部链接|"
    r"分类|标题|子标题|章节|部分|"
    r"colspan|rowspan|__NOTOC__|__TOC__|{|}|"
    r"border|style|class)"
)


def _is_primarily_dialogue(text: str) -> bool:
    """检测内容是否主要由角色对话构成（≥40% 的行是对话格式）。

    对话格式特征：
    - "角色名：对话内容"（中文冒号），且角色名不是已知的系统/道具前缀
    - "1：角色名：对话内容"（Wiki 编号格式）
    - "角色名:对话内容"（英文冒号）
    """
    if not text:
        return False

    lines = text.split("\n")
    if len(lines) < 5:
        return False

    # 排除前 3 行（通常是页面标题和元数据）
    body_lines = lines[3:] if len(lines) > 3 else []
    if not body_lines:
        return False

    # 统计对话行数
    dialog_lines = 0
    meaningful_lines = 0
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue
        meaningful_lines += 1
        # 跳过以系统/道具前缀开头的行（用途：、道具描述：等不算对话）
        if not _NON_DIALOG_PREFIXES.match(stripped):
            if _DIALOG_LINE_RE.search(stripped):
                dialog_lines += 1

    if meaningful_lines < 3:
        return False

    # 对话行占比 ≥ 40% → 判定为剧情对话页
    return dialog_lines >= meaningful_lines * 0.4


# ── 标题前缀匹配：完全排除（无价值的 Wiki 基础设施/数据工具页） ──
_JUNK_TITLE_PATTERNS = [
    # Wiki 编辑工具
    r"^仪表盘$",
    r"^测试界面\d*",
    r"^编写指南$",
    r"^SMongo",
    r"^沙盒$",
    r"^鼠标提示框教程$",
    r"^Manifest:",
    r"^书写规范/",
    r"^编辑计划",
    r"^编辑工具",
    r"^数据库列表$",
    r"^技能图标DIY$",
    r"^ContributionScores$",
    r"^Help:",
    r"^Widget:",
    r"^Testbilibiliplayer$",
    # 数据筛选工具页
    r"^E\.G\.O基础数据筛选",
    r"^E\.G\.O状态效果筛选",
    r"^人格状态效果筛选",
    r"^人格被动筛选",
    r"^E\.G\.O基础数据筛选-新版",
    # 直播/公告
    r"^直播/",
    # 别名查询
    r"^别名查询$",
    # 教程
    r"^教程$",
    # 纯英文缩写标题（如 SMongo, CSS, HTML 等）
    r"^[A-Za-z]+$",
    # 画廊/图片页
    r"^画廊$",
    r"^剧情CG$",
    r"^技能图标$",
    # 音乐列表页
    r"^音乐$",
]
_JUNK_RE = re.compile("|".join(_JUNK_TITLE_PATTERNS))

# ── 标题前缀匹配：非角色页面但可能有参考价值，归为 other ──
_NON_CHARACTER_TITLE_PATTERNS = [
    # 原有模式
    r"^事件-",
    r"^战斗语音播报员-",
    r"^世界观/",
    r"^主线战斗",
    r"^折射轨道",
    r"^间章",
    r"^章节",
    r"^敌方-",
    r"^第[一二三四五六七八九十\d]+赛季",
    r"^人格基础数据筛选",
    r"^探索事件$",
    r"^.*提取券$",
    r"^.*必得十连.*$",
    r"^.*人格自选券.*$",
    r"^.*E\.G\.O自选券.*$",
    # === 新增：游戏系统/概念页 ===
    r"^攻击容量$",
    r"^防御等级$",
    r"^罪孽共鸣$",
    r"^攻击等级$",
    r"^状态效果",          # 状态效果、状态效果/正面、状态效果/负面 等
    r"^播报员",
    r"^异想体$",
    r"^人格$",              # 系统概念页，非具体人格
    r"^脑啡肽",
    r"^电池模块$",
    r"^通行证$",
    r"^纺锤",              # 纺锤、纺锤捆、纺锤箱
    r"^映射战斗$",
    r"^续关功能$",
    r"^光之树苗能力$",
    r"^采光迷宫$",
    r"^个人名片装饰$",
    r"^外观投影$",
    r"^充值系统$",
    r"^活动一览$",
    r"^每日签到$",
    r"^签到活动$",
    r"^剧情角色$",
    r"^敌方单位$",
    r"^狂气$",
    r"^普通乘客复原包$",
    r"^智·勇·仁基础入门手册$",
    r"^假装不是秘籍的秘籍$",
    # === 新增：镜之地牢 ===
    r"^镜中之镜",
    r"^起始之镜",
    r"^湖之镜",
    r"^永生之镜",
    r"^梦中之镜",
    r"^呼啸之镜",
    r"^名与蛛",
    r"^镜像迷宫",           # 镜像迷宫、镜像迷宫-敌方数据、镜像迷宫E.G.O饰品合成
    # === 新增：活动页面 ===
    r"^切磋琢春",
    r"^善意的巡礼",
    r"^绞丝结线",
    r"^地狱鸡",
    r"^20区的奇迹",
    r"^瓦尔普吉斯之夜",
    # === 新增：跨 Wiki 联动 ===
    r"^凯尔希$",
    r"^博士$",
    r"^斯卡蒂$",
    r"^艾丽妮$",
    r"^阿米娅$",
    r"^艾兰$",
    # === 新增：音乐/歌词 ===
    r"^In Hell We Live Lament",
    r"^Between Two Worlds",
    r"^Fly,?\s*My\s*Wings",
    r"^TIAN TIAN",
    r"^Through patches of violet",
    r"^Compass$",
    r"^Hero$",
    r"^SAIKAI$",
    # === 新增：道具/升级物品 ===
    r"^跳跃成长模组",
    r"^人格等级直升券",
    r"^人格训练券",
    r"^自我碎片箱$",
    r"^自我碎片自选箱$",
    r".*的自我碎片$",
    r"^3周年纪念人格自选券",
    r"^第三赛季.*券",
    r"^第[一二三四五六七].*赛季.*必得十连",
    r"^第[一二三四五六七]赛季.*自选",
    # === 新增：地牢/迷宫地图 ===
    r"^1[12]区L公司支部迷宫",
    r"^4区脑叶公司支部",
    r"^J-\d+支部",
    r"^步入黑暗",
    r"^记忆中的自我心道",
    r"^(.+之)?镜-",          # 呼啸之镜-初始增益, 梦中之镜-成就 等
    r"^金笠的心象$",
    r"^魔王希斯克利夫\d*$",   # 魔王希斯克利夫、魔王希斯克利夫2 (BOSS 页非角色)
    # === 新增：周年/杂项 ===
    r"^边狱公司[一二三\d]周年$",
    # 愚人节特殊页面
    r".*LCB罪人/2025愚人节$",
    # 扭曲侦探报告（纯小说文本）
    r"^韦斯帕/扭曲侦探$",
    r"^韩熙俊$",
    r"^韩蔚$",
    # 防歧义页
    r".*\(防歧义页\)$",
    # 物品/道具通用模式 — 来自 dry-run 误判的页面
    r"^装饰品包裹$",
    r"^巨大装饰品包裹$",
    r"^恐鱼标本$",
    r"^发光的恐鱼标本$",
    r"^帝王废料蟹$",
    r"^巴士改装废料$",
    r"^含有稀有马蹄铁的巴士改造废料$",
    r"^成捆怀表$",
    r"^烧制的透镜$",
    r"^染血剑鞘$",
    r"^怨恨剑鞘$",
    r"^被斩断的线$",
    r"^被斩断烧却的线团$",
    r"^清道夫液态燃料罐$",
    r"^特大型液态燃料罐$",
    r"^机密文件$",
    r"^来历不明的无人机$",
    r"^几张照片$",
    r"^许多照片$",
    r"^展开的胶片$",
    r"^可米的礼物$",
    r"^字迹稚嫩的寄语$",
    r"^活动战斗",
    # 误入 character 的角色类型（世界观NPC汇总、通用概念）
    r"^你，在讨打吗？$",
    r"^令人胆战心惊的机械$",
    # === 新增：活动剧情页（归为 other 而非 character）===
    r"^LCB定期体检",
    r"^WARP快车谋杀案",
    r"^时间杀人时间",
    r"^深夜清扫",
    r"^肉斩骨断",
    r"^仲春夜之梦",
    # === 新增：道具/物品（继续补充）===
    r"^人格跳跃成长券",
    r"^怀表$",
    r"^情报文件$",
    r"^解析锁链$",
    r"^永生的星芒$",
    r"^收尾人乘客复原包$",
    r"^同步&异想解析专用人格碎片",
    # === 新增：系统/概念（继续补充）===
    r"^友方单位$",
    r"^探索事件判定$",
    r"^罪孽属性$",
    r"^经验数据$",
    r"^活跃编辑者名单$",
    r"^签到活动\d*$",
    r"^扭曲侦探$",
    # === 新增：敌方角色数据页 ===
    r"^角色-",
    # === 新增：章节引导 ===
    r"^序章\s",
    # === 新增：外观投影子页面 ===
    r"^外观投影/",
    # === 新增：E.G.O/异想体/机制页 ===
    r"^理解的果实$",
    r"^请给我们爱$",
    r"^粉红鞋$",
    r"^你变强了吗$",
]
_NON_CHARACTER_RE = re.compile("|".join(_NON_CHARACTER_TITLE_PATTERNS))


def _classify_page_type(title: str, categories_list: list[str], content: str = "") -> str:
    """根据 categories、标题和内容推导 page_type。

    优先级：
    1. categories 含 "人格" → personality
    2. categories 含 "E.G.O饰品" → accessory（先于 E.G.O 检测，避免被吞噬）
    3. categories 含 "E.G.O"   → ego
    4. categories 含 "剧情"   → plot
    5. categories 含 "角色"   → character
    6. 标题匹配垃圾桶模式 → 完全跳过（由 chunk_documents 检查）
    7. 空 categories:
        a. 标题匹配非角色模式 → other（先检查，避免道具页被对话检测误判）
        b. 内容以对话为主 → plot
        c. 默认 → character
    8. 有 categories 但非以上 → other
    """
    if "人格" in categories_list:
        return "personality"
    if "E.G.O饰品" in categories_list:
        return "accessory"
    if "E.G.O" in categories_list:
        return "ego"
    if "剧情" in categories_list:
        return "plot"
    if "角色" in categories_list:
        return "character"

    if not categories_list:
        # 标题匹配已知非角色模板 → other（优先于对话检测）
        if _NON_CHARACTER_RE.match(title):
            return "other"
        # 内容以对话为主 → 归为剧情
        if content and _is_primarily_dialogue(content):
            return "plot"
        return "character"

    # 有 categories 但非以上 → other (关卡/道具/异想体数据等)
    return "other"


def _is_junk_page(title: str) -> bool:
    """检查标题是否属于完全无价值的垃圾桶页面。"""
    return bool(_JUNK_RE.match(title))


def create_splitter(
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> RecursiveCharacterTextSplitter:
    """创建中文语义感知的文本分块器"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
        length_function=len,
        is_separator_regex=False,
    )


def chunk_documents(
    raw_data: list[dict],
    splitter: RecursiveCharacterTextSplitter,
) -> list[Document]:
    """
    将原始 JSON 数据分块为 LangChain Document 列表。
    每个 Document 携带 metadata（标题、URL、来源、分类等）。

    对 _structured=True 的数据（人格/EGO），使用 chunk_builder 精确分块；
    对其他数据，使用 RecursiveCharacterTextSplitter 盲切。
    """
    documents = []
    for item in raw_data:
        # ── 结构化数据（人格/EGO）走专用 builder ──
        if item.get("_structured"):
            try:
                structured_chunks = build_structured_chunks(item)
                documents.extend(structured_chunks)
            except Exception as e:
                logger.error(f"结构化分块构建失败 [{item.get('title', '?')}]: {e}")
            continue

        content = item.get("content", "")
        if not content or len(content) < 60:
            continue

        # ── 分块前剥离所有 Wiki 模板语法 ──
        # _is_noise_content 的阈值逻辑无法捕获"大量中文+少量模板残留"的场景，
        # 必须在分块前直接清除 {{...}} 模板语法（含平直/嵌套/孤立关闭括号）。
        content = _strip_wiki_templates(content)

        # 跳过高噪声内容：主要由 CSS/HTML/JSON/Wiki模板残留 构成
        if _is_noise_content(content):
            continue

        title = item.get("title", "")

        # 完全跳过垃圾桶页面（Wiki 基础设施/数据工具）
        if _is_junk_page(title):
            continue

        categories_list = item.get("categories", [])
        page_type = _classify_page_type(title, categories_list, content)

        metadata = {
            "source": item.get("source", "wiki"),
            "source_url": item.get("url", ""),
            "page_title": title,
            "page_type": page_type,
            "categories": ",".join(categories_list),
            "published_at": item.get("published_at", ""),
        }

        # ── Tabx 饰品：将 Wiki 自带分类字段透传到 metadata，供结构化过滤 ──
        if item.get("source") == "tabx":
            effect = item.get("effect", "")
            rarity = item.get("rarity", -1)
            if effect:
                metadata["effect"] = effect
            if rarity >= 0:
                metadata["rarity"] = rarity

        chunks = splitter.create_documents(
            texts=[content],
            metadatas=[metadata],
        )
        documents.extend(chunks)

    return documents


def chunk_from_jsonl(
    jsonl_path: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    limit: int = 0,
) -> list[Document]:
    """
    从 JSONL 文件加载数据并分块。
    limit=0 表示不限制。
    """
    path = Path(jsonl_path)
    if not path.exists():
        logger.warning(f"数据文件不存在: {jsonl_path}")
        return []

    raw_data = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw_data.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            if limit > 0 and len(raw_data) >= limit:
                break

    splitter = create_splitter(chunk_size, chunk_overlap)
    docs = chunk_documents(raw_data, splitter)
    logger.info(f"分块完成: {len(raw_data)} 条原始数据 → {len(docs)} 个块")
    return docs
