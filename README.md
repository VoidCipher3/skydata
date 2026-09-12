# skyquest

《Sky: Children of the Light》每日任務與商店資訊追蹤站，支援繁中／簡中／English。

網址：https://voidcipher3.github.io/skydata

## 主要功能

- 每日任務自動更新與歷史紀錄
- IAP／遊戲內商店資訊
- 活動排程與即時上架狀態
- 商品圖片與 Manifest 對照
- 多語系商品名稱與介面
- Cloudflare Worker 手動／自動更新

## 專案架構

```text
scripts/
├── build_quest.py
├── build_shop.py
├── build_i18n.py
├── shop_common.py
├── fetch_raw.py
└── lang

assets/
├── shop.js
└── shop.css

data/
├── data.json
├── iap.json
├── shop.json
└── events.json

archive/
i18n/
config/
worker/

index.html
iap.html
shop.html

.github/workflows/deploy.yml
```

- `build_quest.py`：產生每日任務與歷史資料
- `build_shop.py`：產生商店與活動資料
- `build_i18n.py`：產生多語系資料
- `fetch_raw.py` : 抓取數據
- `shop.js`：IAP／商店共用前端邏輯
- `worker/`：手動及 Cron 更新

## 重要設計

### 圖片

圖片存在性直接查 `raw/manifest.json`，不逐張探測。

`manifest.aliases` 用來處理不同資料來源的圖片名稱差異。

### 上架狀態

Build 不寫死商品狀態，只保存 `eventName`。

前端依 `events.json` + 目前時間即時判斷：

- 常駐
- 常駐（活動）
- 上架中
- 即將上架
- 目前不可購買

因此活動開始後不必等下一次 build 才更新狀態。

### 時間

- 內部統一使用 Epoch
- 遊戲換日使用 `America/Los_Angeles`
- Daily／Weekly reset 會自動外推
- 倒數同時顯示絕對時間

### 資料新鮮度

| 年齡 | 行為 |
|---|---|
| `< 26h` | 正常 |
| `26–50h` | 黃色警告 |
| `> 50h` | 紅色警告，停用倒數 |

`meta.fetchedAt` 使用上游 `server_time`，避免 Runner 時鐘影響判斷。

## 目前待處理

1. Generic 商品翻譯，優先處理 56 筆無圖片商品
2. `tier_price.json` 補 TWD／CNY
3. Worker refresh endpoint 加入失敗節流
4. 決定 GitHub Pages／Cloudflare 的正式資料入口