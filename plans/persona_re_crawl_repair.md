# 人格重爬修复计划（Persona Re-Crawl Repair）

> 关联方案：[`persona_direct_answer.md`](persona_direct_answer.md)（直答架构已实施完成，items 17-21）
> 本计划为 **item 22（重爬阶段 / 数据质量修复）**，属后续单独进行的阶段。

## 一、背景与目标

「人格结构化直答」已上线（直答 14/14 验证通过），但当前 `data/structured/persona_*.json`
的数据源存在三类质量问题，导致直答与向量检索展示的是**残缺数据**：

1. **8 个占位符人格**：`skills[].skill_name` 为 `技能1/技能2/技能3/守备技能4`，
   而非真实技能名（wikitext 中真实名存在，是解析器正则缺陷所致）
2. **passive 全空**：184 个 personality 记录的 `battle_passive` / `support_passive` 全部为空
3. **部分阶段缺失**：部分人格技能三/守备技能仅 I/II 阶（如浮士德LCB罪人 连击/防御），
   无 III/IV 阶
4. **敌方/支援单位提取错误**（详见第八章）：8-30 / 9-38 等主线战斗页存在假单位名
   （"理智值" / "E.G.O特殊语音"）、敌方技能全缺失、支援以斯拉/韦斯帕缺失、
   摩西技能合并（25 = 8 + 7 + 10）、被动 ×2 重复

目标：修复数据提取逻辑后**定向重爬**受影响的页面 → 重建 `data/structured` 与向量库，
使直答与检索输出完整真实数据。**人格修复与敌方/支援单位修复在同一轮重爬阶段一并执行。**

## 二、8 个占位符人格清单

来自 [`diag_crawl_data_survey.txt`](../diag_crawl_data_survey.txt) 统计（全部占位符共 8 条）：

| # | 标题 | 真实技能名（从 coin_effects 提取的候选） |
|---|------|------|
| 1 | 堂吉诃德拉·曼却领总督 | 忍耐已经结束 / 随心所欲地盛放吧 / 将由我来刺穿 / 笑声将归于寂静 |
| 2 | 堂吉诃德脑叶公司E.G.O::以爱与憎之名 | 奉主管老爷之命登场！ / 要用爱！哟！ / 阿卡纳律动！！/小阿卡纳光破斩！！！ / 高速咏唱 |
| 3 | 堂吉诃德食指代行者-绽放E.G.O::代行 | 粉碎颅骨 / 噤、噤声 / 立、立刻执行指令… / 指令所赐、指令织就之布 |
| 4 | 浮士德蜘蛛巢环指子辈 | 屠宰-肋骨 / 法西娅在挨饿呢 / 受压肉体 / 展览准备 |
| 5 | 罗佳脑叶公司E.G.O::泪锋之剑 | 骑士的护佑 / 以正义之力 / 阿卡纳穿刺 / 骑士的信条 |
| 6 | 鸿璐句点事务所代表 | 火力压制 / 标记目标 / 命之句点 / 战斗呼吸 |
| 7 | 鸿璐蜘蛛巢环指父辈 | 解剖 / 材料获取-浴血之物 / 提比娅的旋律… / 展览的主办方 |
| 8 | 鸿璐鸿园的君主 | 我愿开辟前路 / 污血绝志竟成 / 全体黑兽，回应于我 / 黑兽利爪 |

> 说明：8 个占位符人格恰好全部是"特殊技能模板"人格（鸿璐式 / 桑丘派 / 嵌套技能4），
> 印证正则缺陷与模板种类强相关。

## 三、根因分析

### 3.1 技能名解析正则缺陷（8 个占位符人格）

当前实现 [`crawler/html_extractor.py`](../crawler/html_extractor.py:505) `_parse_skill_names_from_wikitext`：

- **模板起始匹配**：`pattern_start = re.compile(r'\{\{\s*技能链接\s*\|')` **只匹配 `{{技能链接|`**
- **命名参数提取**：`r'(\d+)\s*技能-名称\s*=\s*([^|]+)'` **只匹配 `N技能-名称=`**

无法覆盖的变体（来自 4 个示例文件实测）：

