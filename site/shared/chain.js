// chain.js — chain underlying selection (#121), moved out of docs/shared.js
// (B3 slice 5, Nov 2026). Verbatim move: the code below is byte-identical to
// what lived in shared.js lines 33-121. Plain classic script (no module
// system, no build step), like the file it came from: the live page and
// diag.html load it with its own <script> tag (see docs/index.html, right
// after the format.js tag), where it defines CHAIN_UNDERLYINGS /
// chainLatestPath / occRoot / validateChainPayload / ChainSelection /
// relativeOiRanges / summarizeSpyHealth as globals; under Node it exports the
// same seven names (see the guard at the bottom). The chain names are the
// last concern left in shared.js — after this slice shared.js is the header
// comment plus the Node re-export entry only. No ESM conversion in this step.

// Under Node, relativeOiRanges defaults its display to DISPLAY from the
// heatmap concern. That default is read as a bare global, so chain.js must
// NOT declare it: a `var DISPLAY` here would clash with the `const DISPLAY`
// heatmap.js declares — in the browser and in the vm test contexts that is a
// SyntaxError ("Identifier 'DISPLAY' has already been declared") at load
// time, before any guard could run. Instead the Node guard below sets the
// globalThis property (no binding declared), which the bare read resolves to
// under Node; in the browser / vm the guard never runs and the read resolves
// to heatmap.js's const, as before the split. The entry used to carry this
// globalThis line itself; it moved here with the code that needs it.
if (typeof module !== 'undefined' && typeof require === 'function') {
  globalThis.DISPLAY = require('./heatmap.js').DISPLAY;
}

// ── chain underlying selection (#121) ───────────────────────────────────────
// QQQ keeps its original artifact; SPY has its own tree (see
// SPY_COLLECTION.md). Anything not listed is unsupported -- never mapped
// onto QQQ's file.
const CHAIN_UNDERLYINGS = ['QQQ', 'SPY', 'IBIT', 'SLV', 'DRAM'];
const CHAIN_LATEST_PATHS = {
  QQQ: 'intraday/latest.json', SPY: 'intraday/spy/latest.json', IBIT: 'intraday/ibit/latest.json',
  SLV: 'intraday/slv/latest.json', DRAM: 'intraday/dram/latest.json',
};
// Underlyings that collect their nearest listed expiration rather than 0DTE.
const NEAREST_EXPIRY_UNDERLYINGS = ['IBIT', 'SLV', 'DRAM'];

function chainLatestPath(symbol) {
  return Object.hasOwn(CHAIN_LATEST_PATHS, symbol) ? CHAIN_LATEST_PATHS[symbol] : null;
}

function occRoot(occSymbol) {
  const match = /^([A-Z]+)/.exec(String(occSymbol || '').replace(/\s+/g, ''));
  return match ? match[1] : '';
}

// Rejects a payload that doesn't prove it belongs to `symbol`, so a QQQ
// snapshot (or a mixed one) can never be rendered under a SPY heading or
// vice versa. Pre-#121 QQQ payloads carry no top-level symbol; they're only
// accepted when QQQ itself was requested.
function validateChainPayload(data, symbol) {
  if (!CHAIN_UNDERLYINGS.includes(symbol)) return { ok: false, reason: `${symbol} is not a supported underlying` };
  if (!data || typeof data !== 'object' || !Array.isArray(data.rows)) return { ok: false, reason: 'snapshot is malformed' };
  const declared = data.symbol ?? (symbol === 'QQQ' ? 'QQQ' : null);
  if (declared !== symbol) return { ok: false, reason: `snapshot is labeled ${declared ?? 'without a symbol'}, not ${symbol}` };
  const marketSymbol = data.underlying_market?.symbol;
  if (marketSymbol != null && marketSymbol !== symbol) return { ok: false, reason: `underlying readings belong to ${marketSymbol}` };
  if (data.rows.some(row => occRoot(row?.OptionSymbol) !== symbol)) return { ok: false, reason: `snapshot contains non-${symbol} contracts` };
  if (NEAREST_EXPIRY_UNDERLYINGS.includes(symbol) && (!/^\d{4}-\d{2}-\d{2}$/.test(data.expiration || '') ||
      data.rows.some(row => row.Expiration !== data.expiration))) return { ok: false, reason: 'missing or mixed expiration' };
  return { ok: true, reason: null };
}

