// Order Flow replay heatmap: estimated signed-flow gamma by time and actual strike.
// Pure functions; nothing here draws. Classic script in the browser (window.OrderFlowHeatmap,
// needs order-flow-exposure.js, order-flow-model.js and order-flow-profile.js first) or
// CommonJS in node tests.
//
// Each cell is the replay view at the END of its time bin (M.replayAt at that instant), so a
// selected cell and the strike profile and table at the same time and strike come from one
// calculation. Cells after the displayed time are not shown (inputs stay as-of-time). Missing
// or stale inputs stay null, never zero. The colour scale is symmetric, uses the 100%
// participation point estimate of every cell up to the displayed time, and never shrinks, so
// 50% is drawn on the same scale and seek, play and reload agree.

(function (root) {
  const node = typeof module !== 'undefined';
  const M = node ? require('./order-flow-model.js') : root.OrderFlowModel;
  const P = node ? require('./order-flow-profile.js') : root.OrderFlowProfile;

  const BIN_MS = 60 * 1000;

  // Grid over the session at binMs. times[j] is the end of bin j; strikes are the replay's
  // actual strikes, high to low.
  function replayGrid(r, binMs = BIN_MS) {
    const open = r.coverage.open, close = r.coverage.close;
    const times = [];
    for (let t = open + binMs; t <= close; t += binMs) times.push(t);
    if (times[times.length - 1] !== close) times.push(close);
    const strikes = M.strikesOf(r.contracts);
    const cols = times.map((t) => {
      const v = M.replayAt(r, t);
      const spot = v.contracts.find((c) => c.spot != null)?.spot ?? null;
      const byStrike = new Map(v.strikes.map((s) => [s.strike, s]));
      return {
        t, spot, snapshot: v.snapshot,
        cells: strikes.map((k) => {
          const s = byStrike.get(k);
          return s ? { strike: k, flow: s.flow, concentration: s.concentration, observed: s.observed } : null;
        }),
      };
    });
    // Prefix maximum of |100% point estimate| for the colour scale.
    const maxAbs = [];
    let m = 0;
    for (const c of cols) {
      for (const cell of c.cells) if (cell && cell.flow[1].base != null) m = Math.max(m, Math.abs(cell.flow[1].base));
      maxAbs.push(m);
    }
    return { binMs, open, close, times, strikes, cols, maxAbs };
  }

  // Index of the last bin ending at or before t (-1 if none): the columns visible at t.
  function lastColumn(grid, t) {
    let lo = 0, hi = grid.times.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (grid.times[mid] <= t) lo = mid + 1; else hi = mid; }
    return lo - 1;
  }

  // Symmetric colour scale at time t (labelled step, never shrinks).
  function gridScale(grid, t) {
    const j = lastColumn(grid, t);
    return j < 0 ? 0 : P.niceCeil(grid.maxAbs[j]);
  }

  // One cell's display state at participation p. kind: 'value' | 'missing' (inputs missing or
  // stale) | 'future' (after the displayed time) | 'none' (strike not in the replay).
  function cellAt(grid, j, k, p, t) {
    const col = grid.cols[j];
    if (!col || k < 0 || k >= grid.strikes.length) return { kind: 'none' };
    if (col.t > t) return { kind: 'future', t: col.t, strike: grid.strikes[k] };
    const c = col.cells[k];
    if (!c) return { kind: 'none', t: col.t, strike: grid.strikes[k] };
    const f = c.flow[p];
    const base = { t: col.t, strike: c.strike, snapshot: col.snapshot, spot: col.spot, flow: f, concentration: c.concentration, observed: c.observed };
    return f.base == null ? { kind: 'missing', ...base } : { kind: 'value', ...base };
  }

  // Diverging colour for a value on scale s: green positive, red negative, intensity by |v|/s.
  // Returns null for null inputs so callers draw a gap pattern instead.
  function cellColor(v, s) {
    if (v == null) return null;
    if (!s) return 'rgba(127,147,153,0.25)';
    const u = Math.max(-1, Math.min(1, v / s)), a = 0.12 + 0.88 * Math.sqrt(Math.abs(u));
    return u >= 0 ? `rgba(98,227,189,${a.toFixed(3)})` : `rgba(255,150,124,${a.toFixed(3)})`;
  }

  // Text for the selected cell: value, scenario endpoints, sign, coverage and snapshot.
  function cellSummary(cell, p, clock, money) {
    const pp = p === 1 ? '100%' : '50%';
    if (!cell || cell.kind === 'none') return 'No cell selected.';
    const head = `Cell ${clock(cell.t)} ET · $${cell.strike} strike · ${pp} participation: `;
    if (cell.kind === 'future') return head + 'after the displayed time, not shown.';
    const snap = cell.snapshot
      ? `chain snapshot ${clock(cell.snapshot.t)} ET, ${Math.round(cell.snapshot.age / 1000)} s old${cell.snapshot.stale ? ' (stale: not used)' : ''}`
      : 'no chain snapshot';
    if (cell.kind === 'missing') return head + `no coverage (missing or stale input, not zero); ${snap}.`;
    const f = cell.flow, c = cell.concentration;
    const sign = f.uncertain ? 'sign not determined (scenarios cross zero)' : f.base < 0 ? 'negative' : 'positive';
    return head + `estimated flow gamma ${money(f.base)} per 1% QQQ, scenarios ${money(f.low)} … ${money(f.high)}, ${sign}; ` +
      `flow covers ${f.covered}/${f.of} contracts; settled OI concentration ${c.value == null ? 'no coverage' : money(c.value).replace(/^\+/, '') + ' (' + c.covered + '/' + c.of + ')'}; ` +
      `observed B/S/U ${cell.observed.B.toLocaleString('en-US')}/${cell.observed.S.toLocaleString('en-US')}/${cell.observed.U.toLocaleString('en-US')}; ${snap}.`;
  }

  const api = { BIN_MS, replayGrid, lastColumn, gridScale, cellAt, cellColor, cellSummary };
  if (node) module.exports = api;
  else root.OrderFlowHeatmap = api;
})(typeof window !== 'undefined' ? window : globalThis);
