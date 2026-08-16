# 边狱巴士 RAG Agent 不足之处分析与改进方案

> 版本：2026-08-15 · 基于对 `D:\Angela` 全量代码、`logs/agent.log`（约 2.3MB 运行日志）、`data/` 数据目录的审查。
> 目标功能：
> 1. 准确无误地按对话要求返回数据库中各项数据
> 2. 拉取推特指定账号推送，并返回准确的图片/视频
> 3. 基于人格设定，在询问剧情相关问题时发表符合人格的看法

---

## 〇、术语澄清（重要，防止误读）

本项目存在两套"人格"，**不可混用**：

| 概念 | 位置 | 指代 | 用途 |
|------|------|------|------|
| 游戏内人格（Identity） | `data/structured/personas/*.json`、向量库 200+ 档案 | 边狱巴士中罪人的**可操纵角色单位**（如"浮士德黑兽-卯魁首"、"堂吉诃德拉·曼却领总督"） | 仅用于**数据直答**（目标 1），不参与扮演 |
| 扮演人格（Persona） | `personas/*.yaml` | Bot 扮演的 **12 位罪人本人**（如 faust.yaml 扮演浮士德） | 对话扮演与**观点表达**（目标 3） |

**扮演人格的扩展仅限于 12 罪人**（李箱/浮士德/堂吉诃德/良秀/默尔索/鸿璐/希斯克利夫/以实玛利/罗佳/辛克莱/奥提斯/格里高尔，另含执行管理人但丁，共 13 个内置锚点，见 `rag/chain.py:134` `_BUILTIN_SINNERS`）。任何游戏内 Identity 单位（如"黑兽-卯魁首"）不得作为扮演人格。

---

## 一、现状架构盘点（审查结论）

```
NapCatQQ(WS) → router → handle_event → 安全/频率 → _generate_reply
                                                    │
        ┌───────────────────────────────────────────┤
        │ ① 人格切换预拦截 (tools.py 正则 + LLM function-calling)
        │ ② 结构化直答四件套（确定性，绕过 LLM）：
        │    persona_direct / gift_direct / event_direct / enemy_direct
        │ ③ 语义缓存 → ④ RAG Chain（向量+BM25+渐进检索+LLM扩展+Reranker(关)）
        └───────────────────────────────────────────┤
                              后台任务：X RSS 轮询 → 群媒体推送（仅群聊）
```

**已做得很好的部分**（改进时不能破坏）：
- 结构化直答四件套：人格/饰品/事件/敌方数据从 `data/structured/*.json` 确定性取数，日志显示命中率高（"兔浮的技能"、"雷横的数据"、"炸鸡对决"等均精确命中）；
- 昵称/俗称展开（NICKNAME_MAP 80+ 条）、LCB 罪人识别、敌方名去空格/裸名模糊匹配（P21/P23 系列修复）质量较高；
- 渐进式多轮检索 + BM25 混合 + 章节编号前缀直查，召回手段丰富；
- X 推送有幂等（pushed_ids 落盘）与媒体本地化下载。

---

## 二、与三大目标的差距分析（问题清单）

### 目标 1：准确无误按对话要求返回数据 —— 差距：中

| # | 问题 | 证据/代码位置 | 影响 |
|---|------|--------------|------|
| 1.1 | **直答拦截无意图门控，观点/比较类问题被数据表格劫持** | `rag/enemy_direct.py:294` `try_direct_answer` 只排除 is_listing；`rag/persona_direct.py:617` 同理。日志实证：`'你觉得雷横这个人怎么样'` → `敌方直答命中（唯一候选）→ 输出完整数据表格`（agent.log L13774-13775） | 观点问题返回数据表，与目标 3 直接冲突；"兔浮和W浮谁更强"会命中第一个昵称直答输出单人格数据，比较逻辑完全缺失 |
| 1.2 | **对话上下文不参与检索（指代消解缺失）** | `agent/core.py:633` `_generate_reply` 用 `msg.text` 直接进 `run_rag_query`；`rag/chain.py:53` 检索只用 `inputs["question"]`，chat_history 只喂给 LLM 生成 | "那她的被动呢"、"这个技能的硬币效果" 等 follow-up 检索不到上一轮实体 → 答非所问或"未收录" |
| 1.3 | **数据完整性缺口** | 日志：大量页面 `HTML 结构化提取失败，回退到 WikiText 流程`（1-11、2-19、3-22、4-54、7-36 系列）、`故事对话解析失败`（3-22-07/08/09、4-54-09/19/22、7-36-06/10、8-20-15、**8-33-02/06** 等）；曾出现 `name 'extract_story_dialogue_from_wikitext' is not defined` 整批失败 | 剧情/关卡类数据在向量库中缺失或劣化，问"8-33 讲了什么"可能查不到 |
| 1.4 | **向量库重建残留 & 上次重建被中断** | `rag/vector_store.py:47` `delete_collection` 按 `data/vector_db/limbus_wiki` 目录删（Chroma 0.4+ 实际是 UUID 段目录），目录永不存在 → 段文件成孤儿；实测 `data/vector_db` 残留 40+ 个 HNSW 段、约 1.9GB；8-15 17:04 的向量化在完成前被"任务被取消" | 磁盘膨胀；中断重建可能导致当前库不完整、检索质量下降 |
| 1.5 | **测试回归缺失** | `tests/` 目录仅剩 `__pycache__` 的 .pyc，无任何测试源文件 | 改动无回归保障，"准确无误"无客观度量 |
| 1.6 | **置信度评估是长度规则** | `agent/core.py:891` `_assess_confidence`：按"是否含不确定词/字数"打分，0.05~0.75 | 不能反映答案正确性，自我反思闭环（reflect）依据不可靠 |
| 1.7 | **大名单查询上下文截断** | `config.yaml` `max_context_chars: 2000`，但渐进检索最多可输出 top_k×17≈100 个 chunk（`rag/retriever.py:872`） | "浮士德有哪些人格"类穷举查询上下文被截断，LLM 只能看到部分列表 |

