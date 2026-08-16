# 数据清洗/切分方案：HTML DOM 结构化提取

## 问题根因

当前 pipeline 数据流：
```
spider.py (WikiText API) → parser.py (mwparserfromhell) → cleaner.py (regex) → chunker.py (RecursiveCharacterTextSplitter)
```

**根因**：WikiText 中的技能表格极其复杂（嵌套 `{{RichTab}}`、`{{T|震颤}}`、多层 `{|...|}`），`mwparserfromhell` 无法正确处理，`cleaner.py` 的正则清洗进一步摧毁了残留结构。最终所有 LCB 罪人页只剩 1 个 chunk（72 字符的 footer 文本）。

**决策**：不在 WikiText 层面修复（模板复杂度高、维护成本大），改用 `action=parse` API 获取渲染后的 HTML，以 BeautifulSoup DOM 解析做结构化提取。HTML 结构稳定，所有 I-IV 阶数据均在 DOM 中，一次解析即可获取全部数据。

---

## 新 Pipeline 架构

```
spider.py (action=parse API → 渲染 HTML)
    ↓
crawler/html_extractor.py (BeautifulSoup DOM 解析)
    ↓ 输出结构化 JSON（按页面类型分别提取）
    ├── PersonalityExtractor → {personality_name, sinner, skills[], passives[], resistances, ...}
    └── EgoExtractor        → {ego_name, sinner, awakening_stages[], erosion_stages[], passive, ...}
    ↓
crawler/chunk_builder.py (结构化分块，非盲切)
    ↓ 输出 LangChain Document 列表（每块携带丰富 metadata）
rag/vector_store.py → ChromaDB
```

## 一、HTML 获取方式变更

### 从 `action=query&rvprop=content` 切换为 `action=parse`

```python
# 在 spider.py 中新增
params = {
    "action": "parse",
    "page": title,
    "prop": "text",       # 渲染后的 HTML
    "format": "json",
}
```

返回的 `data["parse"]["text"]["*"]` 即渲染后的 HTML 内容，结构与你提供的两个 HTML 文件中的 `#mw-content-text` 部分一致。

**优势**：
- 服务端渲染，不依赖浏览器
- 返回速度与 wikitext 相当
- 所有 I-IV 阶段 tab 数据都在 DOM 中（只是 `display` 不同）
- 图片 `alt` 属性可识别技能类型

---

## 二、HTML DOM 结构化提取

### 2.1 页面类型识别

优先级：
1. `categories` 含 "人格" → `personality`
2. `categories` 含 "E.G.O" → `ego`
3. 其余 → 保持现有 WikiText 解析流程

### 2.2 PersonalityExtractor（人格页）

#### HTML 结构映射

| 游戏数据 | HTML 定位方式 | 示例值 |
|---------|-------------|-------|
| 人格名称 | `#firstHeading` 文本 | 浮士德黎明事务所收尾人 |
| 所属罪人 | 页面标题前缀匹配或 info table 中的 logo 图片 alt | 浮士德 |
| 实装日期 | info table 中 "登场时间" 行 | 2026.07.09 |
| 获得方式 | info table 中 "获取方式" 行 | 第2赛季活动提取 |
| 罪孽亲和 | info table 中 `<img alt="罪孽-傲慢.png">` × N | 傲慢×3 |
| 物理抗性 | info table 中 `斩击×0.5`, `突刺×2`, `打击×1` | 脆弱/普通/抵抗 |
| E.G.O 资源 | info table 底部的 E.G.O 资源行 | 色欲×2, 怠惰×4... |
| **技能 1/2/3** | `table.wikitable`（技能区域的第一个表格） | |
| ↳ 罪孽类型 | 技能图标 img alt `技能-斩击-傲慢.png` → 傲慢 | 傲慢 |
| ↳ 伤害类型 | 技能图标 img alt `技能-斩击-傲慢.png` → 斩击 | 斩击 |
| ↳ I 阶数据 | `tab-pane` 中第一个面板（含 `active` 类） | 基础值:3, 变动值:+3 |
| ↳ II/III/IV 阶 | 后续 `tab-pane` 面板 | 递增数值 |
| ↳ 硬币效果 | 硬币 img alt `硬币1.png` 后的 `[命中时]` 等文本 | |
| ↳ 攻击容量 | `攻击容量：1` 文本 | 1 |
| **守备技能** | 技能区域后续表格（技能图标为闪避/防御/反击） | |
| **特殊技能** | 条件触发的额外技能（如 迸射-正午、联合） | |
| **战斗被动** | `personality-passive` gadget div 中的被动文本 | |
| **支援被动** | 同上，标记为支援被动 | |

#### 技能图标文件名映射

```
技能-斩击-傲慢.png  → sin=傲慢, damage=斩击
技能-斩击-暴食.png  → sin=暴食, damage=斩击
技能-斩击-暴怒.png  → sin=暴怒, damage=斩击
技能-突刺-色欲.png  → sin=色欲, damage=突刺
技能-打击-怠惰.png  → sin=怠惰, damage=打击
技能-打击-忧郁.png  → sin=忧郁, damage=打击
技能-闪避.png      → 守备技能-闪避
技能-防御.png      → 守备技能-防御
技能-反击.png      → 守备技能-反击
```

