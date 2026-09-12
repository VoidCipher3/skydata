# skyquest

《Sky: Children of the Light》每日任務追蹤站，每天自動更新，支援繁體中文／简体中文／English，並提供歷史紀錄查詢。

- 目前網址: https://voidcipher3.github.io/skyquest


## 架構

- `main.py` + GitHub Actions（`.github/workflows/daily_quest.yml`）：每天15:00抓取最新任務資料，寫入 `data.json` 並歸檔到 `archive/`
- `index.html`：前端頁面，讀取 `data.json` 顯示今日／明日任務、蠟燭圖片，並可查詢 `archive/` 歷史紀錄
- `worker/`：Cloudflare Worker，負責（1）用密碼手動觸發 GitHub Actions 立即更新，（2）用 Cron Trigger 準時觸發排程，細節見 `worker/README.md`
