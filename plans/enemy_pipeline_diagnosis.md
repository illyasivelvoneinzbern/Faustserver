# P19-A 敌方数据管线根因诊断报告

> 任务：敌方数据管线 5 个问题根因诊断（只读诊断，不修改生产代码）
> 产出物：本报告 + 已按用户要求整理出独立效果数据 JSON `data/structured/effects.json`
> 日期：2026-08-13

---

## 一、数据流总览

```
灰机 Wiki 敌方页面
   │  ① MediaWiki action=parse 渲染 HTML（Playwright 绕过 CloudFlare）
   ▼
crawler/html_extractor.py  EnemyExtractor.extract()
   │  ② DOM + wikitext 双重解析：
   │      - _extract_abno_parts / _extract_stats / _extract_ally_units（部位/主线/援助）
   │      - _extract_enemy_skills_from_collapsible（DOM 技能）
   │      - _build_skill_dict_from_wikitext（{{敌方技能}} wikitext 技能）
   │      - _apply_wikitext_skill_attribution（wikitext 权威重排归属）
   │      - extract() 内 1730-1753 行按指纹去重
   ▼
crawler/structured_exporter.py  clean_enemy_record / export_enemy_record
   │  ③ 按 enemy_id = "battle_stage|enemy_name" 键控
   ▼
data/structured/enemies/enemy_<stage>_<name>.json   （1915 个文件）
   │  ④
   ├── crawler/chunk_builder.py  build_enemy_chunks（705-833）→ RAG Document（含 ## 恐慌类型 段 771-776）
   └── rag/enemy_direct.py       EnemyDirectStore / format_enemy_full（113-202）→ 直答
```

**各问题发生位置标注：**

| 问题 | 发生层 | 位置 |
|------|--------|------|
| ① 恐慌类型膨胀 | ② DOM 解析 | [`html_extractor.py`](crawler/html_extractor.py:1871) 与 :2143 |
| ② 去重误判/同单位反复利用 | ② 去重 + ③ 键控 | [`html_extractor.py`](crawler/html_extractor.py:1743) / [`structured_exporter.py`](crawler/structured_exporter.py:696) / :725 |
| ③ 技能差异未新建单位 | ② 去重时序 + ③ 键控 | [`html_extractor.py`](crawler/html_extractor.py:1763) / [`structured_exporter.py`](crawler/structured_exporter.py:696) |
| ④ 重要性标注无法识别 | ② wikitext 技能 | [`html_extractor.py`](crawler/html_extractor.py:1469) |
| ⑤ {{BuffPro|...}} 无法识别 | ② wikitext 技能 | [`html_extractor.py`](crawler/html_extractor.py:1557) |

---

## 二、问题 ①：恐慌类型把整页所有 buff 都写进去

### 根因

[`EnemyExtractor._extract_abno_parts()`](crawler/html_extractor.py:1871) 与 [`_extract_stats()`](crawler/html_extractor.py:2143) 两处使用**同一段选择器**，在识别到"恐慌类型"行后，把该行内**所有**带 tooltip/`title` 的链接文本都收进 `panic_types`：

```python
# _extract_abno_parts，1871-1878
if "恐慌类型" in row_text:
    tooltip_links = row.select("a.huiji-tt, a[title]")
    for a in tooltip_links:
        text = a.get_text(strip=True)
        if text:
            current.panic_types.append(text)
    continue
```

```python
# _extract_stats，2143-2150（同款代码）
if "恐慌类型" in row_text:
    tooltip_links = row.select("a.huiji-tt, a[title]")
    ...
```

Wiki 的"恐慌类型"表格行实际只含 1~3 个真正的恐慌效果，但该行 HTML 中还包含**整页通用的 buff 行**（如"伤害强化、易损"）的 tooltip 链接，选择器无差别收集 → 恐慌类型膨胀、且重复。

### 证据

- [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt:3)：1915 个敌方 JSON，panic_types 长度分布出现 **6→300 条、8→52 条，最高 70 个（2 条）**；942/1915 为空。
- 典型膨胀样本（6 个，且重复出现两轮）：
  `['愤怒', '伤害强化', '易损', '愤怒', '伤害强化', '易损']`（见 [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt:55)）
  —— "伤害强化/易损"是页面 buff，不是恐慌；"愤怒"是罪孽属性也被混入。
- [`test.txt`](../test.txt) 中 `恐慌：畏缩、虚弱` 重复出现 13 次（同一单位在多个文件/段落反复列同一组恐慌）。

### 下游传播