### 目标 2：拉取指定推特账号并返回准确图片/视频 —— 差距：大

| # | 问题 | 证据/代码位置 | 影响 |
|---|------|--------------|------|
| 2.1 | **视频链路实际不可用** | `crawler/x_fetcher.py:98` `extract_videos` 只从 RSS description 抓 `<video src>`/`<source>`/裸 `.mp4`，Nitter RSS 的 description 只有 `<img>` 与 `video poster` 缩略图；日志中所有媒体下载均为 .jpg/.png（含 `amplify_video_thumb` 封面），`video_urls` 恒为空 | "返回准确的视频"完全不成立 |
| 2.2 | **数据源单一脆弱** | 默认唯一 RSS 源 `https://nitter.net/{handle}/rss`（`crawler/x_fetcher.py:36`），Nitter 公共实例随时失效/限流（日志中多次出现长时间"拉取到 0 条"） | 推送断供，无自动切换 |
| 2.3 | **转发（RT）污染推送** | `x_posts.jsonl` 实证：`RT by @LimbusCompany_B: 【コミックマーケット108のご案内】`（来源 Ham_PangPang 周边商店）被当作官方内容推送；`handle` 字段错误填为 `LimbusCompany_B` | 用户收到无关商家推文 |
| 2.4 | **正文含噪音 URL** | `html_to_text` 把超链接替换为 href：正文出现 `https://nitter.net/search?f=tweets&q=%23LCB` 等搜索链接（x_posts.jsonl 首条） | 推送文本丑陋 |
| 2.5 | **首次启动洪水推送** | 8-14 04:08 首启：pushed_ids 为空 → RSS 内全部 21 条历史推文在 10 分钟内分 5 批推完（agent.log L14190-14284）；无初始水位线 | 新群/重装即刷屏 |
| 2.6 | **图片准确性无校验** | `adapter/napcat.py:193` `download_media` 不校验 Content-Type/图片解码，`card_img`（链接预览卡图）与真实推图混发；Nitter 代理 URL 可能 404，无重试 | "准确的图片"打折 |
| 2.7 | **会话内无法查询/返回推文媒体** | `create_news_search_tool`（`agent/tools.py:34`）从未接线——全项目仅 `agent/core.py:801` 一处 `bind_tools`（人格切换）；回复发送只有文本（`_send_reply` → `send_group_msg`/`send_private_msg`）；`send_group_msg_media` 仅用于后台推送且**无私聊媒体通道** | 用户问"官方最新推文/发个图"只能得到文本或向量检索的旧文 |

### 目标 3：人格设定下发表剧情看法 —— 差距：大