| 变体 | 示例出处 | 现状 |
|------|---------|------|
| `{{鸿璐式技能链接\|` | 君主宝 [`example/君主宝.txt`](../example/君主宝.txt) | 不匹配 |
| `{{桑丘派技能链接` | 总督唐 / 绝望罗 [`example/绝望罗.txt`](../example/绝望罗.txt) | 不匹配 |
| `{{技能4`（嵌套） | 小指良（技能4 内嵌 5技能-空间斩-缘） | 不匹配 |
| `强化N技能-名称=` | 小指良 / 总督唐 / 绝望罗 | 不匹配 |
| `N技能M-名称=`（子变体） | 君主宝 `3技能2-名称=开辟君主之道吧` / `4技能2-名称=护卫` | 不匹配 |
| `5技能-名称=`（跳号） | 小指良（空间斩-残） | 不匹配 |
| 字段写法 `|强化4技能类型=攻击`（无 `-`） | 绝望罗 [`example/绝望罗.txt`](../example/绝望罗.txt:107) | 当前解析类型字段也可能漏 |
| 普通写法 `|4技能-类型=突刺`（带 `-`） | 绝望罗 [`example/绝望罗.txt`](../example/绝望罗.txt:108) | 已支持 |

### 3.2 被动全空

当前实现 [`crawler/html_extractor.py`](../crawler/html_extractor.py:751) `_extract_passives`
依赖**渲染后 HTML** 的 `h3"被动"` 后非 collapsible div 文本，且要求以 `战斗`/`支援` 开头。

但 wikitext 实际结构为：
```
===被动===
{{人格被动链接|1061302}}
{{人格被动链接|1061301}}
{{人格被动链接|1061321}}
```
被动以**多个模板引用**存在，渲染后不满足"div 文本以 战斗/支援 开头"的假设 → 全部失效。

### 3.3 部分技能三/守备阶段缺失

- 已确认数据源 `stage_groups[0]` 仅 `[I, II]`（如 浮士德LCB罪人 连击、防御）——是**数据本身问题**，
  非直答格式化问题
- 待重爬后重新抓取验证：是模板未含 III/IV 阶字段，还是渲染 HTML 解析遗漏

## 四、修复方案

### Step 1：扩展 `_parse_skill_names_from_wikitext`（`html_extractor.py`）

1. **模板起始**：改为匹配多模板，同时保留大括号深度计数：
   - `{{技能链接|` / `{{鸿璐式技能链接|` / `{{桑丘派技能链接` / `{{技能4`（嵌套模板内部再递归）
2. **命名参数正则**：支持三类键名，全部提取为 `wikitext_key → skill_name`：
   - `(\d+)技能-名称`（基础，如 `1技能-名称`）
   - `强化(\d+)技能-名称`（强化，如 `强化1技能-名称`）
   - `(\d+)技能(\d+)-名称`（子变体/跳号，如 `3技能2-名称`、`5技能-名称`）
3. **字段写法变体**：类型/罪孽/硬币数等字段同时覆盖带 `-` 与不带 `-` 两种写法
   （`|N技能-类型=` 与 `|强化N技能类型=`）
4. **正则占位防御**：键名统一捕获原始 `wikitext_key`，供 `wikitext_key` 字段写入

### Step 2：重写 `_extract_passives`（`html_extractor.py`）

- 改为**直接从 wikitext** 提取 `===被动===` 段内的全部 `{{人格被动链接|ID}}` 引用
- 按顺序解析 ID，尝试通过 `人格被动` 页（或 ID 命名规则）映射被动名称/类型
- 输出兼容现有字段：`battle_passive` / `support_passive`（列表或字符串，需与
  `persona_direct.format_persona_full` 的 `_format_passives` 兼容——其已支持字符串与列表两种形态）
- 若 ID 无法直接映射名称，至少保留 ID 引用与原始顺序，避免再全空

### Step 3：技能阶段缺失排查

- 重爬前先对照 wikitext：确认 `3技能-3阶` / `4技能-3阶` 字段是否存在于模板
- 若存在 → 排查 `_extract_stage_from_pane` / tab-pane 解析遗漏（tab 页切换加载不全）
- 若不存在 → 属 Wiki 数据本身如此，直答按现有阶段展示即可（记录为已知边界）

### Step 4：wikitext_key 真实化 + 负硬币标注（`structured_exporter.py` / `persona_direct.py`）

- `structured_exporter._infer_wikitext_key` 当前按 `skill_index` 推断（`1技能`/`守备技能N`）
  → 重爬后改用 `_parse_skill_names_from_wikitext` 捕获的**真实键名**（`强化N技能`/`N技能M`/`5技能`）
