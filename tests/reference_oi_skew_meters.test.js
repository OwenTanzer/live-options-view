// SPY + IBIT near-money OI skew meters (site/js/reference-oi-skew-meters.js):
// render the collector's reference_oi_skew reading for each underlying with
// the same pure describers as the QQQ meter, reject a mismatched symbol,
// degrade on refresh failure, flag thin bands, reuse the selected chain, keep
// meters independent, and stay hash-pinned.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const shared = require('../site/shared/oi-skew.js');

const root = path.join(__dirname, '..');
const rel = 'js/reference-oi-skew-meters.js';
const src = fs.readFileSync(path.join(root, 'site', rel), 'utf8');
const html = fs.readFileSync(path.join(root, 'site', 'index.html'), 'utf8');
const SYMBOLS = ['SPY', 'IBIT'];
const SUFFIXES = ['meter', 'value', 'lean', 'meta', 'track', 'marker', 'detail'];
const ADAPTIVE_SUFFIXES = ['value', 'lean', 'meta', 'track', 'marker', 'detail'];
let checks = 0;

// ── page wiring ───────────────────────────────────────────────────────────────
const hash = createHash('sha256').update(src).digest('hex').slice(0, 16);
assert.ok(html.includes(`${rel}?v=${hash}`), `${rel} script URL must match its content hash`);
assert.ok(html.indexOf('js/data-fetch-cycle.js?v=') < html.indexOf(`${rel}?v=`), 'loads after fetchChain is defined');
assert.ok(html.indexOf('shared/oi-skew.js?v=') < html.indexOf(`${rel}?v=`), 'loads after the shared describers');
checks += 3;
for (const sym of SYMBOLS) {
  const p = sym.toLowerCase();
  for (const s of SUFFIXES) {
    assert.equal(html.split(`id="${p}-oi-skew-${s}"`).length - 1, 1, `#${p}-oi-skew-${s} exists exactly once`);
    checks++;
  }
  assert.ok(html.includes(`<h2 id="${p}-oi-skew-heading">${sym} near-money OI skew</h2>`));
  assert.ok(!html.includes(`${p}-oi-skew-bot`), `${sym}: no bot-signal row, no bot trades it`);
  checks += 2;
}
// Adaptive block exists for IBIT only, inside the IBIT panel, labelled apart.
for (const s of ['', ...ADAPTIVE_SUFFIXES.map(x => '-' + x)]) {
  assert.equal(html.split(`id="ibit-adaptive-oi-skew${s}"`).length - 1, 1, `#ibit-adaptive-oi-skew${s} exists once`);
  checks++;
}
assert.ok(!/id="(spy|qqq)?-?adaptive-oi-skew/.test(html.replace(/id="ibit-adaptive-oi-skew/g, '')), 'no adaptive block for SPY/QQQ');
{
  const ibitSec = html.slice(html.indexOf('id="ibit-oi-skew-meter"'), html.indexOf('</section>', html.indexOf('id="ibit-oi-skew-meter"')));
  assert.ok(ibitSec.includes('id="ibit-adaptive-oi-skew"'), 'adaptive block sits inside the IBIT panel');
  assert.ok(ibitSec.includes('not the published reading or a bot signal'), 'adaptive block is labelled as neither');
}
checks += 3;
// Order on the page: QQQ, SPY, IBIT.
assert.ok(html.indexOf('id="oi-skew-meter"') < html.indexOf('id="spy-oi-skew-meter"'));
assert.ok(html.indexOf('id="spy-oi-skew-meter"') < html.indexOf('id="ibit-oi-skew-meter"'));
checks += 2;