| # | 问题 | 证据/代码位置 | 影响 |
|---|------|--------------|------|
| 3.1 | **直答劫持观点问题（同 1.1）** | 日志实证 `'你觉得雷横这个人怎么样'` 命中敌方直答输出数据表；`persona_direct` 同样无"看法/评价"意图检测 | 目标 3 的头号障碍 |
| 3.2 | **System Prompt 人格层被数据规则压制** | `rag/chain.py:196` 知识规则：严格依据资料、不编造、"绝不可将角色A的能力归到角色B"；无"剧情/角色类问题需以自己的立场与性格发表看法"的规则 | LLM 倾向输出中性百科式回答而非人设观点 |
| 3.3 | **人格 few-shot 示例从未被注入** | `personas/manager.py:85` `build_system_prompt` 只拼 identity/traits/speech_style/catchphrase/advanced，**不使用 examples / greeting_template / knowledge_scope**；`rag/chain.py:125` Layer1 即此函数 | faust.yaml 精心编写的 5 条示例对话完全浪费，扮演质量全靠模型临场发挥 |
| 3.4 | **剧情数据缺失（同 1.3）** | 8-33-06 等关键剧情页解析失败回退 | 谈剧情无资料可依，观点沦为无根空谈 |
| 3.5 | **无长期/摘要记忆** | `agent/memory.py` 仅窗口 10 轮内存消息，`agent/session.py` 会话 1 小时过期；无 rolling summary | 长剧情讨论、跨会话回顾断裂 |
| 3.6 | **可扮演罪人仅 2/13** | `personas/` 只有 faust.yaml、don_quixote.yaml 两个罪人扮演；其余 11 个内置罪人锚点（`rag/chain.py:134`）无对应 YAML，只能退化为极简内置锚定 | 目标 3 覆盖面窄（详见〇节：扮演仅限 12 罪人 + 但丁） |

### 横切问题

| # | 问题 | 说明 |
|---|------|------|
| A.1 | **工具系统是死代码** | `create_default_tools`（wiki搜索/新闻/掷骰/伤害计算）从未被调用，README 声称的 ToolAgent 架构不存在；LLM 无工具选择能力，无法"按需取数" |
| A.2 | **NapCat 连接兼容问题** | 日志大量 `BaseEventLoop.create_connection() got an unexpected keyword argument 'extra_headers'`（websockets 版本与 Windows Proactor 不兼容，8-10 晚连续 5 分钟重连失败） |
| A.3 | **日志噪音** | 每次请求的 httpx 明细（embeddings POST）刷满 INFO 日志，掩盖关键链路信息；无检索 trace（用什么工具/命中哪条数据） |
| A.4 | **配置与代码耦合** | 敌方/事件等直答开关分散在 config.agent.*，但情感/意图阈值硬编码在代码中，调参需改代码 |

---

## 三、改进方案

### 目标 1：数据返回准确无误

**P0 — 意图门控（先做，一石二鸟解决 1.1/3.1）**
- 新增 `rag/intent_gate.py`：在 `_generate_reply` 中直答四件套**之前**做三级意图分类：
  1. `opinion`（含"你觉得/怎么看/评价/怎么样/喜欢/讨厌/认为"等）→ 跳过全部直答，走人格扮演链路；
  2. `compare`（含"谁更强/对比/区别/哪个好"）→ 走专用比较直答（见下）；
  3. `data_query` / `list` / `other` → 现有直答流程。
- 关键：门控命中即短路，不依赖 LLM（确定性、零成本）；正则词表与 `_FUZZY_NOISE_RE` 同级维护。

**P1 — 比较型直答**
- `persona_direct`/`enemy_direct` 增加 `try_compare_answer(q)`：解析两侧实体名 → 并排输出两列（抗性/技能/资源逐行对齐）→ 附一句客观差异小结。多候选时输出候选清单（已有 P23 模式可复用）。

**P1 — 对话检索上下文化（解决 1.2）**
- 会话级实体槽：`Session` 增加 `last_entities`（最近讨论的角色/人格/敌方/章节）；follow-up 检测（以"她/他/它/这个/那个/这/那/其"开头或含指代词且无实体名）→ 用槽位实体改写查询后再检索；
- 可选 LLM 版：复用 `LLMQueryExpander` 或新增轻量 rewrite（带 chat_history 尾部 4 轮），失败回落原查询。

**P1 — 数据质量闭环（解决 1.3/1.4）**
- 爬虫失败页重试 + `data/raw/crawl_errors.jsonl` 告警清单，重爬时先修 `story_dialogue` 解析（8-33/3-22/4-54 等失败页面）；
- 向量库重建脚本化：`scripts/rebuild_db.py` 完成"全量重建 → 数量校验（chunk 数 vs 源记录）→ 抽样查询自检"；
- 修复 `delete_collection`：用 `chromadb.PersistentClient` 遍历 `chroma.sqlite3` 的段表清理孤儿段（或重建前直接删 `data/vector_db` 整目录+sqlite）；提供 `scripts/cleanup_vector_db.py` 一键清理现存 1.9GB 孤儿段；
- 全量重建流程加"防中断"（分批提交+断点续传），避免再次出现 8-15 被取消的半成品库。

