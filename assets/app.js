/* Vintage Game Sales — site logic.
 * Data:   data/sales.json      (written every 6 h by the scraper on GitHub Actions)
 *         data/overrides.json  (written by this page in owner mode: hidden ✕ / confirmed ✓)
 * On GitHub Pages both files are read from raw.githubusercontent.com so a fresh scrape shows up
 * within minutes, without waiting for Pages to rebuild. Locally they're read from ./data/.
 */
(() => {
'use strict';

// ---------- small helpers ----------
const LS = {
  get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} },
};
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DAY = 864e5;
const toMs = s => Date.parse(s.length === 16 ? s + ':00+09:00' : s);          // stored times are Japan time
const median = a => { if (!a.length) return 0; const b = [...a].sort((x, y) => x - y), n = b.length; return n % 2 ? b[(n - 1) / 2] : Math.round((b[n / 2 - 1] + b[n / 2]) / 2); };
const fmtDate = (ms, year) => new Date(ms).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(year ? { year: 'numeric' } : {}), timeZone: 'Asia/Tokyo' });
const thisYear = new Date().getFullYear();
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const ICON = {
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
};
const SRC_CLASS = { 'Yahoo Auction': 'ya', 'Yahoo Flea': 'yf', 'Mercari': 'me' };
const SRC_SHORT = { all: 'All', 'Yahoo Auction': 'Auction', 'Yahoo Flea': 'Flea', 'Mercari': 'Mercari' };

// ---------- repo / owner config ----------
const IS_PAGES = /\.github\.io$/.test(location.hostname);
function detectRepo() {
  const saved = LS.get('vgt.repo');
  if (saved) return saved;
  if (!IS_PAGES) return '';
  const owner = location.hostname.split('.')[0];
  const seg = location.pathname.split('/').filter(Boolean)[0];
  return seg ? `${owner}/${seg}` : `${owner}/${location.hostname}`;
}
let REPO = detectRepo();
let TOKEN = LS.get('vgt.token') || '';
const BRANCH = LS.get('vgt.branch') || 'main';
const isOwner = () => !!(TOKEN && REPO);

// ---------- state ----------
const state = { data: null, titles: [], sales: [], byId: {}, ov: { hidden: {}, confirmed: {} },
  cur: LS.get('vgt.cur') || 'JPY', cards: {}, pending: new Set() };
const charts = {};
const normOv = d => ({ hidden: (d && d.hidden) || {}, confirmed: (d && d.confirmed) || {} });

// ---------- money ----------
const SYM = { JPY: '¥', USD: 'US$', CAD: 'C$' };
const rate = c => c === 'JPY' ? 1 : (state.data?.fx?.[c] || 1);
const money = (yen, c = state.cur) => SYM[c] + Math.round(yen / rate(c)).toLocaleString('en-US');
const moneyCompact = (yen, c = state.cur) => {
  const v = yen / rate(c);
  return SYM[c] + new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: v >= 1e6 ? 2 : 0 }).format(v);
};