// ── renderer ─────────────────────────────────────────────────────────────────
function harness(extra = {}) {
  const els = {};
  const mk = (id, hidden) => { els[id] = { id, textContent: '', title: '', hidden, dataset: {}, style: {},
    attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); } }; };
  for (const sym of SYMBOLS) for (const s of SUFFIXES) mk(`${sym.toLowerCase()}-oi-skew-${s}`, s === 'marker');
  if (extra.adaptiveDom !== false) {
    mk('ibit-adaptive-oi-skew', false);
    for (const s of ADAPTIVE_SUFFIXES) mk(`ibit-adaptive-oi-skew-${s}`, s === 'marker');
  }
  const { adaptiveDom, ...rest } = extra;
  const ctx = { document: { getElementById: id => els[id] || null }, module: { exports: {} },
    ...shared, Date, Math, Number, Intl, String, Array, Map, Set, Object, ...rest };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const api = ctx.module.exports;
  assert.deepEqual([...api.REFERENCE_OI_SKEW_SYMBOLS], SYMBOLS);
  const m = Object.fromEntries(SYMBOLS.map(s => [s, api.createReferenceOiSkewMeter(s)]));
  return { els, m, e: (sym, s) => els[`${sym.toLowerCase()}-oi-skew-${s}`],
    ad: s => els[s ? `ibit-adaptive-oi-skew-${s}` : 'ibit-adaptive-oi-skew'] };
}

const SNAP = '2026-11-13T17:30:00+00:00';
const NOW = Date.parse('2026-11-13T17:30:40Z');
function payload(symbol, values, { readingSymbol = symbol, status = 'ok', ts = SNAP } = {}) {
  return {
    symbol, timestamp: ts, rows: [{ Strike: 1 }],
    market_readings: [{ reading_id: 'reference_oi_skew', symbol: readingSymbol, values,
      status: { code: status, reason: status === 'ok' ? null : 'no_open_interest' },
      calculation: { version: 'reference_oi_skew.v1', parameters: { near_money_pct: 0.02 } },
      input: { snapshot_timestamp: ts }, freshness: { status: 'live' } }],
  };
}
const spyVals = { imbalance_ratio: -0.307, near_money_call_oi: 154267, near_money_put_oi: 291020, band_strikes: 30 };
const ibitVals = { imbalance_ratio: -0.86, near_money_call_oi: 336, near_money_put_oi: 4472, band_strikes: 1 };

{ // SPY live, well covered; IBIT live, thin; meters do not touch each other
  const { m, e } = harness();
  m.SPY.update(payload('SPY', spyVals), NOW);
  assert.equal(e('SPY', 'meter').dataset.state, 'live');
  assert.equal(e('SPY', 'meter').dataset.lean, 'put-heavy');
  assert.equal(e('SPY', 'meter').dataset.coverage, 'ok');
  assert.equal(e('SPY', 'value').textContent, '−0.307');
  assert.equal(e('SPY', 'lean').textContent, 'Put-heavy');
  assert.equal(e('SPY', 'marker').hidden, false);
  assert.equal(e('SPY', 'marker').style.left, `${shared.oiSkewMeterPercent(-0.307)}%`);
  assert.match(e('SPY', 'meta').textContent, /^SPY · reference ±2% band · snapshot .* · live$/);
  assert.match(e('SPY', 'detail').textContent, /154,267 call vs 291,020 put contracts over 30 strikes/);
  assert.equal(e('SPY', 'track').attrs['aria-valuenow'], '-0.307');
  assert.equal(e('IBIT', 'value').textContent, '', 'IBIT untouched by a SPY update');
  checks += 11;

  m.IBIT.update(payload('IBIT', ibitVals), NOW);
  assert.equal(e('IBIT', 'meter').dataset.coverage, 'thin');
  assert.match(e('IBIT', 'meta').textContent, /^IBIT · .* · live · thin: 1 strike \(< 4\)$/);
  assert.equal(e('IBIT', 'value').textContent, '−0.860', 'value still shown, just flagged');
  assert.equal(e('SPY', 'value').textContent, '−0.307');
  checks += 4;

  // refresh failure: last good reading kept, no longer labelled live
  m.SPY.noteFailure('SPY snapshot could not be loaded.', NOW);
  assert.equal(e('SPY', 'meter').dataset.state, 'stale');
  assert.match(e('SPY', 'meta').textContent, /stale · refresh failed$/);
  assert.equal(e('SPY', 'value').textContent, '−0.307');
  assert.equal(e('IBIT', 'meter').dataset.state, 'live', 'IBIT unaffected by SPY failure');
  checks += 4;

  // age alone moves live -> closed
  m.IBIT.render(NOW + 24 * 3600 * 1000);
  assert.equal(e('IBIT', 'meter').dataset.state, 'closed');
  checks += 1;
}

