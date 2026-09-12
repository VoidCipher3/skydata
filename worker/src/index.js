const THROTTLE_SECONDS = 180;

// --- IP rate limit（針對「密碼錯誤」次數，非成功觸發）---
// 窗內累計失敗次數，達上限就擋。存在現成的 REFRESH_KV，不用開新資源。
// 注意：KV 是最終一致，瞬間高併發可能有幾次 race 漏過去 —— 對「觸發重建」這種
// 低風險端點夠用；要滴水不漏改用 Durable Objects 或原生 Rate Limiting binding。
const RL_WINDOW_SECONDS = 600;   // 計數窗長度（也是達標後的封鎖時間）
const RL_MAX_FAILURES = 5;       // 窗內允許的密碼錯誤次數

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      ...cors(),
      "Content-Type": "application/json",
    },
  });
}

// timing-safe compare
function timingSafeEqual(a, b) {
  if (!a || !b) return false;
  if (a.length !== b.length) return false;

  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

// --- rate limit helpers ---
// 讀目前失敗計數。KV/IP 任何一個拿不到就放行（fail-open）——
// 這是刻意的：rate limit 是防濫用的加分項，不該因為它自己壞掉就讓整個刷新功能掛掉。
async function rlGet(env, ip) {
  if (!env?.REFRESH_KV || !ip) return 0;
  try {
    const raw = await env.REFRESH_KV.get(`rl:${ip}`);
    return raw ? Number(raw) : 0;
  } catch (e) {
    console.log("RL read error:", e);
    return 0;
  }
}

// 失敗一次就 +1，並把 TTL 刷新成整個窗（→「最後一次失敗後再鎖 N 分鐘」）。
async function rlBump(env, ip, current) {
  if (!env?.REFRESH_KV || !ip) return;
  try {
    await env.REFRESH_KV.put(`rl:${ip}`, String(current + 1), {
      expirationTtl: RL_WINDOW_SECONDS,
    });
  } catch (e) {
    console.log("RL write error:", e);
  }
}

// 密碼對了就清掉計數，避免使用者自己 typo 幾次後被鎖。
async function rlClear(env, ip) {
  if (!env?.REFRESH_KV || !ip) return;
  try {
    await env.REFRESH_KV.delete(`rl:${ip}`);
  } catch (e) {
    console.log("RL clear error:", e);
  }
}

// 觸發 GitHub Actions 的 workflow_dispatch，並做 KV 節流。
// 手動刷新按鈕（POST /）跟 Cron Trigger（每 30 分鐘）都走這個函式，
// 節流用同一把 KV key，避免使用者手動按刷新時剛好跟排程撞在一起重複觸發。
async function triggerGithubWorkflow(env) {
  const now = Date.now();

  let last = null;
  try {
    last = await env?.REFRESH_KV?.get("last_trigger");
  } catch (e) {
    console.log("KV error:", e);
  }

  if (last && now - Number(last) < THROTTLE_SECONDS * 1000) {
    const retry_after = Math.ceil((THROTTLE_SECONDS * 1000 - (now - Number(last))) / 1000);
    return { ok: false, error: "too_soon", retry_after, status: 429 };
  }

  const url =
    `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}` +
    `/actions/workflows/${env.GH_WORKFLOW_FILE}/dispatches`;

  let ghResp;
  try {
    ghResp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "skyquest-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: env.GH_REF || "main" }),
    });
  } catch (e) {
    return { ok: false, error: "github_network_error", status: 502 };
  }

  if (ghResp.status !== 204) {
    const detail = await ghResp.text().catch(() => "");
    return { ok: false, error: "github_dispatch_failed", detail, status: ghResp.status, httpStatus: 502 };
  }

  try {
    await env?.REFRESH_KV?.put("last_trigger", String(now));
  } catch (e) {
    console.log("KV write error:", e);
  }

  return { ok: true, triggered_at: now, status: 200 };
}

export default {
  async fetch(request, env, ctx) {
    console.log("REQ:", request.method, request.url);

    // =====================
    // OPTIONS (CORS)
    // =====================
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: cors(),
      });
    }

    // =====================
    // Not POST → let the static assets (repo 根目錄) handle it.
    // This covers GET (viewing the actual site: tw/cn/en, css, js, json data)
    // as well as HEAD, and anything else that isn't our trigger API.
    // =====================
    if (request.method !== "POST") {
      return env.ASSETS.fetch(request);
    }

    // =====================
    // rate limit：先看這個 IP 在窗內錯了幾次，達標直接擋，連密碼比對都不做。
    // =====================
    const ip = request.headers.get("cf-connecting-ip") || "";
    const failures = await rlGet(env, ip);
    if (failures >= RL_MAX_FAILURES) {
      return json({ ok: false, error: "rate_limited", retry_after: RL_WINDOW_SECONDS }, 429);
    }

    // =====================
    // parse body
    // =====================
    let body;
    try {
      body = await request.json();
    } catch (e) {
      return json({ ok: false, error: "bad_json" }, 400);
    }

    const password = String(body?.password || "");
    const expected = String(env?.REFRESH_PASSWORD || "");

    if (!timingSafeEqual(password, expected)) {
      // 密碼錯 → 計數 +1（TTL 刷新成整個窗）
      await rlBump(env, ip, failures);
      return json({ ok: false, error: "wrong_password" }, 401);
    }

    // 密碼對 → 清掉這個 IP 的失敗計數
    await rlClear(env, ip);

    // =====================
    // 觸發 GitHub Actions（含節流），跟 Cron Trigger 共用同一套邏輯
    // =====================
    const result = await triggerGithubWorkflow(env);

    if (!result.ok) {
      if (result.error === "too_soon") {
        return json({ ok: false, error: "too_soon", retry_after: result.retry_after }, 429);
      }
      return json({ ok: false, error: result.error, detail: result.detail }, result.httpStatus || 502);
    }

    return json({ ok: true, triggered_at: result.triggered_at });
  },

  // Cron Trigger：由 wrangler.toml 的 [triggers] 設定觸發時間（例如每 30 分鐘）。
  // 比起 GitHub 自己的 schedule: cron（官方說明本身就有 ±幾分鐘誤差），
  // Cloudflare 的 Cron Trigger 準時很多，這裡直接呼叫跟手動刷新一樣的觸發邏輯。
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      triggerGithubWorkflow(env).then((result) => {
        console.log("cron trigger result:", JSON.stringify(result));
      })
    );
  },
};