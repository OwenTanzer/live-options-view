// Order Flow exposure calculation (Gamma Exposure and Order Flow quest, OA-178 calculation).
// One pure calculation for archive replay and live data. Classic script in the
// browser; CommonJS in node tests. Nothing here fetches or draws.
//
// Inputs per exact contract: observed accepted quantities B (inferred buyer-initiated),
// S (inferred seller-initiated), U (unclassified) since the covered session start;
// settled open interest (a single observation, with its first-seen time); and the
// option gamma and QQQ spot from one snapshot at or before the displayed time.
//
// Kept distinct throughout: recorded prints (B+S+U), inferred aggressor side (B vs S),
// assumed dealer participation (100% primary, 50% illustrative) and unknown opening
// inventory (±OI). Dealer accumulation under the passive-dealer assumption is S − B.
// Attribution cases: unknowns to buyers (S−B−U), omitted (S−B), to sellers (S−B+U).
// Values are scenario bounds, not confidence intervals.

const OF_MULTIPLIER = 100;
const OF_PARTICIPATION = [1, 0.5];
// A chain snapshot older than this at the displayed time is a coverage gap, not a value.
const OF_SNAPSHOT_MAX_AGE_MS = 2 * 60 * 1000;

const ofFinite = (v) => typeof v === 'number' && Number.isFinite(v);

// Dollars per 1% QQQ move for one long contract: 0.01 × multiplier × gamma × spot².
function ofUnitDollars(gamma, spot, multiplier = OF_MULTIPLIER) {
  if (!ofFinite(gamma) || gamma < 0 || !ofFinite(spot) || spot <= 0 || !ofFinite(multiplier) || multiplier <= 0) return null;
  return 0.01 * multiplier * gamma * spot * spot;
}

// Dealer contract quantities for one contract: three attribution cases at one participation.
function ofFlowCases(flow, participation = 1) {
  if (!flow || ![flow.B, flow.S, flow.U].every((v) => ofFinite(v) && v >= 0)) return null;
  const p = participation;
  return {
    low: p * (flow.S - flow.B - flow.U),
    base: p * (flow.S - flow.B),
    high: p * (flow.S - flow.B + flow.U),
  };
}

// Conditional total range: flow cases plus settled OI bounds (applied once, never scaled).
function ofTotalRange(flow, oi, participation = 1) {
  const cases = ofFlowCases(flow, participation);
  if (!cases || !ofFinite(oi) || oi < 0) return null;
  return { low: cases.low - oi, high: cases.high + oi };
}

const ofUncertain = (low, high) => low <= 0 && high >= 0;

// One contract, revalued at the displayed time. Missing inputs stay null.
function ofContract(c) {
  const unit = ofUnitDollars(c.gamma, c.spot, c.multiplier ?? OF_MULTIPLIER);
  const out = { symbol: c.symbol, strike: c.strike, type: c.type, unit,
    observed: c.flow ? { B: c.flow.B, S: c.flow.S, U: c.flow.U } : null, oi: ofFinite(c.oi) ? c.oi : null,
    cases: {}, totals: {}, concentration: null, offset: null };
  for (const p of OF_PARTICIPATION) {
    out.cases[p] = ofFlowCases(c.flow, p);
    out.totals[p] = c.coverageFrom !== undefined ? null : ofTotalRange(c.flow, c.oi, p);
  }
  if (out.cases[1]) out.offset = -out.cases[1].base;
  if (unit != null && out.oi != null) out.concentration = unit * out.oi;
  return out;
}

// Sum contracts at one strike in dollars per 1%. A contract contributes to a sum only when
// every input that sum needs is present; coverage counts say how many did.
function ofStrike(strike, contracts) {
  const rows = contracts.map(ofContract);
  const sum = (pick) => {
    let total = null, n = 0;
    for (const r of rows) {
      const v = pick(r);
      if (v == null || r.unit == null) continue;
      total = (total ?? 0) + v * r.unit;
      n += 1;
    }
    return { value: total, covered: n, of: rows.length };
  };
  const out = { strike, contracts: rows, flow: {}, total: {} };
  out.concentration = sum((r) => r.oi);
  for (const p of OF_PARTICIPATION) {
    const lo = sum((r) => r.cases[p]?.low), base = sum((r) => r.cases[p]?.base), hi = sum((r) => r.cases[p]?.high);
    out.flow[p] = { low: lo.value, base: base.value, high: hi.value, covered: base.covered, of: rows.length,
      uncertain: lo.value != null && hi.value != null && ofUncertain(lo.value, hi.value) };
    const tl = sum((r) => r.totals[p]?.low), th = sum((r) => r.totals[p]?.high);
    out.total[p] = { low: tl.value, high: th.value, covered: tl.covered, of: rows.length,
      uncertain: tl.value != null && th.value != null && ofUncertain(tl.value, th.value) };
  }
  out.observed = rows.reduce((a, r) => r.observed ? { B: a.B + r.observed.B, S: a.S + r.observed.S, U: a.U + r.observed.U } : a,
    { B: 0, S: 0, U: 0 });
  return out;
}