{ // a QQQ reading inside a SPY payload is never shown as SPY
  const { m, e } = harness();
  m.SPY.update(payload('SPY', spyVals, { readingSymbol: 'QQQ' }), NOW);
  assert.equal(e('SPY', 'meter').dataset.state, 'unavailable');
  assert.equal(e('SPY', 'value').textContent, '—');
  assert.equal(e('SPY', 'marker').hidden, true);
  checks += 3;
}

{ // no OI in band
  const { m, e } = harness();
  m.IBIT.update(payload('IBIT', { imbalance_ratio: null, band_strikes: 0 }, { status: 'no_open_interest' }), NOW);
  assert.equal(e('IBIT', 'meter').dataset.state, 'no_data');
  assert.equal(e('IBIT', 'lean').textContent, 'no data');
  assert.equal(e('IBIT', 'meter').dataset.coverage, 'ok', 'no value is not a thin value');
  checks += 3;
}


// ── adaptive window: pure rule (IBIT) ────────────────────────────────────────
const { computeAdaptiveOiSkew, ADAPTIVE_OI_SKEW_PARAMS, ADAPTIVE_OI_SKEW_SYMBOLS } = (() => {
  const ctx = { module: { exports: {} }, Math, Number, Array, Map, Set, Object, String };
  vm.createContext(ctx); vm.runInContext(src, ctx); return ctx.module.exports;
})();
assert.deepEqual({ ...ADAPTIVE_OI_SKEW_PARAMS }, { floor_pct: 0.02, cap_pct: 0.10, min_strikes: 4 });
assert.deepEqual([...ADAPTIVE_OI_SKEW_SYMBOLS], ['IBIT'], 'SPY/QQQ stay on their published readings');
checks += 2;
// rows helper: [strike, callOI, putOI]
const chain = list => list.flatMap(([k, c, p]) => [{ Strike: k, Type: 'call', OpenInterest: c }, { Strike: k, Type: 'put', OpenInterest: p }]);
const near = (a, b) => Math.abs(a - b) < 1e-12;

