// Exposure calculation fixture from the quest's exposure-calculation page.
const test = require('node:test');
const assert = require('node:assert/strict');
const x = require('../site/shared/order-flow-exposure.js');

const flow = { B: 10, S: 4, U: 8 };

test('attribution cases and sign uncertainty (B=10, S=4, U=8)', () => {
  assert.deepEqual(x.ofFlowCases(flow, 1), { low: -14, base: -6, high: 2 });
  assert.equal(x.ofUncertain(-14, 2), true);
  assert.deepEqual(x.ofFlowCases(flow, 0.5), { low: -7, base: -3, high: 1 });
});

test('conditional total range with 20 settled contracts', () => {
  assert.deepEqual(x.ofTotalRange(flow, 20, 1), { low: -34, high: 22 });
  assert.deepEqual(x.ofTotalRange(flow, 20, 0.5), { low: -27, high: 21 });
});

test('one net contract is $200 per 1% at multiplier 100, gamma 0.02, spot 100', () => {
  assert.equal(x.ofUnitDollars(0.02, 100, 100), 200);
  const s = x.ofStrike(100, [{ symbol: 'A', strike: 100, type: 'C', flow, oi: 20, gamma: 0.02, spot: 100 }]);
  assert.equal(s.flow[1].base, -1200);
  assert.equal(s.flow[1].low, -2800);
  assert.equal(s.flow[1].high, 400);
  assert.equal(s.flow[1].uncertain, true);
  assert.equal(s.total[1].low, -6800);
  assert.equal(s.total[1].high, 4400);
  assert.equal(s.concentration.value, 4000);
  assert.equal(s.contracts[0].offset, 6);
});

test('missing inputs are gaps, never zero', () => {
  assert.equal(x.ofUnitDollars(null, 100), null);
  assert.equal(x.ofTotalRange(flow, null), null);
  const s = x.ofStrike(100, [
    { symbol: 'A', strike: 100, type: 'C', flow, oi: null, gamma: 0.02, spot: 100 },
    { symbol: 'B', strike: 100, type: 'P', flow, oi: 5, gamma: null, spot: 100 },
  ]);
  assert.equal(s.concentration.value, null);
  assert.equal(s.total[1].low, null);
  assert.equal(s.flow[1].covered, 1);
  assert.equal(s.flow[1].of, 2);
  const none = x.ofStrike(100, [{ symbol: 'B', strike: 100, type: 'P', flow, oi: 5, gamma: null, spot: 100 }]);
  assert.equal(none.flow[1].base, null);
});

test('snapshot lookup carries age and marks stale snapshots', () => {
  const snaps = [{ t: 1000 }, { t: 61000 }];
  assert.equal(x.ofSnapshotAt(snaps, 500), null);
  assert.deepEqual(x.ofSnapshotAt(snaps, 70000), { snap: snaps[1], age: 9000, stale: false });
  assert.equal(x.ofSnapshotAt(snaps, 61000 + x.OF_SNAPSHOT_MAX_AGE_MS + 1).stale, true);
});

test('replay accumulator sums observed sides through the displayed time', () => {
  const acc = x.ofAccumulator([
    { symbol: 'A', t: 1, size: 3, side: 'buy' }, { symbol: 'A', t: 2, size: 2, side: 'sell' },
    { symbol: 'A', t: 2, size: 5, side: 'unknown' }, { symbol: 'A', t: 9, size: 1, side: 'buy' },
  ]);
  assert.deepEqual(acc('A', 0), { B: 0, S: 0, U: 0, prints: 0 });
  assert.deepEqual(acc('A', 2), { B: 3, S: 2, U: 5, prints: 3 });
  assert.deepEqual(acc('A', 99), { B: 4, S: 2, U: 5, prints: 4 });
  assert.deepEqual(acc('Z', 99), { B: 0, S: 0, U: 0, prints: 0 });
  const col = x.ofColumnAccumulator({ t: [1, 2, 2, 3, 9], contract: [0, 0, 0, 1, 0], size: [3, 2, 5, 4, 1],
    side: [0, 1, 2, 0, 0] }, 3);
  assert.deepEqual(col(0, 2), { B: 3, S: 2, U: 5, prints: 3 });
  assert.deepEqual(col(0, 99), { B: 4, S: 2, U: 5, prints: 4 });
  assert.deepEqual(col(1, 2), { B: 0, S: 0, U: 0, prints: 0 });
  assert.deepEqual(col(2, 99), { B: 0, S: 0, U: 0, prints: 0 });
  assert.equal(x.ofLowerBound([1, 2, 2, 5], 2), 1);
  assert.equal(x.ofLowerBound([1, 2, 2, 5], 6), 4);
});