- 直答格式化已支持：强化N技能→【强化技能N】、N技能M→【技能N衍生】、守备
- **负硬币威力**（绝望罗）：`coin_power` 为负值时保留负号（`变动值：-4`），已支持；
  重爬后在直答输出标注"减算技能"（`_format_stage` 需确认负号展示）

### Step 5：定向重爬（`re_crawl_personalities.py` 扩展）

1. `re_crawl_personalities.py` 支持 `--titles 堂吉诃德拉·曼却领总督 ...` 或新增
   `--placeholders` 参数（自动加载 8 个占位符人格清单）
2. 从 `.crawl_state.json` 移除目标页（强制重新抓取）+ 从 `wiki_pages.jsonl` 删除旧记录
3. 增量爬取（其余页面 revid 未变 → 复用缓存）
4. `crawl_wiki` 末尾已接入 `rebuild_all` → 自动重建 `data/structured/persona_*.json`

### Step 6：全量重建

1. **结构化直答数据**：`rebuild_all` 已接入 `crawl_wiki`，重爬完成后自动重建
2. **向量库**：`python scripts/rebuild_vector_db.py`（重生 `wiki_pages_cleaned.jsonl` +
   `all_data.jsonl` + ChromaDB + BM25）
3. 确认 `all_data.jsonl` 由 `export.py merge_data` 重新合并生成（`data/processed/all_data.jsonl`）
4. 验证向量库 chunk 的 `skill_name` 已更新为真实名

## 五、验证标准

| 检查项 | 标准 |
|--------|------|
| 8 个占位符人格 | `skills[].skill_name` 全部为真实名（对照第二节清单） |
| 被动 | 至少部分人格 `battle_passive`/`support_passive` 非空；全部人格不再全空 |
| 强化/衍生/跳号 | `wikitext_key` 含 `强化N技能`/`N技能M`/`5技能`，直答标注正确 |
| 负硬币 | 绝望罗 强化技能 变动值显示 `-4/-5/-6` |
| 阶段 | 重爬后技能三/守备阶段数不退化（能抓全则抓全） |
| 直答回归 | 重跑 `diag_persona_direct_verify.py` 14/14 通过，输出含被动与真实技能名 |
| 检索回归 | 重跑 `diag_retrieval_final_verify.py` 4 查询全召回 |

## 六、风险与注意事项

1. **重爬范围控制**：只重爬 8 个占位符人格 + 被动受影响的人格；避免全量重爬（revid 不变复用缓存）
2. **被动名称映射**：`{{人格被动链接|ID}}` 仅含 ID，被动名称可能需二次请求被动页或依赖 ID 命名规则；
   若无法映射，先保留 ID 引用（不做假数据）
3. **`all_data.jsonl` 重生**：需确认 `export.py merge_data` 的调用入口与 `main.py` 数据管道；
   向量库重建依赖正确的清洗/分块顺序
4. **直答与向量并行**：重建顺序应为 重爬 → rebuild_all（直答） → rebuild_vector_db（检索）；
   直答数据若重建失败，运行时自动回落 RAG（`try_direct_answer` 返回 None）
5. **不破坏既有数据**：重爬仅移除目标页记录，其余人格页 revid 未变 → 复用缓存，不影响已整理数据

## 七、交付物清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `crawler/html_extractor.py` | 修改 | 扩展技能名正则 + 重写被动提取 |
| `crawler/structured_exporter.py` | 修改 | wikitext_key 真实化 |
| `rag/persona_direct.py` | 修改 | 负硬币"减算技能"标注（如需） |
| `re_crawl_personalities.py` | 修改 | 支持占位符人格批量重爬 |
| 验证脚本 | 新增/运行 | 重爬后直答+检索回归验证 |

## 八、敌方/支援单位提取修复（Enemy / Ally Extraction Repair）

> 新增范围：与人格重爬在同一轮修复中一并执行（数据源均为 `data/raw/wiki_pages.jsonl`，
> 修复 `crawler/html_extractor.py` 的 `EnemyExtractor` 后**定向重爬**主线战斗页 → 重建
> `data/structured` 与向量库）。诊断证据见 [`diag_enemy_dump.txt`](../diag_enemy_dump.txt)。

### 8.1 诊断结论（数据库现状 vs 期望）

**8-30 主线战斗**（期望 2 单位，数据库 2 个，**数量对但内容错**）