{ // four-strike threshold at the floor: 4 strikes within ±2% -> not widened, equals the fixed ±2% sum
  const r = computeAdaptiveOiSkew(chain([[98, 10, 30], [99, 10, 30], [101, 10, 10], [102, 20, 10], [110, 999, 0]]), 100);
  assert.equal(r.ok, true); assert.equal(r.widened, false); assert.equal(r.half_width, 0.02);
  assert.equal(r.band_strikes, 4); assert.equal(r.call_oi, 50); assert.equal(r.put_oi, 80);
  assert.ok(near(r.imbalance, -30 / 130));
  checks += 7;
}
{ // three within ±2%: widen to exactly the 4th-nearest strike (unrounded), no further
  const r = computeAdaptiveOiSkew(chain([[99, 1, 1], [100, 1, 1], [101, 1, 1], [103.7, 5, 0], [104, 9, 9]]), 100);
  assert.equal(r.ok, true); assert.equal(r.widened, true);
  assert.equal(r.half_width, Math.abs(103.7 - 100) / 100, 'half-width is the unrounded 4th-nearest distance');
  assert.equal(r.band_strikes, 4); assert.equal(r.high_strike, 103.7); assert.equal(r.call_oi, 8);
  checks += 6;
}
{ // exact ties at the boundary are all included (band holds > 4)
  const r = computeAdaptiveOiSkew(chain([[40, 1, 1], [39, 1, 1], [41, 1, 1], [38, 2, 0], [42, 0, 2], [37, 50, 50]]), 40);
  assert.equal(r.ok, true); assert.equal(r.half_width, 0.05);
  assert.equal(r.band_strikes, 5, '38 and 42 tie at 5%: both in'); assert.equal(r.low_strike, 38); assert.equal(r.high_strike, 42);
  checks += 4;
}
{ // floating-point mirror ties: spot 38.01 ± $0.80. In doubles 38.81 lands a
  // hair (~2e-16) farther than 37.21, so without tie handling 37.21 would be
  // the 4th strike and 38.81 would be dropped. Both must be in.
  const s0 = 38.01;
  const dLo = Math.abs(37.21 - s0) / s0, dHi = Math.abs(38.81 - s0) / s0;
  assert.ok(dLo < dHi && dHi - dLo < 1e-13, 'fixture really is a float near-tie');
  const r = computeAdaptiveOiSkew(chain([[38, 1, 1], [37.8, 1, 1], [38.2, 1, 1], [37.21, 3, 0], [38.81, 0, 3], [36, 9, 9]]), s0);
  assert.equal(r.ok, true); assert.equal(r.half_width, dLo);
  assert.equal(r.band_strikes, 5); assert.equal(r.low_strike, 37.21); assert.equal(r.high_strike, 38.81);
  assert.equal(r.call_oi, 6); assert.equal(r.put_oi, 6);
  checks += 7;
}
{ // Decimal cap boundary must not reject an exactly 10% strike through float noise.
  const r = computeAdaptiveOiSkew(chain([[30, 1, 1], [31, 1, 1], [32, 1, 1], [33.011, 1, 1]]), 30.01);
  assert.equal(r.ok, true); assert.equal(r.half_width, 0.10); assert.equal(r.band_strikes, 4);
  const invalid = chain([[39, 1, 1], [40, 1, 1], [41, 1, 1]]).concat([
    { Strike: 42, Type: 'unknown', OpenInterest: 10 },
    { Strike: 43, Type: 'call', OpenInterest: Infinity },
  ]);
  assert.equal(computeAdaptiveOiSkew(invalid, 40).ok, false);
  checks += 4;
}
{ // zero-OI strikes neither count toward coverage nor enter the band
  const rows = chain([[100, 0, 0], [100.5, 0, 0], [99, 5, 5], [101, 5, 5], [102, 5, 5], [106, 1, 7], [101.5, 0, 0]]);
  const r = computeAdaptiveOiSkew(rows, 100);
  assert.equal(r.ok, true); assert.equal(r.strikes_with_oi, 4);
  assert.equal(r.half_width, 0.06, 'zero-OI 100/100.5/101.5 skipped; 4th OI strike is 106');
  assert.equal(r.band_strikes, 4); assert.equal(r.call_oi, 16); assert.equal(r.put_oi, 22);
  // a row with OI on only one side still makes the strike count
  assert.equal(computeAdaptiveOiSkew(chain([[99, 0, 4], [100, 3, 0], [101, 0, 1], [102, 1, 0]]), 100).band_strikes, 4);
  checks += 6;
}
{ // 10% cap: 4th strike exactly at the cap is allowed; beyond it there is no value
  const at = computeAdaptiveOiSkew(chain([[100, 1, 1], [101, 1, 1], [99, 1, 1], [110, 1, 0]]), 100);
  assert.equal(at.ok, true); assert.equal(at.half_width, 0.1);
  const past = computeAdaptiveOiSkew(chain([[100, 1, 1], [101, 1, 1], [99, 1, 1], [110.5, 1, 0]]), 100);
  assert.equal(past.ok, false); assert.equal(past.reason, 'beyond_cap'); assert.ok(near(past.nth_distance, 0.105));
  assert.equal(past.imbalance, undefined, 'no value is extrapolated');
  checks += 6;
}
{ // insufficient coverage and bad inputs
  const few = computeAdaptiveOiSkew(chain([[99, 1, 1], [100, 1, 1], [101, 1, 1], [102, 0, 0]]), 100);
  assert.equal(few.ok, false); assert.equal(few.reason, 'insufficient_strikes'); assert.equal(few.strikes_with_oi, 3);
  assert.equal(computeAdaptiveOiSkew([], 100).reason, 'insufficient_strikes');
  assert.equal(computeAdaptiveOiSkew(chain([[1, 1, 1]]), 0).reason, 'no_spot');
  assert.equal(computeAdaptiveOiSkew(chain([[1, 1, 1]]), null).reason, 'no_spot');
  assert.equal(computeAdaptiveOiSkew(null, 100).reason, 'no_rows');
  checks += 7;
}

