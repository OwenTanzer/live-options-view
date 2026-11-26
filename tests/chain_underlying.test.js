// QQQ/SPY chain selection (#121): the pure helpers in site/shared.js, plus
// the page's fetch/render cycle — site/js/data-fetch-cycle.js since B2 slice 5
// (Nov 2026), loaded by index.html in the same position the inline section
// occupied — run against a minimal DOM shim, so the shipped source is what's
// tested. This was the first B2 slice that moved a test: earlier versions
// sliced the section out of index.html by banner text. The SPY health badge
// (fetchSpyHealth) moved again in B2 slice 15 with the paper-trading section —
// its test now loads site/js/paper-trading.js instead of slicing the page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const shared = require('../site/shared.js');
const { chainLatestPath, occRoot, validateChainPayload, ChainSelection, relativeOiRanges, summarizeSpyHealth } = shared;
const sharedFormat = require('../site/shared/format.js');

const NOW = Date.now();
function snapshot(symbol, { declare = true, price = symbol === 'SPY' ? 670.2 : 600.1, rows, ts = new Date(NOW).toISOString(), date = '2026-10-06', expiration = date } = {}) {
  const strike = Math.round(price);
  const occDate = expiration.slice(2).replaceAll('-', '');
  return {
    ...(declare ? { symbol } : {}),
    timestamp: ts, snapshot_time: '10:30 ET', date, expiration, tier: '0DTE_Regular',
    underlying_price: price,
    underlying_market: { symbol, spot: price },
    rows: rows || [
      { OptionSymbol: `${symbol.padEnd(6)}${occDate}C00${strike}000`.replace(/ /g, ' '), Strike: strike, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2, Expiration: expiration },
      { OptionSymbol: `${symbol}${occDate}P00${strike}000`, Strike: strike, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2, Expiration: expiration },
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
const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
// B2 (readability quest): the data fetch cycle moved out of index.html into
// site/js/data-fetch-cycle.js, loaded in the same position. The test loads the
// shipped module instead of slicing the page by banner text, and pins the ?v=
// hash in the page to the module's content (like shared.js).
const source = fs.readFileSync(path.join(__dirname, '..', 'site', 'js', 'data-fetch-cycle.js'), 'utf8');
const moduleHash = createHash('sha256').update(source).digest('hex').slice(0, 16);
assert.ok(html.includes('js/data-fetch-cycle.js?v=' + moduleHash), 'data-fetch-cycle script URL must match its content hash on every hosting path');
assert.ok(!html.includes('async function fetchChain('), 'fetchChain must live in js/data-fetch-cycle.js, not inline in index.html');
assert.ok(!html.includes('function fetchLatest('), 'fetchLatest must live in js/data-fetch-cycle.js, not inline in index.html');
assert.ok(html.includes('// ── data fetch cycle'), 'data fetch cycle banner must stay in index.html as the section marker');

// The price tile and SPY health badge use the shipped page functions, with
// offline artifact responses and no provider/browser network dependency.
// B2 (readability quest): the price strip moved out of index.html into
// site/js/price-strip.js, loaded in the same position. The test loads the
// shipped module instead of slicing fetchPrices out of the page by banner
// text, and pins the ?v= hash in the page to the module's content (like
// shared.js). The module runs against the real shared.js TickerStateStore
// with a stub DOM, so the SPY provider-vs-fallback routing is verified
// through the store the page actually uses.
const priceStripSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'js', 'price-strip.js'), 'utf8');
const priceStripHash = createHash('sha256').update(priceStripSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/price-strip.js?v=' + priceStripHash), 'price-strip script URL must match its content hash on every hosting path');
assert.ok(!html.includes('async function fetchPrices('), 'fetchPrices must live in js/price-strip.js, not inline in index.html');
assert.ok(!html.includes("const MAIN_TICKERS = ['QQQ', 'SPY'"), 'the price-strip tickers must live in js/price-strip.js, not inline in index.html');
assert.ok(html.includes('// ── price strip'), 'price strip banner must stay in index.html as the section marker');
assert.match(priceStripSource, /const MAIN_TICKERS = \['QQQ', 'SPY'/);
assert.equal(shared.isTradeableShareSymbol('SPY'), false, 'SPY tile is display-only');
const testPriceTile = async () => {
  // Stub DOM: one cached tile per id, each with cached .px/.chg/.market-state
  // spans — updatePriceStrip writes the tile state through these, and the
  // getElementById count is the tile-refresh count.
  const makeSpan = () => ({ textContent: '', className: '', dataset: {} });
  const makeTile = () => {
    const spans = {};
    const classList = { _s: new Set(), add(...c) { c.forEach(x => this._s.add(x)); }, remove(...c) { c.forEach(x => this._s.delete(x)); }, contains(c) { return this._s.has(c); } };
    return {
      textContent: '', title: '', dataset: {}, offsetWidth: 0, classList,
      querySelector: sel => (spans[sel] ||= makeSpan()),
    };
  };
  const tiles = {};
  const byIdCount = {};
  const documentStub = {
    getElementById: id => { byIdCount[id] = (byIdCount[id] || 0) + 1; return (tiles[id] ||= makeTile()); },
    createElement: () => makeTile(),
  };
  let prices = { SPY: { price: 670.2, source: 'dxlink', quote_ts: new Date(NOW).toISOString() }, QQQ: { price: 600.1 } };
  const context = vm.createContext({
    console: { warn() {} },
    document: documentStub,
    fetch: async () => ({ ok: true, json: async () => ({ prices }) }),
    BROWSER_DATA: 'https://r2.example',
    // The module constructs its tickerState at load from the real shared.js
    // store, so the provider-vs-fallback routing is exercised for real.
    TickerStateStore: shared.TickerStateStore,
    SHARE_QUOTE_MAX_AGE_MS: shared.SHARE_QUOTE_MAX_AGE_MS,
    isTradeableShareSymbol: shared.isTradeableShareSymbol,
    formatVwapRvol: shared.formatVwapRvol,
    formatMomentum: shared.formatMomentum,
    openShareTicket: () => {},
  });
  vm.runInContext(priceStripSource, context, { filename: 'site/js/price-strip.js' });
  // Primitive reads across the vm boundary (deepEqual fails across realms).
  const g = expr => vm.runInContext(expr, context);
  const fetchPrices = () => vm.runInContext('fetchPrices()', context);
  const spyPrice = () => g("tickerState.get('SPY').price");
  const spySource = () => g("tickerState.get('SPY').source");
  await fetchPrices();
  assert.equal(spyPrice(), 670.2, 'SPY tile uses the SPY provider price');
  assert.equal(spySource(), 'dxlink', 'SPY keeps its provider source, not the macro fallback');
  assert.equal(g("tickerState.get('SPY').observedAt"), prices.SPY.quote_ts, 'SPY tile keeps provider observation time');
  assert.equal(g("tickerState.get('QQQ').source"), 'r2-snapshot', 'QQQ comes through the macro fallback');
  assert.equal(g("tickerState.get('QQQ').price"), 600.1);
  assert.equal(tiles['tk-SPY'].querySelector('.px').textContent, '670.20', 'SPY tile renders the provider price');
  assert.equal(tiles['tk-QQQ'].querySelector('.px').textContent, '600.10');
  assert.equal(tiles['tk-QQQ'].dataset.state, 'fallback', 'QQQ tile is marked fallback');
  assert.equal(byIdCount['tk-SPY'], 1, 'one tile refresh per fetch');
  prices = { SPY: { price: null, source: null, quote_ts: null }, QQQ: { price: 600.1 } };
  await fetchPrices();
  assert.equal(spyPrice(), 670.2, 'missing SPY price is not fabricated');
  assert.equal(spySource(), 'dxlink', 'missing SPY does not fall back to a macro price');
  assert.equal(byIdCount['tk-SPY'], 2, 'tile state is refreshed even when SPY is missing');
  for (const bad of [
    { price: 600, source: 'dxlink', quote_ts: new Date(NOW).toISOString(), symbol: 'QQQ' },
    { price: 670, source: 'yfinance', quote_ts: new Date(NOW).toISOString() },
    { price: 670, source: 'dxlink', quote_ts: null },
    { price: Infinity, source: 'dxlink', quote_ts: new Date(NOW).toISOString() },
  ]) {
    prices = { SPY: bad, QQQ: { price: 600.1 } };
    await fetchPrices();
    assert.equal(spyPrice(), 670.2, 'foreign or unsupported SPY evidence is rejected');
    assert.equal(spySource(), 'dxlink');
  }
  assert.equal(byIdCount['tk-SPY'], 6, 'every fetch refreshes the tile');
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
  // B2 (readability quest): fetchSpyHealth moved out of index.html into
  // site/js/paper-trading.js with the paper-trading section (slice 15), so
  // this test loads the shipped module instead of slicing the page between
  // `let latestSpyHealthRequest` and `liveQuotes.subscribe(`. The module
  // needs the real shared.js in the same context (ChainSelection,
  // summarizeSpyHealth) plus site/shared/quotes.js (B3 slice 3: the
  // LiveQuoteService its top-level `liveQuotes` constructs) plus the page's
  // shared-scope stubs.
  const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'js', 'paper-trading.js'), 'utf8');
  const sharedSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
  const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
  assert.ok(html.includes('js/paper-trading.js?v=' + moduleHash), 'paper-trading script URL must match its content hash on every hosting path');
  const badge = { className: '', textContent: '', title: '' };
  let response = { updated_at: new Date().toISOString(), spy: {
    collection_status: 'collecting', spot: { status: 'live' },
    last_snapshot_upload_time: new Date().toISOString(),
  } };
  const pending = [];
  let fetchImpl = async () => ({ ok: true, json: async () => response });
  const context = vm.createContext({
    console,
    document: { getElementById: () => badge, createElement: () => ({}) },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    crypto: { randomUUID: () => 'test-uuid' },
    BROWSER_DATA: 'https://r2.example',
    fetch: async (url, options) => fetchImpl(url, options),
    botsTimer: null,
    renderBots: () => {},
    showMarginCallScreen: () => {},
    closeTicket: () => {},
    renderSelectedChain: () => {},
    tickerState: { get: () => null },
    selectedChainData: () => null,
  });
  vm.runInContext(sharedSource, context, { filename: 'site/shared.js' });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'quotes.js'), 'utf8'), context, { filename: 'site/shared/quotes.js' });
  // B3 slice 4 (Nov 2026): the panel formatters + order validators moved to
  // site/shared/format.js — same, after quotes.js, in page order.
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'format.js'), 'utf8'), context, { filename: 'site/shared/format.js' });
  // B3 slice 5 (Nov 2026): the chain underlying names moved to
  // site/shared/chain.js — same, after format.js, in page order, so
  // paper-trading.js constructs the real ChainSelection at load.
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'chain.js'), 'utf8'), context, { filename: 'site/shared/chain.js' });
  vm.runInContext(moduleSource, context, { filename: 'site/js/paper-trading.js' });
  const fetchSpyHealth = () => vm.runInContext('fetchSpyHealth()', context);
  await fetchSpyHealth();
  assert.equal(badge.className, 'transport-health live', 'fresh SPY health renders live');
  response = { ...response, updated_at: new Date(Date.now() - 60_000).toISOString() };
  await fetchSpyHealth();
  assert.equal(badge.className, 'transport-health stale', 'old SPY health renders stale');
  response = { updated_at: new Date().toISOString(), spy: { collection_status: 'missing_today_expiration', reason: 'no SPY 0DTE' } };
  await fetchSpyHealth();
  assert.match(badge.textContent, /missing today expiration/, 'missing SPY 0DTE is visible');
  fetchImpl = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
  const oldRequest = fetchSpyHealth();
  const newRequest = fetchSpyHealth();
  pending[1].resolve({ ok: true, json: async () => ({
    updated_at: new Date().toISOString(), spy: { collection_status: 'disabled' },
  }) });
  await newRequest;
  pending[0].reject(new Error('old request failed'));
  await oldRequest;
  assert.equal(badge.textContent, 'SPY: disabled', 'old failure cannot replace newer health');
  const failed = fetchSpyHealth();
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
    const parsed = new URL(url, 'https://app.example');
    assert.equal(parsed.origin, 'https://app.example', 'chain fetch stays same-origin');
    assert.ok(parsed.pathname.startsWith('/browser-data/'), 'chain uses dedicated route');
    const p = parsed.pathname.slice('/browser-data/'.length);
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
    BROWSER_DATA: '/browser-data',
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
    // #178: the dot's freshness judgment and the header's snapshot line are
    // the same shipped formatter, so the harness must inject the real one.
    formatChainSnapshot: sharedFormat.formatChainSnapshot,
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
  // Equal call/put OI across six strikes ties the two middle payouts.
  const hPin = harness();
  const chain = snapshot('QQQ');
  chain.rows = [603, 601, 604, 602, 605, 600].flatMap(Strike =>
    snapshot('QQQ', { price: Strike }).rows.map(row => ({ ...row, OpenInterest: 10 })));
  hPin.routes['intraday/latest.json'] = hPin.reply(chain);
  await hPin.api.fetchLatest();
  assert.equal(hPin.el('max-pain').textContent, 'Max pain $602.00');
  chain.rows.find(row => row.Strike === 605 && row.Type === 'put').OpenInterest = 1000;
  await hPin.api.fetchLatest();
  assert.equal(hPin.el('max-pain').textContent, 'Max pain $605.00');
  delete hPin.routes['intraday/latest.json'];
  await hPin.api.fetchLatest();
  assert.equal(hPin.el('max-pain').textContent, 'Max pain $605.00 · retained');
  hPin.routes['intraday/spy/latest.json'] = hPin.reply(snapshot('SPY'));
  hPin.api.selectUnderlying('SPY');
  assert.equal(hPin.el('max-pain').textContent, 'Max pain —');
  await settle(); await settle();
  assert.equal(hPin.el('max-pain').textContent, 'Max pain unavailable');
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
    assert.equal(h.el('title').textContent, 'SPY Options Live OI', 'heading switches immediately without inventing expiry');
    assert.equal(h.el('heatmap-body').innerHTML, '', 'QQQ rows cleared on switch');
    assert.equal(h.el('no-data').textContent, 'Loading SPY…');
    await settle(); await settle();
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'SPY', dataSymbol: 'SPY', price: 670.2 });
    assert.equal(h.doc.title, 'SPY Live OI — moopertonic');
    assert.ok(h.calls.ingested.includes('SPY'), 'SPY reaches underlying-specific paper state');
    assert.ok(h.calls.qqqLine.every(s => s === 'QQQ'), 'QQQ tile lines only ever get QQQ readings');


    h.api.selectUnderlying('QQQ');
    await settle(); await settle();
    assert.equal(h.el('title').textContent, 'QQQ 0DTE Live OI');
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'QQQ', dataSymbol: 'QQQ', price: 600.1 });
    assert.equal(h.el('underlying-select').value, 'QQQ');
  }

  // #178: the status dot and the header line share one freshness judgment, so a
  // chain that stopped collecting is not marked live just because its rows
  // still carry bids.
  {
    const h = harness();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    await h.api.fetchLatest();
    assert.equal(h.el('status-dot').className, 'live', 'a current snapshot is marked live');
    assert.equal(h.el('status-text').textContent, '10:30 ET', 'the status text stays the producer timestamp label');
    assert.equal(sharedFormat.formatChainSnapshot(h.scope.chainState.QQQ.data).state, 'live');
  }
  {
    const h = harness();
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ', { ts: new Date(Date.now() - 6 * 60_000).toISOString() }));
    await h.api.fetchLatest();
    assert.equal(h.el('status-dot').className, 'stale', 'a six-minute-old chain is not marked live');
    const frozen = sharedFormat.formatChainSnapshot(h.scope.chainState.QQQ.data);
    assert.equal(frozen.state, 'retained', 'and the header calls it a retained reading');
    assert.match(frozen.text, /^snapshot 10:30 ET · Regular session · 6m ago · retained$/);
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

  // IBIT nearest-expiry: a valid snapshot renders under its own expiry
  // heading (never labeled 0DTE), with rows whose expirations all match.
  {
    const h = harness('IBIT');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const ibit = snapshot('IBIT', { price: 50, date: '2026-11-09', expiration: '2026-11-13', rows: [
      { OptionSymbol: 'IBIT261113C00050000', Strike: 50, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2, Expiration: '2026-11-13' },
      { OptionSymbol: 'IBIT261113P00050000', Strike: 50, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2, Expiration: '2026-11-13' },
    ] });
    h.routes['intraday/ibit/latest.json'] = h.reply(ibit);
    await h.api.fetchLatest();
    assert.equal(h.el('title').textContent, 'IBIT exp 2026-11-13 Live OI', 'nearest expiry is labeled by its date, never 0DTE');
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'IBIT', dataSymbol: 'IBIT', price: 50 });
    assert.ok(h.calls.ingested.includes('IBIT'), 'IBIT reaches underlying-specific paper state');
  }

  // SLV (silver) and DRAM (memory) are nearest-expiry like IBIT: each renders
  // from its own tree under its expiry date, and rejects mixed expirations.
  for (const [root, price] of [['SLV', 30], ['DRAM', 40]]) {
    const strike = String(price * 1000).padStart(8, '0');
    const rows = exp => [
      { OptionSymbol: `${root}261120C${strike}`, Strike: price, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2, Expiration: '2026-11-20' },
      { OptionSymbol: `${root}261120P${strike}`, Strike: price, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2, Expiration: exp },
    ];
    const h = harness(root);
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    h.routes[`intraday/${root.toLowerCase()}/latest.json`] = h.reply(snapshot(root, { price, date: '2026-11-16', expiration: '2026-11-20', rows: rows('2026-11-20') }));
    await h.api.fetchLatest();
    assert.equal(h.el('title').textContent, `${root} exp 2026-11-20 Live OI`, `${root} is labeled by its expiry`);
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: root, dataSymbol: root, price });

    const bad = harness(root);
    bad.routes['intraday/latest.json'] = bad.reply(snapshot('QQQ'));
    bad.routes[`intraday/${root.toLowerCase()}/latest.json`] = bad.reply(snapshot(root, { price, date: '2026-11-16', expiration: '2026-11-20', rows: rows('2026-11-27') }));
    await bad.api.fetchLatest();
    assert.equal(bad.calls.heatmap.length, 0, `${root} mixed expirations are rejected`);
    assert.match(bad.el('no-data').textContent, /missing or mixed expiration/);
  }

  // IBIT without an expiration is rejected, never rendered as a chain.
  {
    const h = harness('IBIT');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const noExp = snapshot('IBIT', { price: 50, date: '2026-11-09', expiration: '2026-11-13', rows: [
      { OptionSymbol: 'IBIT261113C00050000', Strike: 50, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2, Expiration: '2026-11-13' },
      { OptionSymbol: 'IBIT261113P00050000', Strike: 50, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2, Expiration: '2026-11-13' },
    ] });
    delete noExp.expiration;
    h.routes['intraday/ibit/latest.json'] = h.reply(noExp);
    await h.api.fetchLatest();
    assert.equal(h.calls.heatmap.length, 0, 'nothing rendered for an IBIT snapshot without an expiration');
    assert.match(h.el('no-data').textContent, /missing or mixed expiration/);
    assert.equal(h.el('status-text').textContent, 'unavailable');
  }

  // IBIT with mixed row expirations is rejected.
  {
    const h = harness('IBIT');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const mixed = snapshot('IBIT', { price: 50, date: '2026-11-09', expiration: '2026-11-13', rows: [
      { OptionSymbol: 'IBIT261113C00050000', Strike: 50, Type: 'call', OpenInterest: 10, Bid: 1, Ask: 1.2, Expiration: '2026-11-13' },
      { OptionSymbol: 'IBIT261120P00050000', Strike: 50, Type: 'put', OpenInterest: 20, Bid: 1, Ask: 1.2, Expiration: '2026-11-20' },
    ] });
    h.routes['intraday/ibit/latest.json'] = h.reply(mixed);
    await h.api.fetchLatest();
    assert.equal(h.calls.heatmap.length, 0, 'nothing rendered for mixed-expiration IBIT rows');
    assert.match(h.el('no-data').textContent, /missing or mixed expiration/);
  }

  // Same-selection overlap: two concurrent SPY fetches; the slower (older)
  // reply must not overwrite the fresher data that already landed.
  {
    const h = harness('SPY');
    h.routes['intraday/latest.json'] = h.reply(snapshot('QQQ'));
    const slow = h.deferred();
    h.routes['intraday/spy/latest.json'] = slow.route;
    const old = h.api.fetchLatest();
    await settle(); await settle();
    h.routes['intraday/spy/latest.json'] = h.reply(snapshot('SPY', { price: 671.5 }));
    await h.api.fetchLatest();
    assert.equal(h.scope.chainState.SPY.data.underlying_price, 671.5, 'newer SPY data landed');
    slow.resolve(snapshot('SPY', { price: 670.2 }));
    await old;
    assert.equal(h.scope.chainState.SPY.data.underlying_price, 671.5, 'late same-selection SPY reply dropped');
    assert.deepEqual(h.calls.heatmap.at(-1), { symbol: 'SPY', dataSymbol: 'SPY', price: 671.5 });
  }

  console.log('chain_underlying: all checks passed');
})().catch(e => { console.error(e); process.exit(1); });
