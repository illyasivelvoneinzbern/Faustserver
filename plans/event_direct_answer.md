# 事件 JSON 直答 + 目录分离（Event Direct Answer & Directory Separation）

> 目标：把"探索事件"也做成与人格/饰品一致的结构化 JSON 直答，且**人格、饰品、事件分文件夹存放**。
> 镜像既有 [`PersonaDirectStore`](../rag/persona_direct.py) / [`GiftDirectStore`](../rag/gift_direct.py) 架构。

---

## 1. 现状与数据事实

### 1.1 事件数据源
事件数据来自 [`crawler/html_extractor.py`](../crawler/html_extractor.py) 的 `_event_to_dict()`，已存在于
`data/raw/wiki_pages.jsonl`（`page_type="event"`），无需重新爬取。

实测统计（`diag_event_probe.py`）：
| 项目 | 数值 |
|---|---|
| 事件总数 | 226（215 完整 + **8 空占位**） |
| title 格式 | `事件-<名称>`（如 `事件-1.76兆赫`） |
| event_name | 裸名（如 `1.76兆赫`），无重名 |
| id | `wiki_事件-<名称>`（全部有 id） |
| 含 `ego_gifts` 事件 | 仅 4 个 |
| `trigger_location` | 绝大多数为空 |
| 纯选择（无判定）事件 | 73 个 |
| `check_sin` 脏数据 | 带 `.png` 后缀（`色欲.png`/`嫉妒.png`/...） |
| `related_abnormalities` 垃圾值 | `事件触发地点[编辑]`、`无`、评级拼接串等 |
| `ego_gifts` 字段异常 | `name` 实为效果文本、`effect` 为另一效果 → 字段错位 |

### 1.2 事件记录结构
```json
{
  "page_type": "event",
  "title": "事件-1.76兆赫",
  "event_name": "1.76兆赫",
  "narration": "...场景叙述...",
  "options": [
    {
      "choice_text": "指派罪人进行压迫工作。",
      "check_type": "有利判定",
      "check_sin": "色欲.png",        // 脏：需去 .png
      "check_threshold": 14,
      "success_outcomes": [...],
      "failure_outcomes": [...]
    }
  ],
  "ego_gifts": [],                    // name/effect 错位，需降级处理
  "related_abnormalities": [...],     // 含垃圾值
  "trigger_location": "",
  "_structured": true,
  "id": "wiki_事件-1.76兆赫",
  "url": "...",
  "source": "wiki"
}
```

### 1.3 当前目录混放问题
`data/structured/` 下 `persona_*.json`（184）与 `gift_*.json`（621）**混合存放**。
新需求要求分文件夹 → 需重构导出路径、索引 glob、配置、agent 默认值，并迁移存量文件。

---

## 2. 目标架构（目录分离）

采用**保持根目录 `data/structured` 不变、按类型分子目录**方案：

```
data/structured/
├── personas/    ← persona_*.json   （184）
├── gifts/       ← gift_*.json      （621）
└── events/      ← event_*.json     （226）
```

**理由**：
- 根目录常量 `DEFAULT_OUT_DIR` 语义不变，改动的只是每个类型的导出/索引子路径
- `data_dir` 配置保留一个根目录值，类型子目录由代码内部拼接，配置不改（或按类型细分，见决策点）
- 与 gitignore（`data/structured/` 派生产物不纳 git）兼容

### 2.1 目录常量（structured_exporter.py）
```python
DEFAULT_OUT_DIR = "data/structured"
DIR_PERSONAS = "personas"
DIR_GIFTS    = "gifts"
DIR_EVENTS   = "events"
```
每个导出函数内部拼 `out_dir / DIR_PERSONAS` 等；`ensure_structured_dir` 自动 `mkdir(parents=True)`。

### 2.2 迁移存量文件
实施时运行一次性迁移脚本（或直接 `rebuild_all` + `rebuild_gifts` + `rebuild_events` 重建到新目录，然后删旧根目录残留文件）：
- 读取 `data/structured/persona_*.json` → 移入 `data/structured/personas/`
- 读取 `data/structured/gift_*.json` → 移入 `data/structured/gifts/`
- 事件：从 `wiki_pages.jsonl` 全新导出 → `data/structured/events/`

> 采用"重建 + 清旧"而非"移动"：保证与 jsonl 全量一致、避免手工移动遗漏。

---

## 3. 事件数据层（structured_exporter.py 新增）

### 3.1 事件清洗 `clean_event_record(record) -> dict`
- **`check_sin` 去 `.png` 后缀**：`色欲.png` → `色欲`（用 `re.sub(r'\.png$', '', sin)`）
- **`ego_gifts` 错位降级**：因 `name`/`effect` 错位且仅 4 个事件有值，导出时把每个 gift 渲染为单行
  `- <name>`（把错位的 name 当作效果文本展示），不构造 `名: 效果` 假配对；同时保留原始字段以便将来解析器修复
- **`related_abnormalities` 过滤垃圾值**：剔除 `事件触发地点[编辑]`、`无`、空串，以及含 `LCE评级|HE评级|TETH评级|ALEPH评级|WAW评级|ZAYIN评级` 的拼接串（评级+编号），保留纯名称
- **`trigger_location` 为空则省略**（不输出空字段）
- 保留 `_structured: true` + `_schema_version`

