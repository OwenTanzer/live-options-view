// Order Flow strike profile: the scale and row layout behind the enlarged profile chart.
// Pure functions; nothing here draws. Classic script in the browser (window.OrderFlowProfile,
// needs order-flow-exposure.js first) or CommonJS in node tests.
//
// Scale rule (accepted design, Chej 2026-11-18): one symmetric flow scale and one OI
// concentration scale, each the largest value reached by any input up to the displayed
// time and never smaller later in the session. Flow uses both scenario endpoints at 100%
// participation, so 50% is drawn on the same axis. Replay derives the scale from the
// archive inputs alone, so play, forward or backward seek and a reload reach the same
// scale at the same time. Live can only use what this browser has observed; its scale is
// a running maximum kept across reconnects and reloads, labelled with when it started.

(function (root) {
  const X = typeof module !== 'undefined' ? require('./order-flow-exposure.js') : root.OrderFlowExposure;

  // Largest |flow endpoint| (100% participation) and largest concentration in one view.
  function viewExtent(view) {
    let flow = 0, conc = 0;
    for (const s of (view && view.strikes) || []) {
      const f = s.flow[1];
      if (f.low != null) flow = Math.max(flow, Math.abs(f.low));
      if (f.high != null) flow = Math.max(flow, Math.abs(f.high));
      if (s.concentration.value != null) conc = Math.max(conc, s.concentration.value);
    }
    return { flow, conc };
  }

  // Replay: prefix maximum over every input time (prints and chain snapshots), computed
  // once from the archive. Values only change at those times; a snapshot going stale
  // removes values and so never raises the maximum. Returns t -> {flow, conc}.
  function replayScale(r) {
    const n = r.contracts.length, cols = r.cols, snaps = r.snapshots, mult = r.multiplier;
    const strikeOf = r.contracts.map((c) => c.strike);
    const strikes = [...new Set(strikeOf)];
    const members = strikes.map((k) => strikeOf.map((s, i) => (s === k ? i : -1)).filter((i) => i >= 0));
    const memberStrike = strikeOf.map((k) => strikes.indexOf(k));
    const B = new Float64Array(n), S = new Float64Array(n), U = new Float64Array(n);
    const unit = new Array(n).fill(null), oi = new Array(n).fill(null);
    const flowAt = new Float64Array(strikes.length), concAt = new Float64Array(strikes.length);
    let snapIdx = -1, stale = true;

    const setUnits = (t) => {
      const snap = snapIdx >= 0 ? snaps[snapIdx] : null;
      stale = !snap || t - snap.t > X.OF_SNAPSHOT_MAX_AGE_MS;
      for (let i = 0; i < n; i++) {
        unit[i] = stale ? null : X.ofUnitDollars(snap.gamma[i], snap.spot, mult);
        const o = stale ? null : snap.oi[i];
        oi[i] = typeof o === 'number' && Number.isFinite(o) && o >= 0 ? o : null;
      }
    };
    const strikeValues = (k) => {
      let lo = 0, hi = 0, any = false, c = 0, anyC = false;
      for (const i of members[k]) {
        if (unit[i] == null) continue;
        any = true;
        lo += (S[i] - B[i] - U[i]) * unit[i];
        hi += (S[i] - B[i] + U[i]) * unit[i];
        if (oi[i] != null) { anyC = true; c += oi[i] * unit[i]; }
      }
      flowAt[k] = any ? Math.max(Math.abs(lo), Math.abs(hi)) : 0;
      concAt[k] = anyC ? c : 0;
    };

    const times = [], flowMax = [], concMax = [];
    let fMax = 0, cMax = 0, p = 0, s = 0;
    const np = cols.t.length;
    while (p < np || s < snaps.length) {
      const t = Math.min(p < np ? cols.t[p] : Infinity, s < snaps.length ? snaps[s].t : Infinity);
      let snapChanged = false;
      while (s < snaps.length && snaps[s].t === t) { snapIdx = s++; snapChanged = true; }
      const wasStale = stale;
      const snap = snapIdx >= 0 ? snaps[snapIdx] : null;
      const nowStale = !snap || t - snap.t > X.OF_SNAPSHOT_MAX_AGE_MS;
      const all = snapChanged || nowStale !== wasStale;
      if (all) setUnits(t);
      const touched = new Set();
      while (p < np && cols.t[p] === t) {
        const i = cols.contract[p], size = cols.size[p], side = cols.side[p];
        if (side === 0) B[i] += size; else if (side === 1) S[i] += size; else U[i] += size;
        touched.add(memberStrike[i]);
        p++;
      }
      if (all) for (let k = 0; k < strikes.length; k++) strikeValues(k);
      else for (const k of touched) strikeValues(k);
      for (let k = 0; k < strikes.length; k++) {
        if (flowAt[k] > fMax) fMax = flowAt[k];
        if (concAt[k] > cMax) cMax = concAt[k];
      }
      times.push(t); flowMax.push(fMax); concMax.push(cMax);
    }
    const T = Float64Array.from(times), F = Float64Array.from(flowMax), C = Float64Array.from(concMax);
    return (t) => {
      let lo = 0, hi = T.length; // first input time > t, so i is the last input time <= t
      while (lo < hi) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid + 1; else hi = mid; }
      const i = lo - 1;
      return i < 0 ? { flow: 0, conc: 0 } : { flow: F[i], conc: C[i] };
    };
  }

  // Live: running maximum of what this page has observed for one session day, kept in the
  // supplied storage (localStorage in the page) so reconnects and reloads never shrink it.
  function liveScale(storage, key = 'orderFlowProfileScale') {
    let state = null;
    try { state = JSON.parse(storage && storage.getItem(key)); } catch (e) { state = null; }
    return {
      update(view, day, now) {
        if (!day) return null;
        if (!state || state.day !== day) state = { day, flow: 0, conc: 0, since: now };
        const e = viewExtent(view);
        state.flow = Math.max(state.flow, e.flow);
        state.conc = Math.max(state.conc, e.conc);
        try { storage && storage.setItem(key, JSON.stringify(state)); } catch (err) { /* storage optional */ }
        return { flow: state.flow, conc: state.conc, since: state.since };
      },
      get: () => state && { ...state },
    };
  }

  // Round a scale up to a labelled step (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8 × 10^k). Monotone, so
  // a growing input never shrinks it, and fine enough that bars keep most of the width.
  function niceCeil(v) {
    if (!(v > 0)) return 0;
    const e = 10 ** Math.floor(Math.log10(v));
    for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v * (1 - 1e-12)) return m * e;
    return 10 * e;
  }

  // Row geometry in scale units: flow positions in [-1, 1] around zero, concentration in
  // [0, 1]. Missing inputs stay null (drawn as gaps); partial coverage carries k/n.
  function profileRows(view, p, scale) {
    const fs = niceCeil(scale.flow), cs = niceCeil(scale.conc);
    const fx = (v) => (v == null || !fs ? null : v / fs), cx = (v) => (v == null || !cs ? null : v / cs);
    return ((view && view.strikes) || []).map((s) => {
      const f = s.flow[p], c = s.concentration;
      return {
        strike: s.strike,
        flow: f.base == null ? null : {
          base: f.base, low: f.low, high: f.high, x: fx(f.base), xLow: fx(f.low), xHigh: fx(f.high),
          uncertain: f.uncertain, sign: f.uncertain ? 0 : Math.sign(f.base),
          partial: f.covered < f.of ? `${f.covered}/${f.of}` : null,
        },
        conc: c.value == null ? null : { value: c.value, x: cx(c.value), coverage: `${c.covered}/${c.of}`, partial: c.covered < c.of },
      };
    });
  }

  // Text equivalent of the chart for screen readers and the details line.
  function profileSummary(view, p, scale, selected, clock, money) {
    if (!view || !view.strikes.length) return 'Strike profile: no covered contracts yet.';
    const rows = profileRows(view, p, scale);
    const fs = niceCeil(scale.flow), cs = niceCeil(scale.conc);
    const head = `Strike profile at ${clock(view.t)} ET, ${p === 1 ? '100%' : '50%'} participation. Flow scale ±${money(fs).replace(/^\+/, '')}, OI scale 0–${money(cs).replace(/^\+/, '')} per 1% QQQ.`;
    const gaps = rows.filter((r) => !r.flow).map((r) => r.strike);
    const pos = rows.filter((r) => r.flow && r.flow.sign > 0).map((r) => r.strike);
    const neg = rows.filter((r) => r.flow && r.flow.sign < 0).map((r) => r.strike);
    const und = rows.filter((r) => r.flow && r.flow.sign === 0).map((r) => r.strike);
    const parts = [head];
    if (pos.length) parts.push(`Positive estimated flow gamma: ${pos.join(', ')}.`);
    if (neg.length) parts.push(`Negative: ${neg.join(', ')}.`);
    if (und.length) parts.push(`Sign not determined (scenarios cross zero): ${und.join(', ')}.`);
    if (gaps.length) parts.push(`No coverage: ${gaps.join(', ')}.`);
    const sel = rows.find((r) => r.strike === selected);
    if (sel) {
      parts.push(`Selected ${sel.strike}: ` + (sel.flow
        ? `flow ${money(sel.flow.base)} (scenarios ${money(sel.flow.low)} … ${money(sel.flow.high)})`
        : 'flow no coverage') + `, OI concentration ${sel.conc ? money(sel.conc.value).replace(/^\+/, '') + ' (' + sel.conc.coverage + ' contracts)' : 'no coverage'}.`);
    }
    return parts.join(' ');
  }

  const api = { viewExtent, replayScale, liveScale, niceCeil, profileRows, profileSummary };
  if (typeof module !== 'undefined') module.exports = api;
  else root.OrderFlowProfile = api;
})(typeof window !== 'undefined' ? window : globalThis);