| 期望 | 数据库实际 | 判定 |
|------|-----------|------|
| 雷横（hp=4239，斩/突/打 0.9/0.9/0.9，色欲0.8/怠惰0.8/暴食1.25/嫉妒1.25） | ✅ [0] 雷横，抗性**完全匹配**，12 被动正确 | ✅ 抗性/被动对，**技能=0** ❌ |
| 穿着整齐的拇指士兵（hp=226，斩1.25/穿刺0.75，技能"猛虎标弹支援"） | ❌ [1] 被命名 **"理智值"**（hp=226 匹配），1 被动对 | ❌ **名字错误 + 技能=0** |

**9-38 主线战斗**（期望 5 单位：2 敌 + 3 支援，数据库仅 3 个，**严重缺失**）

| 期望 | 数据库实际 | 判定 |
|------|-----------|------|
| 中指 父辈 - 马蒂亚斯（AbnormalityData id=1327） | ❌ 被命名 **"E.G.O特殊语音"**（hp=5600 实为马蒂亚斯），被动 **14 = 7×2** | ❌ 名字错误 + 被动×2 + **技能=0** |
| 中指 子辈 - 绮罗（id=1328） | ✅ 名字对，但被动 **12 = 6×2** | ❌ 被动×2 + **技能=0** |
| 摩西（支援） | ✅ 名字对，4 被动对，但 **25 技能** | ⚠️ 25 = 摩西8 + 以斯拉7 + 韦斯帕10（三人合并） |
| 以斯拉（支援） | ❌ **完全缺失** | ❌ |
| 韦斯帕（支援） | ❌ **完全缺失** | ❌ |

### 8.2 根因（源自 Debug 诊断，5→2 蒸馏）

1. **根因 A（核心）：`_find_next_table` 跨小节越界 + 指纹去重吞并真名**
   - `_SECTION_TITLE_KEYWORDS`（[`html_extractor.py`](../crawler/html_extractor.py:1293)）不含
     `理智值` / `E.G.O特殊语音` → 被主循环当敌人收集
   - `_find_next_table`（[`html_extractor.py`](../crawler/html_extractor.py:1551)）向后遍历
     **不因遇到下一个标题而停止** → 越界找到后续敌人的表格 → 产生假单位
   - 真 h3 因指纹（hp+抗性）相同被去重丢弃（[`html_extractor.py`](../crawler/html_extractor.py:1355)）
   - **系统性证据**：`"E.G.O特殊语音"` 出现于 9-38/9-41/9-43/9-44/9-45；`"理智值"` 出现于
     8-30 及 大湖之镜-扭曲的金笠

2. **根因 B：`_find_following_collapsibles` 的 h4 边界假设错误（技能全缺失 + 支援合并/缺失）**
   - 假设"技能标题是 h3，遇到 h4 break"（[`html_extractor.py`](../crawler/html_extractor.py:1580)），
     但 8-30/9-38 技能标题是 h4 `====技能====`（[`example/8-30.txt`](../example/8-30.txt:80)）
   - **敌方路径**从 h3 敌人标题调用 → 遇 h4 立即 break → 敌方技能全收集不到
   - 9-38 摩西的 `<div class="mw-collapsible">` 未闭合（[`example/9-38.txt`](../example/9-38.txt:874)
     只闭合一次）→ 以斯拉/韦斯帕 h3 嵌套在 div 内 → `_extract_ally_units`
     （[`html_extractor.py`](../crawler/html_extractor.py:1935)）的 next_sibling 遍历只看到摩西
     → 以斯拉/韦斯帕缺失；且摩西一路收集 25 技能（8+7+10 精确吻合）

3. **根因 C：被动 ×2 重复**——`_extract_stats` 被动收集（[`html_extractor.py`](../crawler/html_extractor.py:1761)）
   对 AbnormalityData 模板的 td 文本 + li 兜底双路径重复收集

### 8.3 修复方案

| Step | 修复 | 位置 |
|------|------|------|
| E1 | **标题过滤加固**：`_SECTION_TITLE_KEYWORDS` 增加 `理智值` / `E.G.O特殊语音` / `语音` 等非敌人标题词 | `extract()` 关键词元组 |
| E2 | **`_find_next_table` 加标题边界**：向后遍历遇到下一个 h3/h4/h5 标题即停止（不跨小节），杜绝假单位；同时保留指纹去重作为兜底 | `_find_next_table` |
| E3 | **`_find_following_collapsibles` 边界修正**：技能标题同时兼容 h3 与 h4（仅在标题文本含"技能"时停止）；`_extract_single_ally` 对齐 | `_find_following_collapsibles` |
| E4 | **支援遍历改为 DOM 全扫描**：`_extract_ally_units` 不再依赖直接 next_sibling，改为收集 `援助单位`/`友方单位` 区段内**全部** h3 标题（含嵌套在 div 内者），并按 h3 就近匹配技能 collapsible | `_extract_ally_units` |
| E5 | **被动去重**：`_extract_stats` 被动收集改为仅 li 单路径（或对结果去重） | `_extract_stats` |
| E6 | **定向重爬**：重爬 8-30 / 9-38 及其余含敌方的主线战斗页（复用 `re_crawl_personalities.py` 的 revid 缓存机制，仅移除受影响页） | `re_crawl_*.py` |

