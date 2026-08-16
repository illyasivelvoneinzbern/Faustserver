# 边狱巴士 RAG Agent（Limbus RAG Agent）

> 基于 LangChain 构建的边狱巴士（Limbus Company）RAG 智能体。融合灰机 Wiki 知识库 + X/Twitter 官方资讯，以自定义角色人格（12 罪人 + 执行管理人但丁）通过 QQ 与玩家交流。

---

## 功能特性

- **🧠 RAG 知识问答**：向量检索（ChromaDB）+ BM25 混合 + 渐进式多轮检索 + LLM 查询扩展，回答角色、人格、E.G.O、剧情、敌方等设定问题
- **⚡ 结构化直答四件套**：人格 / 饰品 / 事件 / 敌方数据从 `data/structured/*.json` **确定性取数**（绕过 LLM、无幻觉），命中即精确返回
- **🎭 人格扮演**：13 个罪人扮演人格（李箱~格里高尔 + 但丁），数据驱动重拟——含**好恶（likes/dislikes）**、**人物关系（relationships）**、few-shot 示例、问候语；但丁按设定**只以钟表声回应**
- **🗣 观点类问答结合剧情**：意图门控区分「观点 / 比较 / 数据 / 闲聊」——观点类（"你怎么看里恩？"）注入该角色**剧情身份 + 台词 + 人物互动**（含打分选句），且检索仅限剧情来源；"该怎么打"等游戏机制类仍走数据直答
- **🎲 抽奖（Gacha）**：三灯人格 3% / 二灯人格 13% / 一灯人格 81% / EGO 3%，十连保底 1 个二灯；支持单抽/十连，提供 **MCP stdio server** 工具
- **🐦 官方资讯拉取**：Nitter RSS 轮询推送 X/Twitter 官方账号（@LimbusCompany_B）新推；**视频推文自动解析真实 mp4**（syndication API，1080p，分开发送）；RT 过滤、链接清洗、首启水位线防刷屏
- **🛡️ 安全防护**：敏感词过滤 + 频率控制 + 打字延迟模拟 + 违规熔断
- **🔍 检索增强**：昵称/俗称展开（兔浮→浮士德黑兽-卯魁首）、敌方名模糊匹配、章节编号直查、语义缓存、自我反思闭环
- **🪄 输出净化**：回复自动移除神态/动作描写（如"（扫了一眼屏幕）"），只保留角色话语

---

## 项目结构

```
D:\Angela\
├── main.py                    # 程序入口（启动 / 数据管道）
├── config.yaml                # 全局配置
├── .env.example               # 环境变量模板
│
├── crawler/                   # 数据抓取
│   ├── spider.py              # Wiki 爬虫（Playwright 绕过 CloudFlare）
│   ├── x_fetcher.py           # X/Twitter RSS 拉取 + 视频 mp4 解析
│   ├── html_extractor.py      # 渲染 HTML 结构化提取（人格/敌方/事件/剧情）
│   ├── buffs_data.py          # BuffPro 状态效果中文化（页面级配对映射）
│   └── export.py              # JSONL 导出
│
├── rag/                       # RAG 核心
│   ├── chain.py               # RAG Chain（分层 System Prompt）
│   ├── retriever.py           # 渐进式检索（向量+BM25+章节直查）
│   ├── intent_gate.py         # 意图门控（观点/比较/数据/闲聊）
│   ├── story_facts.py         # 剧情事实底座（台词索引+打分+人物互动）
│   ├── persona_direct.py      # 人格结构化直答（含比较直答）
│   ├── gift_direct.py         # 饰品直答
│   ├── event_direct.py        # 事件直答
│   └── enemy_direct.py        # 敌方直答（含比较直答）
│
├── personas/                  # 扮演人格（13 个罪人 YAML）
│   ├── faust.yaml             # 示例（含 likes/dislikes/relationships）
│   ├── dante.yaml             # 但丁（只回钟表声）
│   └── manager.py             # 人格管理器（编译 System Prompt）
│
├── tools/                     # 独立工具
│   ├── gacha.py               # 抽奖核心（概率+保底）
│   └── gacha_mcp_server.py    # 抽奖 MCP stdio server
│
├── agent/                     # Agent 核心
│   ├── core.py                # 主控制器（意图门控/抽奖/推文指令）
│   ├── tools.py               # 工具定义（gacha 等）
│   ├── session.py / memory.py # 会话与短期记忆（10 轮窗口）
│
├── adapter/                   # 消息平台
│   ├── napcat.py              # NapCatQQ 适配（媒体分开发送）
│   └── router.py              # 消息路由
│
├── utils/                     # 安全/工具
│   ├── sensitive_filter.py    # 敏感词过滤
│   ├── rate_limiter.py        # 频率控制
│   └── token_saver.py         # 语义缓存
│
├── scripts/                   # 数据与工具脚本
│   ├── build_gacha_data.py    # 生成抽奖稀有度数据
│   ├── extract_sinner_data.py # 提取罪人语音+剧情素材
│   ├── persona_gen.py         # 生成罪人扮演 YAML 骨架
│   ├── mine_relationships.py  # 统计剧情人物互动
│   └── rebuild_vector_db.py   # 重建向量库
│
└── data/                      # 数据目录
    ├── raw/                   # 爬取原始数据（wiki_pages.jsonl 等）
    ├── structured/            # 结构化 JSON（personas/enemies/events/gifts）
    ├── gacha/                 # 抽奖数据（rarity.json / ego_pool.json）
    ├── vector_db/             # ChromaDB 向量库
    └── media/                 # 推文媒体落地
```