// ---------- data loading ----------
async function fetchJSON(path) {
  const bust = '?t=' + Date.now();
  const bases = [];
  if (REPO && IS_PAGES) bases.push(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/`);
  bases.push('');
  let err;
  for (const b of bases) {
    try {
      const r = await fetch(b + path + bust, { cache: 'no-store' });
      if (r.ok) return await r.json();
      err = new Error(`${path}: HTTP ${r.status}`);
    } catch (e) { err = e; }
  }
  throw err;
}

// ---------- GitHub API (owner mode) ----------
const b64e = s => { const u = new TextEncoder().encode(s); let bin = ''; for (let i = 0; i < u.length; i += 0x8000) bin += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000)); return btoa(bin); };
const b64d = b => new TextDecoder().decode(Uint8Array.from(atob(b.replace(/\s/g, '')), c => c.charCodeAt(0)));

async function gh(method, path, body) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    method, cache: 'no-store',
    headers: { Authorization: `Bearer ${TOKEN}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
      ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
}
async function ghError(r) {
  let detail = '';
  try { detail = (await r.json()).message || ''; } catch (e) {}
  if (r.status === 401) return 'GitHub rejected the token (expired or mistyped). Sign in again from the lock button.';
  if (r.status === 403) return 'The token can read but not write. Give it Contents: Read and write on this repo.';
  if (r.status === 404) return `Repo "${REPO}" not found, or the token can't see it. Check the name and the token's repository access.`;
  return `GitHub error ${r.status}${detail ? ': ' + detail : ''}`;
}
async function ghReadOverrides() {
  const r = await gh('GET', `/contents/data/overrides.json?ref=${encodeURIComponent(BRANCH)}`);
  if (r.status === 404) {
    const repo = await gh('GET', '');
    if (!repo.ok) throw new Error(await ghError(repo));
    return { sha: null, data: normOv({}) };
  }
  if (!r.ok) throw new Error(await ghError(r));
  const j = await r.json();
  return { sha: j.sha, data: normOv(JSON.parse(b64d(j.content))) };
}
async function ghMutate(mutator, message) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const { sha, data } = await ghReadOverrides();
    mutator(data);
    const r = await gh('PUT', '/contents/data/overrides.json', {
      message, branch: BRANCH, content: b64e(JSON.stringify(data, null, 1) + '\n'), ...(sha ? { sha } : {}),
    });
    if (r.ok) return;
    if (r.status === 409 || r.status === 422) continue;          // file changed underneath; re-read and retry
    throw new Error(await ghError(r));
  }
  throw new Error('GitHub kept rejecting the save because the file was changing. Try again in a moment.');
}
let chain = Promise.resolve();
const enqueue = fn => { const p = chain.then(fn); chain = p.catch(() => {}); return p; };

// ---------- derived data ----------
function prep() {
  state.titles = state.data.titles;
  state.sales = state.data.sales.map(s => ({ ...s, ms: toMs(s.closed_at) }));
  state.byId = Object.fromEntries(state.sales.map(s => [s.id, s]));
}
const eff = s => state.ov.hidden[s.id] ? 'hidden' : (s.status === 'charted' || state.ov.confirmed[s.id]) ? 'charted' : 'review';
const salesOf = (tk, vk) => state.sales.filter(s => s.title_key === tk && (!vk || s.variant_key === vk));

function cardState(t) {
  return state.cards[t.key] ||= {
    variant: LS.get('vgt.v.' + t.key) || t.variants[t.variants.length - 1].key,
    sort: 'date', src: 'all', review: false, limit: 30,
  };
}
// Trend line: every 7 days, the median of sales within ±4 weeks (needs at least 3 of them).
function trendLine(pts, half = 28 * DAY, step = 7 * DAY) {
  if (pts.length < 3) return [];
  const xs = pts.map(p => p.x), lo = Math.min(...xs), hi = Math.max(...xs), out = [];
  for (let x = lo; x <= hi + 1; x += step) {
    const near = pts.filter(p => Math.abs(p.x - x) <= half).map(p => p.y);
    if (near.length >= 3) out.push({ x, y: median(near) });
  }
  return out;
}
const dated = s => s.date_basis !== 'first_seen';
function trendPct(rows) {
  const now = Date.now();
  const cur = median(rows.filter(r => now - r.ms <= 90 * DAY).map(r => r.price));
  const prev = median(rows.filter(r => now - r.ms > 90 * DAY && now - r.ms <= 180 * DAY).map(r => r.price));
  return cur && prev ? { pct: (cur - prev) / prev * 100, prev } : null;
}

// ---------- rendering ----------
function renderAll() {
  const cols = $('#cols');
  cols.innerHTML = state.titles.map(t => `
    <section class="card" data-key="${esc(t.key)}" aria-labelledby="h-${esc(t.key)}">
      <div class="card-head"><div class="eyebrow">${esc(t.platform)}</div><h2 id="h-${esc(t.key)}">${esc(t.name)}</h2></div>
      <div class="vt"></div><div class="stats"></div>
      <div class="chart"><canvas role="img" aria-label="Price history for ${esc(t.name)}"></canvas></div>
      <div class="legend"></div><div class="tools"></div><ol class="list"></ol><div class="morebox"></div>
    </section>`).join('');
  for (const k in charts) { charts[k].destroy(); delete charts[k]; }
  state.titles.forEach(renderCard);
  renderFooter();
  renderLede();
}

