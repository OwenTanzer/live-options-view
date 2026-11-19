// Replay and live models agree on the same inputs; the committed replay file matches the audit.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const M = require('../site/shared/order-flow-model.js');

const root = path.join(__dirname, '..');
const replayJson = JSON.parse(fs.readFileSync(path.join(root, 'site/order-flow-replay.json'), 'utf8'));
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/order_flow_live_20260925.json'), 'utf8'));
const replay = M.decodeReplay(replayJson);

test('replay file reproduces the accepted OA-180 audit totals', () => {
  const sums = [0, 0, 0];
  replay.cols.side.forEach((s, i) => { sums[s] += replay.cols.size[i]; });
  const a = replayJson.source.audit_contracts;
  assert.deepEqual(sums, [a.buy, a.sell, a.unknown]);
  assert.deepEqual(sums, [1798966, 1826314, 1158309]);
  assert.equal(replay.cols.t.length, 680161);
  for (let i = 1; i < replay.cols.t.length; i++) assert.ok(replay.cols.t[i] >= replay.cols.t[i - 1]);
});

test('live path (after crash, rebuild and overlap) equals replay at the last print', () => {
  const t = fixture.displayed_t;
  const live = M.liveAt(fixture.feed, fixture.chain, t, replay.multiplier);
  const rep = M.replayAt(replay, t);
  assert.equal(live.strikes.length, 8);
  const strip = (s) => ({ strike: s.strike, observed: s.observed, concentration: s.concentration, flow: s.flow, total: s.total });
  assert.deepEqual(live.strikes.map(strip), rep.strikes.map(strip));
  assert.equal(rep.snapshot.stale, false);
});

test('opening structure: settled OI first seen after the morning refresh, not the stale pre-refresh value', () => {
  const at930 = M.replayAt(replay, replayJson.coverage.open);
  const c = at930.contracts.find((r) => r.symbol === 'QQQ260925C00740000');
  assert.equal(c.oi, 9097);
  assert.equal(new Date(c.oiFirstSeen).toISOString(), '2026-09-25T10:30:50.100Z');
  assert.equal(at930.clearingDate, '2026-09-24');
  assert.deepEqual(c.flow, { B: 0, S: 0, U: 0, prints: 0 });
});

test('stale or missing chain inputs are gaps, never zero', () => {
  const t = fixture.displayed_t;
  const late = M.liveAt(fixture.feed, fixture.chain, t + 10 * 60 * 1000);
  assert.equal(late.snapshot.stale, true);
  assert.ok(late.strikes.every((s) => s.flow[1].base === null && s.concentration.value === null));
  assert.ok(late.strikes.every((s) => s.observed.B + s.observed.S + s.observed.U > 0));
  const none = M.liveAt(fixture.feed, null, t);
  assert.equal(none.snapshot, null);
  const empty = M.liveAt({ rows: [], session: null }, fixture.chain, t);
  assert.deepEqual(empty.strikes, []);
  assert.equal(empty.coverage, null);
});

test('restart gaps make live coverage incomplete', () => {
  const feed = JSON.parse(JSON.stringify(fixture.feed));
  assert.equal(M.liveAt(feed, fixture.chain, fixture.displayed_t).coverage.incomplete, true);
  feed.session.gaps = [];
  feed.session.rebuilt.status = 'rebuilt';
  assert.equal(M.liveAt(feed, fixture.chain, fixture.displayed_t).coverage.incomplete, false);
});