// ── adaptive window: IBIT panel rendering ─────────────────────────────────────
// Friday 2026-11-13 close shape: spot 38.78, OI on 37/38/39/40 (+ farther strikes).
const ibitChain = chain([[35, 4000, 9000], [36, 3000, 8000], [37, 1500, 9000], [38, 2000, 8000],
  [39, 336, 4472], [40, 2469, 2600], [41, 900, 300], [42, 467, 524], [39.5, 0, 0]]);
function ibitPayload(over = {}) {
  return { ...payload('IBIT', ibitVals), rows: ibitChain, underlying_price: 38.78, expiration: '2026-11-13', ...over };
}
{
  const { m, ad, e } = harness();
  m.IBIT.update(ibitPayload(), NOW);
  assert.equal(ad().dataset.state, 'live');
  assert.equal(ad('value').textContent, '−0.585');
  assert.equal(ad('lean').textContent, 'Put-heavy');
  assert.equal(ad('meta').textContent, '±4.59% window · 4 strikes (37–40) · exp 2026-11-13 · live');
  assert.match(ad('detail').textContent, /^6,305 call vs 24,072 put contracts\. Window widened from ±2% to the 4th-nearest strike with OI \(cap ±10%\)/);
  assert.equal(ad('marker').hidden, false);
  assert.equal(e('IBIT', 'value').textContent, '−0.860', 'published reading unchanged beside it');
  assert.match(e('IBIT', 'meta').textContent, /thin: 1 strike/);
  checks += 8;

  // refresh failure: both readings keep the last snapshot but drop to stale
  m.IBIT.noteFailure('IBIT snapshot could not be loaded.', NOW);
  assert.equal(ad().dataset.state, 'stale');
  assert.match(ad('meta').textContent, / · stale · refresh failed$/);
  assert.equal(ad('value').textContent, '−0.585');
  // age alone -> session closed
  m.IBIT.update(ibitPayload(), NOW + 24 * 3600 * 1000);
  assert.equal(ad().dataset.state, 'closed');
  assert.match(ad('meta').textContent, / · session closed$/);
  checks += 5;
}
{ // insufficient coverage: no adaptive value, published reading still shown
  const { m, ad, e } = harness();
  m.IBIT.update(ibitPayload({ rows: chain([[39, 336, 4472], [40, 10, 0], [38, 0, 5]]) }), NOW);
  assert.equal(ad().dataset.state, 'insufficient');
  assert.equal(ad('value').textContent, '—');
  assert.equal(ad('lean').textContent, 'Insufficient coverage');
  assert.equal(ad('marker').hidden, true);
  assert.match(ad('detail').textContent, /^Only 3 strikes carry OI; needs 4\. No adaptive value is shown; the published ±2% reading above still applies\.$/);
  assert.equal(e('IBIT', 'value').textContent, '−0.860');
  m.IBIT.update(ibitPayload({ rows: chain([[39, 1, 1], [38.5, 1, 1], [39.5, 1, 1], [44, 1, 1]]) }), NOW);
  assert.match(ad('detail').textContent, /^The 4th-nearest strike with OI is 13\.46% from spot, beyond the ±10% cap\./);
  checks += 7;
}
{ // wrong-symbol payload and no payload: adaptive unavailable, never computed
  const { m, ad } = harness();
  m.IBIT.render(NOW);
  assert.equal(ad('lean').textContent, 'Waiting for snapshot');
  m.IBIT.update({ ...ibitPayload(), symbol: 'SPY' }, NOW);
  assert.equal(ad().dataset.state, 'unavailable');
  assert.equal(ad('value').textContent, '—');
  checks += 3;
}
{ // An empty published band must not hide the wider reading's snapshot age.
  const { m, ad, e } = harness();
  const p = ibitPayload();
  p.market_readings[0].status.code = 'no_data';
  p.market_readings[0].values = { ...p.market_readings[0].values, imbalance_ratio: null };
  m.IBIT.update(p, NOW + 86400000);
  assert.equal(e('IBIT', 'value').textContent, '—');
  assert.equal(ad().dataset.state, 'closed');
  assert.match(ad('meta').textContent, /session closed/);
  checks += 3;
}
{ // SPY never gets an adaptive reading, even with a thin chain
  const { m, ad } = harness();
  m.SPY.update({ ...payload('SPY', spyVals), rows: chain([[750, 1, 1], [751, 1, 1], [752, 1, 1], [800, 1, 0]]), underlying_price: 752 }, NOW);
  assert.equal(ad('value').textContent, '', 'IBIT adaptive block untouched by SPY');
  checks += 1;
}

