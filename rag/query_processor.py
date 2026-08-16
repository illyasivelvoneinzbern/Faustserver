"""
查询预处理模块：意图分类 + 查询改写 + 穷举列表检测 + 饰品结构化过滤。

职责：
1. 分析用户查询意图 → 输出 ChromaDB filter（page_type 过滤）
2. 对缺少角色名的对话式查询 → 自动补全角色名改写为百科检索式
3. 检测人格/EGO 穷举列表查询 → 触发 Parent-Child 元数据直查
4. 解析饰品查询中的效果类型和稀有度 → 生成 ChromaDB $and 结构化过滤
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Wiki 饰品效果类型映射：查询关键词 → Wiki 分类值 ──
# 来源：Data:Giftchoose.tabx 中 "效果类型" 字段的可选值
EFFECT_KEYWORD_MAP: dict[str, str] = {
    "呼吸": "呼吸法",
    "呼吸法": "呼吸法",
    "烧伤": "烧伤",
    "流血": "流血",
    "破裂": "破裂",
    "震颤": "震颤",
    "沉沦": "沉沦",
    "充能": "充能",
    "泛用": "泛用",
    "钉子": "流血",
    "破裂": "破裂",
    "灼伤": "烧伤",
}
# 编译效果类型匹配正则（按长度降序排列，优先匹配长关键词）
_EFFECT_KEYS_SORTED = sorted(EFFECT_KEYWORD_MAP.keys(), key=len, reverse=True)
_EFFECT_RE = re.compile("|".join(re.escape(k) for k in _EFFECT_KEYS_SORTED))

# ── 稀有度映射：查询关键词 → 稀有度数值 ──
RARITY_KEYWORD_MAP: dict[str, int] = {
    "零级": 0, "0级": 0,
    "一级": 1, "1级": 1,
    "二级": 2, "2级": 2,
    "三级": 3, "3级": 3,
    "四级": 4, "4级": 4,
    "五级": 5, "5级": 5,
    "六级": 6, "6级": 6,
}
_RARITY_RE = re.compile(
    r"([零一二三四五六\d])\s*级"
)

# ── 社区昵称/俗称 → 正式 Wiki 人格标题 ──
# 按字符数降序排列以保证优先级（长昵称优先于短昵称）
# 格式: 昵称(不含角色名后缀) → (角色名, 正式人格标题)
NICKNAME_MAP: dict[str, tuple[str, str]] = {}
for _kw, _name, _title in sorted([
    ("兔浮",     "浮士德",   "浮士德黑兽-卯魁首"),
    ("黑兽卯魁首", "浮士德", "浮士德黑兽-卯魁首"),
    ("黑兽-卯魁首", "浮士德", "浮士德黑兽-卯魁首"),
    ("卯魁首浮", "浮士德", "浮士德黑兽-卯魁首"),
    ("黑兽浮士德", "浮士德", "浮士德黑兽-卯魁首"),
    ("鸡夫",     "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("酉魁首夫", "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("黑兽希斯克利夫", "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("马箱",     "李箱",     "李箱黑兽-午魁首"),
    ("午魁首箱", "李箱",     "李箱黑兽-午魁首"),
    ("黑兽李箱", "李箱",     "李箱黑兽-午魁首"),
    ("兔奥",     "奥提斯",   "奥提斯黑兽-卯"),
    ("兔良",     "良秀",     "良秀黑兽-卯"),
    ("鸡辛",     "辛克莱",   "辛克莱黑兽-酉"),
    ("蛇虫",     "格里高尔", "格里高尔黑兽-巳"),
    ("蛇罗",     "罗佳",     "罗佳黑兽-巳"),
    ("羊堂",     "堂吉诃德", "堂吉诃德黑兽-未"),
    ("黑兽浮",   "浮士德",   "浮士德黑兽-卯魁首"),
    ("黑兽夫",   "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("W浮",      "浮士德",   "浮士德W公司2级清扫人员"),
    ("W箱",      "李箱",     "李箱W公司3级清扫人员"),
    ("W唐",      "堂吉诃德", "堂吉诃德W公司3级清扫人员"),
    ("W夫",      "希斯克利夫", "希斯克利夫W公司4级清扫人员-CCA"),
    ("N夫",      "希斯克利夫",   "希斯克利夫N公司中锤"),
    ("N唐",      "堂吉诃德", "堂吉诃德N公司中锤"),
    ("N罗",      "罗佳",     "罗佳N公司中锤"),
    ("黎明浮",   "浮士德",   "浮士德黎明事务所收尾人"),
    ("黎明辛",   "辛克莱",   "辛克莱黎明事务所收尾人"),
    ("黎明格",   "格里高尔", "格里高尔黎明事务所代表"),
    ("裂纹浮",   "浮士德",   "浮士德多裂纹事务所代表"),
    ("裂纹夫",   "希斯克利夫", "希斯克利夫多裂纹事务所收尾人"),
    ("剑契浮",   "浮士德",   "浮士德剑契组杀手"),
    ("剑契箱",   "李箱",     "李箱剑契组杀手"),
    ("剑契唐",   "堂吉诃德", "堂吉诃德剑契组杀手"),
    ("剑契奥",   "奥提斯",   "奥提斯剑契组杀手"),
    ("剑契辛",   "辛克莱",   "辛克莱剑契组杀手"),
    ("剑契默",   "默尔索",   "默尔索剑契组头领"),
    ("船长玛",   "以实玛利", "以实玛利裴廓德号船长"),
    ("大副箱",   "李箱",     "李箱裴廓德号大副"),
    ("鱼叉夫",   "希斯克利夫", "希斯克利夫裴廓德号鱼叉手"),
    ("六玛",     "以实玛利", "以实玛利六协会南部4科"),
    ("六罗",     "罗佳",     "罗佳六协会南部4科科长"),
    ("六箱",     "李箱",     "李箱六协会南部3科"),
    ("六格",     "格里高尔", "格里高尔六协会南部6科"),
    ("六墨",     "默尔索",   "默尔索六协会南部6科"),
    ("中指唐",   "堂吉诃德", "堂吉诃德中指幼妹"),
    ("中指玛",   "以实玛利", "以实玛利蜘蛛巢中指子辈"),
    ("中指希",   "希斯克利夫", "希斯克利夫中指幼兄"),
    ("食指箱",   "李箱",     "李箱蜘蛛巢食指父辈"),
    ("拇指希",   "希斯克利夫", "希斯克利夫蜘蛛巢拇指子辈"),
    ("雷横墨",   "默尔索",   "默尔索拇指东部指挥官IIII"),
    ("环指奥",   "奥提斯",   "奥提斯环指点彩派学徒"),
    ("环指浮",   "浮士德",   "浮士德蜘蛛巢环指子辈"),
    ("环指箱",   "李箱",     "李箱环指点彩派学徒"),
    ("环指罗",   "罗佳",     "罗佳环指野兽派讲解员"),
    ("玫瑰虫",   "格里高尔", "格里高尔玫瑰扳手工坊收尾人"),
    ("N浮",   "浮士德",   "浮士德执柄者"),
    ("N辛",   "辛克莱",   "辛克莱准执柄者"),
    ("赤瞳良",   "良秀",     "良秀脑叶公司E.G.O::赤瞳·忏悔"),
    ("厨良",   "良秀",     "良秀良·派厨师长"),
    ("狂猎希",   "希斯克利夫", "希斯克利夫狂猎"),
    ("驯鹿玛",   "以实玛利", "以实玛利R公司第四集团军驯鹿队"),
    ("犀牛默",   "默尔索",   "默尔索R公司第四集团军犀牛队"),
    ("管家浮",   "浮士德",   "浮士德呼啸山庄管家"),
    ("管家奥",   "奥提斯",   "奥提斯呼啸山庄首席管家"),
    ("总督唐",   "堂吉诃德", "堂吉诃德拉·曼却领总督"),
    ("公主罗",   "罗佳",     "罗佳拉·曼却领公主"),
    ("理发师奥", "奥提斯",   "奥提斯拉·曼却领理发师"),
    ("神父虫",   "格里高尔", "格里高尔拉·曼却领神父"),
    ("死兔默",   "默尔索",   "默尔索死兔帮老大"),
    ("七浮",     "浮士德",   "浮士德Seven协会南部4科"),
    ("七协浮",   "浮士德",   "浮士德Seven协会南部4科"),
    ("七协浮士德", "浮士德", "浮士德Seven协会南部4科"),
    ("七协南部4科浮", "浮士德", "浮士德Seven协会南部4科"),
    ("七协南4浮", "浮士德", "浮士德Seven协会南部4科"),
    ("七奥",     "奥提斯",   "奥提斯Seven协会南部6科科长"),
    ("七协奥",   "奥提斯",   "奥提斯Seven协会南部6科科长"),
    ("七协奥提斯", "奥提斯", "奥提斯Seven协会南部6科科长"),
    ("七夫",     "希斯克利夫", "希斯克利夫Seven协会南部4科"),
    ("七协夫",   "希斯克利夫", "希斯克利夫Seven协会南部4科"),
    ("七箱",     "李箱",     "李箱Seven协会南部6科"),
    ("七协箱",   "李箱",     "李箱Seven协会南部6科"),
    ("小唐",     "堂吉诃德", "堂吉诃德"),
    ("九罗",     "罗佳",     "罗佳Девять协会北部3科"),
    ("十箱",     "李箱",     "李箱Dieci协会南部4科"),
    ("十罗",     "罗佳",     "罗佳Dieci协会南部4科"),
    ("十墨",     "默尔索",   "默尔索Dieci协会南部4科科长"),
    ("鸿璐",     "鸿潞",     "鸿潞"),
    ("宝子",     "鸿潞",     "鸿潞"),
    ("虫叔",     "格里高尔", "格里高尔"),
    ("小夫",     "希斯克利夫", "希斯克利夫"),
    # ── 黑兽十二生肖剩余变体（无后缀直接称呼）──
    ("黑兽卯",   "浮士德",   "浮士德黑兽-卯魁首"),
    ("黑兽兔",   "浮士德",   "浮士德黑兽-卯魁首"),
    ("黑兽酉",   "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("黑兽午",   "李箱",     "李箱黑兽-午魁首"),
    ("黑兽未",   "堂吉诃德", "堂吉诃德黑兽-未"),
    ("黑兽巳",   "罗佳",     "罗佳黑兽-巳"),
    ("卯魁首",   "浮士德",   "浮士德黑兽-卯魁首"),
    ("酉魁首",   "希斯克利夫", "希斯克利夫黑兽-酉魁首"),
    ("午魁首",   "李箱",     "李箱黑兽-午魁首"),
    # ── 七协其余角色 ──
    ("七协唐",   "堂吉诃德", "堂吉诃德Seven协会南部4科"),
    ("七协罗",   "罗佳",     "罗佳Seven协会南部6科"),
    ("七协良",   "良秀",     "良秀Seven协会南部2科"),
    ("七协默",   "默尔索",   "默尔索Seven协会南部1科"),
    # ===== NPC/世界观角色 =====
    ("猩红凝视", "维吉里乌斯", "维吉里乌斯"),
    ("猩红视线", "维吉里乌斯", "维吉里乌斯"),
    ("红色凝视", "维吉里乌斯", "维吉里乌斯"),
], key=lambda x: len(x[0]), reverse=True):
    NICKNAME_MAP[_kw] = (_name, _title)
del _kw, _name, _title

# ── 人格标题词表（Fix A：直接标题/可靠片段 → 正式人格标题）──
# 用于从查询中提取「具体人格名」，对 page_title/personality_name 精确匹配
# 该人格名的 chunk 追加高 boost（而非 persona 级统一加分）。
# 仅收录可唯一确定人格的可靠片段（取自已验证的 NICKNAME_MAP 正式标题）。
# 注意：键需按特异性/长度排前，避免短片段误吞长片段（如 "卯魁首" ⊆ "黑兽-卯魁首"）。
PERSONALITY_TITLE_KEYWORDS: dict[str, str] = {
    # 黑兽十二生肖（魁首级仅对应单一角色）
    "黑兽-卯魁首": "浮士德黑兽-卯魁首",
    "黑兽卯魁首": "浮士德黑兽-卯魁首",
    "卯魁首": "浮士德黑兽-卯魁首",
    "黑兽-酉魁首": "希斯克利夫黑兽-酉魁首",
    "黑兽酉魁首": "希斯克利夫黑兽-酉魁首",
    "酉魁首": "希斯克利夫黑兽-酉魁首",
    "黑兽-午魁首": "李箱黑兽-午魁首",
    "黑兽午魁首": "李箱黑兽-午魁首",
    "午魁首": "李箱黑兽-午魁首",
    # 机构/事务所（唯一对应；值必须为带罪人名前缀的完整正式标题）
    # 注意：不收录歧义片段（如 "W公司2级清扫人员" 对应浮士德/鸿璐/默尔索三人，
    #       无法唯一确定 → 交由 NICKNAME_MAP 完整标题或 RAG 处理，避免返回残缺标题）。
    "黎明事务所代表": "格里高尔黎明事务所代表",
    "多裂纹事务所代表": "浮士德多裂纹事务所代表",
    "呼啸山庄管家": "浮士德呼啸山庄管家",
    "狂猎": "希斯克利夫狂猎",
    "死兔帮老大": "默尔索死兔帮老大",
}


# ── LCB 初始罪人（12 人）──
# 向量库标题格式为 "{罪人名}LCB罪人"（无空格，见 diag_explore_lcb_titles 结果）。
# 用于识别 "X lcb罪人 / X LCB罪人 / X lcb" 查询（大小写不敏感、动态构造标题，
# 覆盖所有罪人，而非仅补李箱一条）。
LCB_SINNERS: tuple[str, ...] = (
    "以实玛利", "堂吉诃德", "奥提斯", "希斯克利夫", "李箱",
    "格里高尔", "浮士德", "罗佳", "良秀", "辛克莱", "鸿璐", "默尔索",
)
# 罪人名按长度降序（长名优先，避免短名误吞长名，如 "李箱" ⊆ "李箱" 无害但
# "希斯克利夫" 需在 "希斯" 类短名之前）
_LCB_SINNERS_SORTED: tuple[str, ...] = tuple(
    sorted(LCB_SINNERS, key=len, reverse=True)
)
# 罪人别名 → 向量库规范罪人名（NICKNAME_MAP 中 "鸿璐"→"鸿潞"，而向量库标题为
# "鸿璐LCB罪人"，需映射回规范名，避免 LCB 识别被 NICKNAME_MAP 别名抢占）
_LCB_SINNER_ALIASES: dict[str, str] = {
    "鸿潞": "鸿璐",
}
# LCB 标记：匹配 lcb / LCB / LcB 等任意大小写
_LCB_TAG_RE = re.compile(r"lcb", re.IGNORECASE)
# "罪人名 + lcb[罪人]" 模式（允许中间空白/大小写变化）
_LCB_TITLE_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})\s*(?:lcb\s*罪人|罪人lcb)",
    re.IGNORECASE,
)


def _detect_lcb_sinner(query: str) -> Optional[str]:
    """从查询中检测 LCB 初始罪人人格所指的罪人名（大小写不敏感）。

    例：
        "李箱 lcb罪人的技能是？" → "李箱"
        "浮士德LCB罪人 介绍"     → "浮士德"
        "堂吉诃德 lcb 是谁"      → "堂吉诃德"
        "lcb罪人 有哪些"         → None（泛指，未指定具体罪人）

    Returns:
        与向量库标题 "{罪人名}LCB罪人" 对应的罪人名；未识别返回 None。
    """
    if not query or not _LCB_TAG_RE.search(query):
        return None
    # 0) 别名归一化：若查询出现罪人别名（如 "鸿潞"），先映射回规范名
    #    （别名在 NICKNAME_MAP 中可能抢先匹配，故需在此显式映射）
    norm_query = query
    for alias, canon in _LCB_SINNER_ALIASES.items():
        if alias in query:
            norm_query = query.replace(alias, canon)
    # 1) 罪人名 + lcb[罪人] 模式（允许空白/大小写变化）
    m = _LCB_TITLE_RE.search(norm_query)
    if m and m.group(1) in LCB_SINNERS:
        return m.group(1)
    # 2) 兜底：查询同时含 lcb 标记与任一罪人名（如 "堂吉诃德 lcb"）
    for s in _LCB_SINNERS_SORTED:
        if s in norm_query:
            return s
    return None


def extract_personality_name(query: str) -> Optional[str]:
    """从查询中提取「具体人格名」（正式 Wiki 人格标题），供人格名级 boost/锁定。

    优先级：
    1. NICKNAME_MAP 昵称/全名变体匹配（如 "兔浮"、"黑兽卯魁首"、"七协浮士德"）
    2. 标题词表 PERSONALITY_TITLE_KEYWORDS 的可靠片段匹配
    3. NICKNAME_MAP 中已知正式标题出现在查询原文（长标题优先，覆盖
       "浮士德黑兽-卯魁首的技能组" 这类直接带正式标题的查询）

    Returns:
        正式人格标题（如 "浮士德黑兽-卯魁首"）；未识别到返回 None。
    """
    if not query:
        return None

    # 0) LCB 初始罪人人格优先（Fix G：大小写不敏感、动态构造正式标题，覆盖所有罪人）
    #    如 "李箱 lcb罪人的技能是？" → "李箱LCB罪人"。
    #    放在 NICKNAME_MAP 之前：部分罪人（如 "鸿璐"）在 NICKNAME_MAP 有别名条目
    #    （"鸿璐"→"鸿潞"），若不优先会被别名抢占、无法构造与向量库一致的 LCB 标题。
    #    仅含 "lcb" 标记的查询才会命中，不影响普通查询。
    lcb_sinner = _detect_lcb_sinner(query)
    if lcb_sinner:
        title = f"{lcb_sinner}LCB罪人"
        logger.info(f"人格名提取(LCB): 罪人={lcb_sinner} → 标题='{title}'")
        return title

    # 1) NICKNAME_MAP 匹配（长键优先，含 Fix C 补全的全名/变体；大小写不敏感）
    #    "w夫" → "W夫"，"n浮" → "N浮" 等用户输入大小写差异不再漏识别。
    q_lower = query.lower()
    for nickname, (_char, title) in sorted(
        NICKNAME_MAP.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if nickname.lower() in q_lower:
            return title

    # 2) 标题词表片段匹配（键特异性由书写顺序保证）
    for fragment, title in PERSONALITY_TITLE_KEYWORDS.items():
        if fragment in query:
            return title

    # 3) 已知正式标题出现在查询原文（长标题优先；排除裸角色名）
    known_titles = sorted(
        {t for (_c, t) in NICKNAME_MAP.values() if t and t != _c},
        key=len,
        reverse=True,
    )
    for title in known_titles:
        if title in query:
            return title

    # 4) LCB 初始罪人人格（Fix G：大小写不敏感、动态构造正式标题，覆盖所有罪人）
    #    如 "李箱 lcb罪人的技能是？" → "李箱LCB罪人"
    #    NICKNAME_MAP/PERSONALITY_TITLE_KEYWORDS 中无 lcb 条目（大小写敏感）导致
    #    此前识别失败，这里兜底并与向量库 "{罪人名}LCB罪人" 标题精确对齐。
    lcb_sinner = _detect_lcb_sinner(query)
    if lcb_sinner:
        title = f"{lcb_sinner}LCB罪人"
        logger.info(f"人格名提取(LCB): 罪人={lcb_sinner} → 标题='{title}'")
        return title

    return None

# ── 意图规则 ──
# 每条: (正则, page_type, 改写模板或 None, is_listing)
#   page_type:   "character" | "personality" | "plot" | "other" | None (不过滤)
#   rewrite:     改写模板，{name} 会被替换为角色名；None 表示不改写
#   is_listing:  是否为穷举/列表类查询（触发 Parent-Child 元数据直查）
INTENT_RULES: list[tuple[str, Optional[str], Optional[str], bool]] = [
    # ── 角色自身信息 → character ──
    (r"(你是谁|介绍自己|自我介绍|你是[谁什么])",
     "character", "{name} 角色 介绍 身份 背景", False),

    # ── 人格技能组查询 → personality（在通用"技能"规则之前） ──
    # 覆盖"技能组/技能列表/技能有哪些/技能是什么/技能是/技能为/技能有"，
    # 以及"有什么技能/有哪些技能/什么样的技能"（"有/什么"在"技能"前）等问法，
    # 避免落入下方通用"技能"规则被误路由到 character（只检索到角色介绍页，无技能数据）
    (r"(技能组|技能.*(?:列表|哪些|什么|是|为|有)|(?:有什么|有哪些|什么|哪些).{0,3}?技能)",
     "personality", None, False),

    (r"(身高|体重|多高|多重|体型|外貌|长相|年龄|多大|几岁|身材)",
     "character", "{name} 身高 体重 基本数据", False),

    (r"(擅长|能力|会什么|能做什么|专长|本领|技能|弱点|战斗[方式风])",
     "character", "{name} 擅长 能力", False),

    (r"(性格|脾气|个性|爱好|兴趣|喜欢什么|讨厌什么|喜好|厌恶)",
     "character", "{name} 性格 特点 爱好", False),

    (r"(身份|职位|角色|定位|罪人编号|第几号)",
     "character", "{name} 身份 职位 罪人编号", False),

    (r"(名字|叫什么|称呼|姓名)",
     "character", "{name} 姓名 称呼", False),

    # ── 人格穷举/列表查询 → personality + is_listing（触发 Parent-Child 元数据直查） ──
    (r"(人格.*有哪|有哪些.*人格|人格.*列表|所有人格|人格.*哪些|人格.*几个|人格.*多少)",
     "personality", "{name} 人格 列表", True),

    # ── EGO 穷举/列表查询 → ego + is_listing（触发 Parent-Child 元数据直查） ──
    (r"(E\.?G\.?O.*有哪|有哪些.*E\.?G\.?O|E\.?G\.?O.*列表|E\.?G\.?O.*哪些|E\.?G\.?O.*几个|E\.?G\.?O.*多少|ego.*有哪|有哪些.*ego|ego.*列表|ego.*哪些|ego.*几个|ego.*多少)",
     "ego", "{name} EGO 列表", True),

    # ── 普通人格查询 → personality ──
    (r"(人格|异想体)",
     "personality", "{name} 人格", False),

    # ── 普通 EGO 查询 → ego（注意：列表规则已在上方优先匹配） ──
    (r"(E\.G\.O|ego|EGO)",
     "ego", "{name} EGO", False),

    # ── 剧情查询 → plot ──
    (r"(剧情|故事|经历|背景|过往|回忆|发生过|过去|往事)",
     "plot", "{name} 剧情", False),

    # ── 饰品查询 → accessory（E.G.O饰品页面） ──
    (r"(饰品|明镜)",
     "accessory", None, False),

    # ── 角色介绍/这个人查询 → character ──
    (r"(这个人|讲讲|介绍.*一下|说说|是谁|什么人)",
     "character", "{name} 角色 介绍 身份 背景", False),

    # ── 通用 Lore → 不过滤也不改写 ──
    (r"(梅菲斯托|巴士|引擎|都市|协会|公司|帮派|巢|郊区|世界[观设]|收尾人|工坊|异想)",
     None, None, False),

    # ── 战斗/关卡 → other ──
    (r"(战斗|技能|关卡|BOSS|敌人|道具|装备|主线|章节|纺锤|折射|轨道)",
     "other", None, False),
]

# ── 敌方单位名索引（懒加载，供 classify_intent 路由 enemy 意图；Fix P16-C）──
# 此前敌方名查询（如"雷横的技能/弱点"）被上方通用 character 规则（技能/弱点等）
# 误路由到 page_type=character，敌方分块（page_type=enemy）被直接排除而检索不到。
# 此处对查询做敌方名包含匹配，命中即路由到 enemy，确保 RAG 检索不排除敌方分块。
# 注意：_expand_nickname 在 classify_intent 之前执行，"雷横墨"（默尔索人格昵称）
# 已被展开替换，不会与敌方名"雷横"混淆。
_ENEMY_NAMES_CACHE: Optional[set[str]] = None


def _norm_enemy_name(name: str) -> str:
    """去除敌方名中的空格/破折号/间隔号，用于跨空白差异的模糊匹配。

    例："食指 父辈 - 里恩（第一阶段）" → "食指父辈-里恩（第一阶段）"
    （仅移除空白与连字符类，保留中文括号，保证去空格后仍可逆解析）
    """
    return name.replace(" ", "").replace("·", "")


def _bare_enemy_name(name: str) -> str:
    """剥离组织前缀与括号内阶段后缀，得到纯裸名。

    例："食指 父辈 - 里恩（第一阶段）" → "里恩"
        "环指 父辈 - 卡利斯托"           → "卡利斯托"
    """
    bare = name.split(" - ")[-1].strip()
    bare = re.sub(r"[（(].*?[)）]", "", bare).strip()
    return bare


def _load_enemy_names() -> set[str]:
    """懒加载敌方单位名集合（data/structured/enemies/enemy_*.json 的 enemy_name 字段）。

    修复 P21-A：敌方名常带组织前缀（如"环指 父辈 - 卡利斯托"），裸名查询
    （"卡利斯托"）无法命中。此处同时注册去前缀别名（取" - "末段，如"卡利斯托"），
    使裸名也能命中路由。

    修复 P23：敌方名常含空格/破折号/阶段后缀（如"食指 父辈 - 里恩（第一阶段）"），
    用户口语查询（"食指父辈里恩"）因缺空格/破折号/阶段无法命中。此处额外注册：
    - ::裸名        去前缀（保留阶段后缀）别名，如 "::里恩（第一阶段）"
    - ::no:...      去空格规范化别名（真实名/裸名均注册）
    - ::no_bare:... 去空格 + 剥阶段后缀的纯裸名别名（如 "::no_bare:里恩"）

    别名统一用 :: 前缀形式（不与真实名冲突，真实名不含 "::"）。
    """
    global _ENEMY_NAMES_CACHE
    if _ENEMY_NAMES_CACHE is not None:
        return _ENEMY_NAMES_CACHE
    names: set[str] = set()
    try:
        from crawler.structured_exporter import load_enemy_index
        index = load_enemy_index("data/structured")
        for rec in index.values():
            n = (rec.get("enemy_name") or "").strip()
            if not n:
                continue
            names.add(n)
            # 注册去前缀别名：形如 "环指 父辈 - 卡利斯托" → "::卡利斯托"
            # 取 " - " 分隔的末段；无分隔则不注册（避免冗余）
            if " - " in n:
                alias = n.split(" - ")[-1].strip()
                if alias and alias != n:
                    names.add(f"::{alias}")
            # P23：注册无括号纯裸名别名："::里恩"（剥阶段后缀）
            bare = _bare_enemy_name(n)
            if bare and bare != n:
                names.add(f"::{bare}")
            # P23：注册去空格规范化别名："::no:食指父辈-里恩（第一阶段）"
            norm = _norm_enemy_name(n)
            if norm and norm != n:
                names.add(f"::no:{norm}")
            # P23：注册去空格 + 纯裸名别名："::no_bare:里恩"
            norm_bare = _norm_enemy_name(bare)
            if norm_bare and norm_bare != n:
                names.add(f"::no_bare:{norm_bare}")
    except Exception as e:
        logger.warning(f"加载敌方单位名索引失败: {e}")
    _ENEMY_NAMES_CACHE = names
    return names


def _detect_enemy_name(query: str) -> Optional[str]:
    """检测查询中是否提及某个敌方单位名；命中返回该名字（多候选取最长）。

    修复 P21-A：支持双向匹配（n in q 或 q in n），使裸名（"卡利斯托"）能命中
    带前缀名（"环指 父辈 - 卡利斯托"）；别名（::裸名）命中时解析回真实名。

    修复 P23：
    - 增加去空格规范化匹配：查询去空格后与规范化真实名/别名双向包含，
      使 "食指父辈里恩数据" 命中 "食指 父辈 - 里恩（第一阶段）"。
    - 反向匹配（q in n）时过滤过短/泛化片段（如 "第一阶段"、"父辈" 等），
      避免误判为敌方名（此前 "食指父辈 - 里恩（第一阶段）数据" 被误判为
      "第一阶段"，导致直答返回无关敌人）。
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None

    # 泛化词黑名单：反向匹配（q in n）时这些过短/泛化片段不算敌方名命中，
    # 防止 "第一阶段"、"父辈" 等被当作敌方名路由/直答（P23 误匹配修复）。
    _GENERIC_SEGMENTS = {
        "第一阶段", "第二阶段", "第三阶段", "第四阶段", "第五阶段",
        "父辈", "长辈", "子辈", "士兵", "工人", "清扫人员",
    }

    candidates: list[tuple[str, str]] = []  # (真实名, 匹配文本)
    generic_candidates: list[tuple[str, str]] = []  # 泛化词命中（P23 兜底，防误匹配）
    alias_hit: Optional[str] = None  # 别名命中时记录（仅作路由信号）
    names = _load_enemy_names()

    # 第一遍：精确/双向包含匹配（真实名）
    for n in names:
        if not n or n.startswith("::"):
            continue
        if n in q:
            # P23：泛化词命中（如"第一阶段"）不阻塞后续更具体匹配，仅作兜底，
            # 避免 "食指父辈 - 里恩（第一阶段）数据" 被正向命中"第一阶段"误判。
            if n in _GENERIC_SEGMENTS:
                generic_candidates.append((n, n))
            else:
                candidates.append((n, n))
        elif q in n and len(q) >= 2 and q not in _GENERIC_SEGMENTS:
            candidates.append((n, q))

    # 第二遍：去空格规范化匹配（P23）
    # 查询去空格后与真实名去空格比较，解决 "食指父辈里恩" vs "食指 父辈 - 里恩（第一阶段）"
    if not candidates:
        q_no = _norm_enemy_name(q)
        if len(q_no) >= 2:
            for n in names:
                if not n or n.startswith("::"):
                    continue
                n_no = _norm_enemy_name(n)
                if not n_no:
                    continue
                if n_no in q_no:
                    if n in _GENERIC_SEGMENTS:
                        generic_candidates.append((n, n_no))
                    else:
                        candidates.append((n, n_no))
                elif q_no in n_no and q_no not in _GENERIC_SEGMENTS:
                    candidates.append((n, q_no))

    # 第三遍：纯裸名别名/规范化别名反向匹配（P23）
    # 命中 "::里恩" / "::no_bare:里恩" 等别名时记录路由信号，继续收集真实名
    if not candidates:
        q_no = _norm_enemy_name(q)
        for n in names:
            if not n or not n.startswith("::"):
                continue
            if n.startswith("::no:"):
                bare = n[5:]
            elif n.startswith("::no_bare:"):
                bare = n[11:]
            else:
                bare = n[2:]
            if not bare:
                continue
            # 别名命中：去空格别名需对去空格查询匹配；普通别名对原查询匹配
            if n.startswith("::no:"):
                qq = q_no
            else:
                qq = q
            if bare and bare in qq and bare not in _GENERIC_SEGMENTS:
                if alias_hit is None or len(bare) > len(alias_hit):
                    alias_hit = bare

    if candidates:
        # 优先完全相等；否则取匹配文本最长（裸名反向命中时 q 即最长）
        candidates.sort(key=lambda t: (t[1] == q, len(t[1])), reverse=True)
        return candidates[0][0]
    # 仅别名命中：返回裸名作为 enemy 路由信号（classify_intent 只据此设
    # page_type=enemy，不用于精确检索；真实名匹配由 enemy_direct 直答处理）。
    return alias_hit