function renderLede() {
  const g = state.data.generated_at;
  const hrs = Math.max(0, Math.round((Date.now() - toMs(g)) / 36e5));
  const ago = hrs < 1 ? 'less than an hour ago' : hrs < 48 ? `${hrs} h ago` : `${Math.round(hrs / 24)} days ago`;
  $('#lede').innerHTML = `Sold prices for complete-in-box early prints, from <b>Yahoo! Auctions</b>, <b>Yahoo!フリマ</b> and <b>Mercari</b>. Updated ${ago}.`;
}

function renderCard(t) {
  if (typeof t === 'string') t = state.titles.find(x => x.key === t);
  const card = $(`.card[data-key="${CSS.escape(t.key)}"]`);
  if (!card) return;
  const cs = cardState(t);
  if (!t.variants.some(v => v.key === cs.variant)) cs.variant = t.variants[t.variants.length - 1].key;
  const v = t.variants.find(x => x.key === cs.variant);
  const all = salesOf(t.key).filter(s => eff(s) !== 'hidden');
  const charted = all.filter(s => s.variant_key === v.key && eff(s) === 'charted').sort((a, b) => a.ms - b.ms);
  const review = all.filter(s => s.variant_key === v.key && eff(s) === 'review');

  // variant toggle
  card.querySelector('.vt').innerHTML = `<div class="seg" role="group" aria-label="Variant">${[...t.variants].reverse().map(x => {
    const n = all.filter(s => s.variant_key === x.key && eff(s) === 'charted').length;
    return `<button data-variant="${esc(x.key)}" aria-pressed="${x.key === v.key}">${esc(x.short)}<span class="n">${n}</span></button>`;
  }).join('')}</div>`;

  // stats
  const now = Date.now();
  const r90 = charted.filter(s => dated(s) && now - s.ms <= 90 * DAY);
  const med90 = median(r90.map(s => s.price));
  const tr = trendPct(charted.filter(dated));
  const top = charted.reduce((m, s) => (!m || s.price > m.price ? s : m), null);
  const vw = t.variants[0], vo = t.variants[t.variants.length - 1];
  const mw = median(all.filter(s => s.variant_key === vw.key && eff(s) === 'charted').map(s => s.price));
  const mo = median(all.filter(s => s.variant_key === vo.key && eff(s) === 'charted').map(s => s.price));
  const extra = vw.label.replace(/^with (survey )?/i, '');
  const trendHTML = tr
    ? `<span class="${tr.pct > 2 ? 'up' : tr.pct < -2 ? 'dn' : 'flat'}">${tr.pct > 2 ? '▲' : tr.pct < -2 ? '▼' : '▬'} ${Math.abs(tr.pct).toFixed(0)}%</span>`
    : '<span class="flat">—</span>';
  card.querySelector('.stats').innerHTML = `
    <div class="stat"><div class="k">90-day median</div><div class="v num">${med90 ? money(med90) : '—'}</div><div class="s">${r90.length} sale${r90.length === 1 ? '' : 's'} in 90 days</div></div>
    <div class="stat"><div class="k">Trend</div><div class="v num">${trendHTML}</div><div class="s">${tr ? `vs prior 90 days (${money(tr.prev)})` : 'needs 2 windows of data'}</div></div>
    <div class="stat"><div class="k">Highest</div><div class="v num">${top ? money(top.price) : '—'}</div><div class="s">${top ? fmtDate(top.ms, true) + ' · ' + esc(top.source) : 'no sales yet'}</div></div>
    <div class="stat"><div class="k">${esc(extra[0].toUpperCase() + extra.slice(1))} premium</div><div class="v num">${mw && mo ? `${mw >= mo ? '+' : ''}${((mw - mo) / mo * 100).toFixed(0)}%` : '—'}</div><div class="s">${mw && mo ? `median ${moneyCompact(mw)} vs ${moneyCompact(mo)}` : 'not enough data'}</div></div>`;

  // chart + legend
  drawChart(t, v, charted, all);
  card.querySelector('.legend').innerHTML =
    `<span><i style="background:${v.color}"></i>Yahoo sale</span><span><i class="ring" style="border-color:${v.color}"></i>Mercari sale (≈ date)</span><span><i class="bar" style="background:${v.color}"></i>Median of sales within ±4 weeks</span>` +
    (charted.some(s => !dated(s)) ? `<span>${charted.filter(s => !dated(s)).length} listed below aren't plotted: sold date not known yet</span>` : '');

  // tools
  const srcs = ['all', 'Yahoo Auction', 'Yahoo Flea', 'Mercari'];
  card.querySelector('.tools').innerHTML = `
    <div class="seg" role="group" aria-label="Sort"><button data-sort="date" aria-pressed="${cs.sort === 'date'}">Newest</button><button data-sort="price" aria-pressed="${cs.sort === 'price'}">Price</button></div>
    <div class="seg" role="group" aria-label="Marketplace">${srcs.map(s => `<button data-src="${esc(s)}" aria-pressed="${cs.src === s}">${SRC_SHORT[s]}</button>`).join('')}</div>
    <button class="chip" data-review aria-pressed="${cs.review}" title="Probably the right item, but the title doesn't prove it. Not charted.">Needs review<span class="n">${review.length}</span></button>`;

  // list
  let rows = cs.review ? review : charted.slice();
  if (cs.src !== 'all') rows = rows.filter(s => s.source === cs.src);
  rows.sort(cs.sort === 'price' ? (a, b) => b.price - a.price : (a, b) => b.ms - a.ms);
  const list = card.querySelector('.list');
  list.innerHTML = rows.slice(0, cs.limit).map(rowHTML).join('') ||
    `<li class="empty">${cs.review ? 'Nothing waiting for review here.' : 'No sales for this selection yet.'}</li>`;
  const left = rows.length - cs.limit;
  card.querySelector('.morebox').innerHTML = left > 0 ? `<button class="more" data-more>Show ${Math.min(30, left)} more of ${left}</button>` : '';
}