- [`chunk_builder.build_enemy_chunks()`](crawler/chunk_builder.py:705) 第 771-776 行将 `panic_types` **原样逐条**写入 `## 恐慌类型` 段 → RAG chunk 噪音。
- [`enemy_direct.format_enemy_full()`](rag/enemy_direct.py:167) 仅按字符串去重（`p_text not in seen`），无法去除"伤害强化/易损"这类非恐慌项。

### 修复建议（P19-B）

1. 收敛选择器：只取恐慌行中**真正的恐慌链接**（如限定 `a[title*="恐慌"]` 或限定在特定 table 列，而非 `a[title]` 全收）。
2. 去重：`panic_types` 追加前先判重（`if text not in current.panic_types`）。
3. 白名单/黑名单：对"伤害强化/易损/愤怒"等罪孽属性与通用 buff 名做过滤。

---

## 三、问题 ②：去重误判导致同一单位被反复利用

### 根因（两层叠加）

**A. 提取层指纹含 HP，过度去重**

[`EnemyExtractor.extract()`](crawler/html_extractor.py:1730) 的去重指纹：

```python
skill_names = tuple(sorted(
    (s.get("skill_name") or s.get("name") or "") for s in e.skills
))
fp = (
    e.hp,                                    # ← HP 参与指纹
    tuple(sorted(e.physical_resistances.items())),
    tuple(sorted(e.sin_resistances.items())),
    skill_names,
)
```

指纹将"HP+抗性+技能名"完全相同的不同单位（不同关卡/不同部位）合并为同一实体，只保留第一个。**HP 是数值属性，同一模型在不同关卡（等级缩放）会被判为不同单位**，而同一页内同 HP 的两个部位则会被误判为复用被丢弃。

**B. 键控层按 `battle_stage|enemy_name` 去重**

[`structured_exporter.rebuild_enemies()`](crawler/structured_exporter.py:696)：

```python
key = f"{battle_stage}|{enemy_name}"
if not enemy_name or key in seen:
    continue
seen.add(key)
```

[`load_enemy_index()`](crawler/structured_exporter.py:725) 同样以 `battle_stage|enemy_name` 为主键 → 同名同关卡的多个部位会互相覆盖，最终只留 1 个文件。

### 证据

- [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt:67)：**356 个同名单位出现在 >1 个文件**（同名被拆成多关卡多个文件）：`暴怒罪种 26 个文件`、`暴食罪种 25`、`怠惰罪种 22`、`忧郁罪种 20`、`红色小矮人 20`……
- [`test.txt`](../test.txt)：`留胡子的双钩海盗团成员` 出现 13 次（本应合并为同一单位被反复引用），`恐慌：畏缩、虚弱` 重复 13 次。

### 修复建议（P19-B）

1. **指纹去掉 `hp`**：改用 `(physical_resistances, sin_resistances, skill_names)`（可选加 body_part），同一模型跨关卡统一实体；不同技能名 → 视为不同单位（问题③需要）。
2. 键控层增加**部位维度**：`key = f"{battle_stage}|{enemy_name}|{body_part or ''}"`，避免同名多部位互相覆盖。
3. 索引/去重应基于"去重后的实体"而非每文件的 `stage|name` 裸键。

---

## 四、问题 ③：技能比对未按差异新建单位

### 根因

1. **去重时序错误**：`_apply_wikitext_skill_attribution(deduped)` 在 [`extract()`](crawler/html_extractor.py:1763) 中位于**去重之后**执行。去重指纹用的 `skill_names` 来自 DOM（`_extract_enemy_skills_from_collapsible`），而 wikitext 权威技能归属是在去重后才补上的 → **两个 DOM 技能名相同、但 wikitext 实际技能不同（含重要性差异）的单位，会被指纹判为同一单位而丢弃**。
2. **键控单一**：[`rebuild_enemies()`](crawler/structured_exporter.py:696) / [`load_enemy_index()`](crawler/structured_exporter.py:725) 仅用 `battle_stage|enemy_name`，不含技能哈希/部位 → 技能不同的同名同关卡单位被覆盖成 1 个文件。
3. **"不同出现位置"的处理相反**：当前实现中不同关卡（不同 `battle_stage`）天然生成不同文件（如 `暴怒罪种` 26 个文件），即把"出现在不同关卡"当作不同单位；而**真正的单位身份应跨关卡统一**，只有技能/属性实质差异才应新建单位。

### 证据

- [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt:68) 356 个同名 >1 文件；`暴怒罪种 26 个文件` 即同一单位按关卡被切成 26 个"新单位"。
- [`html_extractor.py`](crawler/html_extractor.py:1749) 指纹命中即丢弃（`dedup_count += 1`），wikitext 技能差异无法拆开。