### 3.2 导出函数
```python
EVENT_PAGE_TYPE = "event"

def build_event_filename(event_id: str) -> str   # "event_<safe>.json"（用 id，规避特殊字符）
def export_event_record(record, out_dir=DEFAULT_OUT_DIR) -> Optional[Path]
def export_event_records(records, out_dir=DEFAULT_OUT_DIR) -> int
def rebuild_events(input_jsonl="data/raw/wiki_pages.jsonl", out_dir=DEFAULT_OUT_DIR) -> int
def load_event_index(out_dir=DEFAULT_OUT_DIR) -> dict[str, dict]   # {event_name: record}
```
- 仅处理 `page_type == "event"`；按 `id` 去重
- 空占位事件（narration/options 全空）：**仍导出**，但运行时直答对空内容返回"暂无数据"标注（见 §4），保证索引完整性
- 文件名用 `event_<safe id>.json`（与 gift 一致规避 title 不唯一，虽然事件当前无重名）
- 索引主键用 `event_name`（裸名，用户查询的是裸名）；title 作辅助键

### 3.3 入口扩展
`__main__` 支持 `python -m crawler.structured_exporter event`。

---

## 4. 运行时（rag/event_direct.py 新增）

### 4.1 `EventDirectStore`（镜像 GiftDirectStore）
```python
class EventDirectStore:
    def __init__(self, data_dir=DEFAULT_OUT_DIR, enabled=True)
    def _ensure_index(self) -> dict[str, dict]     # load_event_index
    def reload(self)
    def find_by_name(self, name) -> list[dict]     # event_name 精确 + title 兜底
    def search(self, name_like) -> list[str]
    def try_direct_answer(self, query) -> Optional[str]
```

### 4.2 事件名提取 `extract_event_name(query) -> Optional[str]`
- 去前缀：`事件-<X>` → `<X>`（用户可能带前缀）
- 从索引 event_name 精确匹配 → 包含匹配 → 多候选取精确
- **列表查询规避**：`有哪些事件/列出事件/都有什么事件` → 返回 None（不走直答），避免把列表意图误当单个事件
- 前缀触发词：`事件`、`这个事件`、`怎么触发`、`触发条件` 等可辅助判定，但**核心是 event_name 精确/包含命中**

### 4.3 确定性格式化 `format_event_full(record) -> str`
镜像 chunk_builder 的事件结构，输出纯文本固定格式：
```
【事件】<event_name>
【标题】事件-<event_name>            （若 title 存在）
【触发地点】<trigger_location>       （非空才输出）
【关联异想体】A、B、C               （过滤垃圾值后非空才输出）

【事件描述】
<narration>

【选项与判定】
选项1：<choice_text>
判定：<check_type> | 罪孽：<check_sin(去.png)> | 阈值：<check_threshold>   （无判定则省略该行）
成功结果：
  - <success>
失败结果：
  - <failure>

【E.G.O饰品】                       （非空才输出）
  - <gift 降级渲染行>
```
**空占位处理**：narration 与 options 均空 → 末尾追加
`（注：该事件暂无详细数据，可能为未完善页面。）`，仍返回命中（让用户知道存在该事件但无详情），或返回 None 回落 RAG（决策点，见 §6）。

---

## 5. 接线

### 5.1 agent/core.py
- `initialize_rag`：新增 event_direct 初始化块（镜像 gift_direct，读 `self.config["agent"].get("event_direct", {})`，data_dir 默认 `data/structured`）
- `_generate_reply`：在 gift_direct 块后插入 event_direct 直答优先块

### 5.2 config.yaml
```yaml
  event_direct:
    enabled: true
    data_dir: "data/structured"     # 内部再拼 events 子目录
```
若决策点选定"按类型细分 data_dir"，则改为三个块各自指向 `data/structured/personas|gifts|events`。

### 5.3 crawler/spider.py
`crawl_wiki` 尾部：新增 `rebuild_events(output_dir + "/" + OUTPUT_FILE)`；
并将现有 `rebuild_all` / `rebuild_gifts` 调用升级为按子目录重建（函数内部已处理子目录，无需改调用签名）。

---

## 6. 待确认决策点

1. **目录结构命名**：
   - A（推荐）：`data/structured/personas|gifts|events`，`data_dir` 配置保持 `data/structured`
   - B：`data/persona|gift|event` 平级（根目录都换，改动面更大）
   - C：`data/structured/persona|gift|event`（单数命名）

2. **空占位事件的直答策略**：
   - A（推荐）：仍命中并输出"（暂无详细数据）"标注，保证用户知道事件存在
   - B：空占位直接返回 None 回落 RAG（但 RAG 同样无数据，可能答非所问）

3. **`check_sin` 清洗位置**：导出层清洗（推荐，一次清洗全链路受益） vs 运行时格式化层清洗

4. **事件查询触发词**：是否在 `_generate_reply` 前对含"事件"关键词的查询优先走 event_direct 判定（推荐：与 gift 相同——只靠 event_name 命中，不额外抢查询）

---

## 7. 验证方案（diag_event_direct_verify.py）

- 命中：`1.76兆赫`、`E式次元短剑`（空占位标注）、`膏血`、`人体部位展览`（含饰品降级）
- 去前缀：`事件-1.76兆赫` 也能命中
- 判定清洗：确认 `色欲.png` → `色欲`
- 列表规避：`有哪些事件` / `列出所有事件` → 回落 RAG
- 未命中回落：`福斯特之镜`（不存在的事件）→ None
- 目录分离验证：persona/gift/event 三个索引各自正确加载、旧根目录无残留

---

## 8. 实施顺序

1. structured_exporter.py：目录常量 + 子目录重构（persona/gift 迁移）+ 事件导出/清洗/索引
2. 一次性重建：`rebuild_all` / `rebuild_gifts` / `rebuild_events` → 清理旧根目录残留
3. rag/event_direct.py：EventDirectStore + 事件名提取 + format_event_full
4. agent/core.py 接入 + config.yaml + spider.py 钩子
5. diag_event_direct_verify.py 全场景验证
6. 语法编译检查