---

## 快速开始

### 1. 环境准备

```bash
git clone <repo-url> && cd <repo-dir>
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / macOS
source venv/bin/activate
pip install -r requirements.txt
pip install playwright && playwright install chromium
```

### 2. 配置 API Key

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
```

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | LLM API Key（DeepSeek，推荐） （仅调用chat费用极低）|
| `SILICONFLOW_API_KEY` | Embedding API Key（硅基流动 BGE-M3）（本地与云端均免费）|
| `NAPCAT_TOKEN` | NapCatQQ 鉴权 Token（可选） |
| `NAPCAT_QQ` | Bot 自身 QQ 号（用于群聊 @检测） |

### 3. 抓取 Wiki 数据 & 构建向量库

```bash
# 全量爬取 + 向量化（首次运行，需联网）
python main.py --full-crawl
# 增量爬取（仅更新变更页面）
python main.py crawl
```

### 4. 配置 NapCatQQ

1. 下载并安装 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)，登录 QQ 账号
2. 启用 WebSocket 服务（默认端口 `3001`）

### 5. 启动 Agent

```bash
python main.py
```

启动后：私聊直接发消息；群聊 `@机器人` 或触发关键词（边狱、巴士、limbus、罪人、人格）。

---

## 可用指令

| 指令 | 说明 |
|------|------|
| `/状态` | 查看 Bot 运行状态 |
| `/帮助` | 显示帮助 |
| `/人格列表` | 查看所有可用人格 |
| `/人格切换 <id>` | 切换当前会话人格（如 `/人格切换 堂吉诃德`） |
| `/最新推文 [N]` | 拉取官方最新 N 条推文（含图片/视频，N 默认 3 最多 5） |
| `抽奖` / `十连` / `来一发十连` | 抽奖（三灯3%/二灯13%/一灯81%/EGO 3%，十连保底二灯） |

对话示例：
- 数据类：`兔浮的技能是？` `雷横的数据` `烧伤效果`
- 观点类：`你怎么看里恩？` `你觉得霍恩海姆怎么样`
- 比较类：`兔浮和W浮谁更强` `雷横和拇指士兵谁厉害`
- 剧情类：`第一章讲了什么` `8-33 讲了什么`

---

## 架构概览

```
📦 数据层           🧠 推理层         🤖 Agent 层          🛡️ 安全层        📡 接入层
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ Wiki爬虫  │───▶│ 检索器    │───▶│ 意图门控  │───▶│ 敏感词过滤│───▶│ NapCatQQ │
│ X拉取    │    │ 人格Prompt│    │ 直答四件套│    │ 频率控制  │    │ 消息路由 │
│ 结构化导出│    │ 剧情事实  │    │ 人格扮演  │    │ 打字延迟  │    │ 会话管理 │
└──────────┘    └──────────┘    │ 抽奖/推文 │    └──────────┘    └──────────┘
      │               │         └──────────┘
      └───────────────┴──────────────┬───────────────────────────────┘
                              ChromaDB + data/structured
```

**核心链路**：消息 → 意图门控（opinion/compare/data/other）→ 直答四件套（数据类确定性取数）或 剧情事实+仅剧情检索（观点类）或 RAG（其他）→ 人格 System Prompt（好恶/关系/看法规则）→ LLM 生成 → 神态清洗 → 发送。

---

## 配置说明（config.yaml 关键段）

| 配置段 | 说明 |
|--------|------|
| `llm` | LLM 模型（DeepSeek） |
| `embedding` | Embedding（硅基流动 BGE-M3） |
| `retrieval` | 检索参数（top_k、渐进式、查询扩展、噪声过滤） |
| `x_fetcher` | X 拉取（accounts、`filter_retweets`、`video_quality`、`push_group_id`、水位线） |
| `agent` | 直答开关（persona/gift/event/enemy direct）、默认人格 |
| `confidence` | 置信度评估与自我反思 |
| `napcat` | NapCatQQ 连接 |
| `sensitive_filter` / `rate_limit` / `typing_delay` | 安全防护 |

---

## 角色人格定制

```yaml
id: "faust"                 # 唯一 ID
name / display_name: "浮士德"
identity: "身份与性格概括"
traits:                     # 性格特点（可附台词出处）
likes:                      # 好恶：喜欢（附台词出处）
dislikes:                   # 好恶：讨厌
relationships:              # 人物关系：{角色名: 关系描述}
speech_style:               # 说话风格
catchphrase / greeting_template:   # 口头禅 / 问候语
examples:                   # few-shot 对话示例（user/reply）
knowledge_scope:            # 知识范围
advanced:                   # 进阶（回复长度、回避话题等）
```

素材重拟：`scripts/extract_sinner_data.py 浮士德` 提取语音+剧情台词 → 参考 `faust.yaml` 格式人工打磨。

---

## 免责声明

本项目仅供学习和研究使用。使用者需自行承担：
- QQ 账号安全风险（请先在测试群充分验证）
- API 调用费用
- 遵守相关平台（QQ / X / 灰机 Wiki）使用条款