function rowHTML(s) {
  const e = eff(s);
  const approx = s.date_basis !== 'auction_end';
  const unknown = s.date_basis === 'first_seen';
  const tags = [`<span class="tag ${SRC_CLASS[s.source] || ''}">${esc(s.source)}</span>`];
  if (s.stamp) tags.push(`<span class="tag">stamp ${esc(s.stamp)}</span>`);
  if (e === 'review') tags.push(`<span class="tag warn">${esc(s.review_reason || 'needs review')}</span>`);
  if (state.ov.confirmed[s.id] && s.status !== 'charted') tags.push('<span class="tag ok">confirmed by you</span>');
  if (s.bids > 1) tags.push(`<span class="tag">${s.bids} bids</span>`);
  const dateTitle = unknown ? `Sold on or before ${fmtDate(s.ms, true)}. The exact date fills in on the next Mercari scrape.`
    : approx ? 'Approximate. Mercari shows when the listing last changed, which is the sale or shortly after.' : 'Auction end time';
  return `<li class="row${state.pending.has(s.id) ? ' pending' : ''}" data-id="${esc(s.id)}">
    <a href="${esc(s.url)}" target="_blank" rel="noopener">
      <div class="r1"><span class="price num">${money(s.price)}</span><span class="alt num">${state.cur === 'JPY' ? '' : '¥' + s.price.toLocaleString('en-US')}</span>
        <span class="date num" title="${dateTitle}">${unknown ? 'by ' : approx ? '≈ ' : ''}${fmtDate(s.ms, new Date(s.ms).getFullYear() !== thisYear)}</span></div>
      <div class="ttl" lang="ja">${esc(s.title)}</div>
      <div class="meta">${tags.join('')}</div>
    </a>
    <div class="acts">${e === 'review' ? `<button class="act ok" data-act="confirm" data-id="${esc(s.id)}" title="Confirm: it's the right item, chart it" aria-label="Confirm and chart this sale">${ICON.check}</button>` : ''}<button class="act hide" data-act="hide" data-id="${esc(s.id)}" title="Hide: wrong item, never show it" aria-label="Hide this listing">${ICON.x}</button></div>
  </li>`;
}