# ── 角色名提取模式：用于从查询中识别目标角色
# 当意图为 personality/EGO listing 且未从 persona_name 获取角色名时，
# 尝试从查询文本中提取（如"浮士德的人格有哪些" → "浮士德"）
_TARGET_CHARACTER_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})(?:的|之)?(?:人格|E\.?G\.?O|异想体)"
)

# ── 列表查询宽松角色名提取（"浮士德有哪些ego" → "浮士德"）
# 当 _TARGET_CHARACTER_RE 未命中但 is_listing=True 时作为 fallback
_LISTING_CHAR_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})(?:的|之)?.*?(?:有哪些|有哪|列出|列表|几个|多少)"
)

# ── 章节层级识别（剧情页面编号 → 章/节/小节）──
# 匹配：
#   1) 点分式剧情编号：8-33-06 / 8-33 / 1-10
#      （用 (?<!\d)/(?!\d) 而非 \b，避免 CJK 与数字间无词边界导致漏匹配，如 "主线战斗1-10"）
#   2) 中文口语：第八章 / 第8章 / 8章（含中文数字 第八/十二章）
_CHAPTER_CN_RE = re.compile(r"(?:第)?([0-9零一二三四五六七八九十]+)\s*[章節节]")
_CHAPTER_LINE_RE = re.compile(r"(?<!\d)(\d+-\d+(?:-\d+)?)(?!\d)")

