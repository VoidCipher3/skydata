"""skyquest 商店頁共用模組。只用標準庫。

改動前請先讀這三條前提：

1. 所有時間一律以 epoch（UTC 秒）流動，中途不做任何時區加減。
   遊戲換日錨在美西當地時間，夏令 07:00 UTC、冬令 08:00 UTC，
   任何寫死的 offset 都會在換季時錯一小時。時區只在前端顯示時才轉。

2. get_event_schedule 只回傳「進行中 + 未來」的事件，已結束的直接消失
   （實測 584 筆裡沒有任何一筆是過去式）。因此「已結束」與「明年還沒排檔期」
   在資料上不可區分，狀態一律只到 unscheduled（目前不可購買），
   不要自行推論成「已結束」。

3. 圖片存在與否一律查 manifest，不做 HTTP 探測。
   manifest 由 Skyinfo 端的 build_manifest.py 產生，內含
   files（資產名 → 相對路徑）與 aliases（UiOutfit* → CharSkyKid_*）。
   不在表裡就是沒有，沒有模稜兩可的空間 —— 這也繞開了
   Cloudflare Pages 缺件回 200 的 SPA fallback 問題。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# --- 事件排程 ---------------------------------------------------------------

# duration 的「永久」哨兵值。實測至少有 99999999 與 99999998 兩種，
# 所以用 >= 判斷，不要寫成 ==。
FOREVER_MINUTES = 99999998

# generic_shop_defs.reset_event 對應到排程裡的這幾個事件
# （duration=0，when[0] 就是「下一次重置時刻」）。
RESET_EVENT_KEYS = {
    "gen_shop_daily_reset": "daily",
    "gen_shop_weekly_reset": "weekly",
    "gen_shop_monthly_reset": "monthly",
    "gen_shop_yearly_reset": "yearly",
}


def load_json(path: str) -> Any:
    # 遊戲回應偶爾帶 BOM，utf-8-sig 兩種都吃。
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def dump_json(path: str, data: Any, indent: int | None = 1) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent)
        fh.write("\n")


def build_event_index(schedule: dict) -> dict[str, dict]:
    """把 event_schedule 攤平成 {name: {windows: [[start, end]], permanent: bool}}。

    一個事件可以有多個 when offset，而且視窗會互相重疊
    （實測 skyfest_fireworks_show 有 16 個 1440 分鐘視窗同時進行中）。
    所以全部展開後排序，判定狀態時走聯集 ——
    絕對不要「找到第一個符合的就 break」，那會讓倒數提早歸零。
    """
    base = schedule["base_time"]
    index: dict[str, dict] = {}

    for ev in schedule.get("events", []):
        name = ev.get("name")
        if not name:
            continue
        minutes = int(ev.get("duration", 0))
        dur = minutes * 60
        slot = index.setdefault(name, {"windows": [], "permanent": False})
        slot["permanent"] = slot["permanent"] or minutes >= FOREVER_MINUTES
        for off in ev.get("when", []):
            start = base + int(off)
            slot["windows"].append([start, start + dur])

    for slot in index.values():
        slot["windows"].sort()
    return index


def extract_resets(index: dict[str, dict]) -> dict[str, int | None]:
    """抽出四種商店重置週期的「下一次」時刻。

    上游只給下一次，而抓取排程剛好壓在每日重置線上
    （cron 07:00 UTC 就是遊戲換日），拿到已過期的 daily 值是常態而非例外。
    前端對 daily / weekly 會自行外推；monthly / yearly 不能推
    （月長不一、跨年界、DST 讓錨點在 07:00/08:00 UTC 間跳），過期只能顯示待更新。
    """
    out: dict[str, int | None] = {k: None for k in ("daily", "weekly", "monthly", "yearly")}
    for key, label in RESET_EVENT_KEYS.items():
        slot = index.get(key)
        if slot and slot["windows"]:
            out[label] = slot["windows"][0][0]
    return out


# --- 圖片：manifest 解析 -----------------------------------------------------

_ASSET_SAFE = re.compile(r"^[A-Za-z0-9_]+$")


class Manifest:
    """Skyinfo 圖庫索引。resolve() 做兩段解析：先查檔名，再查別名。

    別名這層是必要的：consumable_defs 給的是 UiOutfitBodyClassicDress，
    而圖被 All.py 依 OutfitDefs.json 改名成 CharSkyKid_Body_Ghost 存檔。
    少了它，generic 有 310 筆商品會查無此圖，覆蓋率從 84% 掉到 48%。
    """

    def __init__(self, data: dict | None = None, base: str = ""):
        data = data or {}
        self.files: dict[str, str] = data.get("files", {})
        self.aliases: dict[str, list[str]] = data.get("aliases", {})
        # base 優先用呼叫端指定的，其次用 manifest 自己記的
        self.base = base or data.get("base", "")
        self.generated = data.get("generated")

    @classmethod
    def load(cls, path: str, base: str = "") -> "Manifest":
        if not path or not os.path.exists(path):
            return cls(base=base)
        return cls(load_json(path), base=base)

    def __bool__(self) -> bool:
        return bool(self.files)

    def path_of(self, name: str) -> str | None:
        if not name or not _ASSET_SAFE.match(name):
            return None
        direct = self.files.get(name)
        if direct:
            return direct
        for target in self.aliases.get(name, []):
            hit = self.files.get(target)
            if hit:
                return hit
        return None

    def url_of(self, name: str) -> str | None:
        rel = self.path_of(name)
        return (self.base + rel) if rel else None

    def resolve_first(self, candidates: list[str]) -> dict | None:
        """依序試候選名稱，回傳第一個有圖的 {name, url}。

        全都沒有時回傳第一個候選並帶 url=None ——
        前端據此顯示原始名稱，而不是留白或放破圖。
        """
        first = None
        for name in candidates:
            if not name:
                continue
            if first is None:
                first = name
            url = self.url_of(name)
            if url:
                return {"name": name, "url": url}
        return {"name": first, "url": None} if first else None


# --- 圖片候選鏈 -------------------------------------------------------------

def consumable_icon_map(consumable_defs: list[dict] | None) -> dict[str, str]:
    """get_consumable_defs 的 name → icon 對照。

    補的是 generic 裡 item_type == "consumable" 那 377 筆：
    它們的 item_name 是 outfit_body_annivbeigesuit 這種小寫命名空間，
    圖庫裡沒有這個名字，得先換成 UiOutfitBodyAnnivBeigeSuit 才查得到。

    這個對照不能用命名規則推導 —— 實測 544 筆裡有 354 筆不符合
    「Ui + PascalCase(name)」，例如 bloom_sakura → UiEmoteAP04Celebrate。
    """
    return {r["name"]: r["icon"] for r in (consumable_defs or [])
            if r.get("name") and r.get("icon")}


def iap_candidates(item: dict) -> list[str]:
    """iaplist 的圖片候選：內容物優先，icon 墊底。

    unlocks 給的是 CharSkyKid_*，直接命中圖庫，命中率遠高於 icon 欄位。
    """
    out = [a for a in item.get("unlocks", []) if a and _ASSET_SAFE.match(a)]
    icon = item.get("icon", "")
    if icon and _ASSET_SAFE.match(icon):
        out.append(icon)
    return out


def shop_candidates(row: dict, icon_map: dict[str, str]) -> list[str]:
    """generic 的圖片候選，依可靠度排序：

    1. item_name 本身 —— CharSkyKid_* 直接命中（339 筆）
    2. consumable_defs 查到的 icon —— 補小寫命名空間（332 筆）
    3. generic 自帶的 icon 欄位 —— 只有 344 筆有值，當最後備援
    """
    out = []
    item_name = row.get("item_name", "")
    if item_name and _ASSET_SAFE.match(item_name):
        out.append(item_name)
    mapped = icon_map.get(item_name)
    if mapped and _ASSET_SAFE.match(mapped):
        out.append(mapped)
    icon = row.get("icon", "")
    if icon and _ASSET_SAFE.match(icon):
        out.append(icon)
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def requirement_candidates(row: dict) -> list[str]:
    """generic 的購買前置條件。

    注意這不是「買到的東西」，是「得先在別處解鎖才能買的前提」。
    115 筆非空，其中 113 筆是 CharSkyKid_Prop_*（有圖），
    2 筆是 quest_ap31_fetch_06 這種任務 ID（沒有圖，會顯示原文）。
    """
    return [r for r in row.get("unlock_requiremnts", []) if r and _ASSET_SAFE.match(r)]
