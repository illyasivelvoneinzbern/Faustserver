# P20-A 诊断报告：推特拉取 + 新推推送到群聊（只读诊断，为 P20-B 提供依据）

- 日期：2026-08-14（UTC+8）
- 范围：只读调查，未修改任何生产 `.py/.yaml/.json/.env`
- 诊断辅助脚本：`diag_p20_x_fetcher_probe.py`、`diag_p20_x_fetcher_probe2.py`（只读 GET、低频率、未落生产数据）
- 用户需求：完善拉取 `https://x.com/LimbusCompany_B` 的推文；收到**新推**时，把**整条内容（含图文，最好视频）**推送到 **agent 自身所在群聊**。

---

## 1) 现状盘点

### 1.1 推特拉取能力：`crawler/x_fetcher.py`（`XFeedFetcher`）

| 项目 | 现状 | 说明 |
|---|---|---|
| 拉取平台/账号 | X/Twitter，官方账号 `LimbusCompany_B`、`ProjMoonOfficial` | 硬编码在 `OFFICIAL_ACCOUNTS`（[`crawler/x_fetcher.py:24`](../crawler/x_fetcher.py:24)） |
| 协议 | **RSS 桥接**（`feedparser` 解析 Nitter RSS：`https://nitter.net/<handle>/rss`） | 非官方 API、非爬虫；见 [`x_fetcher.py:64`](../crawler/x_fetcher.py:64) |
| 返回结构 | `{id, title, url, content, categories, source, handle, account_name, published_at}` | 见 [`x_fetcher.py:98`](../crawler/x_fetcher.py:98) |
| **图片/视频 URL** | **不支持** | `clean_text(summary)` 把 summary 的 HTML 全部剥成纯文本，媒体标记（`<img>/<video>`）全部丢失 |
| **真推文 id（status id）** | **不支持** | `id` 是 `x_<handle>_<时间戳>` 生成的伪 id；真实 status id 在 `guid` 字段，未采集 |
| 状态/增量标记 | 有：`data/x_feed_state.json`（handle→ISO 时间） | 基于时间增量，非 id 增量；见 [`x_fetcher.py:45`](../crawler/x_fetcher.py:45) |
| 落地文件 | `data/raw/x_posts.jsonl`（JSONL 追加） | 见 [`x_fetcher.py:131`](../crawler/x_fetcher.py:131) |
| 是否被调用 | **未被任何代码调用** | 全项目 grep：`x_fetcher`/`fetch_x_feeds`/`XFeedFetcher` 仅存在于自身文件；[`main.py`](../main.py:1) 未引用 |
| 数据是否落地 | **从未落地** | `data/raw/` 下**无** `x_posts.jsonl`（仅 wiki 相关文件）；`data/x_feed_state.json` 不存在 |
| 运行依赖 | `feedparser` | **当前环境未安装**（`ModuleNotFoundError`），进一步印证从未跑通过 |

**实测结论（谨慎探测，仅 GET 2 个 RSS 端点）：**
- `https://nitter.net/LimbusCompany_B/rss` → **HTTP 200，21 条 item**（首次探测 15 条，二次 21 条，说明持续可拉且有增量）。
- item 的 `description`（CDATA HTML）内**含图片**：`<img src="https://nitter.net/pic/media%2FHPbT2FvbcAAXvjS.jpg">`，可正则提取；`<video>` 标记亦存在（探测到 `video` ×4）。
- `ProjMoonOfficial` 端点 → **HTTP 404**（该 Nitter 实例无此账号或已改名），需注意。
- 图片 URL 为 **Nitter 重写后的 `nitter.net/pic/...` 地址**，非原始 `pbs.twimg.com`，需二次跳转才能拿到原图。
- 该 RSS 是**全量公开可访问**（无需登录/cookie/付费 key），但 Nitter 实例常有被墙/失效风险。

