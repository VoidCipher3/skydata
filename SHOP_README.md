# 商店資訊頁（iap.html / shop.html）

## 檔案

```
scripts/shop_common.py    事件排程解析、manifest 圖片解析、候選鏈（共用）
scripts/build_shop.py     raw/ → data/iap.json · shop.json · events.json
scripts/build_quest.py    raw/ → data.json · archive/（從 SCRIPT 搬出來的）
scripts/build_i18n.py     Localizable.strings → i18n/items.<lang>.json
config/tier_price.json    tier → 各幣別金額（TWD / CNY 待填）
assets/shop.js            兩頁共用的全部前端邏輯
assets/shop.css           沿用 index.html 的視覺語彙
iap.html / shop.html      各自只有欄位定義與篩選器不同
i18n/ui.{tw,cn,en}.json   介面字串
i18n/items.{tw,cn,en}.json 商品名稱與描述
SCRIPT_SPLIT.md           SCRIPT 收斂成 auth-only 的做法與 workflow 改動
```
## 圖片：manifest 而不是探測

圖片存在與否一律查 `raw/manifest.json`，由 Skyinfo 端的 `build_manifest.py` 產生。

`build_shop.py` 對每筆商品走一條候選鏈，取第一個查得到的：

**generic**
1. `item_name` 本身 —— `CharSkyKid_*` 直接命中（339 筆）
2. `consumable_defs` 查到的 `icon` —— 補 `outfit_*` 等小寫命名空間（332 筆）
3. generic 自帶的 `icon` 欄位 —— 只有 344 筆有值，最後備援（41 筆）

**iap**：`unlocks`（`CharSkyKid_*`）優先，`icon` 墊底。

manifest 的 `aliases` 那層不能省：`consumable_defs` 給的是
`UiOutfitBodyClassicDress`，而圖被 `All.py` 依 `OutfitDefs.json` 改名成
`CharSkyKid_Body_Ghost` 存檔。**少了它，generic 有 310 筆會查無此圖，
覆蓋率從 84% 掉到 48%。**

這個對照不能用命名規則推導 —— 實測 544 筆裡有 354 筆不符合
「`Ui` + PascalCase(name)」，例如 `bloom_sakura → UiEmoteAP04Celebrate`。

查表取代探測順帶解決三件事：每次 build 從 778 個 HEAD 降到 1 個請求、
不用跟 Cloudflare Pages 缺件回 200 的 SPA fallback 纏鬥、
「為什麼這筆沒圖」查 `imageCandidates` 即知。

---

## 上架狀態在前端即時算

`build_shop.py` **不寫入商品的狀態**，只輸出 `eventName`；
前端拿 `events.json` 的視窗即時判定。

理由：build 是每天一次，活動卻可能在任何時刻開始。狀態寫死會讓
凌晨三點開賣的東西在站上顯示「即將上架」到下午三點，
而旁邊的倒數是即時的 —— 同一張卡上兩個互相矛盾的說法。

實測驗證（把時鐘往前推）：

```
快照當下  {permanent:195, unscheduled:594, upcoming:38, active:16}
+7 天     {permanent:195, unscheduled:621, upcoming:26, active:1}
+30 天    {permanent:195, unscheduled:622, active:26}
```

狀態會正確翻轉。寫死的話這些變化要等隔天 build 才會反映。

### 四個狀態，沒有「已結束」

```
常駐        沒有 event_name（iap）／ event_name === ""（generic）
常駐(活動)  event 在排程裡且 duration 是永久哨兵值
上架中      start <= now < end
即將上架    start > now
目前不可購買 event_name 不在排程裡
```

`get_event_schedule` 只回傳進行中與未來的事件，已結束的直接消失
（實測 584 筆裡沒有一筆是過去式）。所以「已結束」與「明年還沒排檔期」
在資料上完全不可區分，UI 只能寫「目前不可購買」，tooltip 有說明。

要補回「已結束」只有一條路：每天把排程一起存檔，累積自己的歷史。

---

## 時間處理的三條硬規則

**一、全程用 epoch，只在顯示時轉時區。**
遊戲換日錨在美西當地時間：夏令 07:00 UTC、冬令 08:00 UTC。
`assets/shop.js` 裡只有 `formatAbsolute()` 一處做轉換。
`build_quest.py` 已改用 `zoneinfo.ZoneInfo("America/Los_Angeles")`，
取代原本寫死的 `timedelta(hours=-7)`（那在冬令時會整整錯一小時，
而 cron 剛好壓在換日線上，錯一小時就可能抓到前一天的任務）。

