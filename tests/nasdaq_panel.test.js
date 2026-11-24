// NASDAQ-100 Stocks panel tests: interval list, table sort/search helpers,
// % labelling strings, stale-state rendering, candle availability, the
// Heatmap view (default, squarified treemap, toggle, tile tap -> detail),
// index-changes rendering, and the site wiring (Stocks tab +
// #nasdaq-panel section + hashed script tag).
// Plain node:assert, no DOM dependency — render helpers return strings and
// mount() is driven with a minimal fake document/container.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const panel = require('../site/js/nasdaq-panel.js');
const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const tabSwitching = fs.readFileSync(path.join(__dirname, '../site/js/tab-switching.js'), 'utf8');
const panelSource = fs.readFileSync(path.join(__dirname, '../site/js/nasdaq-panel.js'), 'utf8');

// --- fixtures ---------------------------------------------------------------
const NOW = Date.parse('2026-11-23T21:30:00Z');
const FRESH_AS_OF = '2026-11-23T21:00:00Z';
const STALE_AS_OF = '2026-11-23T18:00:00Z';
function stock(symbol, overrides) {
  return Object.assign({ symbol: symbol, name: symbol + ' Corp', price: 100, percent_change: 0,
    market_cap: 1e11, rank: 1, day_high: 101, day_low: 99, volume: 1000000,
    news_count: 0, news: [] }, overrides || {});
}
const snapshot = {
  as_of: FRESH_AS_OF,
  index_changes: { as_of: '2026-11-23', added: [], removed: [], source: 'live', fallback: false },
  index: { level: 25431.10, percent_change: 0.42, market_status: 'closed', prev_close: 25324.50,
    day_low: 25210.00, day_high: 25480.00, advancers: 62, decliners: 38 },
  stocks: [
    stock('NVDA', { name: 'NVIDIA Corp', price: 178.42, percent_change: 1.83, market_cap: 4.35e12, rank: 1,
      news_count: 2, news: [{ title: 'NVDA ships new chip', source: 'Reuters',
        url: 'https://example.com/nvda', published_at: '2026-11-23T18:12:00Z' }] }),
    stock('AAPL', { name: 'Apple Inc', price: 262.10, percent_change: -0.54, market_cap: 3.9e12, rank: 2 }),
    stock('MSFT', { name: 'Microsoft Corp', price: 512.30, percent_change: 0.21, market_cap: 3.8e12, rank: 3 }),
    stock('ZZZ', { name: 'No Price Inc', price: null, percent_change: null, market_cap: null, rank: 101 }),
  ],
  candles: { NVDA: { '1d': [
    { open: 176.5, high: 180.1, low: 176.2, close: 178.42, volume: 182340000, time: '2026-11-23T21:00:00Z' },
    { open: 175.0, high: 177.0, low: 174.5, close: 176.5, volume: 150000000, time: '2026-11-22T21:00:00Z' },
  ] } },
};

// --- interval list: all eleven, in order ------------------------------------
assert.deepEqual(panel.INTERVALS,
  ['1m', '3m', '5m', '9m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'],
  'the panel must offer all eleven candle intervals');
assert.equal(new Set(panel.INTERVALS).size, 11, 'intervals must be unique');

// --- sort helpers -----------------------------------------------------------
const sortedByPctDesc = panel.sortStocks(snapshot.stocks, 'percent_change', -1);
assert.deepEqual(sortedByPctDesc.map(s => s.symbol), ['NVDA', 'MSFT', 'AAPL', 'ZZZ'],
  'sort by % desc puts the biggest gainer first and a missing % last');
const sortedByPctAsc = panel.sortStocks(snapshot.stocks, 'percent_change', 1);
assert.deepEqual(sortedByPctAsc.map(s => s.symbol), ['AAPL', 'MSFT', 'NVDA', 'ZZZ'],
  'sort by % asc puts the biggest loser first and a missing % still last');
assert.deepEqual(panel.sortStocks(snapshot.stocks, 'price', -1).map(s => s.symbol),
  ['MSFT', 'AAPL', 'NVDA', 'ZZZ'], 'sort by price desc, missing price last');
assert.deepEqual(panel.sortStocks(snapshot.stocks, 'market_cap', -1).map(s => s.symbol),
  ['NVDA', 'AAPL', 'MSFT', 'ZZZ'], 'sort by market cap desc');
assert.deepEqual(panel.sortStocks(snapshot.stocks, 'name', 1).map(s => s.symbol),
  ['AAPL', 'MSFT', 'ZZZ', 'NVDA'], 'sort by name asc (Apple, Microsoft, No Price, NVIDIA)');