**P2 — 可度量正确性**
- 建 golden 评估集 `tests/golden/*.json`（约 60~100 条：人格技能/EGO/饰品/敌方/剧情/多轮 follow-up），离线脚本 `scripts/eval_rag.py` 批量跑并输出命中率报告；
- 恢复 pytest 回归测试（tests/ 源文件重写，至少覆盖：意图门控、直答格式化、昵称展开、CQ 码构造、X 解析）；
- 置信度升级为"检索证据对齐"：回答包含的数值/实体能否在注入上下文中找到（规则可做，成本低），替代纯长度规则。

### 目标 2：推特推送 + 准确图片/视频

**P0 — 视频链路（核心缺口）**
- 方案 A（推荐）：对带视频的推文，用其 permalink 走 Nitter 单推页面 HTML 解析 `data-video-url` / `source src` 拿真实 mp4；失败回落官方 `syndication` API（`https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}`，免鉴权，返回 `video` 变体 URL）；
- 方案 B（可选）：配置 X API v2 Bearer Token 作为主源（`media_url` 字段直接给 mp4）；
- `download_media` 增加 Content-Type/文件头校验（`image/jpeg`、`video/mp4`），失败重试 2 次并跳过劣质源。

**P0 — 多源容灾**
- `rss_urls` 改为镜像列表 + 每源健康探测（最近 N 次拉取成功率），自动切换到可用实例；新增 `x_fetcher.mirrors` 配置段（nitter 公共实例清单 + 自定义）。

**P1 — 推文净化（解决 2.3/2.4）**
- RT/引用过滤：`x_fetcher.filter_retweets: true`（默认），白名单账号（如官方自身）可配置放行；`handle` 字段以 permalink 实际作者为准；
- 文本清洗：正文中的 `nitter.net/search?...` 等链接整段删除；标题中 `RT by @xxx:` 前缀剥离。

**P1 — 初始水位线（解决 2.5）**
- 首启（pushed_ids 为空）时只推送 `published_at` 在最近 `initial_backfill_hours`（默认 24h）内的推文，历史不推。

**P1 — 会话内媒体通道（解决 2.7）**
- 把 `fetch_limbus_news` 等工具真正接入 LLM tool-calling（见 A.1）；
- 新增 `send_private_msg_media`（与群版同构），`_send_reply` 升级为支持结构化回复 `{text, images, videos}`；
- 新指令 `/最新推文 [账号]`、`/推文图片 [账号]`：直接拉 RSS → 本地化 → 发媒体，命中即返回，不经过 LLM；
- 媒体本地索引 `data/media/index.json`（tweet_id → 文件路径），重复请求直接发本地缓存，防重复下载。

### 目标 3：人格化剧情观点

**P0 — 观点路由（同 1.1 的 P0）**：opinion 意图 → 跳过直答 → 走人格扮演链路。

**P1 — System Prompt 分层升级**
- `personas/manager.py` `build_system_prompt` 注入 `examples`（few-shot，选最近 2~3 条）、`greeting_template`、`knowledge_scope`；
- `rag/chain.py` 新增 Layer 5「剧情/角色看法规则」：*当用户询问剧情事件、角色评价、动机解读时，先依据参考资料陈述事实，再以自己人格的性格、立场与经历发表个人看法（例如浮士德会用"根据浮士德的分析……"表达理性判断），看法与数据分离表述*；
- 输出约束放宽：允许符合人格的少量语气词/口癖（现有规则 6 一刀切禁神态描写，建议改为按人格配置开关）。

**P1 — 剧情记忆**
- `Session` 增加 `plot_summary`（LLM 滚动摘要，每 6 轮更新一次，存内存+落盘 `data/sessions/`）；
- 剧情话题检测（含"剧情/章节号/角色名+看法"）→ 附带 plot_summary 进 prompt。

**P1 — 罪人扮演人格补齐（仅限 12 罪人，见〇节）**
- 写脚本 `scripts/persona_gen.py`：从 `data/structured/personas/persona_{罪人}LCB罪人.json`（**罪人本体档案**，即 `*LCB罪人` 条目）提取 voice_lines / 剧情台词，自动生成初始 YAML（identity=罪人身份、speech_style=语音风格提炼、examples=取代表性语音与剧情台词），人工打磨后启用；
- 补齐顺序按 `_BUILTIN_SINNERS`（`rag/chain.py:134`）的 13 个内置锚点：已有 faust / donquixote，还缺 yisang / ryoshu / meursault / honglu / heathcliff / ishmael / rodion / dante / sinclair / outis / gregor；
- **注意**：`data/structured/personas/` 中其余 190+ 个 Identity 单位档案只服务于数据直答，不得生成扮演 YAML。

**P2 — 立场一致性**
- 可选：人格 YAML 增加 `values` 字段（如浮士德：理性/效率/忠于公司），prompt 中约束观点回答与价值观一致；不做 LLM 记忆回放，控制成本。

### 横切改进

