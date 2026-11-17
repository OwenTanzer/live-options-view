// Rendered output for partial coverage and a missing opening interval (Chej PR269 review).
const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../site/shared/order-flow-model.js');
const R = require('../site/shared/order-flow-render.js');

const DAY = '2026-09-25';
const OPEN = M.etOpenMs(DAY);
const clock = (t) => new Date(t).toISOString().slice(11, 19);
const chain = (now, rows) => ({ timestamp: new Date(now - 10000).toISOString(), snapshot_key: 'k', rows });
const feed = (session) => ({ lastReceived: null, rows: [], session });
const session = (extra = {}) => ({ day: DAY, first: OPEN + 100, last: OPEN + 5000, prints: 3,
  contracts: { QQQ260925C00745000: { B: 10, S: 4, U: 8, prints: 2 }, QQQ260925P00745000: { B: 1, S: 2, U: 0, prints: 1 } },
  gaps: [], rebuilt: { status: 'rebuilt', segments: 1, through: OPEN + 5000, at: OPEN - 60000, session_open: OPEN }, ...extra });

test('one of two contracts with OI: concentration is marked 1/2 and the total is withheld', () => {
  const now = OPEN + 6000;
  const view = M.liveAt(feed(session()), chain(now, [
    { OptionSymbol: 'QQQ260925C00745000', OpenInterest: 20, Gamma: 0.02, UnderlyingPrice: 100 },
    { OptionSymbol: 'QQQ260925P00745000', OpenInterest: null, Gamma: 0.02, UnderlyingPrice: 100 },
  ]), now);
  const s = view.strikes[0];
  assert.deepEqual([s.concentration.covered, s.concentration.of, s.total[1].covered, s.total[1].of], [1, 2, 1, 2]);
  const html = R.ladderRowsHtml(view, 1, null);
  assert.match(html, /\$4\.0k <span class="gap" title="1 of 2 contracts lack OI or gamma">▨ 1\/2<\/span>/);
  assert.match(html, /withheld: 1\/2 contracts covered/);
  assert.doesNotMatch(html, /−\$6\.8k … \+\$4\.4k/);
});

test('fully covered strike renders the total range with no partial marker', () => {
  const now = OPEN + 6000;
  const view = M.liveAt(feed(session()), chain(now, [
    { OptionSymbol: 'QQQ260925C00745000', OpenInterest: 20, Gamma: 0.02, UnderlyingPrice: 100 },
    { OptionSymbol: 'QQQ260925P00745000', OpenInterest: 5, Gamma: 0.02, UnderlyingPrice: 100 },
  ]), now);
  const html = R.ladderRowsHtml(view, 1, null);
  assert.doesNotMatch(html, /▨|withheld/);
  // Call: −14−20 … 2+20; put (B1 S2 U0, OI 5): 1−5 … 1+5 → −38 … +28 contracts × $200.
  assert.match(html, /−\$7\.6k … \+\$5\.6k/);
});

test('late start with an empty archive is incomplete and names the missing opening', () => {
  const late = OPEN + 47 * 60000;
  const s = session({ first: late + 1000, last: late + 2000, rebuilt: { status: 'rebuilt', segments: 0, through: null, at: late, session_open: OPEN }, gaps: [] });
  const view = M.liveAt(feed(s), null, late + 3000);
  assert.equal(view.coverage.incomplete, true);
  assert.deepEqual(view.coverage.missingOpening, { start: OPEN, end: late + 1000 });
  const text = R.freshnessText(view, clock);
  assert.match(text, /OPENING MISSING: no flow from 13:30:00 to 14:17:01 ET/);
  assert.match(text, /INCOMPLETE HISTORY/);
});

test('collector-published opening gap is shown as a gap without a duplicate opening flag', () => {
  const late = OPEN + 47 * 60000;
  const s = session({ first: late + 1000, last: late + 2000, gaps: [{ start: OPEN, end: late + 1005, reason: 'collector_restart:rebuilt' }] });
  const view = M.liveAt(feed(s), null, late + 3000);
  assert.equal(view.coverage.missingOpening, null);
  assert.equal(view.coverage.incomplete, true);
  assert.match(R.freshnessText(view, clock), /1 gap: 13:30:00–14:17:01/);
});

test('no print yet after the open is incomplete; before the open it is not', () => {
  const s = session({ first: null, last: null, contracts: {}, prints: 0 });
  assert.equal(M.liveAt(feed(s), null, OPEN + 5 * 60000).coverage.incomplete, true);
  assert.equal(M.liveAt(feed(s), null, OPEN - 60000).coverage.incomplete, false);
});
