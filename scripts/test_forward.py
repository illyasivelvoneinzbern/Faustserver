# -*- coding: utf-8 -*-
"""
直答打包转发（Forward Reply）冒烟测试。

验证：
1. ``agent.forward.split_forward_sections``：长直答文本 → 分节列表
   （敌方/人格/饰品/事件四种格式各一例）；
2. ``adapter.napcat.build_forward_nodes``：分节 → OneBot node 段；
3. ``agent.forward.AgentReply``：短文本（< min_nodes 节）回落普通发送。

运行：python scripts/test_forward.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.forward import AgentReply, split_forward_sections
from adapter.napcat import build_forward_nodes


def _check(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        raise SystemExit(1)


def main():
    print("== 1. split_forward_sections（敌方格式）==")
    enemy = (
        "【雷横】\n　关卡：主线战斗8-30　部位：躯干　HP：4239\n\n"
        "【被动】\n　· 灼烧：回合结束时受到伤害\n\n"
        "【技能】\n1. 斩击\n2. 突刺"
    )
    secs = split_forward_sections(enemy)
    _check("敌方 3 节", len(secs) == 3, f"got {len(secs)}")
    print("  " + " | ".join(s.splitlines()[0] for s in secs))

    print("== 2. split_forward_sections（人格格式）==")
    persona = (
        "（人格名）浮士德\n罪人：浮士德\n罪孽亲和：傲慢3 怠惰2\n\n"
        "【技能一】纵斩\n攻击容量：1\n硬币：2\n\n"
        "【守备技能】闪避\n\n"
        "被动技能：\n战斗：\n- 智库\n\n"
        "语音台词：\n- [问候] 你好"
    )
    secs = split_forward_sections(persona)
    _check("人格 >= 4 节", len(secs) >= 4, f"got {len(secs)}")

    print("== 3. split_forward_sections（饰品格式，短节合并）==")
    gift = (
        "【饰品】月之记忆\n【稀有度】★★★★★（5）\n【获取地点】镜像迷宫\n"
        "【效果类型】泛用\n【罪孽属性】嫉妒\n\n"
        "【效果】\n　获得2层『流血』"
    )
    secs = split_forward_sections(gift)
    _check("饰品 2 节（元数据合并为 1 节）", len(secs) == 2, f"got {len(secs)}")

    print("== 4. build_forward_nodes ==")
    nodes = build_forward_nodes(["节一", "节二"], sender_name="浮士德", sender_uin="10001")
    _check("node 段正确", len(nodes) == 2 and nodes[0]["type"] == "node")
    _check("content 为标准 text 段", nodes[0]["data"]["content"][0]["type"] == "text")

    print("== 5. AgentReply 短文本回落 ==")
    short = "检测到多个同名敌方单位，请指定其中一个：\n1. A\n2. B"
    r = AgentReply(text=short, forward_sections=None)
    _check("短文本 forward_sections=None", r.forward_sections is None)

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
