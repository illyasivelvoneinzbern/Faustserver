# -*- coding: utf-8 -*-
"""拟真度评测：基线（仅角色卡） vs 增强（台词样本 + 内心反应建模）。

评测维度（每维 1~5 分）：
1. 性格一致性   —— 是否贴合角色性格设定，不破格
2. 语气与句式   —— 是否像角色的真实台词（用词/句式/口头禅）
3. 情绪反应     —— 是否对话题有符合角色立场的情绪反馈（而非中性百科腔）
4. 知识与边界   —— 是否守住角色认知边界，不越界、不"现代常识化"

流程：加载人格 → 每角色 N 个探测问题（问候/观点/关系/剧情/情绪）→
基线 Prompt 与增强 Prompt 各生成一版回复 → LLM 裁判逐维打分 →
输出对比报告（markdown）与破格规则检测结果。

用法（需 DEEPSEEK_API_KEY）：
    venv\\Scripts\\python.exe scripts\\eval_persona.py                 # 全角色
    venv\\Scripts\\python.exe scripts\\eval_persona.py --persona faust
    venv\\Scripts\\python.exe scripts\\eval_persona.py --limit 2 --out plans/eval_persona_report.md
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 测试集：探测问题（每角色同一套，考察不同维度） ──
PROBES = [
    ("greeting",   "你好，我是新来的经理。"),
    ("opinion",    "你怎么看希斯克利夫这个人？"),
    ("relationship", "你觉得但丁怎么样？"),
    ("lore",       "脑啡肽到底是什么东西？"),
    ("emotion",    "我们这次输得可真惨。"),
    ("chat",       "今天天气不错，出来走走吗？"),
]

JUDGE_PROMPT = """你是角色扮演拟真度评审。下面是一位角色的设定与一段AI扮演回复，请从四个维度打分（每项 1~5 分，整数）：

【角色设定】
{persona_summary}

【用户说的话】
{question}

【AI 扮演回复】
{reply}

打分维度：
- 性格一致性：回复是否符合角色的性格、立场、好恶？
- 语气与句式：用词/句式/口头禅是否像该角色的真实台词（而不是通用AI腔）？
- 情绪反应：是否有符合角色立场的情绪/态度反馈，而非中性客观的百科式陈述？
- 知识与边界：是否守住角色的认知边界（不越界、不套用现代常识）？