### 修复建议（P19-B）

1. **调换顺序**：先 `_apply_wikitext_skill_attribution` 再按"技能名+抗性"去重，保证去重基于最终权威技能。
2. 指纹纳入 wikitext 技能名序列（含 importance 权重），技能名不同 → 不同单位。
3. 键控改为 `battle_stage|enemy_name|skill_fingerprint`（或按实体统一 key + 部位/技能变体），**关卡不作为单位身份**。

---

## 五、问题 ④：对"重要性"标注后的技能/被动没有识别能力

### 根因

[`_build_skill_dict_from_wikitext()`](crawler/html_extractor.py:1469) 解析 `{{敌方技能}}` 时，用 `_field()` 提取 技能名称/图标/基础值/变动值/硬币数/等级/攻击容量/罪孽/类型/效果，**从未解析 `重要性` 字段**（模板 `{{重要性|N|名称}}` 或 `|重要性=N`）：

```python
effect_text = _field("效果", to_line_end=True)   # 1517-1520 附近
# ... 只收集 skill_name/icon_id/sin_type/damage_type/base_value/
#     coin_power/coin_count/attack_level/attack_weight/is_guard/guard_type/coin_effects
return { ... }                                   # 1565-1578，无 importance
```

- DOM 侧过滤 [`_extract_enemy_skills_from_collapsible()`](crawler/html_extractor.py:2295) 存在 `if skill and (skill.sin_type or skill.base_value > 0)` 的条件；[`_apply_wikitext_skill_attribution()`](crawler/html_extractor.py:1580) 的 docstring（1587-1590）明确提到**"重要性=3 的强力攻击被 _extract_enemy_skills_from_collapsible 的过滤条件丢弃"**。
- 技能 dict schema 无 `importance` 字段，下游无法按重要性筛选/排序。

### 证据

- [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt:90)：**7613 个技能中，含 importance 字段的技能 = 0，不含 = 7613**。

### 修复建议（P19-B）

1. `_field("重要性", ...)` / `{{重要性|N|名称}}` 解析，写入 `importance` 字段（int + 名称）。
2. DOM 过滤条件补上 `or skill.get("importance")`，避免高重要性技能被丢弃。
3. wikitext 权威重排时以 importance 排序，确保强力技能进入最终技能列表。

---

## 六、问题 ⑤：wikitext 中的 {{BuffPro|...}} buff 无法识别

### 根因

1. **没有 BuffPro 代码→名称映射表**：全库 1336 条 `status_effect` 记录，字段集为 `['_structured','categories','description','effect_type','id','keywords','name','page_type','properties','sin_affinity','source','title','url']`，**没有任何 `code`/`BuffPro` 字段**（`diag_p19_status_survey.py` 实证：0/1336 含 BuffPro 文本）。
2. 解析时**仅硬编码替换一个 BuffPro**：[`_build_skill_dict_from_wikitext()`](crawler/html_extractor.py:1557)：

```python
# 去掉模板外壳只保留可读文本：{{BuffPro|X}} -> X
seg_readable = re.sub(r'\{\{[^|}]+\|([^}|]+)\}\}', r'\1', seg_clean)
seg_readable = seg_readable.replace("{{BuffPro|SuperCoin}}", "超级硬币")
```

   通用正则把 `{{BuffPro|X}}` 的外壳剥掉只留 `X`（**代码本身**），只有 SuperCoin 例外硬编码成中文。其余 BuffPro 代码（如 `{{BuffPro|Bleed}}`）直接以代码形式进入 coin_effects，无法被 RAG 理解。
3. 爬取来源不足：[`WikiSpider._fetch_tabx_gifts()`](crawler/spider.py:178) 只抓 `Data:Giftchoose.tabx`，[`passives_data.py`](crawler/passives_data.py:137) 只抓 `Data:Personalitypassives.json`；**没有任何"buff 表"（BuffPro 代码→中文名/描述）的爬取**。
4. [`extract_status_effect_from_html()`](crawler/html_extractor.py:3231) 解析状态效果页（类型/罪孽/关键词/属性 + 描述），但**不解析 BuffPro 代码字段** → 效果页中文说明（如 烧伤）与技能里的 `{{BuffPro|Burn}}` 无法建立关联。

### 证据

