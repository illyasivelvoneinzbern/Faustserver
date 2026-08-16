# -*- coding: utf-8 -*-
"""拟真人格台词库构建/预览脚本（P38 数据管道）。

用途：
1. 统计台词库规模（每角色台词条数、来源分布）；
2. 预览某角色的台词样本（按场景/来源筛选），便于校验数据质量；
3. 导出某角色的台词库到文本文件（供人工打磨角色卡/微调数据集）。

用法：
    venv\\Scripts\\python.exe scripts\\build_persona_corpus.py                 # 统计全库
    venv\\Scripts\\python.exe scripts\\build_persona_corpus.py 浮士德          # 预览浮士德台词
    venv\\Scripts\\python.exe scripts\\build_persona_corpus.py 浮士德 --source story --limit 30
    venv\\Scripts\\python.exe scripts\\build_persona_corpus.py 浮士德 --export data/persona_corpus/浮士德.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.persona_corpus import PersonaCorpus  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {}
    for i, a in enumerate(sys.argv[1:]):
        if a == "--source" and i + 2 < len(sys.argv) + 1:
            opts["source"] = sys.argv[i + 2]
        if a == "--limit" and i + 2 < len(sys.argv) + 1:
            opts["limit"] = int(sys.argv[i + 2])
        if a == "--export" and i + 2 < len(sys.argv) + 1:
            opts["export"] = sys.argv[i + 2]

    name = args[0] if args else ""

    if not name:
        # ── 模式 1：全库统计 ──
        stats = PersonaCorpus.stats()
        print("=" * 60)
        print(f"拟真人格台词库统计：{stats['characters']} 个角色 / {stats['total_lines']} 条台词")
        print(f"  剧情台词: {stats['story_lines']} 条 | 官方语音: {stats['voice_lines']} 条")
        print("=" * 60)
        for char, n in list(stats["per_character"].items())[:40]:
            bar = "█" * min(n // 30, 40)
            print(f"  {char:<8} {n:>5} 条 {bar}")
        if len(stats["per_character"]) > 40:
            print(f"  …（共 {len(stats['per_character'])} 个角色）")
        return

    # ── 模式 2：角色台词预览/导出 ──
    limit = opts.get("limit", 20)
    lines = PersonaCorpus.get_lines(name, source=opts.get("source", ""))
    if not lines:
        print(f"未找到角色「{name}」的台词。可用角色：")
        print("、".join(list(PersonaCorpus.stats()["per_character"].keys())[:50]))
        sys.exit(1)

    print(f"角色「{name}」台词库：共 {len(lines)} 条")
    print("=" * 60)

    export_path = opts.get("export")
    fh = None
    if export_path:
        Path(export_path).parent.mkdir(parents=True, exist_ok=True)
        fh = open(export_path, "w", encoding="utf-8")

    for ln in lines[:limit]:
        tag = "🎙语音" if ln.source == "voice" else "📖剧情"
        text = ln.text.replace("\n", " ")
        line_out = f"[{tag}][{ln.scene}] {text}"
        print(line_out)
        if fh:
            fh.write(line_out + "\n")
    if len(lines) > limit:
        print(f"…（共 {len(lines)} 条，仅显示前 {limit} 条，可用 --limit 调整）")
    if fh:
        fh.close()
        print(f"\n已导出到: {export_path}")


if __name__ == "__main__":
    main()