> 验证基准：修复后 8-30 应得 [雷横, 穿着整齐的拇指士兵]（雷横含技能），9-38 应得
> [马蒂亚斯, 绮罗, 摩西, 以斯拉, 韦斯帕]（各含正确技能数，被动不重复）。

### 8.4 敌方合成 JSON 方案（镜像 persona/gift/event 模式）

- **目录**：`data/structured/enemies/`（沿用目录分离约定，`structured_exporter` 新增
  `ensure_structured_dir` 下的 `enemies` 子目录 + `build_enemy_filename` + `export_enemy_record` /
  `export_enemy_records` / `rebuild_enemies`）
- **数据源**：`data/raw/wiki_pages.jsonl` 中 `page_type="enemy"` 的 `enemies` 数组
- **文件命名**：`enemy_<battle_stage>_<enemy_name>.json`（如 `enemy_主线战斗8-30_雷横.json`；
  同一 stage 多单位各自成文件，stage 为 battle_stage）
- **记录结构**（单文件 = 单个敌方单位）：
  ```json
  {
    "enemy_id": "<battle_stage>|<enemy_name>",
    "battle_stage": "主线战斗8-30",
    "enemy_name": "雷横",
    "body_part": "躯干",
    "hp": 4239, "defense": 83, "speed": "2~4", "chaos_threshold": "2967/0",
    "physical_resistances": {"斩击": 0.9, "突刺": 0.9, "打击": 0.9},
    "sin_resistances": {"暴怒": 1.0, "色欲": 0.8, "怠惰": 0.8, "暴食": 1.25,
                        "忧郁": 1.0, "傲慢": 1.0, "嫉妒": 1.25},
    "panic_effects": ["..."],
    "passives": ["..."],
    "skills": [{"skill_name": "...", ...}],
    "is_ally": false
  }
  ```
- **抗性默认值**：3 物理 + 7 罪孽，**未写默认 1.0**（领域规则）
- **运行时直答**（可选，后续）：新增 `rag/enemy_direct.py`（EnemyDirectStore + 单位名提取 +
  `format_enemy_full`），agent 接入直答优先；本期可先只落数据，直答按需追加
- **重爬自动重建**：`crawl_wiki` 末尾接入 `rebuild_enemies`（与 `rebuild_all` / `rebuild_events`
  并列），重爬完成后自动重建

### 8.5 验证标准（敌方）

| 检查项 | 标准 |
|--------|------|
| 8-30 | 2 单位：雷横（含技能）+ 穿着整齐的拇指士兵（含"猛虎标弹支援"），无"理智值"假名 |
| 9-38 | 5 单位：马蒂亚斯 + 绮罗 + 摩西 + 以斯拉 + 韦斯帕，技能数正确，被动无重复 |
| 抗性默认 | 未写抗性默认为 1.0（对照领域规则） |
| 系统性 | 全库 `enemy_name` 无 `理智值` / `E.G.O特殊语音` 假名 |
| 导出 | `data/structured/enemies/` 生成，字段符合 8.4 结构，`rebuild_enemies` 可重建 |
| 回归 | 重跑 `diag_enemy_dump.py` 确认 8-30 / 9-38 数据正确 |

### 8.6 交付物补充（敌方）

| 文件 | 类型 | 说明 |
|------|------|------|
| `crawler/html_extractor.py` | 修改 | E1-E5 五项敌方/支援提取修复 |
| `crawler/structured_exporter.py` | 修改 | enemies 目录 + 敌方记录导出 + rebuild_enemies |
| `crawler/spider.py` | 修改 | crawl_wiki 末尾接入 rebuild_enemies |
| `re_crawl_*.py` | 修改 | 定向重爬主线战斗页（敌方） |
| `rag/enemy_direct.py` | 新增（可选） | 敌方直答（本期可只落数据） |
| `diag_enemy_dump.py` | 运行 | 修复后复验 8-30 / 9-38 |