(async () => {
  const fresh = (sym, vals) => ({ data: payload(sym, vals, { ts: new Date().toISOString() }) });
  { // own fetch path through fetchChain(symbol)
    const { m, e } = harness();
    const asked = [];
    await m.SPY.refresh(async (sym) => { asked.push(sym); return fresh(sym, spyVals); });
    await m.IBIT.refresh(async (sym) => { asked.push(sym); return fresh(sym, ibitVals); });
    assert.deepEqual(asked, ['SPY', 'IBIT']);
    assert.equal(e('SPY', 'value').textContent, '−0.307');
    assert.equal(e('IBIT', 'value').textContent, '−0.860');
    await m.SPY.refresh(async () => ({ error: 'No SPY snapshot has been published yet.' }));
    assert.match(e('SPY', 'meta').textContent, /refresh failed$/);
    checks += 4;
  }
  { // superseded reply is dropped
    const { m, e } = harness();
    let release;
    const slow = m.SPY.refresh(() => new Promise(r => { release = r; }));
    await m.SPY.refresh(async () => fresh('SPY', spyVals));
    release(fresh('SPY', { ...spyVals, imbalance_ratio: 0.5 }));
    await slow;
    assert.equal(e('SPY', 'value').textContent, '−0.307', 'late reply did not overwrite newer data');
    checks += 1;
  }
  { // selected chain is reused, no second request; other symbol still fetches
    const sel = fresh('SPY', spyVals);
    const { m, e } = harness({ chainState: { SPY: sel }, chainSelection: { symbol: 'SPY' } });
    const asked = [];
    const impl = async (sym) => { asked.push(sym); return fresh(sym, ibitVals); };
    await m.SPY.refresh(impl);
    await m.IBIT.refresh(impl);
    assert.deepEqual(asked, ['IBIT']);
    assert.equal(e('SPY', 'value').textContent, '−0.307');
    checks += 2;
  }
  { // A selected chain's failed refresh must not relabel its cached snapshot live.
    const selected = { data: payload('IBIT', ibitVals, { ts: new Date().toISOString() }), error: 'offline' };
    const { m, e } = harness({ chainState: { IBIT: selected }, chainSelection: { symbol: 'IBIT' } });
    await m.IBIT.refresh(async () => { throw new Error('must reuse selected cache'); });
    assert.equal(e('IBIT', 'meter').dataset.state, 'stale');
    assert.match(e('IBIT', 'meta').textContent, /refresh failed/);
    selected.error = null;
    await m.IBIT.refresh();
    assert.equal(e('IBIT', 'meter').dataset.state, 'live');
    checks += 3;
  }
  console.log(`reference_oi_skew_meters: ${checks} checks passed`);
})().catch(e => { console.error(e); process.exit(1); });
