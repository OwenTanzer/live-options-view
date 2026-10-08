// QQQ/SPY chain selection (#121): the pure helpers in docs/shared.js, plus
// docs/index.html's own fetch/render cycle run against a minimal DOM shim
// (same section-extraction approach as bots_panel.test.js, so the shipped
// source is what's tested). If this fails with "could not locate", the
// '// ── data fetch cycle' or '// ── countdown timer' banner moved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const shared = require('../docs/shared.js');
const { chainLatestPath, occRoot, validateChainPayload, ChainSelection, relativeOiRanges, summarizeSpyHealth } = shared;

const NOW = Date.now();
function snapshot(symbol, { declare = true, price = symbol === 'SPY' ? 670.2 : 600.1, rows, ts = new Date(NOW).toISOString() } = {}) {
  const strike = Math.round(price);
  return {
    ...(declare ? { symbol } : {}),
    timestamp: ts, snapshot_time: '10:30 ET', date: '2026-10-06', tier: '0DTE_Regular',
    underlying_price: price,
    underlying_market: { symbol, spot: price },
    rows: rows || [
      { OptionSymbol: `${symbol.padEnd(6)}261006C00${strike}000`.replace(/ /g, ' '), Strike: strike, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2 },
      { OptionSymbol: `${symbol}261006P00${strike}000`, Strike: strike, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2 },
    ],
  };
}