function drawChart(t, v, charted, all) {
  const canvas = $(`.card[data-key="${CSS.escape(t.key)}"] canvas`);
  const pts = charted.filter(dated).map(s => ({ x: s.ms, y: s.price, s }));
  const exact = pts.filter(p => p.s.date_basis === 'auction_end');
  const approx = pts.filter(p => p.s.date_basis !== 'auction_end');
  const med = trendLine(pts);
  const xs = all.filter(dated).map(s => s.ms);
  const xmin = Math.min(...xs, Date.now() - 30 * DAY), xmax = Date.now();
  const datasets = [
    { label: 'Yahoo', data: exact, backgroundColor: v.color + 'CC', borderColor: v.color, pointRadius: 4.5, pointHoverRadius: 7, order: 2 },
    { label: 'Mercari', data: approx, backgroundColor: 'transparent', borderColor: v.color, borderWidth: 1.8, pointRadius: 4.5, pointHoverRadius: 7, order: 2 },
    { type: 'line', label: 'median', data: med, borderColor: v.color, borderWidth: 2.5, pointRadius: 0, pointHitRadius: 0, tension: 0.35, fill: false, order: 1 },
  ];
  if (charts[t.key]) {
    const c = charts[t.key];
    c.data.datasets = datasets;
    c.options.scales.x.min = xmin; c.options.scales.x.max = xmax;
    c.update();
    return;
  }
  if (typeof Chart === 'undefined') return;
  const tick = css('--ink3'), grid = css('--line');
  charts[t.key] = new Chart(canvas, {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 }, parsing: false,
      interaction: { mode: 'nearest', intersect: true },
      onClick: (e, els, chart) => {
        const el = els[0]; if (!el) return;
        const p = chart.data.datasets[el.datasetIndex].data[el.index];
        if (p && p.s) window.open(p.s.url, '_blank', 'noopener');
      },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
      scales: {
        x: { type: 'linear', min: xmin, max: xmax, ticks: { maxTicksLimit: 6, callback: x => fmtDate(x), color: tick, font: { size: 10.5 } }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, ticks: { maxTicksLimit: 5, callback: y => moneyCompact(y), color: tick, font: { size: 10.5 } }, grid: { color: grid }, border: { display: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { displayColors: false, callbacks: {
          title: items => { const r = items[0].raw; return fmtDate(r.x, true) + (r.s && r.s.date_basis !== 'auction_end' ? ' (approx.)' : ''); },
          label: i => i.raw.s ? [`${money(i.raw.y)} · ${i.raw.s.source}`, i.raw.s.title.length > 34 ? i.raw.s.title.slice(0, 34) + '…' : i.raw.s.title] : `Median of nearby sales ${money(i.raw.y)}`,
        } },
      },
    },
  });
}

function renderFooter() {
  const d = state.data, lr = d.last_run || {}, src = lr.sources || {};
  const parts = [];
  for (const [name, label] of [['Yahoo Auction', 'Yahoo! Auctions'], ['Yahoo Flea', 'Yahoo!フリマ'], ['Mercari', 'Mercari']]) {
    if (src[name]) parts.push(`${label} ${src[name].fetched ?? 0}`);
  }
  let warn = '';
  const ms = src.Mercari?.status;
  if (ms && ms !== 'ok') warn = `<br><span class="warn">Mercari last run: ${esc(ms === 'dom-fallback' ? 'read from the page (dates approximate)' : ms)}</span>`;
  if (src.Mercari && !src.Mercari.fetched) warn = '<br><span class="warn">Mercari returned nothing on the last run. It may be blocking GitHub\'s servers.</span>';
  const nHidden = Object.keys(state.ov.hidden).length;
  $('#foot').innerHTML = `
    Last update ${esc(fmtDate(toMs(d.generated_at), true))}, ${esc(d.generated_at.slice(11, 16))} Japan time · checks every 6 hours
    ${parts.length ? `<br>Last scrape fetched: ${parts.join(' · ')}` : ''}${warn}
    <br>Converted at ${esc(d.fx?.note || 'a fixed rate')}: ¥${d.fx?.USD}/US$, ¥${d.fx?.CAD}/C$ · Mercari dates (≈) are when the listing last changed, which is the sale or shortly after.
    <br>Only complete-in-box copies of the exact print are charted. Loose carts, manual-only, sealed and graded copies are left out.
    ${isOwner() ? `<br>Owner mode · ${nHidden} hidden · <button class="linkish" id="manageHidden">Manage hidden</button>` : ''}`;
  const mh = $('#manageHidden');
  if (mh) mh.onclick = openHidden;
}