| 项 | 方案 |
|----|------|
| A.1 工具接线 | 用 `bind_tools(create_default_tools(retriever))` + 循环（max 3 轮）构建真 ToolAgent；工具集扩为：`search_wiki`（兜底）、`fetch_twitter_media(account, n)`、`query_persona_data`、`roll_dice`、`calculate_damage`；每轮工具结果以只读上下文注入，最终由 LLM 汇总 |
| A.2 NapCat 连接 | 锁定 websockets 版本（如 `websockets<12` 或按官方指引升级适配）；连接失败按指数退避（5s→30s→60s 封顶），避免 5 秒固定重连风暴刷日志 |
| A.3 可观测性 | 回复上下文对象 `ReplyContext{path: direct|rag|tool, hits: [...], confidence}` 进日志；httpx 日志级别降为 DEBUG；INFO 只保留关键链路 |
| A.4 配置化 | 意图门控词表、情感阈值、RT 过滤、水位线等全部入 `config.yaml`，运行时可 reload |

---

## 四、里程碑路线图

| 里程碑 | 内容 | 预估工作量 |
|--------|------|-----------|
| **M1（先做，目标2/3 的卡点）** | 意图门控（opinion/compare/data）；X 视频链路（单推页解析+syndication 兜底）；RT 过滤+文本清洗；初始水位线；多源容灾 | 3~5 天 |
| **M2** | 对话检索上下文化（实体槽）；工具接线（news/media 工具 + 私聊媒体通道 + /最新推文 指令）；System Prompt 注入 examples 与剧情看法规则 | 3~4 天 |
| **M3** | 数据质量闭环（爬虫失败页修复+向量库清理脚本+防中断重建）；golden 评估集与回归测试恢复；比较型直答；滚动摘要记忆；12 罪人扮演人格补齐脚本（素材仅取各罪人 `*LCB罪人` 本体档案） | 5~7 天 |

**建议执行顺序**：M1 先行（意图门控是 1.1/3.1 的共同解，视频链路是目标 2 的最大缺口），M1 完成后立即用日志验证（观察"你觉得 X 怎么样"不再直答、推文含 mp4、无 RT 噪音），再进入 M2/M3。

---

## 五、爬取难题修复记录（2026-08-15 已实施）

**难题**：人物/单位界面的状态效果在 wikitext 用 `{{BuffPro|英文code}}` 引用，HTML 渲染为中文；状态效果总集页 wikitext 无内容、HTML 为中文。现有爬取导致状态效果显示英文（如 `Combustion`/`ChoiSwordsmanship` 残留）。

**根因（全部经实证）**：
1. `Data:Buffchoose.tabx` 实测是"人格→拥有的 buff 类型"**布尔表**，不是 code→中文名 映射 → `parse_buffs_tabx` 逻辑无效，`buffs.json` 从未正确生成，运行时只靠约 40 条静态兜底 → 大量 code 残留英文；
2. enemy/event 页面走 `action=parse`（服务端 HTML，JS 未执行），`{{BuffPro|Code}}` 渲染为 `<span class="buffPro">Code</span>` 英文占位；中文化由站点 JS gadget 执行（`buff-pro-processed`）；
3. 敌方被动从 HTML get_text，从未做 code 替换（金笠 JSON 残留实证）；
4. 无时机标签的效果段被 `_effect_kw` 关键词过滤丢弃（折射轨道"影响即将到来的过去…"效果为空）；
5. h2 敌人 + h3"技能"小节（折射轨道格式）的 wikitext 技能归属错误（技能挂到"技能"而非敌人名），wikitext 权威回填失效；
6. JS 渲染后 buffPro span 内嵌 tooltip 容器（`.huiji-tt-preload`，含 `{0}/{1}` 占位符）被 get_text 拼入效果文本。

**修复（文件）**：
| 文件 | 改动 |
|------|------|
| `crawler/buffs_data.py` | 新增 `build_buff_code_map_from_html(html, wikitext)`：渲染 HTML 的 buffPro span 中文名（BeautifulSoup 提取 `a[title]`）与 wikitext `{{BuffPro|Code}}` **按顺序配对**，构建页面级 code→中文名 映射（覆盖官方表/静态表缺失的专属 code，零手工维护）；`resolve_buff_codes_in_text` 增加 `extra_map` 参数；模块头勘误 `Data:Buffchoose.tabx` 为布尔表 |
| `crawler/html_extractor.py` | ① 新增 `_strip_tooltip_preload(soup)`，四个提取器（Personality/Ego/Enemy/Event）统一移除 `.huiji-tt-preload`；② `_resolve_buffpro_in_text` 支持页面级映射优先；③ `EnemyExtractor` 构建 `self._buff_code_map` 并接入技能效果/被动（主线/异想体/援助单位）替换；④ 技能合并时 DOM 效果为空则用 wikitext 效果回填 `coin_effects`；⑤ `_effect_kw` 扩充（施加/获得/影响/拼点/本技能/不可摧毁/鳞粉/过去/现在/未来 等）；⑥ h2/h3 技能归属修复：h3 区段标题继承归属、非区段 h2 视为敌人名；⑦ 效果段剥离内联 HTML 标签 |
| `crawler/spider.py` | enemy/event 页面改走 Playwright 完整渲染优先（与 personality/ego 一致，JS 执行后 buffPro 为中文），失败回退 action=parse；`crawl_wiki` 不再调用无效的 `fetch_buffs` |