#### 提取算法

```python
def extract_personality(html: str, title: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    content_div = soup.select_one('#mw-content-text')
    
    result = {
        "page_type": "personality",
        "title": title,
        "sinner": _extract_sinner(title),       # "浮士德黎明事务所收尾人" → "浮士德"
        "personality_name": title,
        # ... 基本信息、抗性、技能、被动
    }
    return result
```

### 2.3 EgoExtractor（E.G.O 页）

#### HTML 结构映射

| 游戏数据 | HTML 定位方式 | 示例值 |
|---------|-------------|-------|
| E.G.O 名称 | `#firstHeading` 文本 | 永恒-浮士德 |
| 所属罪人 | 页面标题后缀或 logo 图片 alt | 浮士德 |
| 实装日期 | info table | 2024.06.13 |
| 获得方式 | info table | 第4赛季活动提取 |
| 资源消耗 | info table 中 `<img alt="罪孽-色欲.png">` × 2 等 | 色欲×2, 怠惰×4, 忧郁×2, 傲慢×4 |
| 罪孽抗性 | info table 中 `暴怒×0.75`, `色欲×2` 等 | |
| **E.G.O 觉醒** | 觉醒技能区域的 `table.wikitable` | |
| ↳ 技能名称 | 表头 | 永恒 |
| ↳ 硬币数 | "硬币 × N" 文本 | ×4 |
| ↳ 理智消耗 | "理智消耗 10" 文本 | 10 |
| ↳ 攻击加权 | "攻击+0" 文本 | 0 |
| ↳ I-IV 阶基础值 | `tab-pane` 面板中 `基础值：4` | 4 |
| ↳ I-IV 阶硬币威力 | `tab-pane` 面板中 `硬币威力：+5` | +5 |
| ↳ 攻击容量 | `攻击容量：1` 文本 | 1 |
| ↳ 硬币效果 | 硬币 img 后的 `[攻击前]`、`[命中时]` 等 | |
| **E.G.O 侵蚀** | 侵蚀技能区域的 `table.wikitable` | |
| ↳ 同上结构 | 但基础值/硬币威力不同 | 基础值:36, 硬币威力:-12 |
| ↳ 特殊标记 | `[无差别攻击]`、随机指定目标 | |
| **被动** | 被动区域的 `table.wikitable` | 奔腾的时间 |

#### 觉醒 vs 侵蚀 识别

标题/表头含 "觉醒" 或位于觉醒 section → `mode=awakening`
标题/表头含 "侵蚀" 或位于侵蚀 section → `mode=erosion`

---

## 三、分块策略 (Chunking Strategy)

### 核心原则

**不做盲切！** 每个结构化数据单元 = 一个 chunk。分块在「提取之后」而非「提取之前」。

### 3.1 人格页分块方案

```
1 个基本信息块:
  title: "浮士德黎明事务所收尾人 - 基本信息"
  content: 人格名称、实装日期、获得方式、罪孽亲和、物理抗性、EGO资源
  metadata: { page_type: "personality", sinner: "浮士德", personality_name: "浮士德黎明事务所收尾人", section: "info" }

每个技能 × 每个阶段 = N 个技能块:
  title: "浮士德黎明事务所收尾人 - 技能一 - I阶"
  content: 罪孽类型：傲慢 / 伤害类型：斩击 / 基础值：3 / 变动值：+3 / 硬币：2 / 攻击容量：1 / ...
  metadata: { page_type: "personality", sinner: "浮士德", personality_name: "浮士德黎明事务所收尾人", section: "skill", skill_index: 1, skill_name: "技能一", stage: "I", sin_type: "傲慢", damage_type: "斩击" }

每个守备技能 × 阶段:
  title: "浮士德黎明事务所收尾人 - 守备技能 - III阶"
  metadata: { ..., skill_type: "guard" }

每个特殊技能 × 阶段:
  title: "浮士德黎明事务所收尾人 - 迸射-正午 - I阶"
  metadata: { ..., skill_type: "conditional", condition: "..." }

被动块:
  title: "浮士德黎明事务所收尾人 - 被动"
  content: 战斗被动：... / 支援被动：...
  metadata: { ..., section: "passive" }
```

**关键 metadata 字段**（供 ChromaDB 过滤和检索加权）：

| 字段 | 类型 | 说明 | 用途 |
|-----|------|------|------|
| `page_type` | str | `personality` / `ego` | ChromaDB where 过滤 |
| `sinner` | str | 罪人名称（如 "浮士德"） | 按罪人过滤 |
| `personality_name` | str | 完整人格名 | 精确检索 |
| `section` | str | `info` / `skill` / `passive` | 分段检索 |
| `skill_index` | int | 技能序号 1/2/3/4(守备)/5+(特殊) | 按技能号检索 |
| `skill_name` | str | 技能名称 | 检索 |
| `stage` | str | I/II/III/IV | 按阶段过滤 |
| `sin_type` | str | 傲慢/色欲/怠惰/暴食/忧郁/暴怒 | 按罪孽类型过滤 |
| `damage_type` | str | 斩击/突刺/打击/闪避/防御/反击 | 按伤害类型过滤 |

