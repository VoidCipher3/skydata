/* skyquest 商店頁共用邏輯。iap.html 與 shop.html 各傳一份 config 進 SkyShop.init()。
 *
 * 三條不可違反的規則：
 *
 * 1. 所有時間以 epoch 秒流動，只在 formatAbsolute() 一處轉成當地時間。
 *    遊戲換日錨在美西當地時間（夏令 07:00 UTC、冬令 08:00 UTC），
 *    中途做任何 offset 加減都會在換季時錯一小時。
 *
 * 2. 倒數用的是使用者裝置時鐘，所以每個倒數旁邊一律並排顯示絕對時刻 ——
 *    裝置時間有問題的人才看得出來。
 *
 * 3. 上架狀態在這裡即時算，不吃 build 期寫死的值。
 *    build 是每天一次，活動卻可能在任何時刻開始；狀態寫死會讓
 *    凌晨三點開賣的東西顯示「即將上架」到下午三點，跟旁邊的即時倒數互相矛盾。
 */
(function (global) {
  'use strict';

  // 資料年齡門檻。cron 是每天 15:00 GMT+8，正常年齡在 0–24h 之間循環，
  // 所以門檻必須大於 24h，否則每天更新前都會誤報。
  // 26h：漏更一次後兩小時亮燈，那兩小時留給排隊與 retry。
  // 50h：連續漏更兩次，倒數已不可信，停用。
  var STALE_WARN_HOURS = 26;
  var STALE_SEVERE_HOURS = 50;

  var DAY = 86400;
  var WEEK = 604800;

  var S_PERMANENT = 'permanent';
  var S_ACTIVE = 'active';
  var S_UPCOMING = 'upcoming';
  var S_UNSCHEDULED = 'unscheduled';

  var STATUS_ORDER = { active: 0, upcoming: 1, permanent: 2, unscheduled: 3 };

  var state = {
    lang: 'tw',
    ui: {},
    names: { commerce: {}, items: {} },
    data: null,
    events: null,
    config: null,
    filters: {},
    sort: 'default',
    hideFree: false
  };

  /* ---------- 小工具 ---------- */

  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function t(key, vars) {
    var s = state.ui[key];
    if (s === undefined) return key;
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function (m, k) {
      return vars[k] !== undefined ? vars[k] : m;
    });
  }
  function nowSec() { return Math.floor(Date.now() / 1000); }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  function formatAbsolute(epoch) {
    if (!epoch) return '';
    // 唯一做時區轉換的地方 —— 交給瀏覽器，不自己算 offset。
    return new Date(epoch * 1000).toLocaleString(undefined, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit'
    });
  }

  function formatDuration(seconds) {
    if (seconds <= 0) return t('timeMin', { n: 0 });
    var d = Math.floor(seconds / DAY);
    var h = Math.floor((seconds % DAY) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var parts = [];
    if (d) parts.push(t('timeDay', { n: d }));
    if (d < 2 && h) parts.push(t('timeHour', { n: h }));
    if (!d && m) parts.push(t('timeMin', { n: m }));
    return parts.length ? parts.join(' ') : t('timeMin', { n: 0 });
  }

  /* ---------- 上架狀態（即時） ---------- */

  // events.json 的形狀：{ "global_camping": { w: [[start, end], ...], p: false } }
  // w 是所有視窗，可能重疊（skyfest_fireworks_show 有 16 個同時進行中的視窗），
  // 所以要走完全部取聯集，不能找到第一個符合的就 break。
  function resolveStatus(eventName) {
    var now = nowSec();
    if (!eventName) return { status: S_PERMANENT };

    var slot = state.events && state.events.events && state.events.events[eventName];
    if (!slot) return { status: S_UNSCHEDULED };
    if (slot.p) return { status: S_PERMANENT };

    var windows = slot.w || [];
    var activeEnd = null;
    var nextStart = null;

    for (var i = 0; i < windows.length; i++) {
      var s = windows[i][0], e = windows[i][1];
      if (s <= now && now < e) {
        // 視窗重疊時取涵蓋最晚的結束時間，否則倒數會提早歸零
        if (activeEnd === null || e > activeEnd) activeEnd = e;
      } else if (s > now && (nextStart === null || s < nextStart)) {
        nextStart = s;
      }
    }

    if (activeEnd !== null) return { status: S_ACTIVE, end: activeEnd, nextStart: nextStart };
    if (nextStart !== null) return { status: S_UPCOMING, start: nextStart };
    return { status: S_UNSCHEDULED };
  }

  /* ---------- 重置時刻 ---------- */

  // 上游只給「下一次」重置，而抓取排程剛好壓在每日重置線上
  // （cron 07:00 UTC 就是遊戲換日），拿到已過期的 daily 值是常態而非例外。
  // daily / weekly 週期固定可以安全外推；monthly / yearly 不行
  // （月長不一、跨年界、DST 讓錨點在 07:00/08:00 UTC 間跳）。
  function nextReset(kind) {
    var resets = state.events && state.events.resets;
    if (!resets) return null;
    var at = resets[kind];
    if (!at) return null;
    var now = nowSec();
    if (kind === 'daily') { while (at <= now) at += DAY; return at; }
    if (kind === 'weekly') { while (at <= now) at += WEEK; return at; }
    return at > now ? at : null;
  }

  var RESET_KIND = {
    gen_shop_daily_reset: 'daily',
    gen_shop_weekly_reset: 'weekly',
    gen_shop_monthly_reset: 'monthly',
    gen_shop_yearly_reset: 'yearly'
  };

  /* ---------- 資料新鮮度 ---------- */

  function staleness() {
    var meta = state.data && state.data.meta;
    if (!meta || !meta.fetchedAt) return { level: 'ok', hours: 0 };
    var hours = (nowSec() - meta.fetchedAt) / 3600;
    if (hours >= STALE_SEVERE_HOURS) return { level: 'severe', hours: Math.floor(hours) };
    if (hours >= STALE_WARN_HOURS) return { level: 'warn', hours: Math.floor(hours) };
    return { level: 'ok', hours: Math.floor(hours) };
  }
  function countdownsEnabled() { return staleness().level !== 'severe'; }

  /* ---------- 名稱與價格 ---------- */

  function displayName(row) {
    if (state.config.kind === 'iap') {
      return state.names.commerce[row.nameKey] || row.nameKey || row.id;
    }
    return state.names.items[row.itemName] || row.itemName || row.name;
  }
  function displayDesc(row) {
    if (state.config.kind !== 'iap' || !row.descKey) return '';
    return state.names.commerce[row.descKey] || '';
  }

  function formatMoney(prices) {
    var code = (state.events && state.events.defaultCurrency) || 'USD';
    var defs = (state.events && state.events.priceCurrencies) || {};
    var amount = prices && prices[code];
    if (amount === null || amount === undefined) return null;
    var def = defs[code] || { symbol: '', decimals: 0 };
    return def.symbol + Number(amount).toFixed(def.decimals);
  }

  // iap 用 tier（真錢），generic 用 cost（遊戲幣）。兩者不同軸，不互相比較。
  function sortValue(row) {
    return state.config.kind === 'iap' ? (row.tier || 0) : (row.cost || 0);
  }

  /* ---------- 篩選與排序 ---------- */

  // generic 有 21 種幣別（candles / heart / prestige / color_* / event_candle_*），
  // 跨幣別沒有共同單位，硬排會造出一個不存在的匯率。
  // 所以選定單一幣別後才開放依價格排序。
  function priceSortAllowed() {
    return state.config.kind === 'iap'
      || (state.filters.currency && state.filters.currency !== '*');
  }

  function matches(row) {
    var f = state.filters;
    if (f.status && f.status !== '*' && row._status !== f.status) return false;
    if (f.event && f.event !== '*') {
      if ((row.eventName || '__permanent__') !== f.event) return false;
    }
    if (f.currency && f.currency !== '*' && row.currency !== f.currency) return false;
    if (f.shop && f.shop !== '*' && row.shopName !== f.shop) return false;
    if (f.type && f.type !== '*' && row.itemType !== f.type) return false;
    if (state.hideFree && sortValue(row) === 0) return false;
    if (f.q) {
      var hay = [displayName(row), row.id, row.nameKey, row.itemName, row.name, row.eventName]
        .filter(Boolean).join(' ').toLowerCase();
      if (hay.indexOf(f.q.toLowerCase()) === -1) return false;
    }
    return true;
  }

  function sortRows(rows) {
    var s = state.sort;
    if ((s === 'priceAsc' || s === 'priceDesc') && priceSortAllowed()) {
      var dir = s === 'priceAsc' ? 1 : -1;
      return rows.sort(function (a, b) { return (sortValue(a) - sortValue(b)) * dir; });
    }
    if (s === 'nameAsc') {
      return rows.sort(function (a, b) { return displayName(a).localeCompare(displayName(b)); });
    }
    // 預設：上架中 → 即將上架 → 常駐 → 目前不可購買，組內依名稱
    return rows.sort(function (a, b) {
      var d = STATUS_ORDER[a._status] - STATUS_ORDER[b._status];
      return d || displayName(a).localeCompare(displayName(b));
    });
  }

  /* ---------- 繪製 ---------- */

  function statusChip(row) {
    var chip = el('span', 'chip chip-' + row._status, t('status' + cap(row._status)));
    // 「已結束」與「明年還沒排檔期」在上游資料裡不可區分，tooltip 要說明白
    if (row._status === S_UNSCHEDULED) chip.title = t('unscheduledHint');
    return chip;
  }

  function timingLine(row) {
    var now = nowSec();
    var st = row._timing;
    var wrap = el('div', 'timing');
    if (row._status === S_ACTIVE && st.end) {
      if (countdownsEnabled()) {
        wrap.appendChild(el('span', 'countdown', t('endsIn', { t: formatDuration(st.end - now) })));
      }
      wrap.appendChild(el('span', 'abs', t('endsAt', { d: formatAbsolute(st.end) })));
      return wrap;
    }
    if (row._status === S_UPCOMING && st.start) {
      if (countdownsEnabled()) {
        wrap.appendChild(el('span', 'countdown', t('startsIn', { t: formatDuration(st.start - now) })));
      }
      wrap.appendChild(el('span', 'abs', t('startsAt', { d: formatAbsolute(st.start) })));
      return wrap;
    }
    return null;
  }

  function resetLine(row) {
    var kind = RESET_KIND[row.resetEvent];
    if (!kind) return null;
    var at = nextReset(kind);
    var wrap = el('div', 'timing');
    wrap.appendChild(el('span', 'cycle', t('reset' + cap(kind))));
    if (!at) {
      wrap.appendChild(el('span', 'abs', t('resetStale')));
      return wrap;
    }
    if (countdownsEnabled()) {
      wrap.appendChild(el('span', 'countdown', t('resetIn', { t: formatDuration(at - nowSec()) })));
    }
    wrap.appendChild(el('span', 'abs', t('resetAt', { d: formatAbsolute(at) })));
    return wrap;
  }

  function thumb(image) {
    if (!image) return null;
    // 沒圖就顯示原始資產名 —— 不放破圖，也不靠 onerror 事後補救
    if (!image.url) return el('span', 'asset-text', image.name);
    var img = el('img', 'asset-img');
    img.src = image.url;
    img.alt = image.name;
    img.title = image.name;
    img.loading = 'lazy';
    return img;
  }

  function assetRow(label, images) {
    if (!images || !images.length) return null;
    var wrap = el('div', 'assets');
    if (label) wrap.appendChild(el('span', 'assets-label', label));
    var strip = el('div', 'assets-strip');
    images.forEach(function (image) {
      var node = thumb(image);
      if (node) strip.appendChild(node);
    });
    wrap.appendChild(strip);
    return wrap;
  }

  function cardIap(row) {
    var card = el('article', 'item');
    var head = el('header', 'item-head');
    head.appendChild(el('h3', 'item-name', displayName(row)));
    head.appendChild(statusChip(row));
    card.appendChild(head);

    var price = el('div', 'price');
    var money = formatMoney(row.prices);
    if (money) {
      price.appendChild(el('span', 'price-main', money));
      price.appendChild(el('span', 'price-sub', t('tierLabel', { n: row.tier })));
    } else {
      price.appendChild(el('span', 'price-main', t('tierLabel', { n: row.tier })));
    }
    if (row.giftable) price.appendChild(el('span', 'tag', t('giftable')));
    card.appendChild(price);

    var desc = displayDesc(row);
    if (desc) card.appendChild(el('p', 'item-desc', desc));

    var unlocks = assetRow(t('unlocks'), row.unlocks);
    if (unlocks) card.appendChild(unlocks);

    if (row.bonus && row.bonus.length) {
      var b = el('div', 'assets');
      b.appendChild(el('span', 'assets-label', t('bonus')));
      var strip = el('div', 'assets-strip');
      row.bonus.forEach(function (x) {
        strip.appendChild(el('span', 'asset-text', x.count + ' × ' + x.currency));
      });
      b.appendChild(strip);
      card.appendChild(b);
    }

    var timing = timingLine(row);
    if (timing) card.appendChild(timing);
    if (row.eventName) card.appendChild(el('div', 'meta', row.eventName));
    return card;
  }

  function cardShop(row) {
    var card = el('article', 'item');
    var head = el('header', 'item-head');
    head.appendChild(el('h3', 'item-name', displayName(row)));
    head.appendChild(statusChip(row));
    card.appendChild(head);

    var price = el('div', 'price');
    if (row.cost === 0) {
      price.appendChild(el('span', 'price-main free', t('freeTag')));
    } else {
      price.appendChild(el('span', 'price-main', String(row.cost)));
      price.appendChild(el('span', 'price-sub', row.currency));
    }
    if (row.itemCount > 1) price.appendChild(el('span', 'tag', '× ' + row.itemCount));
    card.appendChild(price);

    // 624/843 是「每週期限購 1 次」，全顯示只會變雜訊，所以 >1 才印
    if (row.maxPerCycle > 1) {
      card.appendChild(el('div', 'limit', t('limitPerCycle', { n: row.maxPerCycle })));
    }

    var img = assetRow('', row.image ? [row.image] : []);
    if (img) card.appendChild(img);

    // 這是「得先在別處解鎖才能買」的前提，不是買到的東西
    var reqs = assetRow(t('requires'), row.unlockRequires);
    if (reqs) card.appendChild(reqs);

    var timing = timingLine(row);
    if (timing) card.appendChild(timing);
    var reset = resetLine(row);
    if (reset) card.appendChild(reset);

    var meta = [row.shopName, row.eventName].filter(Boolean).join('  ·  ');
    if (meta) card.appendChild(el('div', 'meta', meta));
    return card;
  }

  /* ---------- 主流程 ---------- */

  function recomputeStatus() {
    state.data.items.forEach(function (row) {
      var st = resolveStatus(row.eventName);
      row._status = st.status;
      row._timing = st;
    });
  }

  function render() {
    recomputeStatus();
    var list = $('#list');
    var rows = sortRows(state.data.items.filter(matches));
    list.innerHTML = '';
    $('#count').textContent = t('count', { n: rows.length });
    $('#sort-note').textContent = priceSortAllowed() ? '' : t('sortNeedCurrency');

    if (!rows.length) {
      list.appendChild(el('p', 'empty', t('empty')));
      return;
    }
    var frag = document.createDocumentFragment();
    var make = state.config.kind === 'iap' ? cardIap : cardShop;
    rows.forEach(function (row) { frag.appendChild(make(row)); });
    list.appendChild(frag);
  }

  function renderBanner() {
    var s = staleness();
    var bar = $('#stale');
    if (s.level === 'ok') { bar.hidden = true; return; }
    bar.hidden = false;
    bar.className = 'stale stale-' + s.level;
    bar.textContent = s.level === 'severe'
      ? t('staleSevere', { h: s.hours })
      : t('staleWarn', { h: s.hours });
  }

  /* ---------- 篩選器 ---------- */

  function fillSelect(id, options, allLabel) {
    var sel = document.getElementById(id);
    if (!sel) return;
    var keep = sel.value;
    sel.innerHTML = '';
    var all = el('option', null, allLabel);
    all.value = '*';
    sel.appendChild(all);
    options.forEach(function (opt) {
      var o = el('option', null, opt.label);
      o.value = opt.value;
      sel.appendChild(o);
    });
    if (keep) sel.value = keep;   // 切換語言時保留當前選擇
  }

  function uniq(rows, key) {
    var seen = {};
    rows.forEach(function (r) { if (r[key]) seen[r[key]] = true; });
    return Object.keys(seen).sort().map(function (v) { return { value: v, label: v }; });
  }

  function buildFilters() {
    var items = state.data.items;

    var seen = {};
    items.forEach(function (r) { seen[r.eventName || '__permanent__'] = true; });
    fillSelect('f-event', Object.keys(seen).sort().map(function (k) {
      return { value: k, label: k === '__permanent__' ? t('eventPermanent') : k };
    }), t('allEvents'));

    fillSelect('f-status', [S_ACTIVE, S_UPCOMING, S_PERMANENT, S_UNSCHEDULED].map(function (s) {
      return { value: s, label: t('status' + cap(s)) };
    }), t('allStatus'));

    if (state.config.kind === 'shop') {
      fillSelect('f-currency', uniq(items, 'currency'), t('allCurrency'));
      fillSelect('f-shop', uniq(items, 'shopName'), t('allShops'));
      fillSelect('f-type', uniq(items, 'itemType'), t('allTypes'));
    }
  }

  function applyStaticText() {
    document.documentElement.lang =
      state.lang === 'en' ? 'en' : (state.lang === 'cn' ? 'zh-Hans' : 'zh-Hant');
    document.title = t(state.config.kind === 'iap' ? 'iapTitle' : 'shopTitle')
      + ' · ' + t('site');
    Array.prototype.forEach.call(document.querySelectorAll('[data-i18n]'), function (n) {
      n.textContent = t(n.getAttribute('data-i18n'));
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-i18n-ph]'), function (n) {
      n.placeholder = t(n.getAttribute('data-i18n-ph'));
    });
    var meta = state.data && state.data.meta;
    if (meta) $('#updated').textContent = t('updatedAt') + ': ' + formatAbsolute(meta.fetchedAt);
  }

  /* ---------- 啟動 ---------- */

  function bind() {
    ['f-event', 'f-status', 'f-currency', 'f-shop', 'f-type'].forEach(function (id) {
      var node = document.getElementById(id);
      if (!node) return;
      node.addEventListener('change', function () {
        state.filters[id.slice(2)] = node.value;
        render();
      });
    });
    var q = $('#f-q');
    if (q) q.addEventListener('input', function () { state.filters.q = q.value.trim(); render(); });
    var sort = $('#f-sort');
    if (sort) sort.addEventListener('change', function () { state.sort = sort.value; render(); });
    var free = $('#f-free');
    if (free) free.addEventListener('change', function () { state.hideFree = free.checked; render(); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-lang]'), function (btn) {
      btn.addEventListener('click', function () { setLang(btn.getAttribute('data-lang')); });
    });
  }

  function markLangButtons() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-lang]'), function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-lang') === state.lang);
    });
  }

  function loadLang(lang) {
    return Promise.all([
      fetch('i18n/ui.' + lang + '.json').then(function (r) { return r.json(); }),
      fetch('i18n/items.' + lang + '.json').then(function (r) { return r.json(); })
        .catch(function () { return {}; })
    ]).then(function (res) {
      state.ui = res[0];
      state.names = { commerce: res[1].commerce || {}, items: res[1].items || {} };
    });
  }

  function setLang(lang) {
    state.lang = lang;
    try { localStorage.setItem('sky_lang', lang); } catch (e) { /* 無痕模式 */ }
    loadLang(lang).then(function () {
      markLangButtons();
      applyStaticText();
      buildFilters();
      renderBanner();
      render();
    });
  }

  function init(config) {
    state.config = config;
    try { state.lang = localStorage.getItem('sky_lang') || 'tw'; } catch (e) { state.lang = 'tw'; }

    Promise.all([
      fetch(config.dataUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }),
      fetch('data/events.json?t=' + Date.now()).then(function (r) { return r.json(); }),
      loadLang(state.lang)
    ]).then(function (res) {
      state.data = res[0];
      state.events = res[1];
      $('#loading').hidden = true;
      $('#content').hidden = false;
      markLangButtons();
      applyStaticText();
      buildFilters();
      bind();
      renderBanner();
      render();
      // 每分鐘重畫一次：倒數走動、狀態也可能翻轉（活動剛好在使用者停留時開賣）。
      // 秒級更新只是視覺噪音。
      setInterval(function () { renderBanner(); render(); }, 60000);
    }).catch(function (err) {
      console.error(err);
      $('#loading').textContent = state.ui.loadError || 'Failed to load data.';
    });
  }

  global.SkyShop = { init: init, setLang: setLang };
})(window);
