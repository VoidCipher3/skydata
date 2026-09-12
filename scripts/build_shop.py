"""把原始 API 回應正規化成前端要吃的 data/*.json。

輸入（raw/，由 SCRIPT 落地，全部 .gitignore）：
    iaplist.json         iap_list
    generic.json         generic_shop_defs
    event_schedule.json  get_event_schedule
    consumable_defs.json get_consumable_defs
    manifest.json        Skyinfo 圖庫索引（另外抓，見 --manifest-url）

輸出（data/）：
    iap.json      禮包
    shop.json     魔法商店等 generic 商店
    events.json   事件視窗、重置時刻、meta（兩頁共用）

注意這支**不寫入商品的上架狀態**。狀態由前端拿 events.json 的視窗即時算，
因為 build 是每天一次、而活動可能在任何時刻開始 ——
把 build 當下算出的狀態寫死，會讓「凌晨三點開賣的活動」在站上顯示
「即將上架」整整十二小時，同時旁邊的倒數卻是即時的，兩者互相矛盾。

用法：
    python scripts/build_shop.py
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shop_common import (  # noqa: E402
    Manifest,
    build_event_index,
    consumable_icon_map,
    dump_json,
    extract_resets,
    iap_candidates,
    load_json,
    requirement_candidates,
    shop_candidates,
)

# meta.fetchedAt 的隨機抖動（秒）。只作用在這一個欄位 ——
# 事件視窗與重置時刻的 epoch 絕不套用，那些一抖倒數就跟著抖。
#
# 據實說明：這不構成實質混淆。抓取排程本來就公開在 wrangler.toml
# （crons = ['0 7 * * *']）、daily_quest.yml 和 README 裡，
# 改事後記錄的數字對上游的 log 沒有任何影響。真要讓觸發時刻不可預測，
# 得在 SCRIPT 開頭 sleep 一個隨機秒數。
FETCHED_AT_JITTER = 10

DEFAULT_IMAGE_BASE = "https://skyinfoweb.pages.dev/image/"


def _price_table(path: str) -> dict:
    if os.path.exists(path):
        return load_json(path)
    print(f"提醒：找不到 {path}，tier 只會輸出原始數字，前端不顯示金額")
    return {"default": "USD", "currencies": {}, "tiers": {}}


def build_iap(iap_list: list[dict], mf: Manifest, tiers: dict) -> list[dict]:
    out = []
    for item in iap_list:
        # iaplist 的常駐品是「完全沒有 event_name 這個 key」，不是空字串 ——
        # 寫成 == "" 會靜默漏掉全部 30 筆。generic 剛好相反，是空字串。
        # 兩邊在這裡都正規化成 None。
        event_name = item.get("event_name") or None

        # currencyCount / currencyType 是禮包「附帶」的東西
        # （買季卡送 30 季蠟），不是售價。售價一律看 tier。
        bonus = []
        for ctype, ccount in (
            (item.get("currencyType"), item.get("currencyCount", 0)),
            (item.get("currencyType2"), item.get("currencyCount2", 0)),
        ):
            if ctype and ccount:
                bonus.append({"currency": ctype, "count": ccount})

        unlocks = [
            mf.resolve_first([a]) for a in item.get("unlocks", []) if a
        ]

        out.append({
            "id": item["id"],
            "oldId": item.get("old_id"),       # 34 筆有；做上架履歷比對時要用
            "nameKey": item.get("name"),
            "descKey": item.get("desc"),
            "type": item.get("type"),
            "tier": item.get("tier"),
            "prices": tiers.get(str(item.get("tier")), {}),
            "bonus": bonus,
            "unlocks": [u for u in unlocks if u],
            "image": mf.resolve_first(iap_candidates(item)),
            "eventName": event_name,
            "platforms": item.get("platforms", "*"),
            "giftable": bool(item.get("giftable")),
        })
    return out


def build_shop(shop_defs: list[dict], mf: Manifest, icon_map: dict[str, str]) -> list[dict]:
    out = []
    for row in shop_defs:
        candidates = shop_candidates(row, icon_map)
        out.append({
            "id": row["id"],
            "name": row.get("name"),            # 內部 ID，不是顯示用
            "itemName": row.get("item_name"),   # 顯示與翻譯的主鍵
            "itemType": row.get("item_type"),
            "itemCount": row.get("item_count", 1),
            "shopName": row.get("shop_name"),
            "cost": row.get("cost", 0),
            "currency": row.get("currency_type"),
            "maxPerCycle": row.get("max_per_cycle", 1),
            "resetEvent": row.get("reset_event"),
            "unlockRequires": [
                u for u in (mf.resolve_first([r]) for r in requirement_candidates(row)) if u
            ],
            "image": mf.resolve_first(candidates),
            # 整條候選鏈留著，之後查「為什麼這筆沒圖」不用重跑
            "imageCandidates": candidates,
            "eventName": row.get("event_name") or None,
        })
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", default="raw")
    p.add_argument("--outdir", default="data")
    p.add_argument("--tiers", default="config/tier_price.json")
    p.add_argument("--image-base", default=DEFAULT_IMAGE_BASE)
    args = p.parse_args()

    def raw(name):
        return os.path.join(args.raw, name)

    iap_raw = load_json(raw("iaplist.json"))
    shop_defs = load_json(raw("generic.json"))["generic_shop_defs"]
    schedule = load_json(raw("event_schedule.json"))["event_schedule"]

    # 上游若開始回傳已購買紀錄，代表抓取帳號買過東西，
    # iap_list 可能已被過濾而不完整。沒有斷言的話這種事會查很久。
    if iap_raw.get("purchased_non_consumables"):
        print("警告：purchased_non_consumables 非空，iap_list 可能已被帳號購買紀錄過濾")

    cpath = raw("consumable_defs.json")
    cdefs = load_json(cpath).get("get_consumable_defs") if os.path.exists(cpath) else None
    if cdefs is None:
        print("提醒：找不到 consumable_defs.json，generic 的 consumable 那批會沒有圖")
    icon_map = consumable_icon_map(cdefs)

    mf = Manifest.load(raw("manifest.json"), base=args.image_base)
    if not mf:
        print("提醒：找不到 manifest.json，所有圖片都會是 null（前端退回顯示原文）")
    else:
        print(f"manifest: {len(mf.files)} 檔名 / {len(mf.aliases)} alias"
              f"（產生於 {mf.generated}）")

    tier_cfg = _price_table(args.tiers)
    events = build_event_index(schedule)

    iap = build_iap(iap_raw["iap_list"], mf, tier_cfg.get("tiers", {}))
    shop = build_shop(shop_defs, mf, icon_map)

    # 資料時點以上游 server_time 為準，不用 runner 的本地時鐘 ——
    # 前端的 staleness 判斷靠它，被 runner 時鐘偏差污染就失準了。
    server_time = int(schedule["server_time"])
    built_at = int(time.time())
    meta = {
        "fetchedAt": server_time + random.randint(-FETCHED_AT_JITTER, FETCHED_AT_JITTER),
        "builtAt": built_at,
        "validUntil": int(schedule.get("valid_until", 0)),
        "baseTime": int(schedule["base_time"]),
    }
    drift = abs(built_at - server_time)
    if drift > 600:
        print(f"警告：builtAt 與上游 server_time 相差 {drift} 秒，請檢查 runner 時鐘")

    dump_json(os.path.join(args.outdir, "iap.json"), {"meta": meta, "items": iap})
    dump_json(os.path.join(args.outdir, "shop.json"), {"meta": meta, "items": shop})
    dump_json(os.path.join(args.outdir, "events.json"), {
        "meta": meta,
        "resets": extract_resets(events),
        "events": {n: {"w": s["windows"], "p": s["permanent"]}
                   for n, s in sorted(events.items())},
        "priceCurrencies": tier_cfg.get("currencies", {}),
        "defaultCurrency": tier_cfg.get("default", "USD"),
    })

    def covered(rows):
        return sum(1 for r in rows if r["image"] and r["image"]["url"])

    print(f"iap.json    {len(iap):>4} 筆   圖片 {covered(iap)}/{len(iap)}"
          f" ({covered(iap) / len(iap) * 100:.1f}%)")
    print(f"shop.json   {len(shop):>4} 筆   圖片 {covered(shop)}/{len(shop)}"
          f" ({covered(shop) / len(shop) * 100:.1f}%)")
    print(f"events.json {len(events):>4} 個事件")

    missing = sorted({r["itemName"] for r in shop
                      if not (r["image"] and r["image"]["url"])})
    if missing:
        print(f"無圖的 generic 名稱 {len(missing)} 個 —— 這些只能靠 i18n 顯示文字，"
              f"優先翻譯它們")


if __name__ == "__main__":
    main()
