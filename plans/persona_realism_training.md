# 拟真人格训练方案（P38）：从"角色卡"到"像角色一样说话"

> **日期**: 2026-08-16
> **范围**: 调研 + Phase 1 实现（台词库引擎 / 拟真生成引擎 / 数据管道 / 拟真度评测）
> **一句话**: 角色卡告诉模型"角色是谁"，拟真方案让模型**用角色的真实台词与心理**去说话。

---

## 一、为什么"角色卡"不够拟真

现状（P28/P29 已落地）：每个罪人一个 YAML 角色卡，编译成 System Prompt——
身份 / 性格 traits / 好恶 / 人物关系 / 说话风格 / 3 条人工 few-shot / 问候语。

| 拟真缺口 | 现状表现 | 根因 |
|----------|----------|------|
| **语气不真** | 回复"像 AI 在扮演"，而不是像角色本人 | 角色卡只给抽象描述，模型没有见过角色**真实说话**的样子；3 条 few-shot 覆盖不了对话空间 |
| **情绪反应缺失** | 对"输得很惨""夸你"等话题回复中性百科腔 | 无"角色此刻心理状态"建模，模型不先"代入"再"开口" |
| **破格无约束** | 偶尔冒出助手腔/括号神态/现代常识 | 无生成后一致性校验闭环 |
| **关系是静态的** | 角色对用户的态度从一而终 | 无会话级关系/记忆演化 |
| **改进不可验证** | 调 prompt 全靠手感 | 无"拟真度"评测集与裁判 |

一句话：**角色卡 = 设定；拟真 = 用角色的真实语料 + 心理过程 + 校验闭环让模型"演"出来。**

---

## 二、技术路线调研（2025-2026 状态）

### 路线 A：检索式人格增强（Retrieval-Augmented Persona, RAP）

核心思想：生成时**检索角色真实台词**（剧情/语音语料）作为说话样本注入，让模型模仿而非凭空想象。

