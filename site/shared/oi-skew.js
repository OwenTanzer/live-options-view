// oi-skew.js — pure state/formatting for the OI skew meter (Skew + Direction
// H1, Nov 2026). Classic script like the other site/shared modules: the page
// loads it with its own <script> tag and gets the names below as globals;
// under Node it exports them (guard at the bottom). No DOM access.
//
// Two different things are shown and must never be confused:
//
//  1. RAW reference skew — `market_readings[reading_id=reference_oi_skew]` on
//     intraday/latest.json (market_data/overview/readings.py
//     compute_reference_oi_skew). Fixed ±2% band, no account parameters, no
//     session anchor: imbalance_ratio = (call_oi - put_oi) / (call_oi + put_oi).
//     It is a measurement, not the bot's signal.
//
//  2. The bot's EFFECTIVE signal — what crassus/crassus/strategies/oi_skew.py
//     `_decide_core` concludes for one account: the account's own band,
//     |imbalance| >= imbalance_threshold, |imbalance - session_start| >=
//     min_change_from_session_start, band_strikes >= min_band_strikes. The
//     session-start anchor lives only in the runner (per account and band,
//     reset by UTC date and by a runner restart), so the page can show this
//     ONLY from a projection the runner publishes. It is never inferred from
//     the browser's own first page load.
//
// classifyOiSkewSignal() reproduces the signal gates of `_decide_core` in the
// same order and with the same comparisons (strict `<` → neutral, so exactly
// at a threshold is directional). tests/oi_skew_parity.test.js runs it over
// fixtures whose expected states were produced by the real Python strategy
// (crassus/scripts/oi_skew_meter_parity.py); position-management branches
// (held positions, quotes) are deliberately outside the meter.
//
// Open interest is prior-day settled positioning. Nothing here is fresh
// intraday flow or a win probability, and the page says so.

const OI_SKEW_DEFAULTS = Object.freeze({
  near_money_pct: 0.02,
  imbalance_threshold: 0.3,
  min_change_from_session_start: 0.1,
  min_band_strikes: 4,
});

// Same bounds the header already uses: a snapshot older than the chain
// "live" bound is stale; older than the VWAP/momentum closed bound is a
// leftover from a prior session.
const OI_SKEW_STALE_AFTER_MS = 2 * 60 * 1000;
const OI_SKEW_CLOSED_AFTER_MS = 4 * 60 * 60 * 1000;

function _finite(x) {
  return typeof x === 'number' && Number.isFinite(x);
}

// The bot's signal gates, in `_decide_core` order. Inputs are the values the
// strategy computes (or a projection carries); params default exactly like
// the strategy's `params.get(..., DEFAULT_*)`.
function classifyOiSkewSignal(input, params) {
  const p = { ...OI_SKEW_DEFAULTS, ...(params || {}) };
  const imbalance = _finite(input?.imbalance_ratio) ? input.imbalance_ratio : null;
  const strikes = Number.isInteger(input?.band_strikes) ? input.band_strikes : 0;
  const start = _finite(input?.session_start_imbalance) ? input.session_start_imbalance : null;
  const delta = imbalance != null && start != null ? imbalance - start : null;
  const base = { imbalance, delta, session_start: start, band_strikes: strikes, params: p };
  if (imbalance == null) {
    return { ...base, state: 'neutral', gate: 'no_open_interest',
      reason: `No open interest in the ±${(p.near_money_pct * 100).toFixed(1)}% band.` };
  }
  if (strikes < p.min_band_strikes) {
    return { ...base, state: 'neutral', gate: 'insufficient_coverage',
      reason: `Only ${strikes} strike(s) carry OI; needs ${p.min_band_strikes}.` };
  }
  if (Math.abs(imbalance) < p.imbalance_threshold) {
    return { ...base, state: 'neutral', gate: 'below_threshold',
      reason: `|${imbalance.toFixed(3)}| is below the ${p.imbalance_threshold} threshold.` };
  }
  if (delta == null || Math.abs(delta) < p.min_change_from_session_start) {
    return { ...base, state: 'neutral', gate: 'insufficient_session_change',
      reason: delta == null
        ? 'No session-start anchor yet.'
        : `Moved ${delta >= 0 ? '+' : ''}${delta.toFixed(3)} from the session start; needs ±${p.min_change_from_session_start}.` };
  }
  return { ...base, state: imbalance > 0 ? 'bullish' : 'bearish', gate: 'passed',
    reason: `Imbalance ${imbalance.toFixed(3)} clears ±${p.imbalance_threshold} and moved `
      + `${delta >= 0 ? '+' : ''}${delta.toFixed(3)} from the session start.` };
}

function _snapshotMs(payload, reading) {
  const ts = reading?.input?.snapshot_timestamp || payload?.timestamp;
  const ms = Date.parse(ts || '');
  return Number.isFinite(ms) ? ms : null;
}