**下游消费（存在但空转）：** [`agent/tools.py:34`](../agent/tools.py:34) 的 `fetch_limbus_news` 工具按 `source="x_twitter"` 检索资讯——但因数据从未落地，该工具实际查不到内容。

### 1.2 NapCat 发送能力：`adapter/napcat.py`（`NapCatAdapter`）

| 能力 | 现状 | 方法 |
|---|---|---|
| 发送群聊文本 | ✅ 支持 | `async def send_group_msg(self, group_id: int, message: str) -> bool`（[`napcat.py:88`](../adapter/napcat.py:88)） |
| 发送私聊文本 | ✅ 支持 | `async def send_private_msg(self, user_id: int, message: str) -> bool`（[`napcat.py:98`](../adapter/napcat.py:98)） |
| 发送图片 | ❌ 无 | 无 `send_image`/图片方法 |
| 发送视频 | ❌ 无 | 无视频方法 |
| 发送本地文件 | ❌ 无 | 无 `send_file`/上传方法 |
| 底层协议 | WebSocket JSON，`action: send_group_msg` / `send_private_msg`，`params.message` 为**字符串** | [`_send()`](../adapter/napcat.py:108) |
| 是否支持 CQ 码/媒体段 | ⚠️ 字符串可携带 CQ 码，但**无段数组（array）组装支持** | NapCat 的 `message` 字段同时接受字符串与段数组，当前代码只传字符串 |

关键点：现有 `_send` 只发**纯字符串**。NapCat（OneBot v11 兼容）的 `send_group_msg` 的 `message` 字段**本身支持** `[CQ:image,file=...]` / `[CQ:video,file=...]` 字符串 CQ 码，也支持**消息段数组** `[{"type":"image","data":{"file":"..."}}]`。因此**发送多模态能力在协议层存在，仅缺适配层封装**——这是 P20-B 主要新增点。

### 1.3 群号获取 / 会话维护

| 项目 | 现状 |
|---|---|
| 群号来源 | **事件来源**：`parse_napcat_event` 从 `event["group_id"]` 提取到 [`QQMessage.group_id`](../adapter/types.py:15) |
| 会话 id | `group_<group_id>` 或 `user_<user_id>`（[`router.get_session_id()`](../adapter/router.py:62)） |
| 回复目标 | `get_response_target()` 返回 `(group_id, is_group)`（[`router.py:68`](../adapter/router.py:68)）；`_send_reply` 据此发送（[`agent/core.py:653`](../agent/core.py:653)） |
| **"自身所在群聊"记录** | **不存在**。Agent 无持久化的"我所在的群"概念；群号只在收到消息时临时从事件里取。无法主动向"某个固定群"推送——P20-B 需新增（配置默认群号 或 首次入群/首条消息记忆） |
| 群名 | `group_name` 字段存在但**未填充**（`parse_napcat_event` 未从事件取 group_name） |

### 1.4 Agent 启动 / 常驻流程

| 项目 | 现状 |
|---|---|
| 常驻方式 | **常驻长连接监听**：`LimbusAgent.start()` 注册 `handle_event` 回调 → `NapCatAdapter.connect()` 进入 WebSocket 长连接 + 断线重连循环（[`napcat.py:51`](../adapter/napcat.py:51)） |
| 事件驱动 | NapCat 正向 WS 推送 → `_listen()` → 所有回调 → `handle_event` |
| 优雅关闭 | `shutdown()` 断开 WS；`main.py` 注册 SIGINT/SIGTERM（Windows 静默跳过）→ `finally: await agent.shutdown()`（[`main.py:204`](../main.py:204)） |
| 定时任务 | **无**。全项目零 `apscheduler` 引用（虽在 `requirements.txt`/`pyproject.toml` 声明）；`x_fetcher.fetch_interval_minutes` 配置项**无人消费** |
| 事件循环 | Windows 用 ProactorEventLoop（支持 Playwright 子进程，见 [`main.py:237`](../main.py:237)） |

**结论**：agent 是 asyncio 常驻进程，天然适合挂 asyncio 后台轮询任务；`start()` 是最合适挂载点。

