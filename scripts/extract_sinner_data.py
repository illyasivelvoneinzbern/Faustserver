# -*- coding: utf-8 -*-
"""提取指定罪人的角色素材：LCB 语音 + 主线/活动剧情台词，供重拟人格 YAML 使用。

用法：venv\\Scripts\\python.exe scripts\\extract_sinner_data.py 浮士德
输出：data/gacha/tmp/sinner_{罪人名}.txt（语音 + 剧情台词样本）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PERSONAS_DIR = Path("data/structured/personas")
OUT_DIR = Path("data/gacha/tmp")
WIKI_JSONL = "data/raw/wiki_pages.jsonl"

# 活动剧情相关分类/标题标记
_ACTIVITY_HINTS = ("活动", "间章", "联动", "外传", "幕间")


def load_lcb_voice(name: str) -> list[dict]:
    """读取 {罪人}LCB罪人.json 的 voice_lines。"""
    for f in PERSONAS_DIR.glob(f"persona_{name}LCB罪人.json"):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  ! 语音读取失败: {e}")
            return []
        return list(d.get("voice_lines") or [])
    return []


def collect_story_lines(name: str, max_total: int = 120) -> dict:
    """扫描 wiki_pages.jsonl，收集该罪人在剧情对话（blocks）中的台词。

    返回 {"main": [(章节, 台词)...], "activity": [(章节, 台词)...]}
    """
    main_lines: list[tuple[str, str]] = []
    activity_lines: list[tuple[str, str]] = []
    for line in open(WIKI_JSONL, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        pt = d.get("page_type")
        if pt not in ("story_dialogue", "story_note", "event"):
            continue
        blocks = d.get("blocks") or []
        if not blocks:
            continue
        title = d.get("title") or ""
        cats = " ".join(d.get("categories") or [])
        is_activity = any(h in title or h in cats for h in _ACTIVITY_HINTS)
        target = activity_lines if is_activity else main_lines
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") != "dialogue":
                continue
            role = (b.get("role") or "").strip()
            text = (b.get("text") or "").strip()
            if role == name and text:
                target.append((title, text))
                if len(target) >= max_total:
                    break
        if len(main_lines) >= max_total and len(activity_lines) >= max_total:
            break
    return {"main": main_lines, "activity": activity_lines}


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/extract_sinner_data.py 罪人名（如 浮士德）")
        sys.exit(1)
    name = sys.argv[1].strip()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"sinner_{name}.txt"
    lines: list[str] = []

    # 1) LCB 语音
    voices = load_lcb_voice(name)
    lines.append(f"# {name} 角色素材（自动提取）")
    lines.append("")
    lines.append(f"## 一、LCB 罪人语音（{len(voices)} 条）")
    for v in voices:
        title = v.get("title") or ""
        text = (v.get("text") or "").strip().lstrip("||").strip()
        if text:
            lines.append(f"- [{title}] {text}")
    lines.append("")

    # 2) 剧情台词
    story = collect_story_lines(name)
    lines.append(f"## 二、主线剧情台词（{len(story['main'])} 条）")
    for title, text in story["main"]:
        lines.append(f"- ({title}) {text}")
    lines.append("")
    lines.append(f"## 三、活动/间章剧情台词（{len(story['activity'])} 条）")
    for title, text in story["activity"]:
        lines.append(f"- ({title}) {text}")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已提取 {name} 素材 -> {out_path}")
    print(f"  语音 {len(voices)} 条，主线台词 {len(story['main'])} 条，活动台词 {len(story['activity'])} 条")


if __name__ == "__main__":
    main()
