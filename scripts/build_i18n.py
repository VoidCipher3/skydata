"""從遊戲語言包 Localizable.strings 產生 i18n/ 底下的翻譯檔。

產出兩個檔：
    i18n/quests.<lang>.json   daily_quest_*（每日任務文字，約 510 條）
    i18n/items.<lang>.json    商店與禮包

quests 原本存在 GH secret（QUEST_TW / QUEST_EN_1 / QUEST_EN_2 / QUEST_CN），
搬進 repo 的理由：保密效果本來就是零（data.json 每天公開翻譯後的任務文字、
archive/ 還在累積），而 secret 的長度上限逼出了 QUEST_EN_1 / _2 這種分段，
名字和內容還錯開一格。搬進來之後有版本控制、有 diff，遊戲改版時看得出差異。

items 涵蓋兩區：
    commerce —— iaplist 的 commerce_item_name_* / commerce_item_desc_*
                語言包裡有現成的，直接撈（約 399 條）
    items    —— generic 的 item_name（資產名）
                語言包**沒有**對應 key（localized_name 843 筆全空就是證據），
                所以這裡只產骨架，值先留空，由你手動補。

前端遇到空值或缺 key 會自動退回顯示原始名稱，不會留白，
所以骨架不完整也不會讓站壞掉 —— 只是那些商品顯示成資產名。

用法：
    python scripts/build_i18n.py --strings tw=lang/tw.strings en=lang/en.strings
    python scripts/build_i18n.py --strings tw=... --raw raw --out i18n
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shop_common import dump_json, load_json  # noqa: E402

# "key" = "value";   —— value 裡可能有跳脫的引號
LINE_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;\s*$')

# 遊戲字串裡的標記語言：
#   <2>返還道具.</2>              數字標籤是字型／顏色樣式
#   <8>感恩白絨斗篷<cape/></8>    自閉合標籤是內嵌圖示
# 直接輸出到 HTML 有兩個問題：<2> 不是合法標籤名，各家瀏覽器行為不一；
# <cape/> 會整個消失，讀者看到的句子會少一個詞。
# 所以在這裡就處理掉，前端一律用 textContent 顯示。
PAIRED_TAG = re.compile(r"</?\d+>")          # <2> 與 </2>
SELF_TAG = re.compile(r"<([A-Za-z][\w]*)\s*/>")  # <cape/>
OTHER_TAG = re.compile(r"</?[A-Za-z][\w]*>")     # 其他成對標籤


def clean(value: str) -> str:
    """剝掉樣式標記，保留可讀文字。"""
    value = value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    value = PAIRED_TAG.sub("", value)
    # 圖示標籤換成空白而不是直接刪，否則前後兩個詞會黏在一起
    value = SELF_TAG.sub(" ", value)
    value = OTHER_TAG.sub("", value)
    # 收斂多餘空白，但保留換行
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def parse_strings(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    bad = 0
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("/*"):
                continue
            m = LINE_RE.match(line)
            if not m:
                bad += 1
                continue
            out[m.group(1)] = m.group(2)
    if bad:
        print(f"    （{bad} 行不符合 \"key\" = \"value\"; 格式，已略過）")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strings", nargs="+", required=True,
                   metavar="LANG=PATH",
                   help="語系與語言包路徑，例如 tw=lang/tw.strings en=lang/en.strings")
    p.add_argument("--raw", default="raw")
    p.add_argument("--out", default="i18n")
    args = p.parse_args()

    QUEST_PREFIX = "daily_quest_"

    iap = load_json(os.path.join(args.raw, "iaplist.json"))["iap_list"]
    shop = load_json(os.path.join(args.raw, "generic.json"))["generic_shop_defs"]

    want_commerce = sorted({k for d in iap for k in (d.get("name"), d.get("desc")) if k})
    want_items = sorted({r["item_name"] for r in shop if r.get("item_name")})
    print(f"需要 commerce key {len(want_commerce)} 條、generic item {len(want_items)} 條")

    for spec in args.strings:
        if "=" not in spec:
            sys.exit(f"--strings 的格式要是 LANG=PATH，收到：{spec}")
        lang, path = spec.split("=", 1)
        if not os.path.exists(path):
            sys.exit(f"找不到語言包：{path}")

        print(f"\n[{lang}] {path}")
        table = parse_strings(path)
        print(f"    解析到 {len(table)} 條字串")

        # --- quests：整批撈 daily_quest_*，key 去掉前綴與 _desc 後綴 ---
        # 去後綴是為了對上 quest_schedule 回傳的 quest_id。
        quests = {}
        for key, raw_val in table.items():
            if not key.startswith(QUEST_PREFIX):
                continue
            qid = key[len(QUEST_PREFIX):]
            if qid.endswith("_desc"):
                qid = qid[:-5]
            # 同一個 id 可能同時有帶 _desc 與不帶的兩筆；_desc 那份才是完整句子，
            # 所以後到的不覆蓋先到的，除非先到的是空的。
            if not quests.get(qid):
                quests[qid] = clean(raw_val)
        dump_json(os.path.join(args.out, f"quests.{lang}.json"), quests)
        print(f"    quests   {len(quests)} 條 → {os.path.join(args.out, f'quests.{lang}.json')}")

        out_path = os.path.join(args.out, f"items.{lang}.json")
        # 保留既有的人工翻譯：只補新的，不覆蓋已經填過的值
        existing = load_json(out_path) if os.path.exists(out_path) else {}
        old_items = existing.get("items", {})

        commerce = {}
        for key in want_commerce:
            raw_val = table.get(key)
            if raw_val is not None:
                commerce[key] = clean(raw_val)

        items = {}
        for key in want_items:
            # 語言包裡通常沒有，但還是試一下 —— 有就省一條手工
            raw_val = table.get(key)
            items[key] = old_items.get(key) or (clean(raw_val) if raw_val else "")

        dump_json(out_path, {
            "_comment": "commerce 區來自遊戲語言包，重跑本腳本會覆蓋；"
                        "items 區是手動翻譯，重跑只會補新 key、不會蓋掉已填的值。"
                        "值為空字串時前端會退回顯示原始名稱。",
            "commerce": commerce,
            "items": items,
        })

        c_pct = len(commerce) / len(want_commerce) * 100 if want_commerce else 0
        filled = sum(1 for v in items.values() if v)
        i_pct = filled / len(want_items) * 100 if want_items else 0
        print(f"    commerce {len(commerce)}/{len(want_commerce)} ({c_pct:.1f}%)")
        print(f"    items    {filled}/{len(want_items)} ({i_pct:.1f}%)")
        print(f"    → {out_path}")

        missing = [k for k in want_commerce if k not in commerce]
        if missing:
            print(f"    語言包缺這 {len(missing)} 條 commerce key，例如：{missing[:3]}")


if __name__ == "__main__":
    main()