assert.equal(panel.sortStocks(snapshot.stocks, 'price', -1)[0].price, 512.30, 'sort must not mutate the input order');
assert.equal(snapshot.stocks[0].symbol, 'NVDA', 'sort must not mutate the input array');

// --- search helpers ---------------------------------------------------------
assert.equal(panel.searchStocks(snapshot.stocks, '').length, 4, 'empty query returns all stocks');
assert.deepEqual(panel.searchStocks(snapshot.stocks, 'nvda').map(s => s.symbol), ['NVDA'],
  'search matches symbol case-insensitively');
assert.deepEqual(panel.searchStocks(snapshot.stocks, 'micro').map(s => s.symbol), ['MSFT'],
  'search matches name case-insensitively');
assert.deepEqual(panel.searchStocks(snapshot.stocks, '  APPLE  ').map(s => s.symbol), ['AAPL'],
  'search trims the query');
assert.equal(panel.searchStocks(snapshot.stocks, 'qqq').length, 0, 'no match returns an empty list');

// --- % labelling strings ----------------------------------------------------
assert.equal(panel.INDEX_PCT_LABEL, 'vs prev close (regular session)');
assert.equal(panel.STOCK_PCT_LABEL, 'Nasdaq latest reported move');
assert.ok(panelSource.includes('INDEX_PCT_LABEL'), 'index label constant must be in the panel source');
const hero = panel.renderHero(snapshot, false);
assert.ok(hero.includes('vs prev close (regular session)'), 'hero must label the index % basis');
assert.ok(hero.includes('As of 2026-11-23 21:00:00 UTC'), 'hero must carry the as-of time');
assert.ok(hero.includes('Nasdaq official API'), 'hero must name the price source');
assert.ok(hero.includes('62 advancers · 38 decliners'), 'hero must show breadth');
assert.ok(!hero.includes('STALE'), 'a fresh snapshot must not be marked stale');
// The page states both bases explicitly under the table.
assert.ok(html.includes('Index % is vs prev close (regular session)'),
  'index.html must state the index % basis under the table');
assert.ok(html.includes("Each stock's % is the Nasdaq latest reported move"),
  'index.html must state the stock % basis under the table');
assert.ok(html.includes('do not compare them directly'),
  'index.html must warn the two bases are different');

// --- validation -------------------------------------------------------------
assert.deepEqual(panel.validateSnapshot(snapshot), [], 'the fixture snapshot must validate');
assert.ok(panel.validateSnapshot({ as_of: FRESH_AS_OF }).length > 0, 'missing index/stocks must fail');
assert.ok(panel.validateSnapshot({ as_of: 'not-a-time', index: { level: 1 }, stocks: [] }).length > 0,
  'an unparseable as_of must fail');
assert.ok(panel.validateSnapshot({ as_of: FRESH_AS_OF, index: { level: 'high' }, stocks: [] }).length > 0,
  'a non-numeric index level must fail');
assert.ok(panel.validateSnapshot({ as_of: FRESH_AS_OF, index: { level: 1 },
  stocks: [{ price: 1 }] }).length > 0, 'a stock without a symbol must fail');
assert.ok(panel.validateSnapshot(null).length > 0, 'a non-object snapshot must fail');

// --- stale-state rendering --------------------------------------------------
assert.equal(panel.isStale(FRESH_AS_OF, NOW), false, 'a 30-minute-old snapshot is fresh');
assert.equal(panel.isStale(STALE_AS_OF, NOW), true, 'a 3.5-hour-old snapshot is stale');
assert.equal(panel.isStale(undefined, NOW), true, 'a missing as_of is stale, never fresh');
assert.equal(panel.isStale('not-a-time', NOW), true, 'an unparseable as_of is stale');
assert.ok(panel.renderHero(Object.assign({}, snapshot, { as_of: STALE_AS_OF }), true).includes('STALE'),
  'a stale snapshot hero must carry the STALE badge');
assert.equal(panel.fmtMarketCap(4.35e12), '$4.35T', 'market cap formats in trillions');
assert.equal(panel.fmtMarketCap(812.3e9), '$812.3B', 'market cap formats in billions');
assert.equal(panel.fmtPrice(null), '—', 'a missing price renders as a dash, never zero');
assert.equal(panel.fmtPct(null), '—', 'a missing % renders as a dash, never zero');

