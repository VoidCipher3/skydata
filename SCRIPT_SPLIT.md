## 本機測試

```bash
export B='VERSION:ACCESS_KEY'
export LOGIN_BODY="$(cat /path/to/login_body.json)"
python scripts/fetch_raw.py --only quest_schedule.json   # 先試一個端點
python scripts/fetch_raw.py                              # 五個都抓
```

`--only` 是除錯用的，省得每次都打五個請求。

**`login_body.json` 放在 repo 外面**，或放進 `secret_data/`（已被兩個 ignore 檔擋住）。

## 已驗證的行為

用假上游測過三種情況：

| 情境 | 行為 |
|---|---|
| 端點回 418（session 被踢，body 不是 JSON） | 自動重新登入、沿用原請求重試，成功 |
| 非必要端點回 500 | 印警告、跳過該檔、job 繼續（退出碼 0） |
| 必要端點回 503 | 例外往上拋，job 失敗 |
| 缺 `LOGIN_BODY` / JSON 格式錯 / `B` 缺失 | 明確訊息 + 退出碼 1，不會半路才炸 |

418 那條特別重要：上游在 session 被別處登入踢掉時回 418 而且 body 是空的，
直接 `.json()` 會炸在偵測到 418 之前。端點從 2 個變 5 個之後執行時間拉長，
中途掉 session 的機率跟著上升，不能只在開頭登入一次就假設全程有效。

## 檢查清單

第一次 `git add` 之前：
- [ ] `git check-ignore -v raw/ secret_data/ .env` 三個都被命中
- [ ] `git status --short` 逐行看過
