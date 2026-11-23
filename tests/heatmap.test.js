// B3 slice 2 (Nov 2026): DISPLAY / TIER_COLORS, the heatmap cell formatters
// (fmtOI / fmtVol / fmtQ / quoteHtml / flowLevel / flowHtml),
// computeAtmWindow and buildHeatmapRows moved verbatim out of site/shared.js
// into site/shared/heatmap.js. Pins the module's ?v= hash in the page,
// asserts the ten names no longer live in shared.js, pins the entry's export
// surface (computeAtmWindow is still re-exported — it was always one of the
// 26 names — while the other nine stay browser globals by design), and drives
// the moved code directly: the formatters and the ATM window under Node, and
// buildHeatmapRows in a vm context with stubbed DOM, loaded in page order
// (csv-ranges.js first, so getThresh / oiBucket are the globals it reads).
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'heatmap.js'), 'utf8');
const heatmap = require('../site/shared/heatmap.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/heatmap.js?v=' + moduleHash),
  'heatmap script URL must match its content hash on every hosting path');

for (const name of ['fmtOI', 'fmtVol', 'fmtQ', 'quoteHtml', 'flowLevel', 'flowHtml', 'computeAtmWindow', 'buildHeatmapRows']) {
  assert.ok(!shared.includes(`function ${name}(`), `${name} must no longer live in shared.js`);
  assert.equal(typeof heatmap[name], 'function', `heatmap.js must export ${name}`);
}
for (const decl of ['const DISPLAY = 20;', 'const TIER_COLORS = {']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
}
assert.equal(heatmap.DISPLAY, 20, 'DISPLAY is the ±20 strike window');
assert.deepEqual(heatmap.TIER_COLORS, { '0DTE_Regular': '#4a90d9', '0DTE_Weekly': '#e8c84b', '0DTE_Monthly': '#e8604b' });

// The entry's export surface is unchanged by the split: exactly the same 26
// names, with computeAtmWindow re-exported from the new home — the same
// function object, not a copy — and the other nine names still browser-only.
assert.equal(typeof entry.computeAtmWindow, 'function', 'the entry must still export computeAtmWindow');
assert.equal(entry.computeAtmWindow, heatmap.computeAtmWindow, 'the entry re-exports the moved function itself');
for (const name of ['DISPLAY', 'TIER_COLORS', 'fmtOI', 'fmtVol', 'fmtQ', 'quoteHtml', 'flowLevel', 'flowHtml', 'buildHeatmapRows']) {
  assert.equal(entry[name], undefined, `${name} stays a browser global — the entry must not re-export it`);
}

// --- cell formatters: the same values the live table renders. ---
assert.equal(heatmap.fmtOI(0), '');
assert.equal(heatmap.fmtOI(999), '999');
assert.equal(heatmap.fmtOI(5432), '5.4K');
assert.equal(heatmap.fmtOI(12345), '12K');
assert.equal(heatmap.fmtVol(0), '');
assert.equal(heatmap.fmtVol(999), '999');
assert.equal(heatmap.fmtVol(5432), '5.4k');
assert.equal(heatmap.fmtVol(12345), '12k');
assert.equal(heatmap.fmtQ(null), '');
assert.equal(heatmap.fmtQ(1.234), '1.23');
assert.equal(heatmap.fmtQ(12.34), '12.3');
assert.equal(heatmap.quoteHtml(null, null), '');
assert.equal(heatmap.quoteHtml(1.1, 1.2), '<span class="quote">1.10 × 1.20</span>');
assert.equal(heatmap.flowLevel(0), 0);
assert.equal(heatmap.flowLevel(19), 1);
assert.equal(heatmap.flowLevel(99), 2);
assert.equal(heatmap.flowLevel(499), 3);
assert.equal(heatmap.flowLevel(500), 4);
assert.equal(heatmap.flowHtml(0), '');
assert.ok(heatmap.flowHtml(10).includes('flow-1'), 'a small delta is a level-1 flow chip');
assert.ok(heatmap.flowHtml(50).includes('flow-2'), 'a mid delta is a level-2 flow chip');
assert.ok(heatmap.flowHtml(2500).includes('+2.5k'), 'a large delta is abbreviated');

