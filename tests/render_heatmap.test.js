// B2 (readability quest): the render-heatmap section moved out of index.html
// into site/js/render-heatmap.js, loaded in the same position. The test
// loads the shipped module instead of slicing the page by banner text, pins
// the ?v= hash in the page to the module's content (like shared.js), and
// drives renderHeatmap with stubbed DOM + page globals so the QQQ /
// non-QQQ / no-spot paths are verified without the network. The real
// buildHeatmapRows is covered by the shared.js suites and by the assembly
// check, which runs the extracted module in the assembled page scope.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/render-heatmap.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/render-heatmap.js?v=' + moduleHash), 'render-heatmap script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function renderHeatmap(data, symbol'), 'renderHeatmap must live in js/render-heatmap.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── render heatmap (live panel)'), 'render heatmap banner must stay in index.html as the section marker');

// --- harness: the page's shared scope, as the module sees it. ---
const RANGES = { '0DTE_Regular': { 0: { call: [1, 2, 3, 4], put: [1, 2, 3, 4] } } };
function makeEl() {
  return {
    textContent: '', style: {}, dataset: {}, classList: { added: [], removed: [],
      add(c) { this.added.push(c); }, remove(c) { this.removed.push(c); } },
  };
}
const els = {};
const calls = { setStatus: [], clearChain: [], buildRows: [], openTicket: [], relative: [] };
// The real #178 chain-snapshot formatter: the header line this test checks is
// the one the page will render, not a stub of it.
const sharedFormat = require('../site/shared/format.js');
const sandbox = {
  console,
  TIER_COLORS: { '0DTE_Regular': '#ff0000' },
  setStatus: (state, ts) => { calls.setStatus.push([state, ts]); },
  clearChain: msg => { calls.clearChain.push(msg); },
  computeBook: () => ({ QQQ260918C00600000: { pos: 2 } }),
  buildHeatmapRows: (tbody, data, ranges, opts) => { calls.buildRows.push({ tbody, data, ranges, opts }); return true; },
  relativeOiRanges: rows => { calls.relative.push(rows); return 'RELATIVE-RANGES'; },
  liveQuotes: { get: sym => ({ bid: 1.1, ask: 1.2, sym }) },
  openTicket: (e, row) => { calls.openTicket.push([e, row]); },
  formatVwapRvol: () => ({ text: 'VWAP 600.50 · RVOL 1.20', state: 'live' }),
  formatMomentum: () => ({ text: 'MOM +0.40', state: 'up' }),
  formatChainSnapshot: sharedFormat.formatChainSnapshot,
  document: { getElementById: id => (els[id] ||= makeEl()) },
};
const context = vm.createContext(sandbox);
// `ranges` is declared in the page's shared scope (just above the loader
// section), before this module loads.
vm.runInContext('let ranges = null;', context);
vm.runInContext('ranges = ' + JSON.stringify(RANGES) + ';', context);
vm.runInContext(moduleSource, context, { filename: 'site/js/render-heatmap.js' });
const get = name => vm.runInContext(name, context);

assert.equal(typeof get('renderHeatmap'), 'function', 'module must define renderHeatmap');

const reset = () => {
  for (const k of Object.keys(els)) delete els[k];
  for (const k of Object.keys(calls)) calls[k] = [];
  vm.runInContext('ranges = ' + JSON.stringify(RANGES) + ';', context);
};
const qqqData = {
  underlying_price: 600.1, tier: '0DTE_Regular', snapshot_time: '16:14 ET',
  timestamp: '11/7/2026 13:14:45',
  underlying_market: {}, rows: [{ Strike: 600, Type: 'call', OpenInterest: 100 }],
};
// The producer's own ET stamp, so the header's session/age line is exercised
// with a real payload shape instead of an ISO string the page never receives.
const SNAP_MS = new Date(2026, 10, 7, 13, 14, 45).getTime();