- 烧伤效果已有中文说明：`回合结束时：受到强度点固定伤害。持续层数（持续回合）回合。`（`data/structured/effects.json` 中 `烧伤`），但**没有任何 code 字段**可被 `{{BuffPro|...}}` 引用。
- `data/structured/effects.json`（按用户要求整理）共 1336 条效果，已验证 `烧伤/流血/麻痹/强壮/守护/虚弱` 存在；**`畏缩` 未收录**（在敌方恐慌中引用但无独立效果页）。
- 技能 coin_effects 中 BuffPro 代码保持代码形态（仅 SuperCoin 被硬编码替换）。

### 修复建议（P19-B）—— 管线顺序调整

按用户要求"先爬 buff 表再建依赖的敌人/人格 JSON"：

1. **新增 BuffPro 表爬取**：新增 `Data:Buffchoose.tabx`（或等效 buff 数据页）解析器，产出一张 `{BuffPro代码 → {中文名, 描述, 效果类型}}` 映射表，输出为 `data/structured/buffs.json`（与 `effects.json` 同构，补 `code` 键）。
2. **统一效果字典**：把 `effects.json`（中文名→描述）与 `buffs.json`（code→中文名）合并，形成 `code → 描述` 完整映射。
3. **解析期替换**：`_build_skill_dict_from_wikitext` 在剥壳后，用 buffs 映射把 BuffPro 代码替换成中文名/描述（替换掉现在的 SuperCoin 硬编码）。
4. **爬取顺序**：`crawl_wiki` 先爬 buff/效果表 → 再爬依赖 BuffPro 的人格/敌人页 → 最后重建 `data/structured/enemies` 与向量库。

---

## 七、buff / 状态效果数据现状总结

| 数据 | 现状 | 来源 |
|------|------|------|
| `data/structured/passives.json` | ✅ 5575 条被动（人格被动按 ID 索引） | [`passives_data.py`](crawler/passives_data.py:137) 抓 `Data:Personalitypassives.json` |
| 状态效果（status_effect 页） | ✅ 1336 条，均有中文描述，**无 code 字段** | [`extract_status_effect_from_html()`](crawler/html_extractor.py:3231) |
| **`data/structured/effects.json`**（新增，按用户要求） | ✅ 1336 条：`{name: {name, effect_type, sin_affinity, keywords, properties, description, url, id}}` | `diag_p19_effects_export.py` 由 wiki_pages.jsonl 生成 |
| BuffPro 代码→名称映射 | ❌ **不存在**（0/1336 含 code） | 无爬取来源 |
| `畏缩` 效果 | ⚠️ 在敌方恐慌中被引用，但**无独立效果页/未收录** | — |

> 说明：`effects.json` 已按用户"如果效果数据没有整理过，也把它们单独整理出来作为 json 调用"的要求生成，可作为 P19-B 效果层基础数据；但当前它**不含 BuffPro code**，仍需补充 buff 表爬取才能完成问题⑤的闭环。

---

## 八、修复优先级与 P19-B 建议清单

1. **P0（数据正确性）**：问题① 收敛恐慌选择器 + 去重；问题② 指纹去掉 hp + 键控加部位。
2. **P0（单位语义）**：问题③ 调换 wikitext 权威归属与去重顺序，技能差异→新单位，关卡不作为身份。
3. **P1（技能完整性）**：问题④ 解析 `重要性` 并修正 DOM 过滤，防止强力技能被丢弃。
4. **P1（效果闭环）**：问题⑤ 先爬 BuffPro 表 → `buffs.json` → 合并 `effects.json` → 解析期替换代码；调整爬取管线顺序。
5. **P2（质量验证）**：重爬后重建 `data/structured/enemies` + 向量库，用 `diag_p19_enemy_scan.py` 复验 panic_types 长度分布、同名文件数、importance 覆盖率。

---

## 附：诊断产物清单

| 文件 | 用途 |
|------|------|
| [`diag_p19_enemy_scan.py`](../diag_p19_enemy_scan.py) | 敌方 JSON 全量扫描（panic/passive/同名/importance 证据） |
| [`diag_p19_enemy_scan.txt`](../diag_p19_enemy_scan.txt) | 上述扫描输出 |
| [`diag_p19_status_survey.py`](../diag_p19_status_survey.py) | 状态效果字段/数量调查（无 BuffPro code 实证） |
| [`diag_p19_effects_export.py`](../diag_p19_effects_export.py) | 生成 `data/structured/effects.json` |
| [`data/structured/effects.json`](data/structured/effects.json) | **新增**独立效果数据（1336 条），供调用 |
| [`diag_p19_burn_probe.py`](../diag_p19_burn_probe.py) | 烧伤效果解释确认 |
| [`diag_p19_test_probe.py`](../diag_p19_test_probe.py) | test.txt + effects.json 证据复核 |
