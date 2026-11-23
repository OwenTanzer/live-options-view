// B2 (readability quest): the historical panel moved out of index.html
// into site/js/historical-panel.js, loaded in the same position. The test
// loads the shipped module instead of slicing the page by banner text, pins
// the ?v= hash in the page to the module's content (like shared.js), and
// drives the module's functions with stubbed DOM + fetch so the tier
// classification, archive validation, chain fetch (including the legacy
// opex fallback), manifest load and heatmap render are verified without the
// network. The real buildHeatmapRows runs here (shared/heatmap.js loaded
// whole, ranges parsed from an inline CSV through the real parseRangesCsv);
// the matrix stays in tests/historical_archive.test.js, which now loads this
// module too. renderHistWidgets is stubbed here — its real implementation
// is covered by tests/sidebar_widgets.test.js + the assembly check.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '../site/shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/historical-panel.js'), 'utf8');
const rangesCsv = (() => {
  const lines = ['Tier,StrikeOffset,n_obs,Call_median,Call_p25,Call_p50,Call_p75,Call_p90,Call_adj,Put_median,Put_p25,Put_p50,Put_p75,Put_p90,Put_adj'];
  for (let o = -20; o <= 20; o++) {
    const base = 1000 + Math.abs(o) * 10;
    lines.push(`0DTE_Regular,${o},97,${base},${base * 0.4},${base * 0.6},${base * 0.8},${base},1.0,${base},${base * 0.4},${base * 0.6},${base * 0.8},${base},1.0`);
  }
  return lines.join('\n');
})();
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/historical-panel.js?v=' + moduleHash), 'historical-panel script URL must match its content hash on every hosting path');
for (const fn of ['loadHistoryManifest', 'fetchChainCsv', 'loadHistoryDate', 'renderHistoryHeatmap',
  'classifyTierSimple', 'validArchiveDate', 'setHistStatus', 'resetHistoryView', 'normalizeHistRow']) {
  assert.ok(!html.includes(`function ${fn}(`), `${fn} must live in js/historical-panel.js, not inline in index.html`);
}
// The banner stays in index.html as the section's marker. Nothing slices
// up to it anymore — bots_panel and bots_dates_browser load the bots module
// (B2 slice 13) instead of slicing the page by banner text.
assert.ok(html.includes('// ── historical panel'), 'historical panel banner must stay in index.html as the section marker');

// --- harness: the page's shared scope, as the module sees it. ---
function makeEl() {
  const el = {
    textContent: '', value: '', disabled: false, style: {}, dataset: {}, children: [],
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
const chainCsv = (() => {
  const lines = ['Expiration,Type,Strike,UnderlyingPrice,OpenInterest,Volume,DTE'];
  for (let s = 580; s <= 620; s++) {
    lines.push(`2026-09-23,call,${s},600,1000,50,0`);
    lines.push(`2026-09-23,put,${s},600,800,40,0`);
  }
  return lines.join('\n');
})();
function harness() {
  const els = {};
  const calls = { hideSimilarPopup: 0, widgets: [], fetch: [] };
  const routes = {};
  const fetchStub = async url => {
    calls.fetch.push(String(url));
    const u = String(url);
    for (const [key, value] of Object.entries(routes)) {
      if (u.includes(key)) {
        if (value instanceof Error) throw value;
        return new Response(typeof value === 'string' ? value : JSON.stringify(value),
          { status: typeof value === 'number' ? value : value === undefined ? 404 : 200 });
      }
    }
    return new Response('', { status: 404 });
  };
  const ctx = vm.createContext({
    console,
    BROWSER_DATA: '/browser-data',
    historyLoaded: false,
    hideSimilarPopup: () => { calls.hideSimilarPopup++; },
    renderHistWidgets: (...args) => { calls.widgets.push(args); },
    fetch: fetchStub,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: {
      getElementById: id => (els[id] ||= makeEl()),
      createElement: () => makeEl(),
    },
  });
  // shared.js + shared/csv-ranges.js + shared/heatmap.js whole, so parseCsv /
  // parseRangesCsv / buildHeatmapRows / TIER_COLORS are the real ones; ranges
  // parsed from the inline CSV above. B3 slice 1 (Nov 2026): parseCsv /
  // parseRangesCsv moved to site/shared/csv-ranges.js — load the shipped
  // module too, in page order. B3 slice 2 (Nov 2026): buildHeatmapRows /
  // TIER_COLORS moved to site/shared/heatmap.js — same, after csv-ranges.js.
  vm.runInContext(shared, ctx, { filename: 'site/shared.js' });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/csv-ranges.js'), 'utf8'), ctx, { filename: 'site/shared/csv-ranges.js' });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/heatmap.js'), 'utf8'), ctx, { filename: 'site/shared/heatmap.js' });
  vm.runInContext(`ranges = parseRangesCsv(${JSON.stringify(rangesCsv)});`, ctx);
  vm.runInContext(moduleSource, ctx, { filename: 'site/js/historical-panel.js' });
  return { ctx, els, calls, routes };
}
const get = (ctx, name) => vm.runInContext(name, ctx);
const rowsFor = (date, spot) => {
  const rows = [];
  for (let s = 580; s <= 620; s++) {
    rows.push({ Expiration: date, Type: 'call', Strike: s, UnderlyingPrice: spot, OpenInterest: 1000, Volume: 50, DTE: 0 });
    rows.push({ Expiration: date, Type: 'put', Strike: s, UnderlyingPrice: spot, OpenInterest: 800, Volume: 40, DTE: 0 });
  }
  return rows;
};