# 中文数字 → 阿拉伯数字（支持个位与"十/十一/二十/二十一"式）
_CN_NUM: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number_to_int(s: str) -> Optional[int]:
    """将中文数字串转为整数（支持 一~九 / 十 / 十一~十九 / 二十 / 二十一 等）。"""
    if not s:
        return None
    if s in _CN_NUM:
        return _CN_NUM[s]
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


def parse_chapter_reference(query: str) -> Optional[str]:
    """从查询中提取剧情层级目标（章/节/小节），返回最具体的编号前缀。

    规则（自上而下取最具体）：
    - "8-33-06 讲了什么" → "8-33-06"（叶子页面）
    - "8-33 有哪些关卡" → "8-33"（节）
    - "第八章讲了什么"   → "8"（章）

    Returns:
        规范化后的章节编号前缀（如 "8"、"8-33"、"8-33-06"）；
        未识别到返回 None。
    """
    # 1) 点分式编号（优先级最高，最具体）
    m = _CHAPTER_LINE_RE.search(query)
    if m:
        prefix = m.group(1)
        logger.debug(f"章节层级识别(点分式): '{prefix}'")
        return prefix

    # 2) 中文口语 "第八章" / "第8章" / "8章"（章级，含中文数字）
    m = _CHAPTER_CN_RE.search(query)
    if m:
        raw = m.group(1)
        if raw.isdigit():
            prefix = str(int(raw))  # 去前导零
        else:
            prefix = str(_cn_number_to_int(raw) or 0)
        logger.debug(f"章节层级识别(中文章): '第{raw}章' → '{prefix}'")
        return prefix

    return None