// Raw reference reading → display model. `availability` is one of:
//   live | stale | closed | unavailable | no_data
function describeReferenceOiSkew(payload, nowMs = Date.now()) {
  const symbol = payload?.symbol || null;
  const readings = Array.isArray(payload?.market_readings) ? payload.market_readings : [];
  const reading = readings.find(r => r && r.reading_id === 'reference_oi_skew') || null;
  if (!payload || !reading) {
    return { availability: 'unavailable', symbol, value: null,
      text: 'OI skew: unavailable', detail: 'This snapshot carries no reference OI skew reading.' };
  }
  if (reading.symbol && symbol && reading.symbol !== symbol) {
    return { availability: 'unavailable', symbol, value: null,
      text: 'OI skew: unavailable', detail: `Reading is for ${reading.symbol}, not ${symbol}.` };
  }
  const v = reading.values || {};
  const value = _finite(v.imbalance_ratio) ? v.imbalance_ratio : null;
  const snapMs = _snapshotMs(payload, reading);
  const ageMs = snapMs == null ? null : nowMs - snapMs;
  const band = reading.calculation?.parameters?.near_money_pct;
  const common = {
    symbol: reading.symbol || symbol, value,
    call_oi: _finite(v.near_money_call_oi) ? v.near_money_call_oi : null,
    put_oi: _finite(v.near_money_put_oi) ? v.near_money_put_oi : null,
    band_strikes: Number.isInteger(v.band_strikes) ? v.band_strikes : null,
    band_pct: _finite(band) ? band : null,
    snapshot_ms: snapMs, age_ms: ageMs,
    calc_version: reading.calculation?.version || null,
  };
  if (value == null || reading.status?.code !== 'ok') {
    return { ...common, availability: 'no_data', value: null,
      text: 'OI skew: no data',
      detail: `No open interest in the reference band (${reading.status?.reason || 'status not ok'}).` };
  }
  let availability = 'live';
  if (ageMs == null || ageMs > OI_SKEW_CLOSED_AFTER_MS) availability = 'closed';
  else if (ageMs > OI_SKEW_STALE_AFTER_MS || reading.freshness?.status !== 'live') availability = 'stale';
  const lean = value > 0 ? 'call-heavy' : value < 0 ? 'put-heavy' : 'balanced';
  const sign = value > 0 ? '+' : '';
  return { ...common, availability, lean,
    text: `OI skew ${sign}${value.toFixed(3)} (${lean})`,
    detail: `Reference ±${((common.band_pct ?? 0.02) * 100).toFixed(0)}% band: `
      + `${Math.round(common.call_oi ?? 0).toLocaleString('en-US')} call vs `
      + `${Math.round(common.put_oi ?? 0).toLocaleString('en-US')} put contracts over `
      + `${common.band_strikes ?? '?'} strikes. Prior-day settled OI — not intraday flow or a win probability.` };
}

// A refresh that failed or returned no rows must never leave a reading
// labelled live: the last good snapshot is kept on screen (it is still the
// latest thing we know) but is at best "stale", and says why. Age-based
// stale/closed from describeReferenceOiSkew still applies on top.
function degradeOiSkewForRefreshFailure(desc, reason) {
  if (!reason) return desc;
  const note = `Latest refresh failed (${reason}); showing the last good snapshot.`;
  const availability = desc.availability === 'live' ? 'stale' : desc.availability;
  return { ...desc, availability, refresh_failed: true, refresh_error: reason,
    detail: `${note} ${desc.detail || ''}`.trim() };
}