// QQQ with a spot: rows built from the OIranges calibration, ticket clicks
// wired, header + VWAP/RVOL + momentum written, no-data hidden.
reset();
vm.runInContext('renderHeatmap(' + JSON.stringify(qqqData) + ", 'QQQ')", context);
assert.equal(calls.buildRows.length, 1, 'QQQ render builds the heatmap once');
assert.equal(calls.buildRows[0].tbody, els['heatmap-body'], 'rows go into #heatmap-body');
assert.equal(JSON.stringify(calls.buildRows[0].ranges), JSON.stringify(RANGES), 'QQQ render uses the OIranges calibration');
assert.equal(calls.buildRows[0].opts.getPosition('QQQ260918C00600000'), 2, 'QQQ positions come from computeBook');
assert.deepEqual(calls.buildRows[0].opts.getQuote('QQQ260918C00600000'), { bid: 1.1, ask: 1.2, sym: 'QQQ260918C00600000' }, 'QQQ quotes come from liveQuotes');
assert.equal(typeof calls.buildRows[0].opts.onCellClick, 'function', 'QQQ cells open tickets');
calls.buildRows[0].opts.onCellClick({ type: 'click' }, { Strike: 600 });
assert.equal(calls.openTicket.length, 1, 'a QQQ cell click opens a ticket');
assert.equal(els['chain-note'].textContent, '', 'QQQ has no view-only note');
assert.equal(els['price'].textContent, '$600.10', 'header shows the spot');
// #178: the header now says which snapshot the price came from, its session
// and its age — and marks the value retained once collection has stopped.
assert.equal(els['price'].dataset.state, 'retained', 'a snapshot from hours ago is not presented as current');
assert.match(els['price'].title, /historical snapshot taken at 16:14 ET during the after hours/);
assert.equal(els['price-age'].dataset.state, 'retained');
assert.match(els['price-age'].textContent,
  /^snapshot 16:14 ET · After hours · .+ · retained$/,
  'the header line carries the snapshot time, its session, its age and the retained marker');
assert.equal(els['tier-badge'].textContent, 'Regular tier', 'the tier badge says it is a tier, not a session');
assert.equal(els['tier-badge'].title, 'Expiration tier 0DTE_Regular — not a market session');
assert.equal(els['tier-badge'].style.color, '#ff0000', 'tier badge takes the tier color');
assert.equal(els['tier-badge'].style.display, '', 'tier badge is shown');
assert.equal(els['vwap-rvol'].textContent, 'VWAP 600.50 · RVOL 1.20', 'VWAP/RVOL line written');
assert.equal(els['vwap-rvol'].dataset.state, 'live', 'VWAP/RVOL state written');
assert.equal(els['momentum'].textContent, 'MOM +0.40', 'momentum line written');
assert.equal(els['momentum'].dataset.state, 'up', 'momentum state written');
assert.ok(els['no-data'].classList.added.includes('hidden'), 'no-data banner hidden on a good render');
assert.ok(els['heatmap-wrap'].classList.removed.includes('hidden'), 'heatmap shown on a good render');
assert.equal(calls.setStatus.length, 0, 'no stale marking on a good render');

// QQQ without a spot: marked stale, nothing built, chain left as-is.
reset();
vm.runInContext('renderHeatmap(' + JSON.stringify({ ...qqqData, underlying_price: null }) + ", 'QQQ')", context);
assert.deepEqual(calls.setStatus, [['stale', '16:14 ET']], 'a missing spot marks the page stale');
assert.equal(calls.buildRows.length, 0, 'no rows built without a spot');
assert.equal(calls.clearChain.length, 0, 'a QQQ chain without a spot is not cleared');

// Non-QQQ without a spot: marked stale and the chain cleared, never filled
// with another underlying's price.
reset();
vm.runInContext('renderHeatmap(' + JSON.stringify({ ...qqqData, underlying_price: null }) + ", 'SPY')", context);
assert.deepEqual(calls.setStatus, [['stale', '16:14 ET']], 'a missing SPY spot marks the page stale');
assert.deepEqual(calls.clearChain, ['SPY spot price unavailable in the latest snapshot.'], 'a non-QQQ chain without a spot is cleared');
assert.equal(calls.buildRows.length, 0, 'no rows built without a spot');

// Non-QQQ with a spot: snapshot-relative colors, real positions from the
// book, and ticket clicks — execution itself is gated by the ticket's fresh
// quote check, not by hiding the ticket.
reset();
vm.runInContext('renderHeatmap(' + JSON.stringify(qqqData) + ", 'SPY')", context);
assert.equal(calls.buildRows.length, 1, 'SPY render builds the heatmap once');
assert.equal(calls.buildRows[0].ranges, 'RELATIVE-RANGES', 'non-QQQ render uses snapshot-relative ranges');
assert.equal(JSON.stringify(calls.relative), JSON.stringify([qqqData.rows]), 'relativeOiRanges gets the snapshot rows');
assert.equal(calls.buildRows[0].opts.getPosition('SPY260918C00600000'), 0, 'non-QQQ positions come from the book (0 when none held)');
assert.equal(typeof calls.buildRows[0].opts.onCellClick, 'function', 'non-QQQ cells open tickets');
calls.buildRows[0].opts.onCellClick({ type: 'click' }, { Strike: 600 });
assert.equal(calls.openTicket.length, 1, 'a SPY cell click opens a ticket');
assert.equal(els['chain-note'].textContent, 'OI colors relative to this snapshot · execution requires a fresh contract quote', 'non-QQQ note names the fresh-quote gate');
assert.equal(els['price'].textContent, '$600.10', 'header still shows the spot');

console.log('render heatmap: hash pinned, extraction verified, QQQ + SPY + no-spot paths passed');