def _parse_accessory_constraints(query: str) -> tuple[Optional[str], Optional[int]]:
    """从饰品查询中解析效果类型和稀有度约束。

    Args:
        query: 用户原始查询字符串

    Returns:
        (effect_value, rarity_int) — 未匹配到返回 (None, None)
    """
    effect_value: Optional[str] = None
    rarity_value: Optional[int] = None

    # 1. 匹配效果类型（按长度降序优先级）
    m_effect = _EFFECT_RE.search(query)
    if m_effect:
        keyword = m_effect.group()
        effect_value = EFFECT_KEYWORD_MAP.get(keyword)

    # 2. 匹配稀有度
    m_rarity = _RARITY_RE.search(query)
    if m_rarity:
        raw = m_rarity.group(1)
        rarity_value = RARITY_KEYWORD_MAP.get(f"{raw}级")

    if effect_value or rarity_value is not None:
        logger.debug(
            f"饰品结构化约束解析: effect={effect_value}, rarity={rarity_value} "
            f"(query='{query[:40]}...')"
        )

    return effect_value, rarity_value


def classify_intent(query: str) -> dict:
    """
    分析查询意图，返回 filter、改写后查询、是否穷举列表。

    Returns:
        {
            "rewritten_query": str,     # 改写后的查询
            "filter": dict | None,      # ChromaDB where 过滤条件
            "page_type": str,           # 意图类型（调试用）
            "rewrite_template": str | None,
            "is_listing": bool,         # 是否为穷举/列表查询
            "target_character": str | None,  # 从查询中提取的目标角色名
            "target_chapter": str | None,    # 从查询中提取的章节层级编号（章/节/小节）
            "effect_filter": str | None,     # 饰品效果类型约束（Wiki 分类值）
            "rarity_filter": int | None,     # 饰品稀有度约束（0~6）
        }
    """
    # 章节层级目标：无论匹配哪个意图规则，均尝试提取剧情编号（如 8-33-06 / 8-33 / 第八章）
    target_chapter = parse_chapter_reference(query)

    # ── 敌方单位名检测（Fix P16-C）──
    # 优先于通用意图规则：命中敌方名（如"雷横的技能""雷横弱点"）时路由到
    # page_type=enemy，避免被上方通用 character 规则（技能/弱点等）误路由到
    # character 而排除敌方分块。同时非穷举查询由 enemy_direct 直答短路。
    enemy_name = _detect_enemy_name(query)
    if enemy_name:
        # 敌方穷举/列表查询仍回落 RAG 列表检索（enemy_direct 也会跳过）
        is_enemy_listing = bool(re.search(r"(敌人|敌方|怪|boss|BOSS).*(有哪|有哪些|列表|几个|多少|什么)|(有哪|有哪些|列表).*(敌人|敌方|怪|boss|BOSS)", query))
        return {
            "rewritten_query": query,
            "rewrite_template": None,
            "filter": {"page_type": "enemy"},
            "page_type": "enemy",
            "is_listing": is_enemy_listing,
            "target_character": None,
            "target_chapter": target_chapter,
            "effect_filter": None,
            "rarity_filter": None,
        }

    for pattern, page_type, rewrite_template, is_listing in INTENT_RULES:
        if re.search(pattern, query):
            filter_dict = None
            effect_filter: Optional[str] = None
            rarity_filter: Optional[int] = None

            if page_type == "accessory":
                # ── 饰品查询：Chromadb 侧仅做 page_type 过滤 ──
                # effect/rarity 稀疏字段的 WHERE 过滤在 Chromadb 中不可靠
                # （字段仅存在于部分文档时索引失效），改为 Python 侧 post-filter
                effect_filter, rarity_filter = _parse_accessory_constraints(query)
                filter_dict = {"page_type": page_type}
            elif page_type:
                filter_dict = {"page_type": page_type}

            # 尝试从查询中提取目标角色名
            target = None
            if is_listing or page_type in ("character", "personality", "plot"):
                m = _TARGET_CHARACTER_RE.search(query)
                if m:
                    target = m.group(1)
                elif is_listing:
                    # 宽松 fallback：如 "浮士德有哪些ego" 中 _TARGET_CHARACTER_RE 未命中
                    # 因为 "有哪些" 不在 (?:的|之)? 的匹配范围内
                    m2 = _LISTING_CHAR_RE.search(query)
                    if m2:
                        target = m2.group(1)
                # ── Fix G：LCB 罪人查询的角色名提取 ──
                # "李箱 lcb罪人的技能是？" 中 _TARGET_CHARACTER_RE 无法跨过 "lcb罪人"
                # 提取角色名（"技能" 也不在匹配模式内），导致 target 错误回落
                # persona_name（如 '浮士德'）。用 LCB 检测兜底（大小写不敏感、覆盖所有罪人）。
                if not target:
                    lcb_sinner = _detect_lcb_sinner(query)
                    if lcb_sinner:
                        target = lcb_sinner
                        logger.info(f"目标角色提取(LCB): 罪人={lcb_sinner}")

            return {
                "rewritten_query": query,
                "rewrite_template": rewrite_template,
                "filter": filter_dict,
                "page_type": page_type or "通用",
                "is_listing": is_listing,
                "target_character": target,
                "target_chapter": target_chapter,
                "effect_filter": effect_filter,
                "rarity_filter": rarity_filter,
            }

    # 默认：不过滤、不改写
    return {
        "rewritten_query": query,
        "rewrite_template": None,
        "filter": None,
        "page_type": "未知",
        "is_listing": False,
        "target_character": None,
        "target_chapter": target_chapter,
        "effect_filter": None,
        "rarity_filter": None,
    }