### 1.5 配置 / 密钥 / 依赖

- **`config.yaml` 现有 X 相关配置**（[`config.yaml:112`](../config.yaml:112)）：`x_fetcher.enabled`、`x_fetcher.fetch_interval_minutes=30`、`x_fetcher.feed_state_path`——**均未被消费**。
- **`config.yaml` NapCat 配置**：`napcat.ws_url`、`napcat.token`（**明文 token 已提交到 config.yaml，属安全隐患**）、`trigger_keywords`、`command_prefix`。
- **`.env.example`**：仅有 `DEEPSEEK_API_KEY`、`SILICONFLOW_API_KEY`，**无任何 Twitter/X 密钥、无 NapCat 地址/token 的 env 约定**（token 直接写死 config.yaml）。
- **`.env` 存在**，但项目级 `utils/config.py` 只做 `${ENV}` 占位符替换，未从 .env 读 X/NapCat 配置。
- **依赖**：`requirements.txt`/`pyproject.toml` 已含 `feedparser`、`httpx`、`websockets`、`playwright`、`apscheduler`、`aiofiles`。**无 tweepy / twitter-api 相关库**。`feedparser` 当前环境实际未安装（需要 `pip install feedparser`）。
- 结论：现有拉取方案（Nitter RSS）**不需要付费 API key / cookie**，是公开方案；若走官方 API 才需付费 key（`BEARER_TOKEN`），若走 Playwright 抓 X 页面才需要登录 cookie。

---

## 2) 差距分析：用户需求 vs 现状

| 用户需求 | 现状 | 差距 | 复杂度 |
|---|---|---|---|
| 完善拉取 `LimbusCompany_B` | 有 RSS 拉取实现但**从未运行**；`ProjMoonOfficial` 404 | 需接入调用 + 修正/替换 Nitter | 低-中 |
| 检测"新推文" | 有基于**时间**的增量状态 | 时间增量会漏推/重推（同一秒多推、时区、Nitter 时间字段），**无 status id 去重** | 低-中 |
| 推送**整条内容**（文本+图片） | 文本能发；`clean_text` 把图片全部丢弃 | 需在拉取时**保留媒体 URL**；NapCat 需**新增图片发送** | 中 |
| 推送**视频** | 完全无视频能力 | NapCat 需新增视频发送；视频需下载/上传；Nitter RSS 里视频为 poster/直链形态待确认 | 高 |
| 推送**到自身所在群** | 无"自身所在群"概念 | 需新增配置默认群号 或 事件记忆群号 | 低 |
| **后台定时轮询** | 无任何定时任务 | 需在 `start()` 挂 asyncio 后台 task | 低-中 |

---

## 3) 可行方案建议

### 3.1 推荐方案（低门槛、最快见效）：扩展现有 RSS 桥接 + 纯 CQ 码推送

**拉取：扩展 `crawler/x_fetcher.py`（不改架构，仅改字段与去重）**
1. 保留 Nitter RSS 作为主源（实测可达，HTTP 200 / 21 条），把 `ProjMoonOfficial` 移除或标记失效（404）。
2. 增量从"时间"改为 **`guid`（status id）去重**：维护 `data/x_pushed_ids.json`（或复用 x_feed_state.json）记录已推送 id，拉取后按 `published` 排序，过滤已推送 id。
3. **保留媒体**：不再 `clean_text(summary)`，改为：
   - 正则提取 `<img src="...">` → `image_urls[]`；
   - 提取 `<video poster="...">` 与视频直链（`.mp4/.mov`）→ `video_urls[]`；
   - 文本用 `html` 转纯文本保留。
   - 返回结构新增字段：`tweet_id`（guid）、`text`（纯文本正文）、`image_urls`、`video_urls`、`permalink`（原推链接）。
4. 暴露 `async def fetch_new_tweets() -> list[dict]`，供轮询任务调用。

