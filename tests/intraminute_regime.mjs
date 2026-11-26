import assert from 'node:assert/strict';
import {regimeNowcast} from '../site/shared/intraminute-regime.js';

const minute = Date.UTC(2026, 10, 13, 15, 31);
const opts = {minute, cutoff: minute + 30000, low: 99.8, high: 100.2, sigma: 0.2, knownAt: minute};
const quote = (seconds, price, receipt = seconds) => ({
  kind: 'quote', bid: price - .01, ask: price + .01,
  at: minute + 1000 * receipt, bidAt: minute + 1000 * seconds, askAt: minute + 1000 * seconds,
});
const rows = [quote(0, 100), quote(30, 100)];
const result = regimeNowcast(rows, [], opts);
// At 30s with zero net movement the predictive SD is 0.2 sqrt(2/3).
assert.ok(Math.abs(result.inside - 0.779328638) < 1e-6);
assert.ok(Math.abs(result.below - result.above) < 1e-6);
assert.equal(result.below + result.inside + result.above, 1);
const lateAndFuture = [quote(20, 200, 31), quote(31, 300, 29), quote(59, 500), quote(-1, 1, 0)];
assert.deepEqual(regimeNowcast([...lateAndFuture, ...rows], [], opts), result);
assert.deepEqual(regimeNowcast([...rows].reverse(), [], opts), result);
assert.ok(regimeNowcast([quote(0, 100), quote(30, 101)], [], opts).above > 0.999);
assert.match(regimeNowcast(rows, [], {...opts, knownAt: opts.cutoff + 1}).reason, /known/);
assert.match(regimeNowcast(rows, [], {...opts, knownAt: minute - 86400000}).reason, /trading date/);
assert.match(regimeNowcast(rows, [[minute, minute + 1]], opts).reason, /coverage/);
assert.match(regimeNowcast([quote(0, 100)], [], opts).reason, /fresh/);
assert.match(regimeNowcast([{...quote(30, 100), bidAt: minute + 24000}], [], opts).reason, /fresh/);
assert.match(regimeNowcast(rows, [], {...opts, sigma: NaN}).reason, /positive/);
console.log('Bayesian calculator distribution, causal cutoffs, stale sides and gaps passed.');