def expand_query(query: str, persona_name: Optional[str] = None) -> str:
    """
    对查询进行意图分析和改写。

    如果查询匹配了改写模板且提供了角色名，则用角色名填充模板。
    否则返回原始查询。
    """
    intent = classify_intent(query)
    template = intent.get("rewrite_template")

    if template and persona_name:
        expanded = template.format(name=persona_name)
        logger.debug(f"查询改写: '{query}' → '{expanded}'")
        return expanded

    return query


# ═══════════════════════════════════════════════════════════════════════
# LLM 查询扩展器（Recall 提升 ~15~25%）
# ═══════════════════════════════════════════════════════════════════════

class LLMQueryExpander:
    """使用 LLM 对用户查询进行语义扩展和规范化。

    将用户的口语化/模糊问题改写为 2~3 个适合向量搜索的简洁查询短语。
    集成位置：在 retriever.retrieve() 的 Phase 1 中，_analyze_intent() 之前调用。
    """

    EXPANSION_PROMPT = (
        "你是一个边狱巴士（Limbus Company）Wiki 搜索引擎的查询优化器。\n"
        "将用户的口语化问题改写为 2~3 个适合向量搜索的简洁查询短语。\n"
        "\n"
        "规则：\n"
        "1. 将俗称/昵称转换为正式名称（如\"火系\"→\"烧伤\"、\"兔浮\"→\"浮士德黑兽-卯魁首\"）\n"
        "2. 将模糊描述具体化（如\"那个拿剑的\"→\"剑契组\"）\n"
        "3. 每个查询短语不超过 15 个字\n"
        "4. 输出格式：每个查询一行，不要编号\n"
        "\n"
        "用户问题：{question}\n"
        "搜索短语："
    )

    def __init__(self, llm=None, max_phrases: int = 3, enabled: bool = True):
        self.llm = llm
        self.max_phrases = max_phrases
        self.enabled = enabled

    async def expand(self, question: str) -> list[str]:
        """返回扩展后的多条查询短语，至少包含原问题自身。

        Args:
            question: 用户原始查询

        Returns:
            最多 max_phrases 条查询短语列表
        """
        if not self.enabled or self.llm is None:
            return [question]

        try:
            response = await self.llm.ainvoke(
                self.EXPANSION_PROMPT.format(question=question)
            )
            text = response.content if hasattr(response, "content") else str(response)
            phrases = [p.strip() for p in text.split("\n") if p.strip()]
            # 过滤空白和编号前缀，最多保留 max_phrases 条
            import re
            cleaned = []
            for p in phrases:
                p = re.sub(r'^[\d\-•\.\)、。]\s*', '', p).strip()
                if p and len(p) <= 30:
                    cleaned.append(p)
            if not cleaned:
                return [question]
            logger.debug(f"LLM 查询扩展: '{question[:40]}...' → {cleaned[:self.max_phrases]}")
            return cleaned[:self.max_phrases]
        except Exception as e:
            logger.warning(f"LLM 查询扩展失败，降级为原查询: {e}")
            return [question]

    def expand_sync(self, question: str) -> list[str]:
        """同步版本（用于不支持 async 的场景）"""
        if not self.enabled or self.llm is None:
            return [question]

        try:
            response = self.llm.invoke(
                self.EXPANSION_PROMPT.format(question=question)
            )
            text = response.content if hasattr(response, "content") else str(response)
            phrases = [p.strip() for p in text.split("\n") if p.strip()]
            import re
            cleaned = []
            for p in phrases:
                p = re.sub(r'^[\d\-•\.\)、。]\s*', '', p).strip()
                if p and len(p) <= 30:
                    cleaned.append(p)
            if not cleaned:
                return [question]
            return cleaned[:self.max_phrases]
        except Exception as e:
            logger.warning(f"LLM 查询扩展失败（同步），降级为原查询: {e}")
            return [question]