// Latest snapshot at or before t (snapshots sorted by t). Returns {snap, age} or null.
function ofSnapshotAt(snapshots, t) {
  let lo = 0, hi = snapshots.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (snapshots[mid].t <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (best < 0) return null;
  const snap = snapshots[best];
  return { snap, age: t - snap.t, stale: t - snap.t > OF_SNAPSHOT_MAX_AGE_MS };
}

// Per-contract cumulative B/S/U from time-sorted prints, for replay. Returns a lookup
// (symbol, t) → {B,S,U,prints} using binary search over each contract's running totals.
function ofAccumulator(prints) {
  const by = new Map();
  for (const p of prints) {
    let a = by.get(p.symbol);
    if (!a) by.set(p.symbol, (a = { t: [], B: [], S: [], U: [] }));
    const n = a.t.length, prev = (k) => (n ? a[k][n - 1] : 0);
    a.t.push(p.t);
    a.B.push(prev('B') + (p.side === 'buy' ? p.size : 0));
    a.S.push(prev('S') + (p.side === 'sell' ? p.size : 0));
    a.U.push(prev('U') + (p.side === 'unknown' ? p.size : 0));
  }
  return (symbol, t) => {
    const a = by.get(symbol);
    if (!a) return { B: 0, S: 0, U: 0, prints: 0 };
    let lo = 0, hi = a.t.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (a.t[mid] <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best < 0 ? { B: 0, S: 0, U: 0, prints: 0 } : { B: a.B[best], S: a.S[best], U: a.U[best], prints: best + 1 };
  };
}

// Columnar form of ofAccumulator for full-session replay: cols = {t, contract, size, side}
// with side codes 0 buy, 1 sell, 2 unknown, time-sorted. Returns (contractIndex, t) → totals.
function ofColumnAccumulator(cols, contractCount) {
  const n = cols.t.length, counts = new Uint32Array(contractCount);
  for (let i = 0; i < n; i++) counts[cols.contract[i]]++;
  const per = Array.from(counts, (c) => ({ t: new Float64Array(c), B: new Float64Array(c), S: new Float64Array(c),
    U: new Float64Array(c), n: 0 }));
  for (let i = 0; i < n; i++) {
    const a = per[cols.contract[i]], k = a.n++, side = cols.side[i], size = cols.size[i];
    a.t[k] = cols.t[i];
    a.B[k] = (k ? a.B[k - 1] : 0) + (side === 0 ? size : 0);
    a.S[k] = (k ? a.S[k - 1] : 0) + (side === 1 ? size : 0);
    a.U[k] = (k ? a.U[k - 1] : 0) + (side === 2 ? size : 0);
  }
  return (index, t) => {
    const a = per[index];
    let lo = 0, hi = (a ? a.n : 0) - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (a.t[mid] <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best < 0 ? { B: 0, S: 0, U: 0, prints: 0 } : { B: a.B[best], S: a.S[best], U: a.U[best], prints: best + 1 };
  };
}

// First index with t[i] >= value in a sorted numeric array.
function ofLowerBound(t, value) {
  let lo = 0, hi = t.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (t[mid] < value) lo = mid + 1; else hi = mid;
  }
  return lo;
}

const OrderFlowExposure = { OF_MULTIPLIER, OF_PARTICIPATION, OF_SNAPSHOT_MAX_AGE_MS, ofUnitDollars, ofFlowCases,
  ofTotalRange, ofUncertain, ofContract, ofStrike, ofSnapshotAt, ofAccumulator, ofColumnAccumulator, ofLowerBound };
if (typeof module !== 'undefined') module.exports = OrderFlowExposure;
else window.OrderFlowExposure = OrderFlowExposure;