// --- heatmap --------------------------------------------------------------
assert.equal(panel.HEATMAP_FULL_PCT, 3, 'heatmap colour reaches full intensity at +/-3%');
assert.equal(panel.heatmapColor(3), 'rgb(0, 204, 85)', '+3% is full green');
assert.equal(panel.heatmapColor(-3), 'rgb(238, 51, 0)', '-3% is full red');
assert.equal(panel.heatmapColor(9), 'rgb(0, 204, 85)', 'beyond +3% clamps to full green');
assert.equal(panel.heatmapColor(-9), 'rgb(238, 51, 0)', 'beyond -3% clamps to full red');
assert.equal(panel.heatmapColor(0), 'rgb(42, 47, 55)', 'a flat stock is the neutral tile colour');
assert.equal(panel.heatmapColor(null), 'rgb(42, 47, 55)', 'a missing % is neutral, never an error');
assert.equal(panel.heatmapColor(1.5), 'rgb(21, 126, 70)', '+1.5% is half-intensity green');
assert.equal(panel.heatmapColor(-1.5), 'rgb(140, 49, 28)', '-1.5% is half-intensity red');

// Minimum-share floor: the smallest weights are raised to at least the
// floor share, and the shares still sum to 1.
const floored = panel.applyMinShare([100, 1, 1, 1], 0.05);
assert.ok(Math.abs(floored.reduce((a, b) => a + b, 0) - 1) < 1e-9, 'floored shares sum to 1');
assert.ok(Math.min(...floored) >= 0.049, 'every share is at least the floor after raising');
assert.ok(floored[0] > floored[1], 'the largest weight stays the largest tile');
assert.deepEqual(panel.applyMinShare([], 0.05), [], 'no weights in, no shares out');

// Squarified layout: tiles the rectangle exactly, no overlaps, in bounds.
function rectsOverlap(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}
const treemap = panel.layoutTreemap(
  [{ key: 'A', weight: 4 }, { key: 'B', weight: 3 }, { key: 'C', weight: 2 }, { key: 'D', weight: 1 }],
  100, 100);
assert.equal(treemap.length, 4, 'one rect per item');
assert.ok(Math.abs(treemap.reduce((s, r) => s + r.w * r.h, 0) - 10000) < 1,
  'tile areas sum to the container area');
treemap.forEach(r => {
  assert.ok(r.x >= -1e-9 && r.y >= -1e-9 && r.x + r.w <= 100 + 1e-9 && r.y + r.h <= 100 + 1e-9,
    'every tile stays inside the container');
});
for (let i = 0; i < treemap.length; i++) {
  for (let j = i + 1; j < treemap.length; j++) {
    assert.ok(!rectsOverlap(treemap[i], treemap[j]), 'tiles must not overlap');
  }
}
assert.deepEqual(panel.layoutTreemap([{ key: 'ONLY', weight: 1 }], 100, 60),
  [{ key: 'ONLY', x: 0, y: 0, w: 100, h: 60 }], 'a single tile fills the container');
assert.deepEqual(panel.layoutTreemap([], 100, 100), [], 'no items, no tiles');
// Square-root scaling with the floor: a 10,000x market-cap ratio becomes a
// 100x weight ratio before the floor, and the smallest tile keeps >= 0.5%.
const tiny = panel.layoutTreemap(
  [{ key: 'BIG', weight: 1 }, { key: 'SMALL', weight: 0.01 }], 100, 100);
assert.ok(tiny.find(r => r.key === 'SMALL').w * tiny.find(r => r.key === 'SMALL').h >= 0.5,
  'the minimum-size floor keeps the smallest tile visible');