def _expand_nickname(query: str) -> tuple[str, str | None, str | None]:
    """
    将社区昵称扩展为正式的 Wiki 人格标题 + 目标角色名。

    "兔浮的技能组" → ("浮士德黑兽-卯魁首 技能", "浮士德", "浮士德黑兽-卯魁首")
    "W浮的ego" → ("浮士德W公司2级清扫人员 EGO", "浮士德", "浮士德W公司2级清扫人员")

    Returns:
        (rewritten_query, target_character, personality_title)
        如果未匹配到昵称，返回 (原查询, None, None)
    """
    # ── Fix G：LCB 罪人昵称归一化（优先于 NICKNAME_MAP）──
    # "李箱 lcb罪人的技能是？" 属 LCB 初始罪人查询。
    # 放在 NICKNAME_MAP 之前：部分罪人（如 "鸿璐"）在 NICKNAME_MAP 有别名条目
    # （"鸿璐"→"鸿潞"），若不优先会被别名抢占，无法归一化为与向量库一致的
    # "{罪人}LCB罪人" 标题。仅含 "lcb" 标记的查询才会命中，不影响普通查询。
    lcb_sinner = _detect_lcb_sinner(query)
    if lcb_sinner:
        formal_lcb = f"{lcb_sinner}LCB罪人"
        rewritten = _LCB_TITLE_RE.sub(formal_lcb, query, count=1)
        logger.info(f"昵称展开(LCB): 罪人={lcb_sinner} → '{formal_lcb}'")
        return rewritten, lcb_sinner, formal_lcb

    # 按长度降序遍历（长的优先；大小写不敏感）
    # "w夫" → "W夫"，"n浮" → "N浮" 等大小写差异不再漏识别。
    q_lower = query.lower()
    for nickname, (char_name, formal_title) in NICKNAME_MAP.items():
        if nickname.lower() in q_lower:
            # 用正式标题替换昵称，同时将原来跟随在昵称后的查询意图保留
            # 如 "兔浮的技能组" → "浮士德黑兽-卯魁首 技能组"
            # 由于匹配是大小写不敏感，需用原查询中实际命中的那个片段替换
            # （query.replace 仅匹配完全一致的子串，大小写不同会漏替换），
            # 故先定位实际片段再替换。
            idx = q_lower.find(nickname.lower())
            rewritten = query[:idx] + formal_title + query[idx + len(nickname):]
            # ── Fix F：角色名去重 ──
            # 当用户已写完整角色名 + 昵称（如 "浮士德黑兽-卯魁首的技能是？"）时，
            # 替换后 formal_title 以 char_name 开头，会产生双重前缀
            # （"浮士德浮士德黑兽-卯魁首…"），污染向量查询语义。
            # 通用处理：若替换后以 "char_name+char_name" 开头则删除重复的一个。
            if (
                char_name
                and rewritten.startswith(char_name + char_name)
            ):
                rewritten = rewritten[len(char_name):]
            logger.info(f"昵称展开: '{nickname}' → '{formal_title}' (角色={char_name})")
            return rewritten, char_name, formal_title

    return query, None, None