**二、daily / weekly 的重置時刻必須外推。**
上游只給「下一次」，而 cron `0 7 * * *` UTC **就是**每日重置時刻。
拿到已過期的 daily 值是常態不是例外。`nextReset()` 對 daily 加 86400、
weekly 加 604800 直到超過現在。monthly / yearly 不能推（月長不一、跨年、DST），
過期就顯示「重置時間待更新」。

**三、倒數旁邊一律並排絕對時刻。**
倒數用的是使用者裝置時鐘，裝置時間偏掉倒數就錯，而且沒人會懷疑。

---

## 資料新鮮度

`meta.fetchedAt` 存的是**上游的 `server_time`**，不是 runner 本地時鐘 ——
staleness 判斷靠它，被 runner 時鐘偏差污染就失準了。
`meta.builtAt` 另存 runner 時刻，兩者差超過 10 分鐘 build 會印警告。

| 年齡 | 行為 |
|---|---|
| < 26h | 正常 |
| 26–50h | 黃色橫幅，倒數繼續但標註可能不準 |
| > 50h | 紅色橫幅，**停用倒數**，只顯示絕對時間 |

cron 每天 15:00 GMT+8，正常年齡在 0–24h 之間循環，所以門檻必須大於 24h。
26h 表示漏更一次後兩小時亮燈，那兩小時留給排隊與 retry。

`FETCHED_AT_JITTER`（±10 秒）只作用在 `meta.fetchedAt`，
事件視窗與重置時刻不套用。**據實說明：這不構成實質混淆** ——
排程本來就公開在 `wrangler.toml`、`daily_quest.yml` 和 README 裡。
真要讓觸發時刻不可預測，得在 `SCRIPT` 開頭 sleep 一個隨機秒數。

---

## 其他已定案的行為

**價格軸。** iap 用 `tier`（真錢，透過 `config/tier_price.json` 換算）；
generic 用 `cost`（遊戲幣）。兩者不同軸，不混排。

**跨幣別排序。** generic 有 21 種幣別，沒有共同單位。選定單一幣別後才
開放依價格排序，否則排序選項旁顯示提示。硬排會造出一個不存在的匯率。

**免費項目。** `cost == 0` 有 335 筆，升冪排序會被洗版，
所以有「隱藏免費項目」開關，卡片上標「免費」而不是「0」。

**常駐的判定。** iaplist 的常駐品是**完全沒有 `event_name` 這個 key**（30 筆），
generic 的是**空字串**（173 筆）。兩邊正規化成 `null`。

**限購。** `max_per_cycle` 照實顯示（99 是伺服器真實上限，不是哨兵值），
但 624/843 是「限購 1 次」，全顯示只是雜訊，所以 `> 1` 才印。

**手動刷新只有一顆**，留在 `index.html`。三頁共用同一個後端動作，
商店頁再放一顆只是把同一個密碼入口多宣傳兩次。
（上一版 `shop.js` 裡那份 `manualRefresh` 有 bug：`REFRESH_ENDPOINT`
只定義在 `index.html` 的 inline script，在 `shop.js` 裡會拋
`ReferenceError`，按了完全沒反應。已隨按鈕一起移除。）

---

## 還沒做完的

**generic 的 595 條商品名稱翻譯。** `i18n/items.*.json` 的 `items` 區是空的，
語言包沒有對應 key（`localized_name` 843 筆全空就是證據）。
前端遇到空值會退回顯示原始資產名，不會留白 —— 但那 56 個無圖的商品
會變成純粹的 `outfit_body_bloomgreentunic`，可讀性等於零。先翻那 56 個。

**`config/tier_price.json` 的 TWD / CNY。** 目前 USD 直接等於 tier 值，
另兩個是 `null`（前端退回只顯示 `Tier N`）。從 skyinfo 的
`TIER_TWD` / `TIER_CNY` 表填入。

**`consumable_defs` 的時效。** 手上這份是 2026-07-16、generic 是 08-26，
差六週，導致 15 筆新裝扮查不到 icon。兩個端點在同一次執行裡抓就沒有這問題
（`SCRIPT_SPLIT.md` 的做法已經是了）。

---

## 仍未處理的既有問題

1. Worker 的刷新端點沒有「失敗次數」節流 —— 密碼錯誤的請求不計數、不擋，
   而且 `Access-Control-Allow-Origin: *`。另外刷新現在會跑完整條 pipeline
   （五個端點 + 兩支 build），180 秒的 KV 節流可能要往上調。
2. GitHub Pages 與 Cloudflare Worker 各 serve 一份，更新時機不同
   （Pages 靠 commit、Worker 靠 deploy）。多了三個資料檔之後會出現
   「商店是新的、事件表是舊的」這種局部不一致。先決定正式入口是哪一個。
3. skyinfoweb 有 SPA fallback（缺件回 200 + `text/html`）。
   商店頁已經不受影響（改查 manifest），但那個站本身的 404 行為仍不正確。