// Tracks which underlying is on screen. Every request takes a token; a
// response is applied only if its token is still current, so a slow reply
// for the previous selection can't overwrite (or be labeled as) the new one.
class ChainSelection {
  constructor(symbol = 'QQQ') {
    this.symbol = CHAIN_UNDERLYINGS.includes(symbol) ? symbol : 'QQQ';
    this.generation = 0;
  }
  select(symbol) {
    if (!CHAIN_UNDERLYINGS.includes(symbol)) return false;
    if (symbol !== this.symbol) { this.symbol = symbol; this.generation++; }
    return true;
  }
  token() { return { symbol: this.symbol, generation: this.generation }; }
  accepts(token) { return !!token && token.symbol === this.symbol && token.generation === this.generation; }
}

// OIranges.csv is calibrated on QQQ's history only. For another underlying
// there is no calibration yet, so heat levels are relative to this snapshot's
// own nonzero OI distribution (p25/p50/p75/p90) -- labeled as such in the UI.
// Returns the same shape parseRangesCsv() produces, for getThresh().
function relativeOiRanges(rows, display = DISPLAY) {
  const values = (rows || []).map(r => parseInt(r?.OpenInterest ?? 0) || 0).filter(v => v > 0).sort((a, b) => a - b);
  if (!values.length) return null;
  const q = p => values[Math.min(values.length - 1, Math.floor(p * (values.length - 1)))];
  const thresholds = [q(0.25), q(0.5), q(0.75), q(0.9)];
  const regular = {};
  for (let o = -display; o <= display; o++) regular[o] = { call: thresholds, put: thresholds };
  return { '0DTE_Regular': regular };
}

function summarizeSpyHealth(payload, nowMs = Date.now()) {
  const spy = payload?.spy;
  if (!spy) return { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY health has not been published' };
  const updated = Date.parse(payload.updated_at);
  if (!Number.isFinite(updated) || nowMs - updated > 45_000 || nowMs - updated < -30_000) {
    return { state: 'stale', text: 'SPY: stale', detail: 'SPY health update is old or untimed' };
  }
  if (spy.collection_status !== 'collecting') {
    const status = spy.collection_status || 'unavailable';
    return { state: 'unavailable', text: `SPY: ${status.replaceAll('_', ' ')}`, detail: spy.reason || status };
  }
  const snapshotAt = Date.parse(spy.last_snapshot_upload_time);
  if (!Number.isFinite(snapshotAt)) {
    const started = Date.parse(payload.process_start_time);
    if (Number.isFinite(started) && nowMs - started > 180_000) {
      return { state: 'unavailable', text: 'SPY: unavailable', detail: 'No SPY snapshot uploaded' };
    }
    return { state: 'connecting', text: 'SPY: warming up', detail: 'No SPY snapshot uploaded yet' };
  }
  if (nowMs - snapshotAt > 180_000 || nowMs - snapshotAt < -30_000 || spy.spot?.status === 'stale') {
    return { state: 'stale', text: 'SPY: stale', detail: 'SPY snapshot or spot is stale' };
  }
  if (spy.spot?.status !== 'live') {
    return { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY spot has no current provider observation' };
  }
  return { state: 'live', text: 'SPY: live', detail: 'SPY snapshot and spot are current' };
}
if (typeof module !== 'undefined') {
  module.exports = { CHAIN_UNDERLYINGS, chainLatestPath, occRoot, validateChainPayload, ChainSelection, relativeOiRanges, summarizeSpyHealth };
}