def get_filter_and_query(
    query: str,
    persona_name: Optional[str] = None,
) -> tuple[str, Optional[dict], bool, Optional[str], Optional[str], Optional[int], Optional[str]]:
    """
    一站式预处理：返回 (改写后的查询, ChromaDB filter, is_listing, target_character, effect_filter, rarity_filter, target_chapter)。

    调用方直接使用返回值进行检索。

    当意图为 character 且提供了 persona_name 时，
    自动叠加 page_title 二级过滤 ($contains)，
    将候选池从 ~1870 缩窄到 ~12 条目标角色的基础页面。

    is_listing=True 时调用方应触发 Parent-Child 元数据直查，
    而非语义搜索。
    """
    # ── 昵称展开：将社区俗称替换为正式 Wiki 人格标题 ──
    # 如 "兔浮的技能组" → "浮士德黑兽-卯魁首 技能组"
    query, nick_char, nick_title = _expand_nickname(query)

    intent = classify_intent(query)

    # ── 目标角色名：昵称展开的角色 > 查询中明确提及的 > persona_name 默认值 ──
    # 如 "堂吉诃德的人格有哪些" → target="堂吉诃德"，忽略 persona_name="浮士德"
    query_target = intent.get("target_character")
    target = nick_char or query_target or persona_name

    # 查询改写：使用实际目标角色名（query_target 或 persona_name）
    rewritten = query
    template = intent.get("rewrite_template")
    if template and target:
        rewritten = template.format(name=target)

    # ── 注意：不在此处叠加 $contains 过滤 ──
    # ChromaDB 的 $contains 操作符对中文/英文均不兼容（经 diag_contains.py 验证），
    # title 匹配改为在 retriever.retrieve() 中做 Python 侧 post-retrieval 过滤。
    filter_dict = intent["filter"]

    return (
        rewritten,
        filter_dict,
        intent["is_listing"],
        target,
        intent["effect_filter"],
        intent["rarity_filter"],
        intent["target_chapter"],
    )