// ---------- toast ----------
let toastTimer;
function toast(msg, btn, onBtn, err) {
  const t = $('#toast'), b = $('#toastBtn');
  $('#toastMsg').textContent = msg;
  t.classList.toggle('err', !!err);
  b.hidden = !btn;
  b.textContent = btn || '';
  b.onclick = () => { t.classList.remove('on'); onBtn && onBtn(); };
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), err ? 9000 : 6000);
}

// ---------- owner actions ----------
function rerender(id) {
  const s = state.byId[id];
  if (s) renderCard(s.title_key);
  renderFooter();
}
async function act(kind, id, undo) {
  if (!isOwner()) return;
  const s = state.byId[id];
  const entry = s ? { url: s.url, title: s.title, price: s.price, at: new Date().toISOString().slice(0, 10) } : { at: new Date().toISOString().slice(0, 10) };
  const before = JSON.parse(JSON.stringify(state.ov));
  const apply = d => {
    if (kind === 'hide') { d.hidden[id] = d.hidden[id] || entry; delete d.confirmed[id]; }
    if (kind === 'confirm') { d.confirmed[id] = d.confirmed[id] || entry; delete d.hidden[id]; }
    if (kind === 'unhide') delete d.hidden[id];
    if (kind === 'unconfirm') delete d.confirmed[id];
  };
  apply(state.ov);
  state.pending.add(id);
  rerender(id);
  if (!undo) toast(kind === 'hide' ? 'Hiding…' : kind === 'confirm' ? 'Confirming…' : 'Restoring…');
  const verb = { hide: 'Hide', confirm: 'Confirm', unhide: 'Restore', unconfirm: 'Un-confirm' }[kind];
  try {
    await enqueue(() => ghMutate(apply, `${verb} listing ${id}${s ? ': ' + s.title.slice(0, 48) : ''}`));
    state.pending.delete(id);
    rerender(id);
    const back = { hide: 'unhide', confirm: 'unconfirm', unhide: 'hide', unconfirm: 'confirm' }[kind];
    toast({ hide: 'Hidden', confirm: 'Confirmed and charted', unhide: 'Restored', unconfirm: 'Moved back to review' }[kind] + ' · saved to GitHub', 'Undo', () => act(back, id, true));
    if ($('#hiddenDlg').open) openHidden();
  } catch (e) {
    state.ov = before;
    state.pending.delete(id);
    rerender(id);
    toast(e.message, null, null, true);
  }
}

