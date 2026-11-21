// Order Flow data models: turn an archive replay file or the live feed into the same
// per-strike exposure view at a displayed time. Classic script (needs
// order-flow-exposure.js loaded first) or CommonJS in node tests.

(function (root) {
  const X = typeof module !== 'undefined' ? require('./order-flow-exposure.js') : root.OrderFlowExposure;
  const SIDE_CODES = ['buy', 'sell', 'unknown'];
  const OPTION = /^QQQ(\d{6})([CP])(\d{8})$/;

  function parseSymbol(symbol) {
    const m = OPTION.exec(symbol || '');
    if (!m) return null;
    return { symbol, expiration: `20${m[1].slice(0, 2)}-${m[1].slice(2, 4)}-${m[1].slice(4, 6)}`, type: m[2],
      strike: Number(m[3]) / 1000 };
  }

  // The prior weekday: the clearing date settled OI most likely reflects. Holidays are not
  // modelled, so the page always labels this as inferred.
  function priorWeekday(day) {
    const d = new Date(`${day}T12:00:00Z`);
    do d.setUTCDate(d.getUTCDate() - 1); while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
    return d.toISOString().slice(0, 10);
  }

  function decodeReplay(json) {
    if (!json || json.schema !== 'order-flow-replay/v2') throw Error('Unsupported replay schema');
    const p = json.prints, n = p.count;
    if (![p.dt, p.contract, p.size, p.cents, p.side].every((a) => Array.isArray(a) && a.length === n)) throw Error('Invalid replay prints');
    const t = new Float64Array(n);
    let clock = p.t0;
    for (let i = 0; i < n; i++) { clock += p.dt[i]; t[i] = clock; }
    const cols = { t, contract: Uint8Array.from(p.contract), size: Uint32Array.from(p.size),
      cents: Uint32Array.from(p.cents), side: Uint8Array.from(p.side) };
    const snapshots = json.snapshots.slice().sort((a, b) => a.t - b.t);
    return { json, contracts: json.contracts, cols, snapshots, oi: json.oi, coverage: json.coverage,
      multiplier: json.multiplier, session: json.session, acc: X.ofColumnAccumulator(cols, json.contracts.length) };
  }

  function strikesOf(contracts) {
    return [...new Set(contracts.map((c) => c.strike))].sort((a, b) => b - a);
  }

  function group(contractRows) {
    const by = new Map();
    for (const c of contractRows) {
      if (!by.has(c.strike)) by.set(c.strike, []);
      by.get(c.strike).push(c);
    }
    return [...by.entries()].sort((a, b) => b[0] - a[0]).map(([strike, cs]) => X.ofStrike(strike, cs));
  }

  // Replay view at time t. Snapshot inputs come from the latest snapshot at or before t.
  function replayAt(r, t) {
    const found = X.ofSnapshotAt(r.snapshots, t);
    const usable = found && !found.stale;
    const rows = r.contracts.map((c, i) => {
      const obs = (r.oi[i] || []).filter((o) => o.first_seen <= t).at(-1) || null;
      const snapOi = usable ? found.snap.oi[i] : null;
      return { symbol: c.symbol, strike: c.strike, type: c.type, multiplier: r.multiplier,
        flow: r.acc(i, t), oi: snapOi ?? null, oiFirstSeen: obs && obs.value === snapOi ? obs.first_seen : null,
        gamma: usable ? found.snap.gamma[i] : null, spot: usable ? found.snap.spot : null };
    });
    return { mode: 'replay', t, strikes: group(rows), contracts: rows,
      snapshot: found ? { t: found.snap.t, key: found.snap.key, age: found.age, stale: found.stale } : null,
      clearingDate: priorWeekday(r.session), coverage: coverageAt(r.coverage, t) };
  }

  function coverageAt(c, t) {
    const gaps = (c.gaps || []).filter((g) => g.start == null || g.start <= t);
    return { from: c.first_print ?? c.open, to: Math.min(t, c.last_print ?? t), open: c.open, close: c.close,
      gaps, incomplete: (c.first_print ?? Infinity) - c.open > 60000 || gaps.length > 0 };
  }

  // Live view: session counters from the collector feed plus the latest public chain snapshot.
  function liveAt(feed, chain, now, multiplier = X.OF_MULTIPLIER) {
    const session = feed && feed.session;
    const contracts = new Map();
    for (const [symbol, c] of Object.entries((session && session.contracts) || {})) {
      const parsed = parseSymbol(symbol);
      if (parsed) contracts.set(symbol, { ...parsed, flow: { B: c.B, S: c.S, U: c.U, prints: c.prints } });
    }
    for (const row of (feed && feed.rows) || []) {
      if (!contracts.has(row.symbol) && parseSymbol(row.symbol)) contracts.set(row.symbol, { ...parseSymbol(row.symbol), flow: null });
    }
    for (const [symbol, from] of Object.entries(session?.added || {})) {
      const parsed = parseSymbol(symbol);
      if (!parsed) continue;
      const contract = contracts.get(symbol) || { ...parsed, flow: null };
      contract.coverageFrom = from;
      if (from != null && !contract.flow) contract.flow = { B: 0, S: 0, U: 0, prints: 0 };
      contracts.set(symbol, contract);
    }
    const snapT = chain ? Date.parse(chain.timestamp) : NaN;
    const age = Number.isFinite(snapT) ? now - snapT : null;
    const usable = age != null && age >= 0 && age <= X.OF_SNAPSHOT_MAX_AGE_MS;
    const chainRows = new Map(((chain && chain.rows) || []).map((r) => [r.OptionSymbol, r]));
    const finite = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null);
    const rows = [...contracts.values()].map((c) => {
      const cr = usable ? chainRows.get(c.symbol) : null;
      return { symbol: c.symbol, strike: c.strike, type: c.type, multiplier, flow: c.flow, coverageFrom: c.coverageFrom,
        oi: cr ? finite(cr.OpenInterest) : null, oiFirstSeen: null, gamma: cr ? finite(cr.Gamma) : null,
        spot: cr ? finite(cr.UnderlyingPrice ?? chain.underlying_price) : null };
    });
    const gaps = (session && session.gaps) || [];
    let coverage = null;
    if (session) {
      const open = (session.rebuilt && session.rebuilt.session_open) || etOpenMs(session.day);
      // History must start at the open: a first print more than a minute after it, or a
      // session that has no print yet although the open has passed, is a missing opening.
      const late = session.first == null ? now > open + OPENING_GRACE_MS : session.first > open + OPENING_GRACE_MS;
      const missingOpening = late && !gaps.some((g) => g.start != null && g.start <= open + OPENING_GRACE_MS)
        ? { start: open, end: session.first ?? null } : null;
      coverage = { from: session.first, to: session.last, open, gaps, rebuilt: session.rebuilt, missingOpening,
        incomplete: gaps.length > 0 || late || (session.rebuilt != null && session.rebuilt.status !== 'rebuilt') };
    }
    return { mode: 'live', t: now, strikes: group(rows), contracts: rows,
      snapshot: Number.isFinite(snapT) ? { t: snapT, key: chain.snapshot_key, age, stale: !usable } : null,
      clearingDate: session ? priorWeekday(session.day) : null, coverage };
  }

  const OPENING_GRACE_MS = 60 * 1000;

  // 09:30 America/New_York on an ISO date, as epoch milliseconds.
  function etOpenMs(day) {
    const guess = Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10), 13, 30);
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour12: false,
      hour: '2-digit', minute: '2-digit' }).formatToParts(new Date(guess));
    const h = +parts.find((p) => p.type === 'hour').value % 24, m = +parts.find((p) => p.type === 'minute').value;
    return guess - ((h * 60 + m) - (9 * 60 + 30)) * 60000;
  }

  const api = { SIDE_CODES, parseSymbol, priorWeekday, decodeReplay, replayAt, liveAt, strikesOf, etOpenMs };
  if (typeof module !== 'undefined') module.exports = api;
  else root.OrderFlowModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