// Heatmap rendering: same rows as the table, ticker + % on every tile,
// -3%/+3% legend, and a tile even for the stock with no market cap.
const heatmap = panel.renderHeatmap(snapshot.stocks);
assert.ok(heatmap.includes('nq-heatmap'), 'heatmap renders its container');
assert.equal((heatmap.match(/data-symbol="/g) || []).length, 4,
  'one tile per stock row, including the no-price row');
for (const sym of ['NVDA', 'AAPL', 'MSFT', 'ZZZ']) {
  assert.ok(heatmap.includes('data-symbol="' + sym + '"'), 'heatmap has a tile for ' + sym);
}
assert.ok(heatmap.includes('NVDA') && heatmap.includes('+1.83%'), 'tiles label ticker + % change');
assert.ok(heatmap.includes('-3%') && heatmap.includes('+3%'), 'legend spans -3% to +3%');
assert.ok(heatmap.includes('nq-legend-bar'), 'legend has its gradient bar');
const nvdaTile = /data-symbol="NVDA"[^>]*background:([^;"]+)/.exec(heatmap);
assert.ok(nvdaTile, 'the NVDA tile carries a background colour');
assert.equal(nvdaTile[1], panel.heatmapColor(1.83), 'the +1.83% tile is coloured by its % change');
assert.match(panel.renderHeatmap([]), /No stocks to show in the heatmap/,
  'an empty heatmap says so explicitly');
// Extreme caps: a $100T listing next to a $1M one — square-root scaling
// plus the floor keeps the small tile at its minimum share of the area.
const extreme = panel.renderHeatmap([
  stock('BIG', { market_cap: 1e14, percent_change: 1 }),
  stock('SMALL', { market_cap: 1e6, percent_change: -1 }),
]);
const smallTile = /data-symbol="SMALL"[^>]*width:([\d.]+)%;height:([\d.]+)%/.exec(extreme);
assert.ok(smallTile, 'the small-cap tile renders');
assert.ok((parseFloat(smallTile[1]) / 100) * (parseFloat(smallTile[2]) / 100) >= 0.004,
  'the small-cap tile keeps at least ~0.4% of the heatmap area');

// --- index changes ----------------------------------------------------------
const changes = panel.renderIndexChanges({
  index_changes: { as_of: '2026-11-23',
    added: [{ symbol: 'NEW1', name: 'New Entrant Inc.' }],
    removed: [{ symbol: 'XEL', name: 'Xcel Energy Inc.' }],
    source: 'live', fallback: false } });
assert.ok(changes.includes('Index changes as of 2026-11-23'), 'changes carry the run date');
assert.ok(changes.includes('NEW1') && changes.includes('New Entrant Inc.'), 'entrants are named');
assert.ok(changes.includes('XEL') && changes.includes('Xcel Energy Inc.'), 'leavers are named');
assert.ok(panel.renderIndexChanges(snapshot).includes('no changes'),
  'a live run with no changes says so');
assert.ok(panel.renderIndexChanges({
  index_changes: { as_of: '2026-11-23', added: [], removed: [],
    source: 'embedded_fallback', fallback: true } }).includes('embedded universe'),
  'a fallback run says the live list was unavailable');
assert.equal(panel.renderIndexChanges({}), '', 'no index_changes, no changes line');
assert.equal(panel.renderIndexChanges(null), '', 'a null snapshot has no changes line');
const hostileChange = panel.renderIndexChanges({
  index_changes: { as_of: '2026-11-23', added: [{ symbol: 'X', name: '<script>' }],
    removed: [], source: 'live', fallback: false } });
assert.doesNotMatch(hostileChange, /<script>/, 'change names are escaped');
// Detail rank uses the live listing count, not a hard-coded 101.
assert.ok(panel.renderDetail(snapshot.stocks[0], '1d', snapshot.candles, 4).includes('Rank: 1 of 4'),
  'detail ranks against the snapshot size');
assert.ok(!panel.renderDetail(snapshot.stocks[0], '1d', snapshot.candles).includes(' of '),
  'detail without a total states no count');

// --- candles ----------------------------------------------------------------
const chart = panel.renderCandles('NVDA', '1d', snapshot.candles);
assert.ok(chart.includes('<svg'), 'candles render as an SVG chart');
assert.ok(chart.includes('Range $174.50 – $180.10'), 'chart carries its range stats');
assert.ok(chart.includes('2 candles'), 'chart states its candle count');
assert.match(panel.renderCandles('NVDA', '1m', snapshot.candles), /Candles unavailable for NVDA at the 1m interval/,
  'an interval with no candles states that explicitly');
assert.match(panel.renderCandles('AAPL', '1d', snapshot.candles), /Candles unavailable for AAPL at the 1d interval/,
  'a stock with no candles states that explicitly');
const detail = panel.renderDetail(snapshot.stocks[0], '1d', snapshot.candles);
assert.ok(detail.includes('NVDA — NVIDIA Corp'), 'detail names the stock');
assert.ok(panel.INTERVALS.every(iv => detail.includes('data-interval="' + iv + '"')),
  'detail offers every interval as a button');
assert.ok(detail.includes('Day range: $99.00 – $101.00'), 'detail shows range stats');
assert.ok(detail.includes('Volume: 1,000,000'), 'detail shows volume');
assert.ok(detail.includes('NVDA ships new chip'), 'detail shows the stock news headline');
assert.ok(detail.includes('href="https://example.com/nvda"'), 'detail links the headline');
assert.ok(detail.includes('Reuters'), 'detail names the headline source');
// A hostile title must not break out of the markup.
const hostile = panel.renderNews({ symbol: 'X', news: [{ title: '<script>alert(1)</script>', source: 'Evil',
  url: 'javascript:alert(1)', published_at: '2026-11-23T18:00:00Z' }] });
assert.doesNotMatch(hostile, /<script>/, 'news titles are escaped');
assert.doesNotMatch(hostile, /javascript:/, 'non-http news links are rejected');
assert.ok(panel.safeUrl('https://example.com/a?b=c') !== null, 'plain https links pass');
assert.equal(panel.safeUrl('https://user:pass@example.com/'), null, 'links with userinfo are rejected');

// --- collector contract shape (docs/specs/NASDAQ_SNAPSHOT.md) ---------------
// The collector publishes nested fields: index.breadth, index.market_status
// as {status, reason}, index.previous_close, and no news_count / candles
// block. The panel must read those, not the flat fixture shape above.
const collectorSnapshot = {
  as_of: FRESH_AS_OF,
  index: { symbol: 'NDX', status: 'available', level: 25431.10, percent_change: 0.42,
    percent_change_basis: 'regular-session move vs previous close',
    previous_close: 25324.50, day_low: 25210.00, day_high: 25480.00,
    market_status: { status: 'closed', reason: 'outside_hours' },
    breadth: { advancers: 62, decliners: 38, unchanged: 1 },
    top_gainers: [{ symbol: 'NVDA', percent_change: 1.83 }],
    top_losers: [{ symbol: 'MSFT', percent_change: -0.54 }] },
  stocks: [
    stock('NVDA', { name: 'NVIDIA Corp', price: 178.42, percent_change: 1.83, market_cap: 4.35e12, rank: 1,
      news: [{ title: 'NVDA ships new chip', source: 'Reuters',
        url: 'https://example.com/nvda', published_at: '2026-11-23T18:12:00Z' }] }),
    stock('AAPL', { name: 'Apple Inc', price: 262.10, percent_change: -0.54, market_cap: 3.9e12, rank: 2 }),
  ],
};
assert.deepEqual(panel.validateSnapshot(collectorSnapshot), [],
  'the collector-contract snapshot must validate');
const collectorHero = panel.renderHero(collectorSnapshot, false);
assert.ok(collectorHero.includes('62 advancers · 38 decliners'),
  'hero must read breadth from index.breadth');
assert.ok(collectorHero.includes('Market at hourly snapshot: closed (outside_hours)'),
  'hero must render market_status as status + reason, never [object Object]');
assert.ok(collectorHero.includes('25,324.50'), 'hero must read index.previous_close');
assert.ok(!collectorHero.includes('[object Object]'), 'hero must never render [object Object]');
assert.equal(panel.marketStatusText({ market_status: { status: 'open', reason: 'in_session' } }),
  'open (in_session)', 'marketStatusText formats the object shape');
assert.equal(panel.marketStatusText({ market_status: 'closed' }), 'closed',
  'marketStatusText accepts a plain string as a fallback');
// A failed NDX fetch leaves index.level null; the hero shows a dash and the
// stock rows still stand — validation must not reject the whole snapshot.
assert.deepEqual(panel.validateSnapshot(
  Object.assign({}, collectorSnapshot, { index: Object.assign({}, collectorSnapshot.index, { level: null }) })),
  [], 'a null index level (NDX source unavailable) must still validate');
assert.ok(panel.renderHero(
  Object.assign({}, collectorSnapshot, { index: Object.assign({}, collectorSnapshot.index, { level: null }) }), false)
  .includes('—'), 'a null index level renders as a dash');
// news_count is absent in the collector shape; the count comes from news.length.
assert.ok(panel.renderRows(collectorSnapshot.stocks).includes('<td class="nq-num">1</td>'),
  'row news count falls back to the news array length');

// --- on-demand candles (Yahoo Finance) --------------------------------------
function bars1m(minutes, startIso) {
  const start = Date.parse(startIso);
  const bars = [];
  for (let i = 0; i < minutes; i++) {
    bars.push({ open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i,
      volume: 1000, time: new Date(start + i * 60000).toISOString() });
  }
  return bars;
}
// 9m buckets from 09:30 ET open: 18 one-minute bars make two 9m bars.
const agg9 = panel.aggregateBars(bars1m(18, '2026-11-23T14:30:00Z'), 9);
assert.equal(agg9.length, 2, '18 one-minute bars aggregate into two 9m bars');
assert.equal(agg9[0].open, 100, 'first 9m bar opens at the first 1m open');
assert.equal(agg9[0].close, 108.5, 'first 9m bar closes at the ninth 1m close');
assert.equal(agg9[0].high, 109, 'first 9m bar high is the max of its 1m highs');
assert.equal(agg9[0].low, 99, 'first 9m bar low is the min of its 1m lows');
assert.equal(agg9[0].volume, 9000, 'first 9m bar volume sums its 1m volumes');
const sparseBars = bars1m(20, '2026-11-23T14:30:00Z').filter((_, index) => [2, 8, 9, 18].includes(index));
const sparse9 = panel.aggregateBars(sparseBars, 9);
assert.deepEqual(sparse9.map(bar => bar.time), ['2026-11-23T14:30:00.000Z', '2026-11-23T14:39:00.000Z', '2026-11-23T14:48:00.000Z']);
assert.deepEqual(sparse9.map(bar => bar.volume), [2000, 1000, 1000]);
const summerBars = bars1m(10, '2026-07-01T13:30:00Z');
assert.equal(panel.aggregateBars(summerBars, 9)[1].time, '2026-07-01T13:39:00.000Z');
assert.equal(panel.aggregateBars(bars1m(5, '2026-11-23T14:30:00Z'), 1).length, 5,
  '1m aggregation is the identity');
// A partial trailing group (live, still-forming bar) is kept.
assert.equal(panel.aggregateBars(bars1m(10, '2026-11-23T14:30:00Z'), 9).length, 2,
  'a partial trailing group is kept as the forming bar');
assert.match(panel.renderCandles('NVDA', '1m', { NVDA: { '1m': 'loading' } }), /Loading candles/,
  'a loading candle entry renders as loading, not unavailable');

// --- mount() with a fake document -------------------------------------------
function fakeEl() {
  const el = { innerHTML: '', textContent: '', hidden: false, value: '',
    dataset: {}, _attrs: {}, _listeners: {},
    setAttribute(k, v) { el._attrs[k] = v; },
    getAttribute(k) { return el._attrs[k]; },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); } };
  return el;
}
function fakeDoc(ids) {
  const els = {};
  ids.forEach(id => { els[id] = fakeEl(); });
  return { els, getElementById: id => els[id] || null };
}
const DOC_IDS = ['nasdaq-status', 'nasdaq-hero', 'nasdaq-movers', 'nasdaq-tbody', 'nasdaq-detail',
  'nasdaq-count', 'nasdaq-heatmap', 'nasdaq-changes', 'nasdaq-table-wrap',
  'nasdaq-view-heatmap', 'nasdaq-view-table'];
function clickTarget(container, attrs) {
  const target = { getAttribute: k => (k in attrs ? attrs[k] : null), parentNode: container };
  container._listeners.click.forEach(fn => fn({ target }));
}
(async () => {
  const doc = fakeDoc(DOC_IDS);
  const container = fakeEl();
  const noCandles = async () => ({ ok: false, json: async () => ({}) });
  await panel.mount(doc, container, async () => snapshot, { nowMs: NOW, fetchCandles: noCandles });
  assert.equal(container.getAttribute('data-state'), 'ready', 'a fresh snapshot mounts ready');
  assert.equal(doc.els['nasdaq-status'].getAttribute('data-state'), 'live', 'status is live for a fresh snapshot');
  assert.ok(doc.els['nasdaq-hero'].innerHTML.includes('25,431.10'), 'hero renders the index level');
  assert.ok(doc.els['nasdaq-movers'].innerHTML.includes('Top gainers'), 'movers render');
  assert.ok(doc.els['nasdaq-tbody'].innerHTML.includes('NVDA'), 'table renders the stocks');
  assert.ok(doc.els['nasdaq-tbody'].innerHTML.includes('Nasdaq latest reported move') === false,
    'table rows carry no per-row basis label; the basis lives in the hero and footnote');
  assert.equal(doc.els['nasdaq-count'].textContent, '4 of 4 stocks', 'count line renders');

  // --- heatmap is the default view -----------------------------------------
  assert.equal(doc.els['nasdaq-heatmap'].hidden, false, 'the heatmap is shown by default');
  assert.equal(doc.els['nasdaq-table-wrap'].hidden, true, 'the table is hidden by default');
  assert.equal(doc.els['nasdaq-view-heatmap'].getAttribute('aria-pressed'), 'true',
    'the Heatmap toggle is pressed by default');
  assert.equal(doc.els['nasdaq-view-table'].getAttribute('aria-pressed'), 'false',
    'the Table toggle is not pressed by default');
  assert.ok(doc.els['nasdaq-heatmap'].innerHTML.includes('data-symbol="NVDA"'),
    'the heatmap renders tiles from the same snapshot rows as the table');
  assert.ok(doc.els['nasdaq-heatmap'].innerHTML.includes('nq-legend-bar'),
    'the heatmap renders its legend');
  assert.ok(doc.els['nasdaq-changes'].innerHTML.includes('no changes'),
    'the index-changes line renders under the controls');

  // --- toggle switches views -------------------------------------------------
  clickTarget(container, { 'data-view': 'table' });
  assert.equal(doc.els['nasdaq-table-wrap'].hidden, false, 'Table view shows the table');
  assert.equal(doc.els['nasdaq-heatmap'].hidden, true, 'Table view hides the heatmap');
  assert.equal(doc.els['nasdaq-view-table'].getAttribute('aria-pressed'), 'true',
    'the Table toggle is pressed after switching');
  clickTarget(container, { 'data-view': 'heatmap' });
  assert.equal(doc.els['nasdaq-heatmap'].hidden, false, 'Heatmap view shows the heatmap again');
  assert.equal(doc.els['nasdaq-table-wrap'].hidden, true, 'Heatmap view hides the table again');

  // --- tile tap opens the same detail view as a table row -------------------
  clickTarget(container, { 'data-symbol': 'AAPL' });
  assert.equal(doc.els['nasdaq-detail'].hidden, false, 'tapping a tile opens the detail view');
  assert.ok(doc.els['nasdaq-detail'].innerHTML.includes('AAPL — Apple Inc'),
    'tile tap shows the tapped stock detail');
  assert.ok(doc.els['nasdaq-detail'].innerHTML.includes('Rank: 2 of 4'),
    'tile-tap detail ranks against the snapshot size');
  assert.ok(doc.els['nasdaq-detail'].innerHTML.includes('Candles unavailable'),
    'tile-tap detail states candle availability explicitly');
  clickTarget(container, { 'data-close': '1' });
  assert.equal(doc.els['nasdaq-detail'].hidden, true, 'closing the detail hides it again');

  // Stale snapshot: same table, but the stale state is explicit everywhere.
  const staleDoc = fakeDoc(DOC_IDS);
  const staleContainer = fakeEl();
  await panel.mount(staleDoc, staleContainer,
    async () => Object.assign({}, snapshot, { as_of: STALE_AS_OF }), { nowMs: NOW });
  assert.equal(staleContainer.getAttribute('data-state'), 'stale', 'an old snapshot mounts stale');
  assert.equal(staleDoc.els['nasdaq-status'].getAttribute('data-state'), 'stale', 'status is stale');
  assert.match(staleDoc.els['nasdaq-status'].textContent, /STALE/, 'status text says STALE');
  assert.match(staleDoc.els['nasdaq-status'].textContent, /2026-11-23 18:00:00 UTC/, 'status text carries the as-of time');
  assert.ok(staleDoc.els['nasdaq-tbody'].innerHTML.includes('NVDA'),
    'a stale snapshot still shows its last known table, labelled stale');

  // Source unavailable (404): explicit absent state, no numbers shown.
  const absentDoc = fakeDoc(DOC_IDS);
  const absentContainer = fakeEl();
  const err404 = new Error('HTTP 404'); err404.status = 404;
  await panel.mount(absentDoc, absentContainer, async () => { throw err404; }, { nowMs: NOW });
  assert.equal(absentContainer.getAttribute('data-state'), 'absent', 'a 404 mounts absent');
  assert.match(absentDoc.els['nasdaq-status'].textContent, /not published yet/,
    'a 404 says the snapshot is not published yet');
  assert.equal(absentDoc.els['nasdaq-hero'].innerHTML, '', 'no hero renders when the source is absent');

  // Invalid snapshot: visible failure, not a half table.
  const badDoc = fakeDoc(DOC_IDS);
  const badContainer = fakeEl();
  await panel.mount(badDoc, badContainer, async () => ({ as_of: FRESH_AS_OF }), { nowMs: NOW });
  assert.equal(badContainer.getAttribute('data-state'), 'failed', 'an invalid snapshot mounts failed');
  assert.match(badDoc.els['nasdaq-status'].textContent, /Could not render/, 'failure is stated');

  // --- on-demand candles through mount --------------------------------------
  // Stub Yahoo chart JSON: three 1m bars on 2026-11-23.
  function yahooJson() {
    return { chart: { result: [{ timestamp: [1795444200, 1795444260, 1795444320],
      indicators: { quote: [{ open: [100, 101, 102], high: [101, 102, 103],
        low: [99, 100, 101], close: [100.5, 101.5, 102.5], volume: [1000, 2000, 3000] }] } }] } };
  }
  const stubFetch = async (url) => {
    assert.ok(url.startsWith('https://query1.finance.yahoo.com/v8/finance/chart/'),
      'candle fetch goes to the Yahoo chart API');
    return { ok: true, json: async () => yahooJson() };
  };
  const bars3m = await panel.yahooBars('NVDA', '3m', stubFetch);
  assert.equal(bars3m.length, 1, 'three 1m bars aggregate into one 3m bar');
  assert.equal(bars3m[0].open, 100, 'aggregated 3m bar opens at the first 1m open');
  assert.equal(bars3m[0].close, 102.5, 'aggregated 3m bar closes at the last 1m close');
  assert.equal(bars3m[0].volume, 6000, 'aggregated 3m bar volume sums');
  const bars1mFetched = await panel.yahooBars('NVDA', '1m', stubFetch);
  assert.equal(bars1mFetched.length, 3, '1m interval returns the raw 1m bars');
  const barsFail = await panel.yahooBars('NVDA', '1d', async () => { throw new Error('down'); });
  assert.deepEqual(barsFail, [], 'a failed candle fetch resolves to an empty list, never throws');
  const cache = {};
  const loaded = await panel.loadCandles('NVDA', '3m', stubFetch, cache);
  assert.equal(loaded.length, 1, 'loadCandles returns the fetched bars');
  assert.equal(cache['NVDA|3m'].length, 1, 'loadCandles caches by symbol|interval');
  assert.match(panel.renderCandles('NVDA', '3m', cache), /<svg/, 'fetched cache renders a chart');
  let fetchCount = 0;
  await panel.loadCandles('NVDA', '3m', async () => { fetchCount++; return stubFetch('x'); }, cache);
  assert.equal(fetchCount, 0, 'a cached interval is not re-fetched');

  // --- site wiring ----------------------------------------------------------
  const panelHash = createHash('sha256').update(panelSource).digest('hex').slice(0, 16);
  assert.ok(html.includes('<script src="js/nasdaq-panel.js?v=' + panelHash + '"></script>'),
    'nasdaq-panel script URL must match its content hash on every hosting path');
  assert.equal(html.split('<script src="js/nasdaq-panel.js').length, 2, 'exactly one nasdaq-panel script tag');
  assert.ok(html.includes('<button id="tab-nasdaq" class="tab-btn">Stocks</button>'), 'Stocks tab button exists');
  assert.ok(html.includes('<div id="nasdaq-panel" class="hidden"'), 'the Stocks panel starts hidden');
  assert.ok(html.includes('id="nasdaq-tbody"'), 'the panel has a stock table body');
  assert.ok(html.includes('id="nasdaq-search"'), 'the panel has a search box');
  assert.ok(html.includes('id="nasdaq-detail"'), 'the panel has a detail section');
  assert.ok(html.includes('id="nasdaq-heatmap"'), 'the panel has a heatmap container');
  assert.ok(html.includes('id="nasdaq-changes"'), 'the panel has an index-changes line');
  assert.ok(html.includes('id="nasdaq-view-heatmap"'), 'the panel has a Heatmap toggle button');
  assert.ok(html.includes('id="nasdaq-view-table"'), 'the panel has a Table toggle button');
  assert.ok(html.includes('data-view="heatmap"') && html.includes('data-view="table"'),
    'the toggle buttons carry their view names');
  assert.ok(html.includes('id="nasdaq-table-wrap" hidden'), 'the table starts hidden; the heatmap is the default view');
  assert.ok(panelSource.includes('HEATMAP_FULL_PCT'), 'the full-intensity % constant must be in the panel source');
  assert.ok(panelSource.includes('renderHeatmap'), 'the panel source must define the heatmap renderer');
  assert.ok(panelSource.includes('renderIndexChanges'), 'the panel source must define the index-changes renderer');
  assert.ok(/#nasdaq-panel\.hidden \{ display: none; \}/.test(html) ||
    html.includes('#nasdaq-panel.hidden'), 'the hidden rule covers the new panel');
  assert.ok(tabSwitching.includes("nasdaq: ['nasdaq-panel',  'tab-nasdaq']"),
    'tab-switching maps the nasdaq tab');
  assert.ok(tabSwitching.includes('loadNasdaqPanel'), 'tab-switching mounts the panel on first open');
  assert.ok(tabSwitching.includes('nasdaqLoaded'), 'tab-switching mounts the panel only once');
  const order = s => html.indexOf(s);
  assert.ok(order('js/news-panel.js?v=') < order('js/nasdaq-panel.js?v=') &&
    order('js/nasdaq-panel.js?v=') < order('js/tab-switching.js?v='),
    'nasdaq-panel loads after news-panel and before tab-switching');
  console.log('PASS nasdaq panel: intervals, sort/search, % labelling, stale/absent/failed states, candles, escaping, heatmap + toggle + tile tap, index changes, site wiring');
})().catch(e => { console.error(e); process.exitCode = 1; });
