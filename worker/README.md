# 手動刷新用的 Cloudflare Worker（skyquest）

密碼比對通過 → Worker 呼叫 GitHub API 的 `workflow_dispatch` → 立刻執行一次 `.github/workflows/daily_quest.yml`，
不用等隔天15:00的 cron。GitHub Token 只放在 Worker 的環境變數，前端完全看不到。

## 部署步驟

```bash
cd worker
wrangler kv namespace create SKYQUEST_REFRESH_KV
wrangler secret put REFRESH_PASSWORD=
wrangler secret put GH_TOKEN
wrangler deploy
```