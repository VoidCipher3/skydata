"""登入遊戲 API，把五個端點的原始回應落地到 raw/。

這支取代原本塞在 GH secret 裡的 SCRIPT。搬出來的分界是：
**程式進 repo，只有憑證進 secret。**

端點網址、418 重試、失敗分級、隨機間隔 —— 這些沒有一行是機密，
卻是你會反覆修改的部分。放在 secret 裡等於放棄版本控制、diff、
code review 和本機測試，只為了藏一個 dict。

本機測試可以放一個 .env（已被 .gitignore 擋住），格式是 KEY=VALUE：

    B=0.34.5.410941:1743442606-dc4e25...
    LOGIN_BODY={"type":"...","sig":"...","hashes":[1135420871]}

LOGIN_BODY 要壓成單行 JSON。CI 上不需要 .env，
GitHub 注入的環境變數優先，本機檔案不會蓋掉它。

需要的環境變數：
    B            "VERSION:BUILD_ACCESS_KEY"（沿用既有的 secret）
    LOGIN_BODY   登入 body 的 JSON 字串（sig / hashes / device_key 都在這裡）
    LOGIN_HEADERS  選用，額外 header 的 JSON 字串，會覆蓋預設值
    IAP_COUNTRY    選用，iaplist 要查的國碼，預設 US

raw/ 只活在 runner 的磁碟上，.gitignore 與 .assetsignore 都擋著。

用法：
    python scripts/fetch_raw.py
    python scripts/fetch_raw.py --only quest_schedule.json   # 只抓一個，除錯用
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import uuid

import requests

BASE = "https://live.radiance.thatgamecompany.com"
URL_LOGIN = f"{BASE}/account/auth/login"

# iaplist 要多帶兩個參數。
#   platform: "fake" —— 讓伺服器不要依平台過濾，回傳完整清單
#                        （回應裡的 platforms 欄位才會有 *, funtap, xsolla, nx 各種值）
#   country:  影響哪些商品可見、以及定價分級
#
# country 用環境變數蓋掉就能換區域。預設 US，因為 config/tier_price.json
# 的 tier→金額對照是以美金為基準；換區域的話那張表也要跟著換。
IAP_COUNTRY = os.environ.get("IAP_COUNTRY", "US")

# 每個端點：輸出檔名、網址、掛掉要不要讓整個 job 失敗、額外的 body 參數。
#
# 任務是每天要看的，商店資料放兩天也沒人在意 —— 所以商店那三個 required=False：
# 失敗就印警告、跳過該檔，舊的 data/shop.json 留著、fetchedAt 不動，
# 前端的 staleness 橫幅 26 小時後自己會亮。
ENDPOINTS = [
    {
        "file": "quest_schedule.json",
        "url": f"{BASE}/service/quest/api/v1/get_quest_schedule",
        "required": True,
    },
    {
        "file": "event_schedule.json",
        "url": f"{BASE}/account/get_event_schedule",
        "required": True,
    },
    {
        "file": "generic.json",
        "url": f"{BASE}/account/get_generic_shops",
        "required": False,
    },
    {
        "file": "iaplist.json",
        "url": f"{BASE}/account/iaplist",
        "required": False,
        "body": {"platform": "fake", "country": IAP_COUNTRY},
    },
    {
        "file": "consumable_defs.json",
        "url": f"{BASE}/account/consumable/get_consumable_defs",
        "required": False,
    },
]

TIMEOUT = 30
# 請求之間的隨機間隔。五個請求連發的時序太整齊。
GAP = (0.5, 2.0)


def load_dotenv(path: str = ".env") -> None:
    """本機測試用的極簡 .env 載入器，不需要 python-dotenv。

    只認 KEY=VALUE，值不做跳脫處理（LOGIN_BODY 請放單行 JSON）。
    已存在的環境變數優先 —— CI 上 GitHub 注入的 secret 不會被本機檔案蓋掉，
    而 CI 的工作目錄裡本來就沒有 .env（它被 .gitignore 擋著）。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # 去掉整段包住的引號，但保留 JSON 內部的引號
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
    print(f"已載入 {path}")