**验证（端到端实测通过）**：
- 折射轨道6号线-第一区段：23 个 BuffPro code ↔ 23 个 span 配对 → 7 条映射；`Combustion→烧伤`、`HeatedWingScalesExplain→灼热的鳞粉（蛹）`、`SuperCoin→不可摧毁的硬币` 等全部中文，无 tooltip 污染；
- 主线战斗8-30（雷横，`{{状态2|中文}}` 模板）：被动/技能全中文，回归无破坏；
- ？？？的心象-关底BOSS（金笠，247 个 BuffPro code → 27 条映射）：`BrandedKimSatgat→烙印【奴】`、`ChoiSwordsmanship→本国剑术【肉】`、`PanicChangeLock→固定恐慌`、`Combustion→烧伤` 等全部中文化。

**部署提示**：`data/structured/enemies/*.json` 中的英文残留需**强制重抓对应页面**（增量模式按 revid 复用不会重抓）——运行 `python main.py crawl --full-crawl` 或删除对应页面的增量缓存后重跑；验证脚本 `scripts/verify_*.py` 可作回归工具。

---

## 六、抽奖（Gacha）功能（2026-08-15 已实施）

**需求**：抽奖功能作为工具供 agent 调用；概率 = 三灯人格 3%、二灯人格 13%、一灯人格 81%、EGO 3%；支持 MCP 架构。

**稀有度划分（用户提供，2026-08-15 确认）**：
- **一灯（12）**：全部 LCB 罪人人格（初始人格）
- **二灯（51）**：用户名单（12 罪人 × 各机构，含澄清：四协会=「し协会」（日文"死"假名）、技术解放联盟=脑叶E.G.O 荡漾/朱符、脑叶公司支部=幸存者、脑叶公司本部=提灯、LCE=提灯）
- **三灯（121）**：其余全部人格（含未列入的 E.G.O 型人格如 悔恨/以爱与憎之名/次元撕裂者）
- **EGO 池（111）**：wiki 独立 E.G.O 页面（乌瞰刀、他人之锁等，page_type=ego）

**实现**：
| 文件 | 说明 |
|------|------|
| `scripts/build_gacha_data.py` | 生成 `data/gacha/rarity.json`（1/2/3 灯分类）+ `data/gacha/ego_pool.json`（EGO 池）；分类规则集中于此，改名单后重跑即可 |
| `tools/gacha.py` | 核心模块：`GachaPool` 按权重抽取（one 81%/two 13%/three 3%/ego 3%），`gacha_pull(times)` 便捷函数，`format_results` 输出"灯级 · 名称"（用户确认：仅名称+灯级） |
| `tools/gacha_mcp_server.py` | **MCP stdio server**（自实现 JSON-RPC 2.0，离线无需官方 SDK）：`initialize / tools/list / tools/call`，暴露 `gacha_pull` 工具；独立进程运行，供任意 MCP client 连接 |
| `agent/tools.py` | `create_gacha_tool()`：StructuredTool 包装（未来 ToolAgent 接线即用） |
| `agent/core.py` | P26 抽奖指令预拦截：强信号正则（抽卡/抽奖/十连/单抽/来一发/gacha/N连）→ 确定性调用 `gacha_pull`（零 LLM 成本），未命中回落常规链路 |

**验证**：
- 蒙特卡洛 10000 次：81.05% / 12.89% / 3.12% / 2.94% ≈ 配置 81/13/3/3 ✓
- MCP server 四项方法（initialize/tools/list/tools/call×2）实测通过 ✓
- 指令解析：`来一发十连`→10、`抽个三连`→3、`单抽`→1；`兔浮的技能`/`雷横的数据` 不误触发 ✓

**十连保底（2026-08-15 追加）**：十连（≥10 抽）中若没有二灯或三灯人格，则将最后一抽替换为随机二灯人格（保证至少一个二灯人格）；单抽概率与权重完全不变，十连仅二灯略升（保底贡献）、一灯/EGO 微降。实测：5000 次十连全部含二灯及以上；单抽 10000 次 ≈ 81/13/3/3 不变。