- [Dynamic Context Adaptation for Consistent Role-Playing Agents with Retrieval-Augmented Generation](https://arxiv.org/html/2508.02016)（arXiv 2508.02016）：按对话动态检索角色设定片段注入，显著提升一致性。
- [DR.Roleplay: Role-Play LLM with DPO + RAG](https://ieeexplore.ieee.org/document/11461546)：RAG 检索 + 直接偏好优化（DPO）组合，台词检索供事实，DPO 学"像不像"。
- [CharacterGPT: Persona Reconstruction Framework](https://aclanthology.org/2025.naacl-industry.24/)：从角色语料重建人格表征。
- 社区实践（SillyTavern 系）也大量使用"台词库/角色语录"作为 few-shot 锚点。

**结论**：本项目**数据现成**（4.5 万条剧情台词 + 每角色官方语音），RAP 是成本最低、见效最快的路线 → **已实现**。

### 路线 B：推理时角色心理建模（Thinking in Character / Role-Aware Reasoning）

核心思想：生成台词前，先让模型**以角色视角思考**（此刻感受、态度、想说什么），再出口成句；推理痕迹不发送。

- [Thinking in Character: Role-Aware Reasoning](https://neurips.cc/virtual/2025/loc/san-diego/poster/116722)（NeurIPS 2025）："角色感知推理"显著提升角色扮演质量。
- [Act-LLM: whole-process chain for character-centric role-playing](https://www.sciencedirect.com/science/article/abs/pii/S0957417425026417)：完整角色扮演链路（感知→思考→行动→表达）。
- [Codifying Character Logic in Role-Playing](https://mlanthology.org/neurips/2025/peng2025neurips-codifying/)（NeurIPS 2025）：把角色"行为逻辑"显式编码，先逻辑后表达。

**结论**：两阶段（心理→台词）是业界共识；本项目用**单次调用内嵌【内心反应】段再剥离**实现，不增加延迟 → **已实现**（mode=thinking）。

### 路线 C：一致性后校验与修复（Consistency Check）

核心思想：生成后检测"破格"（AI 腔/括号神态/人设冲突），必要时重写。

- [CharacterBench: Benchmarking Character Customization](https://mlanthology.org/aaai/2025/zhou2025aaai-characterbench/)（AAAI 2025）：CharacterBench 基准把角色定制能力拆成可评分的维度。
- [Enhancing Persona Consistency with Large Language Models](https://dl.acm.org/doi/fullHtml/10.1145/3670105.3670140)：用 LLM 生成一致性问答并自我校验。
- [Post Persona Alignment for Multi-Session Dialogue](https://aclanthology.org/2025.findings-emnlp.1098/)（EMNLP 2025 Findings）：生成后对齐修正。

**结论**：规则检测（零成本）+ 可选 LLM 判定两级方案 → **已实现**（consistency=rules/llm）。

### 路线 D：微调（LoRA / DPO / 全参）

核心思想：用**角色对话数据集**微调模型本身，让"像这个角色"成为模型能力而非 prompt 技巧。

- [PEFT of Lightweight LLMs for Persona-Based Dialogue](https://nur.nu.edu.kz/items/10b6532c-872b-456c-97ff-cc327d69dbda)：LoRA 级参数高效微调即可学出人格风格。
- [Fine-Tuning LLMs for Consistent Character-Based AI](https://luc.finna.fi/ulapland/Record/theseus_lapinamk.10024_924547)：角色一致性可被微调显著增强。

**评估**：本项目走 DeepSeek API（无本地 GPU、无微调接口诉求），
微调**不适合当前架构**；但台词库可导出为微调数据集，作为**远期可选路线**
（自托管 Qwen 等开源模型时使用）→ **数据管道已具备导出能力，微调列为 Phase 3**。

### 路线 E：多会话记忆与关系演化

- [Post Persona Alignment for Multi-Session Dialogue](https://aclanthology.org/2025.findings-emnlp.1098/)：跨会话保持人格连续。
- CharacterGPT / 社区"好感度系统"：角色对用户的好感、称呼、共同经历随会话演化，是"拟人感"的重要来源。

**结论**：列为 Phase 2（会话级角色状态：好感度/称呼/共同记忆，简单规则 + 可选 LLM 抽取）。

### 评测基准

- [CharacterBench](https://mlanthology.org/aaai/2025/zhou2025aaai-characterbench/) 提供"角色定制能力"评分框架；
- 社区常用 LLM-as-Judge 对"像不像"打分（性格/语气/情绪/边界四维）。
→ 本项目评测脚本按四维裁判实现（见 `scripts/eval_persona.py`）。

---

## 三、推荐架构（本项目落地）

```
┌─────────────────── 数据底座（一次性/增量，离线） ───────────────────┐
│ wiki_pages.jsonl 剧情台词（45k+ 条，含章节/前后句/互动对象）           │
│ + data/structured/personas/*.json 官方语音（20 条/人，含场景）         │
│        ↓  rag/persona_corpus.py（懒加载 + 进程内缓存，零 API 成本）   │
│        台词库：{角色 → PersonaLine(text, scene, source, 上下文)}      │
└───────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────── 回复时（运行时） ──────────────────────────────────┐
│ 1. 意图门控（已有）→ 直答四件套（已有）→ RAG 检索（已有）              │
│ 2. rag/persona_engine.py（P38 新增）                                  │
│     ├─ 台词检索：query=用户消息+近期对话 → 该角色 top-k 真实台词        │
│    │   打分：jieba 词面命中 + 观点/问候/战斗意图加权 + 语音权重         │
│    ├─ 【说话样本】注入 Human Prompt（模仿语气，禁止照抄）              │
│    ├─ mode=thinking：【内心反应】段（角色视角思考）→ 台词，输出后剥离  │
│    └─ 一致性自检：rules（括号神态/AI腔/客服腔/第三人称自称）→          │
│         轻量修复；consistency=llm 时追加 LLM 判定/重写                 │
│ 3. 现有输出净化（P32 神态清洗）                                       │
└───────────────────────────────────────────────────────────────────────┘
```

### 关键设计决策

1. **零额外延迟（单次 LLM 调用）**：thinking 模式在**同一调用**内先写【内心反应】再写台词，
   程序剥离内心段后发送——不增加调用次数，只多几十 token。
2. **零额外 API 成本**：台词检索为纯词面算法（jieba + 加权），不调 embedding/LLM。
3. **默认关闭**：`persona_training.enabled=false`，启用前后行为完全兼容，可灰度。
4. **数据驱动**：全部素材来自官方剧情/语音，台词样本带场景标注，LLM 能理解"什么场合说什么"。
5. **双保险破格防护**：规则检测零成本常开；LLM 判定可选（更准，多一次调用）。

---

## 四、Phase 1 已实现清单（本方案落地）

| 模块 | 文件 | 说明 |
|------|------|------|
| 台词库引擎 | [`rag/persona_corpus.py`](../rag/persona_corpus.py) | 剧情台词 + 官方语音 → 角色台词索引；`retrieve_lines()` 按语境检索；零 API 成本 |
| 拟真生成引擎 | [`rag/persona_engine.py`](../rag/persona_engine.py) | 说话样本注入、内心反应建模（剥离）、规则/LLM 一致性自检、快速修复 |
| Chain 接线 | [`rag/chain.py`](../rag/chain.py) | `build_rag_chain` 支持 `persona_engine`；`run_rag_query` 生成后自检 |
| Agent 接线 | [`agent/core.py`](../agent/core.py) | 按配置构建引擎并注入链；两个 RAG 调用点透传 |
| 配置 | [`config.yaml`](../config.yaml) | 新增 `persona_training` 段（默认关） |
| 数据管道 | [`scripts/build_persona_corpus.py`](../scripts/build_persona_corpus.py) | 台词库统计 / 角色台词预览 / 导出（兼作微调数据集源） |
| 拟真度评测 | [`scripts/eval_persona.py`](../scripts/eval_persona.py) | 基线 vs 增强 A/B，四维 LLM 裁判打分，输出对比报告 |

### 启用方式

```yaml
persona_training:
  enabled: true
  mode: "thinking"        # samples | thinking
  max_samples: 3
  consistency: "rules"    # rules | llm
```

体验阶梯：`samples`（先感受语气差异）→ `thinking`（推荐，心理建模）→ `consistency: llm`（最严）。

---

## 五、分阶段路线图

### Phase 2：会话级角色状态（关系演化）— 建议下一迭代
- `agent/session.py` 增加 `persona_state`：好感度（-3~+3）、称呼、共同记忆（最近 5 条关键事件）；
- 每轮用轻量规则/可选 LLM 抽取"用户行为 → 关系影响"（夸赞 +1、冒犯 -1、帮助 +1…）；
- 注入 System Prompt：【当前与这位经理的关系】——让角色**记得用户、态度随相处演化**；
- 关系记忆持久化（JSON，按会话）。

### Phase 3：微调数据集（远期，自托管模型时）
- `scripts/build_persona_corpus.py --export` 已能导出每角色台词库；
- 用 LLM 依据台词库合成「用户提问 → 角色台词」多轮对话对（参照 DR.Roleplay 的 RAG+DPO 思路）；
- 对开源模型（Qwen 7B/14B 等）做 LoRA 微调，得到角色专属底座，推理时再叠加 RAG。

### Phase 4：评测自动化与回归
- 把 `scripts/eval_persona.py` 的测试集固化为回归集（每角色 6 题 × 4 维）；
- 每次人格/引擎改动后跑评测，要求增强均分不降、破格数不增；
- 报告落盘 `plans/eval_persona_report.md`，纳入 Git 对比历史。

---

## 六、评测方案（已实现）

| 维度 | 定义 | 打分 |
|------|------|------|
| 性格一致性 | 是否符合角色性格/立场/好恶 | 1~5 |
| 语气与句式 | 是否像角色真实台词（用词/口头禅/句式） | 1~5 |
| 情绪反应 | 是否有符合立场的情绪反馈，非中性百科腔 | 1~5 |
| 知识与边界 | 是否守住角色认知边界，不越界/不现代常识化 | 1~5 |

- 探测题覆盖：问候 / 观点 / 人物关系 / 剧情知识 / 情绪触发 / 闲聊；
- 基线（角色卡 only）与增强（thinking）A/B，同一裁判同题打分；
- 另附规则破格检测计数（括号神态/AI 腔/客服腔/自称偏差）。

运行：
```bash
venv\Scripts\python.exe scripts\eval_persona.py --persona faust --limit 3 --out plans\eval_persona_report.md
```

---

## 七、风险与成本

| 风险 | 说明 | 对策 |
|------|------|------|
| 台词样本被"照抄" | LLM 可能直接复读检索到的台词 | Prompt 明确"模仿语气，不得照抄"；检索打分偏向**短句/通用表达**而非长独白 |
| thinking 段剥离失败 | LLM 不按【内心反应】格式输出 | 剥离逻辑兼容 3 种格式；剥离后为空则原样返回 |
| token 增加 | thinking 多输出 ~50 token/轮 | 仅启用后产生；默认关闭；可用 `samples` 模式省掉 |
| 裁判主观性 | LLM 裁判打分有噪声 | 每轮 4 维取均值；回归看趋势而非单次绝对值 |
| 与现有链路冲突 | 直答数据表（人格/敌方）不走此链路 | 引擎只作用于 RAG 生成路径，直答保持确定性 |

**成本估算**（DeepSeek，启用 thinking + rules）：每轮额外 ~60~120 token（样本注入 + 内心反应），
按 100 万 token 约 1~2 元计，单条消息增加成本可忽略；`consistency: llm` 翻倍调用，按需开启。

---

## 八、参考资料

- Dynamic Context Adaptation for Consistent Role-Playing Agents with RAG — https://arxiv.org/html/2508.02016
- DR.Roleplay: Role-Play LLM with DPO + RAG — https://ieeexplore.ieee.org/document/11461546
- Thinking in Character: Role-Aware Reasoning (NeurIPS 2025) — https://neurips.cc/virtual/2025/loc/san-diego/poster/116722
- Act-LLM: whole-process chain for character-centric role-playing — https://www.sciencedirect.com/science/article/abs/pii/S0957417425026417
- Codifying Character Logic in Role-Playing (NeurIPS 2025) — https://mlanthology.org/neurips/2025/peng2025neurips-codifying/
- CharacterGPT: Persona Reconstruction (NAACL 2025 Industry) — https://aclanthology.org/2025.naacl-industry.24/
- CharacterBench: Benchmarking Character Customization (AAAI 2025) — https://mlanthology.org/aaai/2025/zhou2025aaai-characterbench/
- Enhancing Persona Consistency with LLMs — https://dl.acm.org/doi/fullHtml/10.1145/3670105.3670140
- Post Persona Alignment for Multi-Session Dialogue (EMNLP 2025 Findings) — https://aclanthology.org/2025.findings-emnlp.1098/
- PEFT of Lightweight LLMs for Persona-Based Dialogue — https://nur.nu.edu.kz/items/10b6532c-872b-456c-97ff-cc327d69dbda
- Fine-Tuning LLMs for Consistent Character-Based AI — https://luc.finna.fi/ulapland/Record/theseus_lapinamk.10024_924547