def env_json(name: str, required: bool = True) -> dict:
    raw = os.environ.get(name, "")
    if not raw:
        if required:
            sys.exit(f"缺少環境變數 {name}")
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # 只給 column number 的話，要自己去數第 223 個字元是什麼，很難用。
        # 直接把出事位置前後印出來，並點名最常見的成因。
        lo, hi = max(0, e.pos - 30), e.pos + 30
        snippet = raw[lo:hi].replace("\n", "\\n")
        caret = " " * (e.pos - lo) + "^"
        hint = ""
        tail = raw[e.pos:e.pos + 5]
        for py, js in (("False", "false"), ("True", "true"), ("None", "null")):
            if tail.startswith(py):
                hint = f"\n  這裡是 Python 的 {py}，JSON 要寫成 {js}"
        if tail.startswith("'"):
            hint = "\n  這裡是單引號，JSON 只接受雙引號"
        sys.exit(f"{name} 不是合法的 JSON：{e.msg}（位置 {e.pos}）\n"
                 f"  …{snippet}…\n"
                 f"   {caret}{hint}\n"
                 f"  提示：不要手工把 Python dict 改成 JSON，用 "
                 f"python -c \"import json; print(json.dumps(THE_DICT))\" 產生。")


def build_headers() -> dict:
    b = os.environ.get("B", "")
    if ":" not in b:
        sys.exit('缺少環境變數 B，格式應為 "VERSION:BUILD_ACCESS_KEY"')
    version, access_key = b.split(":", 1)

    headers = {
        "User-Agent": f"Sky-Live-com.tgc.sky.android/{version}"
                      f"(OPPOA11;android29.0.0;zh-Hant)",
        "X-Session-ID": str(uuid.uuid4()),
        "trace-id": "".join(random.choices(string.ascii_letters + string.digits, k=7)),
        "x-sky-build-access-key": access_key,
    }
    # LOGIN_HEADERS 裡的值覆蓋預設 —— Content-Type、x-sky-install-source、
    # x-sky-level-id 之類跟帳號綁定的欄位放那裡。
    headers.update(env_json("LOGIN_HEADERS", required=False))
    return headers


class Client:
    def __init__(self):
        self.s = requests.Session()
        self.login_headers = build_headers()
        self.login_body = env_json("LOGIN_BODY")
        self.headers: dict = {}
        self.body: dict = {}

    def login(self) -> None:
        r = self.s.post(URL_LOGIN, headers=self.login_headers,
                        json=self.login_body, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        session = data.get("session")
        user = data.get("authinfo", {}).get("user")
        if not session:
            raise RuntimeError("登入失敗：回應裡沒有 session")

        self.headers = dict(self.login_headers)
        self.headers.update({
            "session": session, "user-id": user,
            "x-user-id": user, "x-session-token": session,
        })
        self.body = {"user": user, "user-id": user, "session": session}
        print("登入成功")

    def fetch(self, url: str, extra: dict | None = None, retries: int = 2) -> dict:
        """打一個端點，遇到 418 自動重登後重試。

        上游在 session 被別處登入踢掉時回 HTTP 418，而且 body 不是 JSON（空的）——
        直接 .json() 會炸在偵測到 418 之前。端點從 2 個變 5 個之後執行時間拉長，
        中途掉 session 的機率跟著上升，所以不能只在開頭登入一次就假設全程有效。
        """
        body = dict(self.body)
        if extra:
            body.update(extra)
        for attempt in range(retries + 1):
            r = self.s.post(url, headers=self.headers, json=body, timeout=TIMEOUT)
            if r.status_code == 418:
                if attempt == retries:
                    raise RuntimeError(f"重登 {retries} 次後仍然收到 418：{url}")
                print("  收到 418（session 被踢），重新登入後重試")
                self.login()
                # 重登之後 session / user 都換了，body 要跟著更新
                body = dict(self.body)
                if extra:
                    body.update(extra)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="raw")
    p.add_argument("--only", help="只抓這個檔名，除錯用")
    p.add_argument("--env", default=".env", help="本機用的環境變數檔，不存在就略過")
    args = p.parse_args()

    load_dotenv(args.env)

    os.makedirs(args.out, exist_ok=True)
    targets = [e for e in ENDPOINTS if not args.only or e["file"] == args.only]
    if args.only and not targets:
        sys.exit(f"--only 的值要是這幾個之一：{[e['file'] for e in ENDPOINTS]}")

    client = Client()
    client.login()

    failed = []
    for ep in targets:
        filename, url = ep["file"], ep["url"]
        try:
            data = client.fetch(url, ep.get("body"))
        except Exception as e:
            if ep["required"]:
                raise
            print(f"警告：{filename} 抓取失敗（{e}），跳過，沿用上次的資料")
            failed.append(filename)
            continue

        path = os.path.join(args.out, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"  {path}  {os.path.getsize(path) / 1024:.0f} KB")
        time.sleep(random.uniform(*GAP))

    if failed:
        print(f"共 {len(failed)} 個非必要端點失敗：{failed}")
    print("抓取完成")


if __name__ == "__main__":
    main()