**说明**：
- MCP 为**自实现 stdio 协议**（离线环境 `pip install mcp` 失败）；未来装官方 SDK 后可无缝替换为 `FastMCP` 包装（工具逻辑不变）。
- 本地 agent 直接 import `tools.gacha.gacha_pull`（零进程开销）；MCP server 供外部/未来 client 使用。
- 抽奖是确定性指令（不消耗 LLM），与人格切换（P17）同一拦截层级。

---

## 七、按改进计划实施记录（2026-08-15 第二批，未重爬）

本轮按改进计划落地以下代码改进（全部经编译 + 单测验证）：

### 7.1 意图门控（解决 1.1/3.1：观点问题被直答劫持）
- **`rag/intent_gate.py`**（新）：`classify_user_intent(text)` → `opinion | compare | data | other`。优先级：强观点词（你觉得/你怎么看/评价一下/你的看法…）→ 比较词（谁更强/对比/区别/vs…）→ 弱观点词+无数据强词 → 数据词 → other。
- **`agent/core.py`**（P27）：`_generate_reply` 中直答链前计算意图；`opinion/compare` 跳过四个直答（persona/gift/event/enemy），观点类问题走人格扮演链路；`compare` 走 `_try_compare_answer`。
- 日志实证问题（"你觉得雷横这个人怎么样"→数据表）已修复：现在判为 opinion → 绕过直答 → LLM 以人格发表看法。

### 7.2 比较型直答
- **`rag/persona_direct.py`**：`try_compare_answer`（按"和/与/vs/对比"拆两侧 → `extract_personality_name` → 并排输出 抗性/罪孽亲和/技能数/被动/实装日期）。
- **`rag/enemy_direct.py`**：`try_compare_answer`（并排输出 HP/防御/速度/混乱阈值/物理抗性）。
- 实测：`兔浮和W浮谁更强`、`雷横和穿着整齐的拇指士兵` 均正确输出。

### 7.3 X 推文净化（目标 2）
- **`crawler/x_fetcher.py`**：`parse_tweet_entry` 增加 **RT 检测**（`retweet` 字段 + RT 前缀剥离）与 **nitter 搜索链接清洗**；`fetch_new_tweets` 支持 `filter_retweets` 与 `min_published_at`（首启水位线）。
- **`agent/core.py`** `_x_poll_loop`：传 `filter_retweets`（默认 true）与首启水位线（pushed_ids 为空时仅推最近 `initial_backfill_hours`=24h 内推文，防历史刷屏）。
- **`config.yaml`**：新增 `filter_retweets` / `initial_backfill_hours` 配置。

### 7.4 System Prompt 人格层升级（目标 3）
- **`personas/manager.py`** `build_system_prompt`：注入 `examples`（few-shot 前 3 条）、`greeting_template`、**看法规则**（剧情/角色评价类先述事实再以人格立场发表看法）。
- **`rag/chain.py`**：新增 Layer 4「剧情与看法规则」（观点可以主观但不得编造剧情事实；追问数值时回到数据模式）。

### 7.5 12 罪人扮演人格补齐
- **`scripts/persona_gen.py`**（新）：从 `persona_{罪人}LCB罪人.json` 的 voice_lines 自动生成 `personas/{id}.yaml` 骨架（identity/traits/speech_style/examples），人工打磨后启用。
- 已生成 11 个（yisang/ryoshu/meursault/honglu/heathcliff/ishmael/rodion/dante/sinclair/outis/gregor），跳过已有 faust/don_quixote；PersonaManager 现可加载 **13 个罪人扮演人格**。

### 7.6 回归测试恢复（M3）
- `tests/` 重建：`conftest.py` + `test_intent_gate.py` / `test_gacha.py` / `test_x_fetcher.py` / `test_personas.py`，共 **36 个测试全部通过**（`pytest tests/ -v`，pytest 经清华镜像安装）。
- 覆盖：意图门控分类、抽奖概率/保底/数据完整性、X 净化（RT/链接/媒体）、人格加载与 prompt 注入。

### 验证汇总
| 项 | 结果 |
|---|---|
| 意图门控 11 组用例 | 全部符合预期（opinion/data/compare/other）|
| 比较直答（人格/敌方） | 输出正确；非比较查询返回 None |
| X 净化（RT/链接清洗） | 通过（单测） |
| Prompt 注入（examples/看法规则/问候语） | 通过 |
| 13 罪人 YAML 加载 | 全部通过 PersonaManager 校验 |
| pytest 回归 | **36 passed** |

---

## 八、角色好恶字段 + 人格数据化重拟（2026-08-15）

