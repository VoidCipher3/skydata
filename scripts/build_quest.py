"""從 raw/ 產生 data.json 與 archive/YYYY-MM.json。

這段邏輯原本住在 GH secret 的 SCRIPT 裡。搬出來的理由很實際：
secret 裡的程式沒有版本控制、沒有 diff、沒法在本機單獨測，
改一行就得整段複製貼進 GitHub 的輸入框，改壞了也看不出上次改了什麼。
而這裡沒有一行是機密 —— 都是「怎麼把公開資料排版」。

SCRIPT 現在只負責登入與抓取，把原始回應落地到 raw/。

輸入：
    raw/quest_schedule.json   get_quest_schedule
    raw/event_schedule.json   get_event_schedule（與商店頁共用同一份）
    i18n/quests.{tw,cn,en}.json  任務文字（由 build_i18n.py 從語言包產生）

任務文字原本存在 GH secret（QUEST_TW / QUEST_EN_1 / QUEST_EN_2 / QUEST_CN），
現在改讀 repo 裡的 json。舊的環境變數仍然支援，但只在 json 不存在時才用，
方便你分階段搬遷 —— 確認 json 正常之後就可以把那四個 secret 刪掉。

用法：
    python scripts/build_quest.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shop_common import dump_json, load_json  # noqa: E402

# 遊戲換日錨在美西當地時間。原本寫死 timezone(timedelta(hours=-7))，
# 那在冬令時（PST, GMT-8）會整整錯一小時 —— 而 cron 剛好壓在換日線上，
# 錯一小時就可能抓到前一天的任務。用 IANA 時區讓 DST 自動處理。
#
# Windows 沒有內建 IANA 時區資料庫，要靠 PyPI 的 tzdata 套件；
# Linux / macOS 讀系統的就有。這裡刻意不做「抓不到就退回固定 offset」的
# fallback —— 那等於悄悄回到會錯一小時的舊行為，寧可直接中止。
try:
    SERVER_TZ = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:
    sys.exit(
        "找不到時區資料庫（America/Los_Angeles）。\n"
        "  Windows 沒有內建 IANA tzdata，請安裝：pip install tzdata\n"
        "  CI 上請把 workflow 的安裝步驟改成：pip install requests tzdata"
    )

TAIPEI_TZ = timezone(timedelta(hours=8))

REALMS = {
    "dawn": {"tw": "晨島", "cn": "晨岛", "en": "Isle of Dawn"},
    "prairie": {"tw": "雲野", "cn": "云野", "en": "Daylight Prairie"},
    "rain": {"tw": "雨林", "cn": "雨林", "en": "Hidden Forest"},
    "sunset": {"tw": "霞谷", "cn": "霞谷", "en": "Valley of Triumph"},
    "dusk": {"tw": "暮土", "cn": "暮土", "en": "Golden Wasteland"},
    "night": {"tw": "禁閣", "cn": "禁阁", "en": "Vault of Knowledge"},
}

TCANDLE_RE = re.compile(r"event_([a-z]+)_tcandles(\d+)")
SCANDLE_RE = re.compile(r"season_([a-z]+)_([a-z]+)_candles(\d+)")


def load_quest_dict(lang: str, i18n_dir: str) -> dict[str, str]:
    """優先讀 i18n/quests.<lang>.json，讀不到才退回舊的環境變數。"""
    path = os.path.join(i18n_dir, f"quests.{lang}.json")
    if os.path.exists(path):
        return load_json(path)
    legacy = join_secret_parts(f"QUEST_{lang.upper()}")
    if legacy:
        print(f"提醒：{lang} 仍在使用 QUEST_{lang.upper()} 環境變數，"
              f"建議改用 {path}")
        return parse_txt_to_dict(legacy)
    return {}


def parse_txt_to_dict(txt: str) -> dict[str, str]:
    """把 daily_quest_*.strings 轉成 {quest_id: 文字}。"""
    out: dict[str, str] = {}
    if not txt:
        return out
    for line in io.StringIO(txt):
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().replace("daily_quest_", "").replace("_desc", "").strip('"')
        v = v.strip().rstrip(";").strip().strip('"')
        out[k] = re.sub(r"<[^>]+>", "", v)   # 剝掉遊戲的樣式標記
    return out


def join_secret_parts(prefix: str, max_parts: int = 10) -> str:
    """GH secret 有長度上限，長的語言包要拆成 QUEST_TW / QUEST_TW_1 / _2…"""
    parts = []
    base = os.environ.get(prefix)
    if base:
        parts.append(base)
    for i in range(1, max_parts + 1):
        v = os.environ.get(f"{prefix}_{i}")
        if v:
            parts.append(v)
    return "\n".join(parts)


def translate_candle(name: str) -> dict[str, str] | None:
    """把蠟燭事件名翻成三語顯示名；不是蠟燭事件回 None。"""
    m = TCANDLE_RE.search(name)
    if m:
        r = REALMS.get(m.group(1), {k: m.group(1) for k in ("tw", "cn", "en")})
        i = m.group(2)
        return {
            "tw": f"{r['tw']}大蠟燭-{i}",
            "cn": f"{r['cn']}大蜡烛-{i}",
            "en": f"{r['en']} Treasure Candle-{i}",
        }
    m = SCANDLE_RE.search(name)
    if m:
        grp, r_key, i = m.group(1), m.group(2), m.group(3)
        r = REALMS.get(r_key, {k: r_key for k in ("tw", "cn", "en")})
        return {
            "tw": f"{r['tw']}季蠟{grp}組-{i}",
            "cn": f"{r['cn']}季蜡{grp}组-{i}",
            "en": f"{r['en']} Season Candle group:{grp}-{i}",
        }
    return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", default="raw")
    p.add_argument("--out", default="data.json")
    p.add_argument("--archive", default="archive")
    p.add_argument("--i18n", default="i18n")
    args = p.parse_args()

    quest_schedule = load_json(os.path.join(args.raw, "quest_schedule.json")) \
        .get("quest_schedule", [])
    event = load_json(os.path.join(args.raw, "event_schedule.json"))["event_schedule"]
    base_time = event.get("base_time", 0)
    events_list = event.get("events", [])

    dicts = {lang: load_quest_dict(lang, args.i18n) for lang in ("tw", "en", "cn")}
    for lang, d in dicts.items():
        if not d:
            print(f"警告：{lang} 的任務字典是空的，該語系會顯示原始 quest_id")

    now_server = datetime.now(SERVER_TZ)
    start_today = now_server.replace(hour=0, minute=0, second=0, microsecond=0)
    start_tomorrow = start_today + timedelta(days=1)
    ep_today = int(start_today.timestamp())
    ep_tomorrow = int(start_tomorrow.timestamp())
    ep_after = int((start_tomorrow + timedelta(days=1)).timestamp())

    def quests(target: dict, fallback: dict) -> dict:
        def window(s: int, e: int) -> list[str]:
            return [target.get(q["quest_id"]) or fallback.get(q["quest_id"]) or q["quest_id"]
                    for q in quest_schedule if s <= q["epoch"] < e]
        return {"today": window(ep_today, ep_tomorrow),
                "tomorrow": window(ep_tomorrow, ep_after)}

    q = {lang: quests(dicts[lang], dicts["en"]) for lang in ("tw", "cn", "en")}

    def candles(start: int, end: int) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {"tw": [], "cn": [], "en": []}
        for evt in events_list:
            names = translate_candle(evt.get("name", ""))
            if not names:
                continue
            dur = int(evt.get("duration", 0)) * 60
            for off in evt.get("when", []):
                s = base_time + off
                if s + dur > start and s < end:
                    for lang in out:
                        out[lang].append(names[lang])
                    break
        return out

    c_today = candles(ep_today, ep_tomorrow)
    c_tomorrow = candles(ep_tomorrow, ep_after)

    day = start_today.strftime("%Y-%m-%d")
    day_next = start_tomorrow.strftime("%Y-%m-%d")
    titles = {"tw": "繁體中文", "cn": "简体中文", "en": "English"}

    output = {
        "updated_at": datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S (GMT+8)"),
        **{lang: {
            "title": titles[lang],
            "date_today": day,
            "date_tomorrow": day_next,
            "quests_today": q[lang]["today"],
            "quests_tomorrow": q[lang]["tomorrow"],
            "candles_today": c_today[lang],
            "candles_tomorrow": c_tomorrow[lang],
        } for lang in ("tw", "cn", "en")},
    }
    dump_json(args.out, output, indent=4)

    archive_path = os.path.join(args.archive, start_today.strftime("%Y-%m") + ".json")
    archive = {}
    if os.path.exists(archive_path):
        try:
            archive = load_json(archive_path)
        except (json.JSONDecodeError, OSError) as e:
            # 原本是裸 except，會把「檔案壞了」跟「磁碟滿了」一起吞掉，
            # 然後安靜地覆蓋整個月的歷史。至少要印出來。
            print(f"警告：{archive_path} 讀取失敗（{e}），本月歷史會被重建")
    archive[day] = {
        "tw": q["tw"]["today"], "candles_tw": c_today["tw"],
        "cn": q["cn"]["today"], "candles_cn": c_today["cn"],
        "en": q["en"]["today"], "candles_en": c_today["en"],
    }
    dump_json(archive_path, archive, indent=4)

    print(f"data.json 完成（{day}，今日 {len(q['tw']['today'])} 個任務、"
          f"{len(c_today['tw'])} 個蠟燭事件）")
    print(f"archive 更新：{archive_path}（累計 {len(archive)} 天）")


if __name__ == "__main__":
    main()