**新推检测与推送（新增 `crawler/x_push.py` 或 `agent/x_push.py` 模块）**
- 轮询间隔建议：**60~300 秒**（官方账号更新不频繁，RSS 秒级/分钟级可达；别对 Nitter 打太勤，防封 IP）。首推 120s。
- 推送逻辑：对新推按时间序 → 组装 CQ 消息 → 发送到目标群。

**多模态推送（NapCat 纯 CQ 码，最简路径，无需下载）**
- NapCat 的 `send_group_msg` 的 `message` 字段**可直接传 CQ 码字符串**。图片支持**远程 URL** 与本地路径；视频一般需**先下载到本地再传 `file://` 绝对路径**（NapCat 对远程视频 URL 支持不稳定）。
- 图片：`[CQ:image,file=https://...]`（远程直发，无需下载；若 Nitter `nitter.net/pic/...` 地址 NapCat 取不到，就先用 httpx 下载到 `data/media/` 再 `file://D:/Angela/data/media/xxx.jpg`）。
- 视频：httpx 下载 → `data/media/xxx.mp4` → `[CQ:video,file=file:///D:/Angela/data/media/xxx.mp4]`。
- 多图：同一消息可拼接多条 CQ 码（`text\n[CQ:image...]\n[CQ:image...]`），或拆成多条消息发送。
- **适配层新增方法**（不改旧方法，新增）：
  ```python
  async def send_group_msg_media(self, group_id: int, text: str,
                                 images: list[str] = None, videos: list[str] = None) -> bool
  ```
  内部组装 CQ 码字符串或消息段数组，走现有 `_send`。为兼容，也可新增 `send_group_msg_cq(group_id, cq_message: str)`。

**群号获取（P20-B 新增，三选一，建议组合）**
1. **配置默认群号**（最稳）：`config.yaml.napcat.push_group_id: 0`（用户填）。优先级最高。
2. **事件记忆**：`handle_event` 收到群消息时，把 `msg.group_id` 记录到 agent 的 `self.push_group_ids` 集合（内存/落盘 `data/push_groups.json`）；有推送时若未配置默认群则推给所有记忆过的群。
3. 发送方群号（等同 2）。

**后台任务挂载点（推荐：`LimbusAgent.start()`）**
```python
async def start(self):
    ...
    self.adapter.on_message(self.handle_event)
    if self.config.get("x_fetcher", {}).get("enabled", False):
        self._x_push_task = asyncio.create_task(self._x_poll_loop())
    await self.adapter.connect()

async def _x_poll_loop(self):
    while True:
        try:
            new = await fetch_new_tweets()
            for t in new:
                await self._push_tweet_to_groups(t)
        except Exception as e:
            self.log.error(...)
        await asyncio.sleep(self.config["x_fetcher"].get("fetch_interval_minutes", 2) * 60)

async def shutdown(self):
    if self._x_push_task:
        self._x_push_task.cancel()  # 捕获 CancelledError
    await self.adapter.disconnect()
```
- 优雅关闭：`shutdown()` 里 `cancel()` 轮询 task 并 `await` 吞掉 `CancelledError`（`main.py` 的 `finally` 已调用 `agent.shutdown()`）。
- 备选挂载点：`main.py` 里 `asyncio.create_task(...)`（不推荐，绕开 agent 封装）。

**所需配置/密钥清单（推荐方案只需 1 项，无密钥）**
| 配置项 | 用途 | 必填 |
|---|---|---|
| `x_fetcher.enabled` | 是否开启轮询 | ✅（已有） |
| `x_fetcher.fetch_interval_minutes` | 轮询间隔 | ✅（已有，改默认 2） |
| `x_fetcher.push_group_id` | 目标群号 | ✅ 新增 |
| `x_fetcher.rss_urls` | 自定义 RSS 端点（应对 Nitter 失效/换实例） | 推荐新增 |
| `napcat.media_dir` | 视频/图片临时下载目录 | 推荐新增 |
| 密钥 | **无需**（Nitter RSS 公开无需 cookie/token） | — |