### 8.1 好恶字段（likes/dislikes）
- persona YAML 新增 `likes` / `dislikes` 列表字段（`personas/manager.py` OPTIONAL_FIELDS 扩展）；
- `build_system_prompt` 注入「喜欢：…」「讨厌：…」规则，让 LLM 在对话中体现角色好恶。

### 8.2 数据化重拟流程
- **`scripts/extract_sinner_data.py`**（新）：提取指定罪人的 LCB 语音（voice_lines）+ 主线/活动剧情台词（story_dialogue blocks 中 role=罪人名）→ `data/gacha/tmp/sinner_{名}.txt`（带出处）。
- **重拟标准**（以 `personas/faust.yaml` 为示例）：identity / traits（每条附『台词』+出处）/ **likes·dislikes（各 3-5 条，带台词出处，素材不足时注明「（推断）」）** / speech_style / examples（含体现好恶的对话）。
- 素材规模：12 罪人语音各 21-23 条、主线台词 312-527 条。
- 进度（已完成）：**13 个罪人扮演人格全部重拟完成**——浮士德（人工示例）+ 11 罪人 + 但丁（4 个并行子代理按统一规范制作）。全部含 likes/dislikes（各 4-5 条，带台词出处，素材不足处标注「（推断）」）、traits（6 条带原文佐证）、examples（5-6 条含好恶对话）；台词引用逐字核验零编造；13 个 YAML 语法零错误、PersonaManager 全量加载通过、36 个 pytest 回归通过。后续：但丁改为**只回应钟表的声音**（speech_style/examples 全部为钟表拟声，不输出语言台词）。

### 8.3 观点类问题注入剧情事实（P29/P29b/P29c）
- **问题**：日志实证"你怎么看里恩？"→ LLM 把食指父辈里恩编造成 N 公司异端审判官。
- **P29（初版）**：opinion/compare 注入单位数据（HP/抗性/技能）——经用户修正。
- **P29b（用户确认）**：opinion **结合剧情发表看法**——注入该角色在剧情中的身份与真实台词（`rag/story_facts.py` 从 wiki_pages.jsonl 的 story_dialogue blocks 构建「角色→剧情台词」懒加载索引，过滤无意义台词）；**单位数值仅"怎么打/弱点"类明确游戏意图使用**（走直答/数据链路）；compare（谁更强）保留单位数据（强度比较需要数值）。
- **P29c（追加）**：日志实证"你怎么看霍恩海姆"仍混入被动/护盾战术数据——原因：opinion 的常规 RAG 检索仍会拉入敌方单位页。修复：opinion 意图的检索**限定剧情来源**（`story_only=True` → retrieve filter `{"page_type": "story_dialogue"}`），上下文只剩剧情对话，配合剧情事实底座发表看法。
- **P29d（追加）**：日志实证"你怎么看霍恩海姆"回答称"值得认可"，忽略了原剧情中浮士德与霍恩海姆**互相看不上的关系**（7.5-04战后：浮士德暗讽"担任助手意味着能力不如霍恩海姆"、霍恩海姆回敬"我同样没有想过选择你"）。修复：剧情事实底座增加 **【人物互动】区块**——`story_facts.get_interactions(entity, focus_role)` 提取被问角色与**当前扮演罪人**的对话交锋（每页最多 1 组最佳互动、相邻优先、同距取长），LLM 发表看法时符合原著人物关系。
- 验证：里恩/霍恩海姆均能提取剧情事实与互动（霍恩海姆↔浮士德"助手交锋"命中）；story_only 检索过滤生效；49 个 pytest 全部通过。

### 8.4 人物关系字段 + 剧情台词打分（P30）
- **relationships 字段**：persona YAML 新增 `relationships`（{角色名: 关系描述}），`personas/manager.py` 支持并注入 System Prompt「人物关系：…」。数据来源：`scripts/mine_relationships.py` 统计每个罪人在剧情对话中的**互动对象 Top 15**（邻接计数 + 台词样本），13 个罪人 YAML 由子代理基于统计+素材撰写（保留全部现有字段）。
- **剧情台词打分**（`rag/story_facts.py`）：`score_story_line()` 对每条剧情台词打分——态度/评价词（喜欢/认为/必须…）+0.5、人物关系线索 +0.4、第一人称主观 +0.2、与 focus_role 相关 +0.8、信息量 +0.3；减分：纯事实陈述 -0.3、过短 -0.5、纯省略 -2.0。`get_story_lines` 按分数降序取高分台词（每节选分高的对话输出），供 opinion 事实底座使用。
- 验证：打分机制（"浮士德认为日光浴有益"+1.45 vs "……"-2.0 vs 事实陈述-0.15）；霍恩海姆台词从"自我介绍"变为"有态度的观点句"；52 个 pytest 全部通过。