// --- computeAtmWindow: the ATM strike + the ±DISPLAY ladder window. ---
{
  // A sparse ladder: only the strikes the snapshot published exist, so the
  // window is those strikes, not 41 synthesized `atm + offset` values (#219).
  const rows = [{ Strike: 598 }, { Strike: 600 }, { Strike: 602 }];
  const w = heatmap.computeAtmWindow(rows, 600.5);
  assert.equal(w.atm, 600, 'ATM is the nearest PUBLISHED strike');
  assert.equal(w.atmIndex, 1);
  assert.deepEqual(w.offsets, [1, 0, -1], 'offsets are ladder steps, highest strike first, clipped to the ladder');
  assert.deepEqual(w.window.map(e => e.strike), [602, 600, 598]);
  assert.deepEqual([...w.strikes].sort((a, b) => a - b), [598, 600, 602], 'the window never invents a strike');
  assert.equal(w.strikes.has(580), false, 'no synthesized strike outside the published ladder');
  assert.deepEqual(heatmap.computeAtmWindow(rows, null),
    { atm: null, atmIndex: -1, offsets: [], strikes: new Set(), window: [] });
}

// --- #219: IBIT's mixed ladder — the exact strikes the November 10 snapshot
// published (whole dollars 30–39 and 51–59, half dollars 39.5–50). A synthetic
// `atm + offset` grid centered on 41.5 invented 30 strikes that were never
// published, so those cells rendered empty and the quote request asked for
// contracts that don't exist. ---
const IBIT_LADDER = [30, 31, 32, 33, 34, 35, 36, 37, 38, 39,
  39.5, 40, 40.5, 41, 41.5, 42, 42.5, 43, 43.5, 44, 44.5, 45, 45.5, 46,
  46.5, 47, 47.5, 48, 48.5, 49, 49.5, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59];
const ibitRows = IBIT_LADDER.flatMap(strike => ['call', 'put'].map(Type => ({
  Strike: strike,
  Type,
  OpenInterest: Type === 'call' ? 120 : 90,
  Volume: 11,
  VolDelta: 3,
  Bid: 0.45,
  Ask: 0.48,
  OptionSymbol: `IBIT261110${Type === 'call' ? 'C' : 'P'}${String(Math.round(strike * 1000)).padStart(8, '0')}`,
  Expiration: '2026-11-10',
})));
{
  const w = heatmap.computeAtmWindow(ibitRows, 41.45);
  assert.equal(w.atm, 41.5, 'ATM is the published 41.5 strike nearest the 41.45 spot');
  assert.ok([...w.strikes].every(s => IBIT_LADDER.includes(s)), 'every window strike was published');
  assert.equal(w.window.length, IBIT_LADDER.length - 6,
    'the window is clipped to the ladder (ATM is 14 steps above the lowest published strike)');
  assert.equal(w.offsets[0], 20, 'the highest strike the ladder can supply sits +20 steps up');
  assert.equal(w.offsets.at(-1), -14, 'below ATM the ladder runs out before -20');
  assert.equal(w.strikes.has(61.5), false, 'no 61.5 row: IBIT never published that strike');
  assert.equal(w.strikes.has(41.5), true);
  // The old grid, kept here as the regression witness: 41 values, 30 of them
  // absent from the published ladder — exactly the empty cells in the report.
  const synthetic = [];
  for (let o = 20; o >= -20; o--) synthetic.push(41.5 + o);
  assert.equal(synthetic.filter(s => !IBIT_LADDER.includes(s)).length, 30,
    'the retired atm+offset grid invented 30 of its 41 strikes');
}

// --- buildHeatmapRows: renders the same 41 rows through the real parsers. ---
function makeEl() {
  const el = {
    textContent: '', style: {}, dataset: {}, children: [],
    className: '', title: '', onclick: null,
    classList: { added: [], removed: [],
      add(c) { this.added.push(c); }, remove(c) { this.removed.push(c); } },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
  };
  let _innerHTML = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => _innerHTML,
    set: v => { _innerHTML = String(v); el.children.length = 0; },
    enumerable: true, configurable: true,
  });
  return el;
}
const rangesCsv = (() => {
  const lines = ['Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90'];
  for (let o = -20; o <= 20; o++) {
    const base = 1000 + Math.abs(o) * 10;
    lines.push(`0DTE_Regular,${o},${base * 0.4},${base * 0.6},${base * 0.8},${base},${base * 0.4},${base * 0.6},${base * 0.8},${base}`);
  }
  return lines.join('\n');
})();
const ctx = vm.createContext({
  console,
  document: { createElement: () => makeEl() },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'csv-ranges.js'), 'utf8'), ctx, { filename: 'site/shared/csv-ranges.js' });