### 3.2 备选方案 A：官方 Twitter API v2（最稳但付费/复杂）
- 用 `tweepy` 或直连 `https://api.twitter.com/2/users/<id>/tweets?tweet.fields=attachments,media`。
- 需 `BEARER_TOKEN`（付费/申请开发者账号），推文 id 稳定、媒体 URL（`pbs.twimg.com`/视频 `video.twimg.com`）原生、含 `media_type`（photo/video）。
- 若走此路，`.env` 需新增 `TWITTER_BEARER_TOKEN`；`x_fetcher` 增一个 `TwitterAPIProvider` 与 RSS Provider 并存。**需要用户提供凭据**。

### 3.3 备选方案 B：Playwright 无头浏览器抓 X 页面（复用 spider.py 思路，最重）
- 项目已有 Playwright 无头浏览器方案（[`crawler/spider.py`](../crawler/spider.py:1) 绕过 CloudFlare），可仿照写一个 `fetch_twitter` 用 Chromium 打开 `https://x.com/LimbusCompany_B` 抓 `article[data-testid="tweet"]` 结构与 `img/video`。
- **限制**：X 强制登录/弹窗，未登录页面常被反爬拦截；需要用户提供**登录 cookie**；Playwright 常驻成本高、易被风控。**不推荐作为首选**，仅当 Nitter/RSSHub 全挂时兜底。

### 3.4 媒体发送细节（NapCat / OneBot v11 事实标准）
| 媒体 | 推荐发送方式 | 说明 |
|---|---|---|
| 图片（远程 URL） | `[CQ:image,file=https://...]` | 最简；NapCat 自动下载。若 URL 被墙/重定向（nitter.net/pic）失败，改本地 |
| 图片（本地） | 先 httpx/aiohttp 下载 → `[CQ:image,file=file:///D:/...jpg]` | 最稳 |
| 多图 | 拼接多条 CQ 码 或 分多条消息 | QQ 单消息图片数量有上限（~20） |
| 视频（本地，推荐） | 下载 → `[CQ:video,file=file:///D:/...mp4]` | NapCat 对远程视频 URL 支持差，必须本地 |
| 文件（备用） | `[CQ:file,file=file:///...]` | 视频超大时降级发文件 |

- 建议在 `adapter/napcat.py` 新增 `send_group_msg_media(group_id, text, images, videos)`，内部：文本段 + 图片段 + 视频段拼成 CQ 字符串或段数组，走 `_send`。**同时保留 `send_group_msg` 兼容旧调用**。
- `QQMessage`/`router` 无需改动（推送是主动行为，不走 `handle_event` 链路）。

---

## 4) 风险与前置条件

1. **Nitter 稳定性**：Nitter 实例常被墙/封/失效（本次 `ProjMoonOfficial` 即 404）。RSS 端点不可达时轮询应静默降级并告警日志，不要 crash。建议 RSS URL 做成可配置，支持多个镜像（如 `nitter.poast.org`、`nitter.privacydev.net` 等公共实例）。
2. **图片 URL 为 Nitter 重写地址**：`nitter.net/pic/media%2F...` 需二次跳转；NapCat 直发远程 URL 可能拿不到原图 → 优先本地下载后发 `file://`。
3. **视频获取限制**：Nitter RSS 的 `<video poster>` 是封面，**实际视频流 URL 需解析推文页/媒体源**；视频文件通常较大（几十~几百 MB），NapCat/QQ 对群视频有**大小与时长限制**（QQ 群视频一般 ≤ 500MB、不同客户端显示不同）。超大视频建议降级为发 `[CQ:file]` 或仅发文字+链接。
4. **NapCat 上传限制**：NapCat 默认限制本地文件/视频大小（可调 NapCat 配置），超限发送会失败，需 try/except 并日志记录。
5. **凭据安全**：`config.yaml` 中 `napcat.token` 为**明文**（当前已存在），建议 P20-B 顺带迁移到 `.env`（`NAPCAT_TOKEN`）。RSS 方案无需新增密钥，但官方 API / Playwright 方案需要 `TWITTER_BEARER_TOKEN` / X cookie（由用户提供）。
6. **频率/封 IP**：轮询间隔别太短（≥60s），对 Nitter 打太勤有封 IP 风险。
7. **幂等/防重推**：必须用 status id 去重（不能只靠时间），否则重启后可能重推；本地状态文件 `data/x_pushed_ids.json` 持久化。
8. **Windows 事件循环**：main.py 用 ProactorEventLoop，asyncio.create_task + httpx 无冲突；若未来用 Playwright 抓 X，需在**主 agent 进程内**或单独进程，避免阻塞 WS 监听。