### 3.2 E.G.O 页分块方案

```
1 个基本信息块:
  title: "永恒-浮士德 - 基本信息"
  content: 资源消耗、罪孽抗性、实装日期、获得方式
  metadata: { page_type: "ego", sinner: "浮士德", ego_name: "永恒", section: "info" }

觉醒每个阶段 = 4 个块:
  title: "永恒-浮士德 - 觉醒 - I阶"
  content: 硬币×4 / 理智消耗:10 / 基础值:4 / 硬币威力:+5 / 攻击容量:1 / ...
  metadata: { ..., section: "awakening", stage: "I" }

侵蚀每个阶段 = 4 个块:
  title: "永恒-浮士德 - 侵蚀 - I阶"
  content: 硬币×1 / 理智消耗:40 / 基础值:36 / 硬币威力:-12 / 攻击容量:3 / [无差别攻击] / ...
  metadata: { ..., section: "erosion", stage: "I" }

被动块:
  title: "永恒-浮士德 - 被动"
  content: 奔腾的时间：...
  metadata: { ..., section: "passive" }
```

### 3.3 为什么这样分？

1. **精确检索**：用户问 "浮士德黎明事务所收尾人技能一 III 阶的硬币效果"，chunk 元数据可直接过滤到唯一块
2. **metadata 过滤**：`where={"page_type": "personality", "sinner": "浮士德"}` 大幅缩小检索空间
3. **无信息丢失**：所有 I-IV 阶数据都在独立 chunk 中，不会被切碎或丢弃
4. **Reranker 友好**：每个 chunk 是自包含的技能描述，LLM Reranker 评分准确

### 3.4 保留现有流程的页面类型

以下页面类型继续使用现有 WikiText 解析流程（不需要 HTML 提取）：
- `character` - 角色对话/剧情
- `plot` - 主线剧情
- `accessory` - E.G.O 饰品（已走 Tabx 结构化）
- `other` - 世界观/道具/系统等

---

## 四、实现计划

### Phase 1: 新建 `crawler/html_extractor.py`

```python
# 核心类
class HtmlExtractor:
    """HTML DOM 结构化提取器"""
    def extract(self, html: str, title: str, categories: list[str]) -> dict | None:
        page_type = _classify(categories)
        if page_type == "personality":
            return PersonalityExtractor(html, title).extract()
        elif page_type == "ego":
            return EgoExtractor(html, title).extract()
        return None  # 回退到现有 WikiText 流程

class PersonalityExtractor: ...
class EgoExtractor: ...
```

依赖：`beautifulsoup4`（需添加到 requirements.txt）

### Phase 2: 新建 `crawler/chunk_builder.py`

```python
def build_personality_chunks(data: dict) -> list[Document]:
    """将结构化人格数据构建为 LangChain Document 列表"""
    ...

def build_ego_chunks(data: dict) -> list[Document]:
    """将结构化 E.G.O 数据构建为 LangChain Document 列表"""
    ...
```

### Phase 3: 修改 `spider.py`

- 对 personality/ego 页面使用 `action=parse` API
- 返回结果增加 `html` 字段（仅在 personality/ego 类型时）
- 或在 `crawl_page` 中直接调用 `HtmlExtractor`

### Phase 4: 修改 `chunker.py`

- `chunk_documents()` 中检测是否为 structured 数据
- 对 structured 数据走 `chunk_builder` 而非 `RecursiveCharacterTextSplitter`

### Phase 5: 重建向量数据库

```bash
python scripts/rebuild_vector_db.py --full
```

---

## 五、`buffPro` / `huiji-tt` 等特殊 Span 处理

HTML 中 buff/状态效果表示为：
```html
<span class="huiji-tt" data-params="震颤">
    <img alt="震颤.png" src="...">
    <span><a href="...">震颤</a></span>
</span>
```

提取规则：
- 取最内层 `<a>` 标签的文本作为效果名称
- `data-params` 属性作为备用
- 颜色编码（红=负面, 绿=正面, 蓝=特殊）从 `<span style="color: #FF0000">` 提取

---

## 六、抗性提取

物理抗性格式：`斩击×0.5`（抵抗）、`突刺×2`（脆弱）、`打击×1`（普通）
罪孽抗性格式（EGO）：`暴怒×0.75`、`色欲×2`

提取规则：
- `×0.5` / `×0.75` → 抵抗
- `×1` → 普通
- `×2` → 脆弱

---

## 七、与 `示例.txt` 输出格式的映射

提取的结构化数据可直接映射到 `示例.txt` 要求的输出格式。LLM 在生成回复时，从检索到的 chunk 中组装信息即可。

人格输出：`{personality_name}` → 抗性 → 技能 1/2/3 → 守备技能 → 被动
EGO 输出：`{ego_name}` → 资源 → 觉醒 → 侵蚀 → 被动

---

## 八、依赖变更

`requirements.txt` 新增：
```
beautifulsoup4>=4.12.0
lxml>=5.0.0
```

`pyproject.toml` 同步更新。