// Bot projection → display model. `projection` is the read-only document the
// runner would publish (crassus oi_skew_meter projection, schema
// oi_skew_meter.v1). Absent → the state is reported as not published; the
// meter never substitutes the raw reading for the bot's state.
function describeBotOiSkew(projection, nowMs = Date.now()) {
  if (!projection || projection.schema_version !== 'oi_skew_meter.v1') {
    return { availability: 'not_published', state: 'unknown',
      text: 'Bot signal: not published',
      detail: 'The OI Skew bot\'s session anchor lives only in the strategy runner; '
        + 'no read-only projection of it is published yet, so its effective state is not shown.' };
  }
  // The projection says when its own evidence is incomplete (legacy ledger
  // rows, unknown source revision...). That is shown as unavailable, never
  // classified from whatever fields happen to be present.
  // Review 5:42 PM: a status of ok is not trusted on its own -- every field
  // the display relies on is re-checked here with the builder's own rules
  // (crassus/crassus/oi_skew_projection.py), so a document missing or
  // mangling provenance can never produce a live signal.
  const problems = projection.status?.code !== 'ok'
    ? [projection.status?.reason || 'status not ok']
    : oiSkewProjectionProblems(projection);
  if (problems.length) {
    return { availability: 'unavailable', state: 'unknown', text: 'Bot signal: unavailable',
      problems,
      detail: `The published bot state is incomplete (${problems.join('; ')}); `
        + 'its effective signal is not shown.' };
  }
  const obsMs = Date.parse(projection.observed_snapshot_timestamp || '');
  const ageMs = Number.isFinite(obsMs) ? nowMs - obsMs : null;
  const cls = classifyOiSkewSignal(projection, projection.params);
  let availability = 'live';
  if (ageMs == null || ageMs > OI_SKEW_CLOSED_AFTER_MS) availability = 'closed';
  else if (ageMs > OI_SKEW_STALE_AFTER_MS) availability = 'stale';
  const label = { bullish: 'Bullish', bearish: 'Bearish', neutral: 'Neutral' }[cls.state];
  const rev = typeof projection.code_revision === 'string' ? projection.code_revision.slice(0, 7) : 'unknown';
  const provenance = `Anchor ${projection.session_start_imbalance} observed ${projection.anchor_observed_at} `
    + `(session ${projection.session_key}, ${projection.run_identity}); decided ${projection.decision_at}; `
    + `source ${rev}, ${projection.strategy_id} ${projection.strategy_version}.`;
  return { ...cls, availability, age_ms: ageMs,
    session_phase: projection.session_phase || null,
    anchor_observed_at: projection.anchor_observed_at, session_key: projection.session_key,
    run_identity: projection.run_identity, code_revision: projection.code_revision,
    decision_at: projection.decision_at,
    text: `Bot signal: ${label}${availability === 'live' ? '' : ` (${availability})`}`,
    detail: `${cls.reason} ${provenance}` };
}

// Field rules mirrored from the builder: an aware ISO instant, a real UTC
// calendar date, a lowercase git SHA, the digest run identity, finite
// numbers and complete params. Returns the offending field names.
// Date.parse normalizes impossible components (2026-02-30 -> Mar 2), so
// each calendar/clock component is validated explicitly, matching the
// Python builder's datetime.fromisoformat (review 7:04 PM).
function _awareInstant(v) {
  if (typeof v !== 'string') return false;
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|([+-])(\d{2}):(\d{2}))$/.exec(v);
  if (!m || !_calendarDate(m[1])) return false;
  if (Number(m[2]) > 23 || Number(m[3]) > 59 || (m[4] !== undefined && Number(m[4]) > 59)) return false;
  if (m[5] !== 'Z' && (Number(m[7]) > 23 || Number(m[8]) > 59)) return false;
  return Number.isFinite(Date.parse(v));
}
function _calendarDate(v) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
  const d = new Date(`${v}T00:00:00Z`);
  return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === v;
}
function oiSkewProjectionProblems(p) {
  const bad = [];
  if (typeof p.strategy_version !== 'string' || !p.strategy_version.trim()) bad.push('strategy_version');
  if (typeof p.code_revision !== 'string' || !/^[0-9a-f]{7,40}$/.test(p.code_revision)) bad.push('code_revision');
  if (typeof p.run_identity !== 'string' || !/^run:[0-9a-f]{16}$/.test(p.run_identity)) bad.push('run_identity');
  if (!_calendarDate(p.session_key)) bad.push('session_key');
  if (!_awareInstant(p.anchor_observed_at)) bad.push('anchor_observed_at');
  if (!_finite(p.session_start_imbalance)) bad.push('session_start_imbalance');
  if (!(Number.isInteger(p.band_strikes) && p.band_strikes >= 0)) bad.push('band_strikes');
  const prm = p.params;
  if (!prm || !_finite(prm.near_money_pct) || !_finite(prm.imbalance_threshold)
      || !_finite(prm.min_change_from_session_start) || !Number.isInteger(prm.min_band_strikes)) bad.push('params');
  if (!_awareInstant(p.observed_snapshot_timestamp)) bad.push('observed_snapshot_timestamp');
  if (!_awareInstant(p.decision_at)) bad.push('decision_at');
  return bad;
}

// A projection fetch that failed: unavailable, never the last state as live.
function describeBotOiSkewFetchFailure(reason) {
  return { availability: 'unavailable', state: 'unknown', text: 'Bot signal: unavailable',
    detail: `The bot state could not be loaded (${reason}).` };
}

// Marker position on a −1…+1 diverging scale, as a 0…100 percentage.
function oiSkewMeterPercent(value) {
  if (!_finite(value)) return null;
  const clamped = Math.max(-1, Math.min(1, value));
  return (clamped + 1) * 50;
}

if (typeof module !== 'undefined') {
  module.exports = { OI_SKEW_DEFAULTS, OI_SKEW_STALE_AFTER_MS, OI_SKEW_CLOSED_AFTER_MS,
    classifyOiSkewSignal, describeReferenceOiSkew, degradeOiSkewForRefreshFailure, describeBotOiSkew,
    describeBotOiSkewFetchFailure, oiSkewProjectionProblems,
    oiSkewMeterPercent };
}