// Session strip (CDT pass): one cell per weekday from the first archived date
// to the newest, grouped by month; weekends never appear; a weekday without
// an archive is a gap, never dropped and never filled; duplicates collapse.
{
  const { ctx, els } = harness();
  const months = JSON.parse(vm.runInContext(
    "JSON.stringify(buildHistStripModel(['2026-09-30','2026-09-28','2026-10-02','2026-09-28']))", ctx));
  assert.deepEqual(months.map(m => m.label), ['Sep 2026', 'Oct 2026']);
  assert.deepEqual(months[0].days, [{ date: '2026-09-28', archived: true }, { date: '2026-09-29', archived: false },
    { date: '2026-09-30', archived: true }]);
  assert.deepEqual(months[1].days.map(d => [d.date, d.archived]), [['2026-10-01', false], ['2026-10-02', true]]);
  assert.equal(months[0].archived, 2);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(buildHistStripModel([]))', ctx)), []);
  vm.runInContext("histManifestDates = ['2026-09-28','2026-09-30']; renderHistStrip('2026-09-30')", ctx);
  const strip = els['hist-strip'].innerHTML;
  assert.match(strip, /data-date="2026-09-30"[^>]*aria-current="date"/);
  assert.match(strip, /hs-gap" title="2026-09-29: no archive \(a market holiday or a missing capture\)"/);
  assert.equal(els['hist-prev'].disabled, false);
  assert.equal(els['hist-next'].disabled, true);
}

// Tier from the expiration date alone (Friday = weekly, 3rd Friday = monthly
// opex): 2026-09-18 is the 3rd Friday of September, 2026-09-25 a plain
// Friday, 2026-09-23 a Wednesday; garbage is Regular, not an exception.
{
  const { ctx } = harness();
  assert.equal(vm.runInContext("classifyTierSimple('2026-09-18')", ctx), '0DTE_Monthly');
  assert.equal(vm.runInContext("classifyTierSimple('2026-09-25')", ctx), '0DTE_Weekly');
  assert.equal(vm.runInContext("classifyTierSimple('2026-09-23')", ctx), '0DTE_Regular');
  assert.equal(vm.runInContext("classifyTierSimple('not-a-date')", ctx), '0DTE_Regular');
}

// Archive-date validation: strict YYYY-MM-DD plus the ISO round-trip, so
// 2026-02-30 and non-strings are rejected (the fetch path must never be
// handed a traversal string).
{
  const { ctx } = harness();
  assert.equal(vm.runInContext("validArchiveDate('2026-09-23')", ctx), true);
  assert.equal(vm.runInContext("validArchiveDate('2026-02-30')", ctx), false);
  assert.equal(vm.runInContext("validArchiveDate('2026-9-23')", ctx), false);
  assert.equal(vm.runInContext('validArchiveDate(42)', ctx), false);
  assert.equal(vm.runInContext("validArchiveDate('')", ctx), false);
}

// Staleness math (Q1, issue #148): weekdays after the newest date up to
// today; stale past 5 trading days. 2026-09-18 is a Friday, 2026-11-08 a
// Sunday: Sep 21–25, Sep 28–Oct 2, Oct 5–9, Oct 12–16, Oct 19–23,
// Oct 26–30, Nov 2–6 = 35 weekdays.
{
  const { ctx } = harness();
  const days = d => vm.runInContext(`tradingDaysSince('2026-09-18', new Date('${d}T12:00:00Z'))`, ctx);
  assert.equal(days('2026-09-18'), 0, 'same day is not stale');
  assert.equal(days('2026-09-19'), 0, 'Saturday adds nothing');
  assert.equal(days('2026-09-20'), 0, 'Sunday adds nothing');
  assert.equal(days('2026-09-25'), 5, 'one full week is exactly 5 — not stale');
  assert.equal(days('2026-09-28'), 6, 'the next Monday tips it over');
  assert.equal(days('2026-11-08'), 35, 'the live seven-week gap is 35 trading days');
  const stale = (date, today) => vm.runInContext(`archiveStaleState('${date}', new Date('${today}T12:00:00Z'))`, ctx);
  assert.equal(stale('2026-09-18', '2026-09-25').stale, false, '5 trading days is not stale');
  assert.equal(stale('2026-09-18', '2026-09-25').tradingDaysOld, 5);
  assert.equal(stale('2026-09-18', '2026-09-28').stale, true, '6 trading days is stale');
  assert.equal(stale('2026-09-18', '2026-11-08').tradingDaysOld, 35);
}

// Row normalization: strings become numbers, blank Bid/Ask become null
// (a blank must not render as a zero quote).
{
  const { ctx } = harness();
  const row = vm.runInContext(
    "normalizeHistRow({ Strike: '600', OpenInterest: '100', Volume: '', Bid: '', Ask: '1.2', UnderlyingPrice: '600', DTE: '0' })", ctx);
  // Field-by-field: deepEqual fails across the vm realm boundary.
  assert.equal(row.Strike, 600);
  assert.equal(row.OpenInterest, 100);
  assert.equal(row.Volume, 0);
  assert.equal(row.Bid, null);
  assert.equal(row.Ask, 1.2);
  assert.equal(row.UnderlyingPrice, 600);
  assert.equal(row.DTE, 0);
}

// renderHistoryHeatmap through the real buildHeatmapRows: 41 rows
// (±20 strikes), spot + tier badge written, status line set, sidebar shown.
{
  const { ctx, els } = harness();
  const ok = vm.runInContext(
    `renderHistoryHeatmap({ rows: ${JSON.stringify(rowsFor('2026-09-23', 600))}, underlying_price: 600, tier: '0DTE_Regular', expiration: '2026-09-23', snapshot_time: '2026-09-23' })`, ctx);
  assert.equal(ok, true);
  assert.equal(els['hist-heatmap-body'].children.length, 41, '41 rows rendered (±20 strikes)');
  assert.ok(els['hist-heatmap-body'].children.some(tr => tr.children.some(td => td.textContent === '600 ★')), 'ATM row present');
  assert.equal(els['hist-price'].textContent, '$600.00', 'header shows the spot');
  assert.equal(els['hist-tier-badge'].textContent, 'Regular', 'tier badge drops the 0DTE_ prefix');
  assert.equal(els['hist-tier-badge'].style.color, get(ctx, "TIER_COLORS['0DTE_Regular']"), 'tier badge takes the tier color');
  assert.equal(els['hist-tier-badge'].style.display, '', 'tier badge is shown');
  assert.equal(els['hist-status'].textContent, 'Trade date 2026-09-23', 'status names the trade date');
  assert.ok(els['hist-heatmap-wrap'].classList.removed.includes('hidden'), 'heatmap shown on a good render');
  assert.ok(els['hist-no-data'].classList.added.includes('hidden'), 'no-data banner hidden on a good render');
}

// renderHistoryHeatmap with no spot: false, status explains it, the wrap
// stays hidden and the no-data banner shows.
{
  const { ctx, els } = harness();
  const ok = vm.runInContext(
    `renderHistoryHeatmap({ rows: ${JSON.stringify(rowsFor('2026-09-23', null))}, underlying_price: null, tier: '0DTE_Regular', expiration: '2026-09-23', snapshot_time: '2026-09-23' })`, ctx);
  assert.equal(ok, false);
  assert.equal(els['hist-status'].textContent, 'No usable spot/strike data for 2026-09-23.');
  assert.ok(els['hist-heatmap-wrap'].classList.added.includes('hidden'), 'heatmap hidden without a spot');
  assert.ok(els['hist-no-data'].classList.removed.includes('hidden'), 'no-data banner shown without a spot');
}

(async () => {
  // loadHistoryDate success: fetches the chain, renders the 0DTE slice,
  // hands the widgets the sub-chain + spot + tier + date.
  {
    const { ctx, els, calls, routes } = harness();
    routes['raw/qqq_chain_20260923.csv'] = chainCsv;
    await vm.runInContext("loadHistoryDate('2026-09-23')", ctx);
    assert.equal(els['hist-status'].textContent, 'Trade date 2026-09-23');
    assert.equal(calls.widgets.length, 1, 'widgets rendered once');
    assert.equal(calls.widgets[0][1], 600, 'widgets get the spot');
    assert.equal(calls.widgets[0][2], '0DTE_Regular', 'widgets get the tier');
    assert.equal(calls.widgets[0][3], '2026-09-23', 'widgets get the date');
    assert.equal(calls.widgets[0][0].length, 82, 'widgets get the full 0DTE sub-chain');
    assert.ok(els['hist-sidebar'].classList.removed.includes('hidden'), 'sidebar shown on a good load');
    assert.equal(els['hist-heatmap-body'].children.length, 41, 'heatmap rendered through the real row builder');
  }

  // loadHistoryDate 404 on both paths: the panel says plainly there is no
  // archived chain (not a generic failure).
  {
    const { ctx, els, calls } = harness();
    await vm.runInContext("loadHistoryDate('2026-09-23')", ctx);
    assert.match(els['hist-status'].textContent, /^Error loading 2026-09-23/);
    assert.equal(els['hist-no-data'].textContent, 'No archived chain for 2026-09-23.');
    assert.deepEqual(calls.widgets, [], 'no widgets on a failed load');
  }

  // fetchChainCsv: a 404 on the daily path falls back to the legacy opex
  // path, and the second call for the same date is served from the cache.
  {
    const { ctx, calls, routes } = harness();
    routes['raw/qqq_chain_20260923.csv'] = 404;
    routes['raw/opex/qqq_chain_20260923.csv'] = chainCsv;
    const first = await vm.runInContext("fetchChainCsv('2026-09-23')", ctx);
    assert.equal(first.length, 82, 'legacy fallback returns the chain');
    assert.equal(first[0].Strike, 580, 'rows are normalized numbers, not CSV strings');
    const fetchesAfterFirst = calls.fetch.length;
    const second = await vm.runInContext("fetchChainCsv('2026-09-23')", ctx);
    assert.equal(second, first, 'same cached rows object returned');
    assert.equal(calls.fetch.length, fetchesAfterFirst, 'no re-fetch for a cached date');
    await assert.rejects(vm.runInContext("fetchChainCsv('../auth')", ctx), /Invalid archive date/);
  }

  // loadHistoryManifest success: the select is populated, the latest date
  // is selected and loaded (widgets prove the load ran). "Today" is pinned
  // to 2026-09-24 (Q1): the fixture's newest date, 2026-09-23, is 1 trading
  // day old then, so the archive is fresh and the status has no stale note.
  {
    const { ctx, els, calls, routes } = harness();
    routes['manifest.json'] = JSON.stringify({ dates: ['2026-09-22', '2026-09-23'] });
    routes['raw/qqq_chain_20260923.csv'] = chainCsv;
    await vm.runInContext("loadHistoryManifest(new Date('2026-09-24T12:00:00Z'))", ctx);
    assert.equal(els['hist-date'].disabled, false, 'select enabled once the index loads');
    assert.equal(els['hist-date'].value, '2026-09-23', 'latest date selected');
    assert.equal(els['hist-status'].textContent, 'Trade date 2026-09-23');
    assert.equal(calls.widgets.length, 1, 'latest date loaded');
  }

  // loadHistoryManifest with a stale archive (Q1, issue #148): the status
  // names the staleness explicitly and the dates stay usable.
  {
    const { ctx, els, calls, routes } = harness();
    routes['manifest.json'] = JSON.stringify({ dates: ['2026-09-16', '2026-09-17', '2026-09-18'] });
    routes['raw/qqq_chain_20260918.csv'] = chainCsv.replaceAll('2026-09-23', '2026-09-18');
    await vm.runInContext("loadHistoryManifest(new Date('2026-11-08T12:00:00Z'))", ctx);
    assert.equal(els['hist-date'].disabled, false, 'stale archive still offers its dates');
    assert.equal(els['hist-date'].value, '2026-09-18', 'latest date selected');
    assert.equal(els['hist-status'].textContent,
      'Trade date 2026-09-18 — archive stale: newest date 2026-09-18 is 35 trading days old');
    assert.equal(calls.widgets.length, 1, 'latest date still loaded');
  }

  // loadHistoryManifest failure: the status names the failure, the select
  // stays disabled, and historyLoaded is reset so reopening retries.
  {
    const { ctx, els, routes } = harness();
    routes['manifest.json'] = 503;
    await vm.runInContext('loadHistoryManifest()', ctx);
    assert.match(els['hist-status'].textContent, /^Failed to load archive index: HTTP 503/);
    assert.equal(els['hist-date'].disabled, true, 'select stays disabled on a failed index');
    assert.equal(get(ctx, 'historyLoaded'), false, 'failed index resets historyLoaded');
    assert.equal(els['hist-no-data'].textContent, 'Could not load historical archive.');
  }
  console.log('historical panel: hash pinned, extraction verified, tier/dates/normalization/render/fetch/manifest paths passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