输出格式（每行一项，只输出四项，不要其他内容）：
性格一致性: N
语气与句式: N
情绪反应: N
知识与边界: N"""


def build_llm(config: dict):
    from langchain_openai import ChatOpenAI
    llm_cfg = config["llm"]
    return ChatOpenAI(
        model=llm_cfg["model"],
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg.get("base_url"),
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 512),
    )


def persona_summary(persona: dict) -> str:
    name = persona.get("display_name") or persona.get("name", "?")
    parts = [f"{name}：{persona.get('identity', '')}"]
    parts.append("性格：" + "；".join(persona.get("traits", []) or []))
    parts.append("口吻：" + "；".join(persona.get("speech_style", []) or []))
    if persona.get("catchphrase"):
        parts.append(f"口头禅：{persona['catchphrase']}")
    return "\n".join(parts)


def parse_scores(text: str) -> dict[str, int]:
    scores = {"性格一致性": 0, "语气与句式": 0, "情绪反应": 0, "知识与边界": 0}
    for line in (text or "").split("\n"):
        for dim in scores:
            if dim in line:
                try:
                    scores[dim] = min(5, max(1, int(line.split(":")[-1].strip())))
                except Exception:
                    pass
    return scores


async def generate(llm, prompt: str) -> str:
    try:
        resp = await llm.ainvoke(prompt)
        return (resp.content or "").strip()
    except Exception as e:
        print(f"  ! 生成失败: {e}")
        return ""


async def judge(llm, persona_summary_txt: str, question: str, reply: str) -> dict[str, int]:
    prompt = JUDGE_PROMPT.format(
        persona_summary=persona_summary_txt, question=question, reply=reply
    )
    resp = await generate(llm, prompt)
    return parse_scores(resp)


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="拟真度评测")
    parser.add_argument("--persona", default="", help="只评测指定人格 ID（默认全部）")
    parser.add_argument("--limit", type=int, default=3, help="每角色最多评测题数")
    parser.add_argument("--out", default="", help="报告输出路径（markdown）")
    args = parser.parse_args()

    from utils.config import get_config
    try:
        config = get_config()
    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)

    api_key = config["llm"].get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key or api_key.startswith("${"):
        print("错误：缺少 DEEPSEEK_API_KEY，无法评测。请先配置 .env")
        sys.exit(1)

    from personas.manager import PersonaManager
    from rag.persona_corpus import PersonaCorpus
    from rag.persona_engine import PersonaEngine, rules_consistency_check, strip_inner_reaction

    pm = PersonaManager()
    pm.load_all()
    personas = pm.personas
    if args.persona:
        personas = {k: v for k, v in personas.items() if k == args.persona}
    if not personas:
        print("未加载到任何人格")
        sys.exit(1)

    llm = build_llm(config)
    engine = PersonaEngine(
        corpus=PersonaCorpus,
        persona_manager=pm,
        llm=llm,
        mode="thinking",
        max_samples=3,
        consistency="rules",
    )

    report: list[str] = [
        "# 拟真人格评测报告",
        "",
        f"- 时间：{__import__('datetime').datetime.now().isoformat(timespec='seconds')}",
        "- 方法：基线（角色卡 only）vs 增强（真实台词样本 + 内心反应建模）",
        "- 评分：LLM 裁判逐维 1~5 分（性格一致性 / 语气与句式 / 情绪反应 / 知识与边界）",
        "",
    ]
    agg = {"base": [0.0] * 4, "enh": [0.0] * 4}
    total = 0

    for pid, persona in personas.items():
        name = persona.get("display_name") or persona.get("name", pid)
        print(f"\n▶ 评测人格: {name} ({pid})")
        report.append(f"## {name}（{pid}）")
        report.append("")
        report.append("| # | 探测问题 | 维度 | 基线 | 增强 | 差 |")
        report.append("|---|----------|------|------|------|----|")

        probes = PROBES[: args.limit]
        for qi, (kind, question) in enumerate(probes):
            chat_history = "（无对话历史）"
            # 基线：现有 build_full_prompt（角色卡 only）
            base_prompt = pm.build_full_prompt(pid, "", chat_history, question)
            # 增强：台词样本 + 内心反应
            enh_prompt = engine.build_enhanced_prompt(persona, question, chat_history)

            base_reply = await generate(llm, base_prompt)
            enh_reply = await generate(llm, enh_prompt)
            enh_reply_clean = strip_inner_reaction(enh_reply) if enh_reply else ""

            base_scores = await judge(llm, persona_summary(persona), question, base_reply)
            enh_scores = await judge(llm, persona_summary(persona), question, enh_reply_clean)

            dims = list(base_scores.keys())
            base_issues = rules_consistency_check(persona, base_reply)
            enh_issues = rules_consistency_check(persona, enh_reply_clean)
            total += 1
            for di, dim in enumerate(dims):
                b, e = base_scores[dim], enh_scores[dim]
                agg["base"][di] += b
                agg["enh"][di] += e
                diff = f"+{e - b}" if e > b else str(e - b)
                report.append(
                    f"| {qi + 1} | {kind}：{question[:18]} | {dim} | {b} | {e} | {diff} |"
                )
            print(
                f"  [{kind}] 基线均分 {sum(base_scores.values())/4:.1f} / "
                f"增强均分 {sum(enh_scores.values())/4:.1f}  "
                f"(破格: 基线{len(base_issues)} 增强{len(enh_issues)})"
            )
            report.append(
                f"\n**基线回复（{name}）**：{base_reply or '（空）'}\n"
                f"\n**增强回复（{name}）**：{enh_reply_clean or '（空）'}\n"
            )
        report.append("")

    # 汇总
    report.append("## 汇总")
    report.append("")
    report.append("| 维度 | 基线均值 | 增强均值 | 提升 |")
    report.append("|------|----------|----------|------|")
    dims = ["性格一致性", "语气与句式", "情绪反应", "知识与边界"]
    for di, dim in enumerate(dims):
        bm = agg["base"][di] / max(total, 1)
        em = agg["enh"][di] / max(total, 1)
        report.append(f"| {dim} | {bm:.2f} | {em:.2f} | {em - bm:+.2f} |")
    report.append("")
    report.append(f"评测轮数：{total}（每轮基线+增强各一次生成 + 两次 LLM 裁判打分）")

    out = "\n".join(report)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"\n报告已写入: {args.out}")
    print("\n" + "=" * 40)
    print(out)


if __name__ == "__main__":
    asyncio.run(main())