---

## 5) 供 P20-B 使用的实现要点清单

- [ ] **A1 依赖**：`pip install feedparser`（当前环境缺）；确认 `httpx`/`aiofiles` 已装。
- [ ] **A2 改造 `crawler/x_fetcher.py`**：
  - 移除/注释失效的 `ProjMoonOfficial`（404）。
  - 增量状态改为 **status id 去重**（持久化 `data/x_pushed_ids.json`）。
  - 保留 HTML summary → 提取 `text / image_urls[] / video_urls[] / tweet_id / permalink`，不再全量 `clean_text`。
  - 新增 `async def fetch_new_tweets()`。
- [ ] **A3 新增 `crawler/x_push.py`（或 `agent/x_push.py`）**：`async def poll_and_push(fetcher, adapter, push_group_ids, media_dir)`，负责拉新推→下载媒体→组装 CQ→发送→记录已推 id。
- [ ] **A4 改造 `adapter/napcat.py`**：新增 `send_group_msg_media(group_id, text, images, videos)`（CQ 码拼接/消息段数组），保留旧 `send_group_msg`。图片优先本地 `file://`；视频本地 `file://`；超大降级 `[CQ:file]`。
- [ ] **A5 群号**：`config.yaml` 新增 `napcat.push_group_id`（默认群，优先级最高）；`handle_event` 记忆群消息的 `group_id` 到 `data/push_groups.json`（兜底）。
- [ ] **A6 后台任务**：`agent/core.py` `start()` 里 `asyncio.create_task(self._x_poll_loop())`（读 `x_fetcher.enabled`/`fetch_interval_minutes`）；`shutdown()` 里 cancel + 吞 `CancelledError`。
- [ ] **A7 配置**：`config.yaml` 增加 `x_fetcher.push_group_id`、`rss_urls`（可配置镜像）、`napcat.media_dir`；`x_fetcher.fetch_interval_minutes` 默认改 2。
- [ ] **A8 媒体下载**：`aiofiles`/`httpx` 下载图片/视频到 `media_dir`，文件名用 tweet_id，避免重名。
- [ ] **A9 容错**：Nitter 不可达→静默重试+日志告警；单条失败不影响后续；发送失败记录待重试。
- [ ] **A10 可选安全**：把 `napcat.token` 明文迁移到 `.env`（`NAPCAT_TOKEN`），config 用 `${NAPCAT_TOKEN}`。
- [ ] **A11 测试**：`python diag_p20_x_fetcher_probe.py` 验证 RSS 可达；`send_group_msg_media` 先用测试群手测（发 1 图/1 视频确认 NapCat 端效果）。

---

## 附：诊断脚本说明（只读，未写生产数据）
- `diag_p20_x_fetcher_probe.py`：GET 主/备 RSS 端点，粗检条目数、`<img>/<video>/pic.twitter` 标记。实测：`LimbusCompany_B` HTTP 200 / 15~21 条 / 含 img×21、video×4；`ProjMoonOfficial` 404。
- `diag_p20_x_fetcher_probe2.py`：单次 GET，解析前 2 条 item 的原始 HTML，确认图片 URL 形态（`nitter.net/pic/media%2F....jpg`）与文本正文。两者均未写入 `data/raw`、未触碰账号凭据。