// ── pure helpers ───────────────────────────────────────────────────────────
{
  assert.equal(chainLatestPath('QQQ'), 'intraday/latest.json', 'QQQ keeps its original artifact');
  assert.equal(chainLatestPath('SPY'), 'intraday/spy/latest.json');
  assert.equal(chainLatestPath('IWM'), null, 'unsupported symbols never map onto QQQ');
  assert.equal(chainLatestPath('__proto__'), null);
  assert.equal(occRoot('SPY   261006C00670000'), 'SPY');
  assert.equal(occRoot('QQQ261006P00600000'), 'QQQ');
}
{
  assert.equal(validateChainPayload(snapshot('SPY'), 'SPY').ok, true);
  assert.equal(validateChainPayload(snapshot('QQQ', { declare: false }), 'QQQ').ok, true, 'pre-#121 QQQ payloads are accepted for QQQ');
  assert.equal(validateChainPayload(snapshot('QQQ', { declare: false }), 'SPY').ok, false, 'an unlabeled payload is never SPY');
  assert.match(validateChainPayload(snapshot('QQQ'), 'SPY').reason, /labeled QQQ/);
  const mixed = snapshot('SPY');
  mixed.rows.push({ OptionSymbol: 'QQQ261006C00600000', Strike: 600, Type: 'call' });
  assert.match(validateChainPayload(mixed, 'SPY').reason, /non-SPY contracts/, 'one foreign contract rejects the snapshot');
  const wrongMarket = snapshot('SPY');
  wrongMarket.underlying_market.symbol = 'QQQ';
  assert.match(validateChainPayload(wrongMarket, 'SPY').reason, /readings belong to QQQ/);
  assert.equal(validateChainPayload({ symbol: 'SPY' }, 'SPY').ok, false, 'rows are required');
  assert.equal(validateChainPayload(snapshot('SPY'), 'IWM').ok, false, 'unsupported symbol');
}
{
  const sel = new ChainSelection('nonsense');
  assert.equal(sel.symbol, 'QQQ', 'unknown saved preference falls back to QQQ');
  const t0 = sel.token();
  assert.equal(sel.select('SPY'), true);
  assert.equal(sel.accepts(t0), false, 'a QQQ-era token is rejected after switching');
  const t1 = sel.token();
  sel.select('QQQ'); sel.select('SPY');
  assert.equal(sel.accepts(t1), false, 'an older SPY request is rejected after SPY→QQQ→SPY');
  assert.equal(sel.accepts(sel.token()), true);
  assert.equal(sel.select('IWM'), false);
  assert.equal(sel.symbol, 'SPY');
  const before = sel.token();
  sel.select('SPY');
  assert.equal(sel.accepts(before), true, 're-selecting the same symbol keeps in-flight requests valid');
}
{
  assert.equal(relativeOiRanges([{ OpenInterest: 0 }]), null);
  const r = relativeOiRanges([1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(OpenInterest => ({ OpenInterest })));
  assert.deepEqual(r['0DTE_Regular'][0].call, [3, 5, 7, 9]);
  assert.deepEqual(r['0DTE_Regular'][-20].put, [3, 5, 7, 9], 'every displayed offset gets the same snapshot-relative thresholds');
}

// ── index.html fetch/render cycle ────────────────────────────────────────────
const html = fs.readFileSync(path.join(__dirname, '..', 'docs', 'index.html'), 'utf8');
const START = '// ── data fetch cycle';
const END = '// ── countdown timer';
const from = html.indexOf(START);
const to = html.indexOf(END, from);
assert.ok(from !== -1 && to !== -1, 'could not locate the data fetch cycle in docs/index.html');
const source = html.slice(from, to);

// The price tile and SPY health badge use the shipped HTML functions, with
// offline artifact responses and no provider/browser network dependency.
assert.match(html, /const MAIN_TICKERS = \['QQQ', 'SPY'/);
assert.equal(shared.isTradeableShareSymbol('SPY'), false, 'SPY tile is display-only');
const testPriceTile = async () => {
  const published = [];
  const fallback = [];
  const rendered = [];
  let prices = { SPY: { price: 670.2, source: 'dxlink', quote_ts: new Date(NOW).toISOString() }, QQQ: { price: 600.1 } };
  const tickerState = {
    publishFallback: p => fallback.push(p), publish: q => published.push(...q),
    snapshot: () => ({ SPY: published.at(-1) }),
  };
  const priceSource = html.slice(html.indexOf('async function fetchPrices()'), html.indexOf('// EIA STEO crude calibration', html.indexOf('async function fetchPrices()')));
  const fetchPrices = new Function('fetch', 'R2', 'tickerState', 'updatePriceStrip', 'console', `${priceSource}\nreturn fetchPrices;`)(
    async () => ({ ok: true, json: async () => ({ prices }) }), 'https://r2.example', tickerState,
    state => rendered.push(state), { warn() {} },
  );
  await fetchPrices();
  assert.equal(published[0].price, 670.2, 'SPY tile uses the SPY provider price');
  assert.equal(published[0].quote_ts, prices.SPY.quote_ts, 'SPY tile keeps provider observation time');
  assert.equal(fallback[0].SPY, undefined, 'SPY does not use macro fallback');
  prices = { SPY: { price: null, source: null, quote_ts: null }, QQQ: { price: 600.1 } };
  await fetchPrices();
  assert.equal(published.length, 1, 'missing SPY price is not fabricated');
  assert.equal(rendered.length, 2, 'tile state is refreshed even when SPY is missing');
  for (const bad of [
    { price: 600, source: 'dxlink', quote_ts: new Date(NOW).toISOString(), symbol: 'QQQ' },
    { price: 670, source: 'yfinance', quote_ts: new Date(NOW).toISOString() },
    { price: 670, source: 'dxlink', quote_ts: null },
    { price: Infinity, source: 'dxlink', quote_ts: new Date(NOW).toISOString() },
  ]) {
    prices = { SPY: bad, QQQ: { price: 600.1 } };
    await fetchPrices();
    assert.equal(published.length, 1, 'foreign or unsupported SPY evidence is rejected');
    assert.equal(fallback.at(-1).SPY, undefined);
  }
  let now = Date.parse('2026-10-07T15:00:00Z');
  const store = new shared.TickerStateStore({ SPY: 'equity', QQQ: 'equity' }, { nowFn: () => now });
  assert.equal(store.get('SPY'), null, 'missing SPY stays missing');
  store.publish([{ symbol: 'QQQ', price: 600.1, source: 'dxlink', quote_ts: new Date(now).toISOString() }]);
  assert.equal(store.get('SPY'), null, 'QQQ never supplies a missing SPY quote');
  store.publish([{ symbol: 'SPY', price: 670.2, source: 'dxlink', quote_ts: new Date(now).toISOString() }]);
  assert.equal(store.get('SPY').state, 'live');
  now += 31_000;
  assert.equal(store.get('SPY').state, 'stale', 'SPY ages from its provider observation');
  assert.equal(store.get('SPY').price, 670.2);

};
const testSpyHealthBadge = async () => {
  const badge = { className: '', textContent: '', title: '' };
  let response = { updated_at: new Date().toISOString(), spy: {
    collection_status: 'collecting', spot: { status: 'live' },
    last_snapshot_upload_time: new Date().toISOString(),
  } };
  const start = html.indexOf('let latestSpyHealthRequest = 0;');
  const end = html.indexOf('liveQuotes.subscribe(', start);
  assert.ok(start > 0 && end > start, 'shipped SPY health badge code found');
  const fetchSpyHealth = new Function('fetch', 'R2', 'document', 'summarizeSpyHealth',
    `${html.slice(start, end)}\nreturn fetchSpyHealth;`)(
      async () => ({ ok: true, json: async () => response }), 'https://r2.example',
      { getElementById: () => badge }, summarizeSpyHealth,
    );
  await fetchSpyHealth();
  assert.equal(badge.className, 'transport-health live', 'fresh SPY health renders live');
  response = { ...response, updated_at: new Date(Date.now() - 60_000).toISOString() };
  await fetchSpyHealth();
  assert.equal(badge.className, 'transport-health stale', 'old SPY health renders stale');
  response = { updated_at: new Date().toISOString(), spy: { collection_status: 'missing_today_expiration', reason: 'no SPY 0DTE' } };
  await fetchSpyHealth();
  assert.match(badge.textContent, /missing today expiration/, 'missing SPY 0DTE is visible');
  const pending = [];
  const ordered = new Function('fetch', 'R2', 'document', 'summarizeSpyHealth',
    html.slice(start, end) + '\nreturn fetchSpyHealth;')(
      () => new Promise((resolve, reject) => pending.push({ resolve, reject })), 'https://r2.example',
      { getElementById: () => badge }, summarizeSpyHealth);
  const oldRequest = ordered();
  const newRequest = ordered();
  pending[1].resolve({ ok: true, json: async () => ({
    updated_at: new Date().toISOString(), spy: { collection_status: 'disabled' },
  }) });
  await newRequest;
  pending[0].reject(new Error('old request failed'));
  await oldRequest;
  assert.equal(badge.textContent, 'SPY: disabled', 'old failure cannot replace newer health');
  const failed = ordered();
  pending[2].reject(new Error('network'));
  await failed;
  assert.equal(badge.className, 'transport-health unavailable');

};
{
  const now = Date.now();
  const health = { updated_at: new Date(now).toISOString(), spy: {
    collection_status: 'collecting', spot: { status: 'live' },
    last_snapshot_upload_time: new Date(now - 10_000).toISOString(),
  } };
  assert.equal(summarizeSpyHealth(health, now).state, 'live');
  assert.equal(summarizeSpyHealth(health, now + 60_000).state, 'stale', 'old health artifact cannot stay live');
  health.spy.spot.status = 'unavailable';
  assert.equal(summarizeSpyHealth(health, now).state, 'unavailable');
  health.spy.collection_status = 'missing_today_expiration';
  assert.match(summarizeSpyHealth(health, now).text, /missing today expiration/);
  health.spy.collection_status = 'collecting';
  health.spy.last_snapshot_upload_time = null;
  health.process_start_time = new Date(now - 5 * 60_000).toISOString();
  assert.equal(summarizeSpyHealth(health, now).state, 'unavailable', 'long missing SPY snapshot is unavailable');
  assert.equal(summarizeSpyHealth(null, now).state, 'unavailable');
  health.spy.collection_status = 'disabled';
  assert.equal(summarizeSpyHealth(health, now).text, 'SPY: disabled');
  assert.equal(summarizeSpyHealth(health, now).state, 'unavailable');
  assert.equal(summarizeSpyHealth({ updated_at: health.updated_at, last_snapshot_upload_time: health.updated_at }, now).state,
    'unavailable', 'QQQ health cannot stand in for SPY');
  health.spy.collection_status = 'collecting';
  health.spy.spot.status = 'live';
  health.spy.last_snapshot_upload_time = new Date(now - 181_000).toISOString();
  assert.equal(summarizeSpyHealth(health, now).state, 'stale');
  health.updated_at = new Date(now + 31_000).toISOString();
  assert.equal(summarizeSpyHealth(health, now).state, 'stale', 'future health timestamp cannot claim live');

}

function harness(saved = 'QQQ') {
  const els = {};
  const el = (id) => els[id] || (els[id] = {
    id, textContent: id === 'no-data' ? 'No live data yet.' : '', innerHTML: '', value: '', className: '',
    style: {}, dataset: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
  });
  const calls = { heatmap: [], ingested: [], published: [], qqqLine: [], momentum: [], paper: 0 };
  const routes = {};          // path -> () => Promise<Response>
  const fetchFn = (url) => {
    const p = url.slice(url.indexOf('/', 8) + 1).split('?')[0];
    const route = routes[p];
    if (!route) return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
    return route();
  };
  const reply = (body) => () => Promise.resolve({ ok: true, status: 200, json: async () => body });
  const deferred = () => {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return { route: () => promise, resolve: (body) => resolve({ ok: true, status: 200, json: async () => body }) };
  };
  const doc = { getElementById: el, title: '' };
  const scope = {
    document: doc,
    fetch: fetchFn,
    console: { error() {}, warn() {} },
    localStorage: { setItem() {}, getItem() { return saved; } },
    R2: 'https://r2.example',
    NO_DATA_TEXT: 'No live data yet.',
    chainSelection: new ChainSelection(saved),
    chainState: {},
    chainLatestPath, validateChainPayload,
    ingestSnapshotQuotes: (d) => calls.ingested.push(d.symbol ?? 'QQQ'),
    publishSnapshotQuotes: (d) => calls.published.push(d.symbol),
    updateQqqVwapRvolLine: (um) => calls.qqqLine.push(um?.symbol),
    updateQqqMomentumLine: (um) => calls.momentum.push(um?.symbol),
    updateQuoteInterests: () => calls.published.push('interests'),
    renderPaper: () => { calls.paper++; },
    renderHeatmap: (data, symbol) => {
      calls.heatmap.push({ symbol, dataSymbol: data.symbol, price: data.underlying_price });
      el('heatmap-body').innerHTML = `rows:${data.symbol}`;
      el('heatmap-wrap').classList.remove('hidden');
    },
    startCountdown: () => {},
  };
  const names = Object.keys(scope);
  const api = new Function(...names, `let lastTs = null, lastData = null;\n${source}\nreturn { fetchLatest, selectUnderlying, renderSelectedChain,
    get lastData(){ return lastData; }, get lastTs(){ return lastTs; } };`)(...names.map(n => scope[n]));
  return { api, els, el, calls, routes, reply, deferred, doc, scope };
}
const settle = () => new Promise(r => setImmediate(r));

(async () => {
  await testPriceTile();
  await testSpyHealthBadge();
  // QQQ → SPY → QQQ: headings, rendered data and QQQ-only side effects stay consistent.
  {
    const h = harness();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    h.routes['intraday/spy/latest.json'] = h.reply(snapshot('SPY'));
    await h.api.fetchLatest();
    assert.equal(h.el('title').textContent, 'QQQ 0DTE Live OI');
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'QQQ', dataSymbol: 'QQQ', price: 600.1 });

    h.api.selectUnderlying('SPY');
    assert.equal(h.el('title').textContent, 'SPY 0DTE Live OI', 'heading switches immediately');
    assert.equal(h.el('heatmap-body').innerHTML, '', 'QQQ rows cleared on switch');
    assert.equal(h.el('no-data').textContent, 'Loading SPY…');
    await settle(); await settle();
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'SPY', dataSymbol: 'SPY', price: 670.2 });
    assert.equal(h.doc.title, 'SPY Live OI — moopertonic');
    assert.ok(h.calls.ingested.every(s => s === 'QQQ'), 'only QQQ snapshots reach paper-trading settlement');
    assert.ok(h.calls.qqqLine.every(s => s === 'QQQ'), 'QQQ tile lines only ever get QQQ readings');
    const spyAt = h.calls.published.indexOf('SPY');
    assert.ok(spyAt > 0 && h.calls.published[spyAt - 1] === 'interests', 'SPY contracts registered as visible before their quotes are published');
    assert.deepEqual(h.calls.published.filter(x => x !== 'interests'), ['SPY'], 'SPY rows seed display quotes only');

    h.api.selectUnderlying('QQQ');
    await settle(); await settle();
    assert.equal(h.el('title').textContent, 'QQQ 0DTE Live OI');
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'QQQ', dataSymbol: 'QQQ', price: 600.1 });
    assert.equal(h.el('underlying-select').value, 'QQQ');
  }

  // A delayed SPY reply that lands after switching back to QQQ is dropped.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const slow = h.deferred();
    h.routes['intraday/spy/latest.json'] = slow.route;
    const inFlight = h.api.fetchLatest();
    await settle(); await settle();
    h.api.selectUnderlying('QQQ');
    await settle(); await settle();
    slow.resolve(snapshot('SPY'));
    await inFlight;
    assert.equal(h.el('title').textContent, 'QQQ 0DTE Live OI');
    assert.ok(h.calls.heatmap.every(c => c.symbol === 'QQQ' && c.dataSymbol === 'QQQ'), 'late SPY reply never rendered');
    assert.equal(h.scope.chainState.SPY, undefined, 'late SPY reply not cached either');
  }

  // QQQ -> SPY -> QQQ: an older QQQ response must not overwrite the newer
  // QQQ cache or feed paper settlement/indicators after the reverse switch.
  {
    const h = harness();
    const slow = h.deferred();
    h.routes['intraday/latest.json'] = slow.route;
    const old = h.api.fetchLatest();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ', { price: 601.1 }));
    h.routes['intraday/spy/latest.json'] = h.reply(snapshot('SPY'));
    h.api.selectUnderlying('SPY');
    await settle(); await settle();
    h.api.selectUnderlying('QQQ');
    await settle(); await settle();
    const ingested = h.calls.ingested.length;
    const indicators = h.calls.qqqLine.length;
    const paper = h.calls.paper;
    slow.resolve(snapshot('QQQ', { price: 599.1 }));
    await old;
    assert.equal(h.api.lastData.underlying_price, 601.1, 'late QQQ does not replace newer cache');
    assert.equal(h.scope.chainState.QQQ.data.underlying_price, 601.1);
    assert.equal(h.calls.ingested.length, ingested, 'late QQQ does not reach settlement');
    assert.equal(h.calls.qqqLine.length, indicators, 'late QQQ does not replace indicator state');
    assert.equal(h.calls.paper, paper, 'late QQQ does not render paper state');
    assert.equal(h.calls.momentum.length, indicators);
  }

  // Rapid SPY → QQQ → SPY: the first SPY request's reply is stale and dropped;
  // only the reply for the current selection renders.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const first = h.deferred();
    h.routes['intraday/spy/latest.json'] = first.route;
    const firstFetch = h.api.fetchLatest();
    await settle(); await settle();
    h.api.selectUnderlying('QQQ');
    const second = h.deferred();
    h.routes['intraday/spy/latest.json'] = second.route;
    h.api.selectUnderlying('SPY');
    await settle(); await settle();
    first.resolve(snapshot('SPY', { price: 111.1 }));
    await firstFetch; await settle();
    assert.ok(!h.calls.heatmap.some(c => c.price === 111.1), 'stale SPY reply dropped');
    second.resolve(snapshot('SPY', { price: 670.2 }));
    await settle(); await settle(); await settle();
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'SPY', dataSymbol: 'SPY', price: 670.2 });
  }

  // A QQQ-labeled payload at the SPY path is rejected, never shown as SPY.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    h.routes['intraday/spy/latest.json'] = h.reply(snapshot('QQQ'));
    await h.api.fetchLatest();
    assert.equal(h.calls.heatmap.length, 0, 'nothing rendered for a mislabeled SPY snapshot');
    assert.match(h.el('no-data').textContent, /SPY snapshot rejected: snapshot is labeled QQQ/);
    assert.equal(h.el('status-text').textContent, 'unavailable');
    assert.equal(h.el('price').textContent, '—', 'no price shown under the SPY heading');
  }

  // SPY not published yet (404) is reported explicitly; QQQ still refreshes.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    await h.api.fetchLatest();
    assert.equal(h.el('no-data').textContent, 'No SPY snapshot has been published yet.');
    assert.deepEqual(h.calls.ingested, ['QQQ'], 'QQQ still refreshed for paper trading while SPY is missing');
    assert.equal(h.calls.heatmap.length, 0);
  }

  // A transient SPY failure keeps even a fresh validated snapshot (marked stale)
  // instead of blanking it or substituting QQQ.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    h.routes['intraday/spy/latest.json'] = h.reply(snapshot('SPY'));
    await h.api.fetchLatest();
    h.routes['intraday/spy/latest.json'] = () => Promise.reject(new Error('network'));
    await h.api.fetchLatest();
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'SPY', dataSymbol: 'SPY', price: 670.2 });
    assert.equal(h.el('status-dot').className, 'stale', 'fresh cached SPY data is marked stale after failure');
    assert.match(h.scope.chainState.SPY.error, /could not be loaded/);
  }

  // QQQ has the same fresh-cache behavior; stale failures cannot clear a
  // newer success, and stale successes cannot clear a newer failure.
  {
    const h = harness();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    await h.api.fetchLatest();
    const slowFailure = h.deferred();
    h.routes['intraday/latest.json'] = slowFailure.route;
    const old = h.api.fetchLatest();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ', { price: 601.1 }));
    await h.api.fetchLatest();
    slowFailure.resolve({});
    await old;
    assert.equal(h.scope.chainState.QQQ.data.underlying_price, 601.1);
    assert.equal(h.scope.chainState.QQQ.error, null, 'old failure does not mark new success stale');
    const slowSuccess = h.deferred();
    h.routes['intraday/latest.json'] = slowSuccess.route;
    const older = h.api.fetchLatest();
    h.routes['intraday/latest.json'] = () => Promise.reject(new Error('network'));
    await h.api.fetchLatest();
    slowSuccess.resolve(snapshot('QQQ', { price: 602.1 }));
    await older;
    assert.equal(h.scope.chainState.QQQ.data.underlying_price, 601.1, 'cached QQQ kept');
    assert.match(h.scope.chainState.QQQ.error, /could not be loaded/, 'newer failure remains visible');
    assert.equal(h.el('status-dot').className, 'stale', 'fresh cached QQQ is stale after failure');
  }

  // A first QQQ load that fails is reported, not left on "Loading…".
  {
    const h = harness();
    h.routes['intraday/latest.json'] = () => Promise.reject(new Error('network'));
    await h.api.fetchLatest();
    assert.equal(h.el('status-text').textContent, 'unavailable');
    assert.equal(h.el('no-data').textContent, 'QQQ snapshot could not be loaded.');
  }

  console.log('chain_underlying: all checks passed');
})().catch(e => { console.error(e); process.exit(1); });