// ---------- dialogs ----------
function openOwner() {
  $('#repoIn').value = REPO;
  $('#tokIn').value = '';
  $('#tokIn').placeholder = TOKEN ? '•••••••• (saved, leave blank to keep)' : 'github_pat_…';
  $('#signOut').hidden = !TOKEN;
  $('#ownerMsg').textContent = ''; $('#ownerMsg').className = 'msg';
  $('#ownerDlg').showModal();
}
async function saveOwner(e) {
  e.preventDefault();
  const msg = $('#ownerMsg');
  const repo = $('#repoIn').value.trim().replace(/^https?:\/\/github\.com\//, '').replace(/\/+$/, '');
  const tok = $('#tokIn').value.trim() || TOKEN;
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) { msg.textContent = 'Enter the repo as owner/name, e.g. branavan/vintage-game-tracker.'; msg.className = 'msg err'; return; }
  if (!tok) { msg.textContent = 'Paste a token.'; msg.className = 'msg err'; return; }
  const prev = [REPO, TOKEN];
  REPO = repo; TOKEN = tok;
  msg.textContent = 'Checking with GitHub…'; msg.className = 'msg';
  try {
    const { data } = await ghReadOverrides();
    LS.set('vgt.repo', repo); LS.set('vgt.token', tok);
    state.ov = data;
    document.body.classList.add('owner-on');
    msg.textContent = 'Connected. ✕ and ✓ now save straight to GitHub.'; msg.className = 'msg ok';
    if (state.data) { state.titles.forEach(renderCard); renderFooter(); }
    setTimeout(() => $('#ownerDlg').close(), 900);
  } catch (err) {
    [REPO, TOKEN] = prev;
    msg.textContent = err.message; msg.className = 'msg err';
  }
}
function signOut() {
  TOKEN = ''; LS.set('vgt.token', null);
  document.body.classList.remove('owner-on');
  $('#ownerDlg').close();
  if (state.data) { state.titles.forEach(renderCard); renderFooter(); }
  toast('Signed out on this device.');
}
function openHidden() {
  const items = Object.entries(state.ov.hidden).sort((a, b) => String(b[1].at || '').localeCompare(String(a[1].at || '')));
  $('#hlist').innerHTML = items.length ? items.map(([id, h]) => `
    <div class="hrow"><span class="t" lang="ja" title="${esc(h.title || id)}">${h.price ? '¥' + Number(h.price).toLocaleString('en-US') + ' · ' : ''}${esc(h.title || id)}</span>
      <a class="note" href="${esc(h.url || '#')}" target="_blank" rel="noopener">open</a>
      <button class="btn" data-restore="${esc(id)}">Restore</button></div>`).join('')
    : '<div class="hrow"><span class="t">Nothing hidden.</span></div>';
  if (!$('#hiddenDlg').open) $('#hiddenDlg').showModal();
}

// ---------- events ----------
function wire() {
  $('#cols').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    const card = b.closest('.card');
    const t = card && state.titles.find(x => x.key === card.dataset.key);
    if (b.dataset.act) { e.preventDefault(); act(b.dataset.act, b.dataset.id); return; }
    if (!t) return;
    const cs = cardState(t);
    if (b.dataset.variant) { cs.variant = b.dataset.variant; cs.limit = 30; LS.set('vgt.v.' + t.key, cs.variant); }
    else if (b.dataset.sort) cs.sort = b.dataset.sort;
    else if (b.dataset.src) { cs.src = b.dataset.src; cs.limit = 30; }
    else if ('review' in b.dataset) { cs.review = !cs.review; cs.limit = 30; }
    else if ('more' in b.dataset) cs.limit += 30;
    else return;
    renderCard(t);
  });
  document.querySelectorAll('#cur button').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.c === state.cur));
    b.onclick = () => {
      state.cur = b.dataset.c; LS.set('vgt.cur', state.cur);
      document.querySelectorAll('#cur button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
      if (state.data) { state.titles.forEach(renderCard); }
    };
  });
  $('#ownerBtn').onclick = openOwner;
  $('#ownerForm').addEventListener('submit', saveOwner);
  $('#ownerCancel').onclick = () => $('#ownerDlg').close();
  $('#signOut').onclick = signOut;
  $('#hiddenClose').onclick = () => $('#hiddenDlg').close();
  $('#hlist').addEventListener('click', e => { const b = e.target.closest('[data-restore]'); if (b) act('unhide', b.dataset.restore); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => state.data && renderAll());
}

// ---------- boot ----------
async function boot() {
  wire();
  document.body.classList.toggle('owner-on', isOwner());
  try {
    state.data = await fetchJSON('data/sales.json');
  } catch (e) {
    $('#cols').innerHTML = `<div id="boot">Couldn't load the sales data.${location.protocol === 'file:'
      ? '<br><br>Browsers block this when the page is opened as a file. In this folder run <code>python3 -m http.server</code> and open <code>http://localhost:8000</code>.'
      : ' Try refreshing.'}</div>`;
    return;
  }
  if (isOwner()) {
    try { state.ov = (await ghReadOverrides()).data; }
    catch (e) {
      toast(e.message, 'Sign in', openOwner, true);
      try { state.ov = normOv(await fetchJSON('data/overrides.json')); } catch (_) {}
    }
  } else {
    try { state.ov = normOv(await fetchJSON('data/overrides.json')); } catch (_) {}
  }
  prep();
  renderAll();
}
boot();
})();
