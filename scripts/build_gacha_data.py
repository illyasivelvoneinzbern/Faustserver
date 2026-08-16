# -*- coding: utf-8 -*-
"""生成抽奖（gacha）数据：人格稀有度分类 + E.G.O 池。

- 稀有度规则由用户提供（2026-08-15 确认）：
  1灯 = 全部 LCB 罪人人格（初始人格）
  2灯 = 用户名单（含澄清：四协会=し协会、技术解放联盟=脑叶E.G.O荡漾/朱符；
        脑叶公司支部=幸存者、脑叶公司本部=提灯、LCE=提灯）
  其余人格均为 3 灯
- EGO 池 = wiki 中 page_type=ego 的独立 E.G.O 页面（如 乌瞰刀、他人之锁）

输出：
  data/gacha/rarity.json    {one_star: [...], two_star: [...], three_star: [...]}
  data/gacha/ego_pool.json  {items: [{"name": ...}, ...]}
"""
import json
import glob
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path("data/gacha")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 用户 2 灯名单（罪人: 机构关键词）──
TWO_STAR = {
    "李箱": ["七协会", "臼齿事务所", "裴廓德号", "十协会", "lce"],
    "浮士德": ["w公司", "脑叶公司支部", "二协会", "呼啸山庄"],
    "堂吉诃德": ["四协会", "n公司", "脑叶公司本部", "剑契组"],
    "良秀": ["七协会", "lccb", "六协会", "圣愚"],
    "默尔索": ["六协会", "玫瑰扳手工坊", "中指", "死兔帮"],
    "鸿璐": ["六协会", "w公司", "猎牙事务所", "吊钩事务所", "黑云会"],
    "希斯克利夫": ["四协会", "n公司", "七协会", "多裂纹事务所"],
    "以实玛利": ["lccb", "四协会", "技术解放联盟", "埃德加家族"],
    "罗佳": ["lccb", "n公司", "二协会", "t公司"],
    "辛克莱": ["二协会3科", "流浪乐队", "技术解放联盟", "臼齿修船厂", "二协会6科"],
    "奥提斯": ["旧g公司", "剑契组", "五协会", "环指点彩派"],
    "格里高尔": ["六协会", "良派", "玫瑰扳手工坊", "黑云会"],
}
# 机构关键词 → 数据标题中的写法
KW_ALIAS = {
    "七协会": ["Seven协会"], "十协会": ["Dieci协会"], "二协会": ["Zwei协会"],
    "二协会3科": ["Zwei协会西部3科"], "二协会6科": ["Zwei协会南部6科"],
    "四协会": ["し协会"], "五协会": ["Cinq协会"], "六协会": ["六协会"],
    "lccb": ["LCCB"], "lce": ["LCE E.G.O::提灯"], "旧g公司": ["G公司"], "良派": ["良·派"],
    "技术解放联盟": ["脑叶公司E.G.O::荡漾", "脑叶公司E.G.O::朱符"],
    # 用户确认的精确归属（避免歧义误匹配）
    "脑叶公司支部": ["脑叶公司幸存者"],   # 悔恨 归 3 灯
    "脑叶公司本部": ["脑叶公司E.G.O::提灯"],  # 以爱与憎之名 归 3 灯
    "中指": ["中指"], "w公司": ["W公司"], "n公司": ["N公司"], "t公司": ["T公司"],
    "臼齿事务所": ["臼齿事务所"], "臼齿修船厂": ["臼齿修船厂"],
    "裴廓德号": ["裴廓德号"], "呼啸山庄": ["呼啸山庄"], "剑契组": ["剑契组"],
    "圣愚": ["圣愚"], "玫瑰扳手工坊": ["玫瑰扳手工坊"], "死兔帮": ["死兔帮"],
    "猎牙事务所": ["猎牙事务所"], "吊钩事务所": ["吊钩事务所"], "黑云会": ["黑云会"],
    "多裂纹事务所": ["多裂纹事务所"], "埃德加家族": ["埃德加家族"],
    "流浪乐队": ["流浪乐队"], "环指点彩派": ["环指点彩派"],
}
SINNER_ALIAS = {"以实玛丽": "以实玛利", "鸿路": "鸿璐"}


def load_persona_titles() -> list[str]:
    titles = []
    for f in glob.glob("data/structured/personas/*.json"):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        t = d.get("personality_name") or d.get("title") or ""
        if t:
            titles.append(t)
    return sorted(set(titles))


def build_rarity(titles: list[str]) -> dict:
    one_star = [t for t in titles if "LCB罪人" in t]
    two_star_set = set()
    for sinner, kws in TWO_STAR.items():
        canon = SINNER_ALIAS.get(sinner, sinner)
        for kw in kws:
            aliases = KW_ALIAS.get(kw, [kw])
            for t in titles:
                if canon in t and any(a.lower() in t.lower() for a in aliases):
                    two_star_set.add(t)
    two_star = sorted(two_star_set)
    three_star = sorted(t for t in titles if t not in one_star and t not in two_star)
    return {
        "one_star": one_star,
        "two_star": two_star,
        "three_star": three_star,
        "_meta": {
            "total": len(titles),
            "rule": "1灯=LCB罪人; 2灯=用户名单; 其余=3灯 (2026-08-15 用户确认)",
        },
    }


def build_ego_pool() -> list[dict]:
    items = []
    seen = set()
    for line in open("data/raw/wiki_pages.jsonl", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("page_type") != "ego":
            continue
        name = (d.get("title") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        # 简单描述：content 前 60 字
        content = (d.get("content") or "").strip().replace("\n", " ")
        items.append({"name": name, "desc": content[:60]})
    return items


def main():
    titles = load_persona_titles()
    rarity = build_rarity(titles)
    n1, n2, n3 = len(rarity["one_star"]), len(rarity["two_star"]), len(rarity["three_star"])
    print(f"人格总数 {len(titles)} = 1灯{n1} + 2灯{n2} + 3灯{n3}")
    (OUT_DIR / "rarity.json").write_text(
        json.dumps(rarity, ensure_ascii=False, indent=2), encoding="utf-8")

    ego_items = build_ego_pool()
    print(f"EGO 池: {len(ego_items)} 个独立 E.G.O")
    (OUT_DIR / "ego_pool.json").write_text(
        json.dumps({"items": ego_items, "_meta": {"count": len(ego_items)}},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("done -> data/gacha/rarity.json, data/gacha/ego_pool.json")


if __name__ == "__main__":
    main()
