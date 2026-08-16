# -*- coding: utf-8 -*-
"""12 罪人扮演人格 YAML 生成脚本（改进计划 M3）。

从 data/structured/personas/persona_{罪人}LCB罪人.json 的 voice_lines / 技能语音
自动生成 personas/{id}.yaml 骨架（identity / traits / speech_style / examples），
供人工打磨后启用。已存在的 YAML（如 faust.yaml、don_quixote.yaml）不覆盖。

运行：venv\\Scripts\\python.exe scripts\\persona_gen.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PERSONAS_DIR = Path("data/structured/personas")
OUT_DIR = Path("personas")

# 罪人编号（LCB 顺序）：(中文名, 英文id, 编号)
SINNERS = [
    ("李箱", "yisang", 1),
    ("浮士德", "faust", 2),
    ("堂吉诃德", "donquixote", 3),
    ("良秀", "ryoshu", 4),
    ("默尔索", "meursault", 5),
    ("鸿璐", "honglu", 6),
    ("希斯克利夫", "heathcliff", 7),
    ("以实玛利", "ishmael", 8),
    ("罗佳", "rodion", 9),
    ("但丁", "dante", None),  # 执行管理人（非罪人编号）
    ("辛克莱", "sinclair", 11),
    ("奥提斯", "outis", 12),
    ("格里高尔", "gregor", 13),
]

# 已有人工打磨的 YAML（不覆盖）
EXISTING = {"faust", "donquixote"}

# 语音文本前缀标记（"||" 为站内语音文本格式）
_VOICE_PREFIX = "||"


def _clean_voice(text: str) -> str:
    t = (text or "").strip()
    if t.startswith(_VOICE_PREFIX):
        t = t[len(_VOICE_PREFIX):].strip()
    return t


def load_sinner_record(name: str) -> dict | None:
    for f in PERSONAS_DIR.glob(f"persona_{name}LCB罪人.json"):
        try:
            return json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  ! 读取失败 {f}: {e}")
    return None


def build_yaml(name: str, pid: str, num) -> str:
    rec = load_sinner_record(name)
    voices = []
    if rec:
        for v in rec.get("voice_lines") or []:
            text = _clean_voice(v.get("text") or "")
            title = (v.get("title") or "").strip()
            if text and title:
                voices.append((title, text))
    if not voices:
        print(f"  ! {name} 无语音数据，生成最小骨架")
        voices = [("获得时", f"我是{name}。")]

    # 身份描述
    if num is None:
        identity = (
            f"边狱公司（LCB）巴士部门的执行管理人（Manager），"
            f"负责指挥罪人与回收金枝，拥有时钟般的能力。"
        )
    else:
        identity = f"边狱公司（LCB）巴士部门的{num}号罪人。"

    # traits：取 3~4 条代表性语音台词作为性格佐证（截断）
    trait_lines = []
    for title, text in voices[:4]:
        snippet = text if len(text) <= 40 else text[:40] + "…"
        trait_lines.append(f"- 语音「{title}」：『{snippet}』")

    # speech_style：通用扮演规则 + 一条代表台词
    rep_title, rep_text = voices[0] if voices else ("", "")
    style_lines = [
        "- 始终以角色身份说话，语言风格贴合上述语音台词",
        "- 对话简短自然，不输出神态/动作描写，只说角色说的话",
    ]
    if rep_text:
        style_lines.append(f"- 代表台词（{rep_title}）：『{rep_text[:60]}』")

    # examples：取前 2 条语音构造对话示例
    example_lines = []
    for title, text in voices[:2]:
        u = f"（{title}）" if title else "……"
        example_lines.append(f"  - user: \"{u}\"")
        example_lines.append(f"    reply: \"{text}\"")

    yaml_lines = [
        f"# 由 scripts/persona_gen.py 自动生成（素材：{name}LCB罪人 语音），请人工打磨",
        f"id: \"{pid}\"",
        f"name: \"{name}\"",
        f"display_name: \"{name}\"",
        f"identity: \"{identity}\"",
        "traits:",
        *trait_lines,
        "speech_style:",
        *style_lines,
        "examples:",
        *example_lines,
        "advanced:",
        "  max_response_length: 400",
        "  avoid_topics:",
        "    - \"现实世界的政治与宗教\"",
        "    - \"色情内容\"",
    ]
    return "\n".join(yaml_lines) + "\n"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = []
    skipped = []
    for name, pid, num in SINNERS:
        if pid in EXISTING:
            skipped.append(pid)
            continue
        out_path = OUT_DIR / f"{pid}.yaml"
        if out_path.exists():
            skipped.append(pid)
            continue
        content = build_yaml(name, pid, num)
        out_path.write_text(content, encoding="utf-8")
        generated.append(pid)
        print(f"  生成 {out_path.name}（{name}）")
    print(f"\n完成：生成 {len(generated)} 个（{', '.join(generated) or '无'}），跳过 {len(skipped)} 个（{', '.join(skipped)}）")


if __name__ == "__main__":
    main()