vm.runInContext(moduleSource, ctx, { filename: 'site/shared/heatmap.js' });
const get = name => vm.runInContext(name, ctx);
assert.equal(get('typeof buildHeatmapRows'), 'function', 'the module must define buildHeatmapRows in the page scope');
assert.equal(get('typeof getThresh'), 'function', 'csv-ranges.js must load first so the module reads its globals');
vm.runInContext(`ranges = parseRangesCsv(${JSON.stringify(rangesCsv)});`, ctx);
const rows = [];
for (let s = 580; s <= 620; s++) {
  rows.push({ Strike: s, Type: 'call', OpenInterest: 1000, Volume: 50, VolDelta: 10, Bid: 1.1, Ask: 1.2, OptionSymbol: `QQQ261006C00${s}000` });
  rows.push({ Strike: s, Type: 'put', OpenInterest: 800, Volume: 40, VolDelta: 5, Bid: 1.0, Ask: 1.1, OptionSymbol: `QQQ261006P00${s}000` });
}
vm.runInContext('tbody = document.createElement("tbody");', ctx);
const ok = vm.runInContext(`buildHeatmapRows(tbody, ${JSON.stringify({ rows, underlying_price: 600, tier: '0DTE_Regular' })}, ranges)`, ctx);
assert.equal(ok, true, 'a usable spot + rows renders');
assert.equal(vm.runInContext('tbody.children.length', ctx), 41, 'one row per strike in the ±20 window');
assert.ok(vm.runInContext('tbody.children[20].classList.added.includes("atm-row")', ctx), 'the middle row is the ATM row');
assert.ok(vm.runInContext('tbody.children[20].children[1].textContent', ctx).includes('600 ★'), 'the ATM strike cell is marked');
// --- #219: the same renderer on IBIT's mixed ladder fills every cell. ---
vm.runInContext('ibitBody = document.createElement("tbody");', ctx);
const ibitOk = vm.runInContext(
  `buildHeatmapRows(ibitBody, ${JSON.stringify({ rows: ibitRows, underlying_price: 41.45, tier: '0DTE_Regular' })}, ranges)`, ctx);
assert.equal(ibitOk, true, 'the IBIT snapshot renders');
const ibitStrikeCells = vm.runInContext('ibitBody.children.map(tr => tr.children[1].textContent)', ctx);
const ibitCallCells = vm.runInContext('ibitBody.children.map(tr => tr.children[0].innerHTML)', ctx);
const ibitPutCells = vm.runInContext('ibitBody.children.map(tr => tr.children[2].innerHTML)', ctx);
assert.equal(ibitStrikeCells.length, 35, 'one row per published strike in the window, no phantom rows');
assert.deepEqual(ibitStrikeCells.map(t => parseFloat(t)),
  [53, 52, 51, 50, 49.5, 49, 48.5, 48, 47.5, 47, 46.5, 46, 45.5, 45,
    44.5, 44, 43.5, 43, 42.5, 42, 41.5, 41, 40.5, 40, 39.5, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30],
  'the rendered strikes are exactly the published ladder window, highest first');
assert.ok(ibitStrikeCells.some(t => t.includes('41.5 ★')), '41.5 is the ATM row');
for (let i = 0; i < ibitCallCells.length; i++) {
  assert.ok(ibitCallCells[i].length > 0, `call cell ${ibitStrikeCells[i]} is populated`);
  assert.ok(ibitPutCells[i].length > 0, `put cell ${ibitStrikeCells[i]} is populated`);
  assert.ok(ibitCallCells[i].includes('120'), `call cell ${ibitStrikeCells[i]} carries its published OI`);
  assert.ok(ibitPutCells[i].includes('90'), `put cell ${ibitStrikeCells[i]} carries its published OI`);
}
assert.equal(new Set(ibitStrikeCells).size, ibitStrikeCells.length, 'no strike is rendered twice');
assert.equal(vm.runInContext('buildHeatmapRows(tbody, { rows: [], underlying_price: 600 }, ranges)', ctx), false, 'no rows renders nothing');
assert.equal(vm.runInContext(`buildHeatmapRows(tbody, ${JSON.stringify({ rows, underlying_price: null })}, ranges)`, ctx), false, 'no spot renders nothing');
console.log('heatmap: hash pinned, formatters, ladder window (#219 IBIT mixed ladder + no phantom strikes) and populated cells passed');
