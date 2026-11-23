// SPY and IBIT near-money OI skew meters. Classic script, loaded after
// js/data-fetch-cycle.js and js/oi-skew-meter.js. Each meter mirrors the QQQ
// meter's reference reading (the collector's `reference_oi_skew` reading, the
// same pure describers in shared/oi-skew.js) for that underlying's snapshot
// (intraday/spy/latest.json, intraday/ibit/latest.json — both already on the
// bounded browser-data allowlist). No bot trades SPY or IBIT, so these panels
// show the raw reference reading only, never a bot signal. IBIT additionally
// shows a separately labelled, page-calculated adaptive reading (see below).
//
// Each meter fetches on its own REFRESH cadence through fetchChain(symbol)
// (payload validated against the requested symbol), so the shared
// fetchLatest cycle is untouched; when the symbol is the selected chain it
// reuses that payload instead of requesting it twice.

const REFERENCE_OI_SKEW_SYMBOLS = ['SPY', 'IBIT'];

const REFERENCE_OI_SKEW_LABEL = {
  live: 'live', stale: 'stale', closed: 'session closed', unavailable: 'unavailable', no_data: 'no data',
};

function _refSkewEtTime(ms) {
  if (ms == null) return 'time unknown';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', second: '2-digit',
    }).format(new Date(ms)) + ' ET';
  } catch (_) {
    return new Date(ms).toISOString().slice(11, 19) + ' UTC';
  }
}

function _refSkewSigned(value) {
  return value > 0 ? `+${value.toFixed(3)}` : value < 0 ? `−${Math.abs(value).toFixed(3)}` : '0.000';
}

// ── Adaptive near-money OI skew (IBIT only, page-calculated) ────────────────
// Approved by Chej/Owen 2026-11-16 for IBIT, whose $1 strikes on a ~$40
// underlying leave the published ±2% band with one or two strikes. This is a
// separate, labelled reading: it is NOT the collector's reference_oi_skew and
// NOT the QQQ bot signal. SPY and QQQ keep their published readings only.
//
// Rule: half-width = max(floor, distance to the min_strikes-th nearest strike
// carrying OI), as a fraction of spot, unrounded. Every strike with OI whose
// distance is <= that half-width is included, so strikes tied at the boundary
// are all in (the band can hold more than min_strikes). If fewer than
// min_strikes strikes carry OI, or the min_strikes-th one is beyond the cap,
// there is no adaptive value. Strike/OI semantics mirror the collector and
// bot (_near_money_oi): a strike counts when any row at it has OI > 0; call
// and put OI are summed by Type. ADAPTIVE_OI_SKEW_TIE_EPS only absorbs
// floating-point noise so mirror-image strikes (e.g. spot ± $1.78) tie.
const ADAPTIVE_OI_SKEW_PARAMS = Object.freeze({ floor_pct: 0.02, cap_pct: 0.10, min_strikes: 4 });
const ADAPTIVE_OI_SKEW_SYMBOLS = ['IBIT'];
const ADAPTIVE_OI_SKEW_TIE_EPS = 1e-12;

function computeAdaptiveOiSkew(rows, spot, params = ADAPTIVE_OI_SKEW_PARAMS) {
  const p = { ...ADAPTIVE_OI_SKEW_PARAMS, ...(params || {}) };
  const base = { params: p };
  if (!Array.isArray(rows)) return { ...base, ok: false, reason: 'no_rows', strikes_with_oi: 0 };
  if (!(typeof spot === 'number' && Number.isFinite(spot) && spot > 0)) {
    return { ...base, ok: false, reason: 'no_spot', strikes_with_oi: 0 };
  }
  const distance = new Map();                       // strike -> |strike - spot| / spot
  for (const r of rows) {
    const k = r ? r.Strike : null;
    const oi = r ? r.OpenInterest : null;
    if (typeof k !== 'number' || !Number.isFinite(k) || !(typeof oi === 'number' && Number.isFinite(oi) && oi > 0) || !['call', 'put'].includes(r.Type)) continue;
    distance.set(k, Math.abs(k - spot) / spot);
  }
  const sorted = [...distance.values()].sort((a, b) => a - b);
  if (sorted.length < p.min_strikes) {
    return { ...base, ok: false, reason: 'insufficient_strikes', strikes_with_oi: sorted.length };
  }
  const nth = sorted[p.min_strikes - 1];
  if (nth > p.cap_pct + ADAPTIVE_OI_SKEW_TIE_EPS) {
    return { ...base, ok: false, reason: 'beyond_cap', strikes_with_oi: sorted.length, nth_distance: nth };
  }
  const halfWidth = Math.min(p.cap_pct, Math.max(p.floor_pct, nth));
  let callOi = 0, putOi = 0;
  const inBand = new Set();
  for (const r of rows) {
    const k = r ? r.Strike : null;
    const oi = r ? r.OpenInterest : null;
    if (typeof k !== 'number' || !Number.isFinite(k) || !(typeof oi === 'number' && Number.isFinite(oi) && oi > 0) || !['call', 'put'].includes(r.Type)) continue;
    if (Math.abs(k - spot) / spot > halfWidth + ADAPTIVE_OI_SKEW_TIE_EPS) continue;
    inBand.add(k);
    if (r.Type === 'call') callOi += oi;
    else if (r.Type === 'put') putOi += oi;
  }
  const total = callOi + putOi;
  if (!(total > 0)) return { ...base, ok: false, reason: 'no_typed_oi', strikes_with_oi: sorted.length };
  const strikes = [...inBand].sort((a, b) => a - b);
  return { ...base, ok: true, half_width: halfWidth, nth_distance: nth, widened: nth > p.floor_pct,
    band_strikes: strikes.length, low_strike: strikes[0], high_strike: strikes[strikes.length - 1],
    call_oi: callOi, put_oi: putOi, imbalance: (callOi - putOi) / total, strikes_with_oi: sorted.length };
}

function _fmtStrike(k) { return Number.isInteger(k) ? String(k) : String(+k.toFixed(2)); }

// One meter per symbol; DOM ids are `${prefix}-oi-skew-*` with prefix = symbol lowercased.
function createReferenceOiSkewMeter(symbol) {
  const prefix = symbol.toLowerCase();
  const state = { payload: null, refreshError: null, seq: 0 };
  const el = suffix => document.getElementById(`${prefix}-oi-skew-${suffix}`);

  function update(payload, nowMs = Date.now()) {
    state.payload = payload;
    state.refreshError = null;
    render(nowMs);
  }

  function noteFailure(reason, nowMs = Date.now()) {
    state.refreshError = String(reason || 'refresh failed');
    render(nowMs);
  }

  async function refresh(fetchChainImpl = (typeof fetchChain === 'function' ? fetchChain : null)) {
    try {
      const selected = (typeof chainState === 'object' && chainState) ? chainState[symbol] : null;
      if (selected && selected.data && typeof chainSelection === 'object'
          && chainSelection && chainSelection.symbol === symbol) {
        state.seq++;                                 // supersede any in-flight own fetch
        update(selected.data);
        if (selected.error) noteFailure(selected.error);
        return;
      }
    } catch (_) { /* fall through to our own fetch */ }
    if (!fetchChainImpl) return;
    const seq = ++state.seq;
    const res = await fetchChainImpl(symbol);
    if (seq !== state.seq) return;                   // a newer refresh superseded this one
    if (res && res.data && Array.isArray(res.data.rows) && res.data.rows.length) update(res.data);
    else noteFailure((res && res.error) || `No ${symbol} rows in the latest snapshot.`);
  }

  function render(nowMs = Date.now()) {
    const root = el('meter');
    if (!root) return;
    const raw = degradeOiSkewForRefreshFailure(describeReferenceOiSkew(state.payload, nowMs), state.refreshError);
    root.dataset.state = raw.availability;
    root.dataset.lean = raw.lean || 'none';

    const track = el('track');
    const marker = el('marker');
    const label = REFERENCE_OI_SKEW_LABEL[raw.availability] || 'unavailable';
    const pct = oiSkewMeterPercent(raw.value);
    if (pct == null) {
      marker.hidden = true;
      el('value').textContent = '—';
      el('lean').textContent = state.payload || state.refreshError ? label : 'Waiting for snapshot';
      track.setAttribute('aria-valuenow', '0');
      track.setAttribute('aria-valuetext', `${symbol} OI skew ${label}`);
    } else {
      marker.hidden = false;
      marker.style.left = `${pct}%`;
      el('value').textContent = _refSkewSigned(raw.value);
      el('lean').textContent = raw.lean.charAt(0).toUpperCase() + raw.lean.slice(1);
      track.setAttribute('aria-valuenow', raw.value.toFixed(3));
      track.setAttribute('aria-valuetext', `${symbol} OI skew ${raw.value.toFixed(3)}, ${raw.lean}, ${label}`);
    }

    // A ±2% band on a low-priced underlying with $1 strikes (IBIT ≈ $40) can
    // hold only one or two strikes. Flag anything under the bot's own minimum
    // coverage so a one-strike ratio is not read as a solid lean.
    const minStrikes = (typeof OI_SKEW_DEFAULTS === 'object' && OI_SKEW_DEFAULTS.min_band_strikes) || 4;
    const thin = raw.value != null && Number.isInteger(raw.band_strikes) && raw.band_strikes < minStrikes;
    root.dataset.coverage = thin ? 'thin' : 'ok';
    const status = label + (raw.refresh_failed ? ' · refresh failed' : '')
      + (thin ? ` · thin: ${raw.band_strikes} strike${raw.band_strikes === 1 ? '' : 's'} (< ${minStrikes})` : '');
    el('meta').textContent =
      `${raw.symbol || symbol} · reference ±${((raw.band_pct ?? 0.02) * 100).toFixed(0)}% band · `
      + `snapshot ${_refSkewEtTime(raw.snapshot_ms)} · ${status}`;
    el('detail').textContent = raw.detail || '';
    root.title = raw.detail || '';
    if (typeof describeTruth === 'function') {
      // Coverage must not hide stale/closed observations or failed refreshes.
      renderTruthChip(el('truth'), describeTruth(thin && raw.availability === 'live' ? 'thin' : raw.availability, {
        observedMs: raw.snapshot_ms ?? null, nowMs,
        note: [thin ? `thin: ${raw.band_strikes} strike${raw.band_strikes === 1 ? '' : 's'}` : null,
               raw.refresh_failed ? 'refresh failed' : null].filter(Boolean).join(', ') || null,
      }));
    }
    if (ADAPTIVE_OI_SKEW_SYMBOLS.includes(symbol)) renderAdaptive(raw, label, nowMs);
  }

  // Adaptive block: same payload, same live/stale/closed/refresh-failed
  // availability as the published reading above it, never merged into it.
  function renderAdaptive(raw, label, nowMs) {
    // A published empty ±2% band can still have enough OI in the wider
    // window. Its no_data status says nothing about the snapshot's age.
    if (raw.availability === 'no_data') {
      const age = raw.snapshot_ms == null ? null : nowMs - raw.snapshot_ms;
      const freshness = state.payload?.market_readings?.find(r => r?.reading_id === 'reference_oi_skew')?.freshness?.status;
      const availability = age == null || age > OI_SKEW_CLOSED_AFTER_MS ? 'closed'
        : age > OI_SKEW_STALE_AFTER_MS || freshness !== 'live' || raw.refresh_failed ? 'stale' : 'live';
      raw = { ...raw, availability };
      label = REFERENCE_OI_SKEW_LABEL[availability];
    }
    const box = document.getElementById(`${prefix}-adaptive-oi-skew`);
    if (!box) return;
    const a = (id) => document.getElementById(`${prefix}-adaptive-oi-skew-${id}`);
    const payload = state.payload;
    const usable = payload && payload.symbol === symbol && Array.isArray(payload.rows)
      && raw.availability !== 'unavailable';
    const res = usable ? computeAdaptiveOiSkew(payload.rows, payload.underlying_price) : null;
    const marker = a('marker');
    const track = a('track');
    const p = ADAPTIVE_OI_SKEW_PARAMS;
    box.dataset.state = !usable ? 'unavailable' : res.ok ? raw.availability : 'insufficient';
    box.dataset.lean = res && res.ok ? (res.imbalance > 0 ? 'call-heavy' : res.imbalance < 0 ? 'put-heavy' : 'balanced') : 'none';
    const expiry = payload && payload.expiration ? `exp ${payload.expiration}` : 'expiry unknown';
    const status = label + (raw.refresh_failed ? ' · refresh failed' : '');
    if (typeof describeTruth === 'function') {
      renderTruthChip(a('truth'), describeTruth(box.dataset.state, {
        observedMs: raw.snapshot_ms ?? null, nowMs,
        note: raw.refresh_failed ? 'refresh failed' : null,
      }));
    }
    if (!res || !res.ok) {
      marker.hidden = true;
      a('value').textContent = '—';
      a('lean').textContent = !usable ? (payload || state.refreshError ? label : 'Waiting for snapshot') : 'Insufficient coverage';
      track.setAttribute('aria-valuenow', '0');
      track.setAttribute('aria-valuetext', `${symbol} adaptive OI skew ${!usable ? label : 'insufficient coverage'}`);
      let why = '';
      if (res && res.reason === 'insufficient_strikes') {
        why = `Only ${res.strikes_with_oi} strike${res.strikes_with_oi === 1 ? '' : 's'} carry OI; needs ${p.min_strikes}.`;
      } else if (res && res.reason === 'beyond_cap') {
        why = `The ${p.min_strikes}th-nearest strike with OI is ${(res.nth_distance * 100).toFixed(2)}% from spot, beyond the ±${(p.cap_pct * 100).toFixed(0)}% cap.`;
      } else if (res && res.reason === 'no_spot') {
        why = 'The snapshot has no usable spot price.';
      } else if (res) {
        why = 'No call/put open interest in the window.';
      }
      a('meta').textContent = usable ? `${expiry} · ${status}` : '';
      a('detail').textContent = why ? `${why} No adaptive value is shown; the published ±2% reading above still applies.` : '';
      return;
    }
    const pct = oiSkewMeterPercent(res.imbalance);
    marker.hidden = pct == null;
    if (pct != null) marker.style.left = `${pct}%`;
    a('value').textContent = _refSkewSigned(res.imbalance);
    const lean = box.dataset.lean;
    a('lean').textContent = lean.charAt(0).toUpperCase() + lean.slice(1);
    track.setAttribute('aria-valuenow', res.imbalance.toFixed(3));
    track.setAttribute('aria-valuetext', `${symbol} adaptive OI skew ${res.imbalance.toFixed(3)}, ${lean}, `
      + `±${(res.half_width * 100).toFixed(2)}% window, ${res.band_strikes} strikes, ${label}`);
    a('meta').textContent = `±${(res.half_width * 100).toFixed(2)}% window · ${res.band_strikes} strikes `
      + `(${_fmtStrike(res.low_strike)}–${_fmtStrike(res.high_strike)}) · ${expiry} · ${status}`;
    a('detail').textContent = `${Math.round(res.call_oi).toLocaleString('en-US')} call vs `
      + `${Math.round(res.put_oi).toLocaleString('en-US')} put contracts. `
      + (res.widened
        ? `Window widened from ±${(p.floor_pct * 100).toFixed(0)}% to the ${p.min_strikes}th-nearest strike with OI (cap ±${(p.cap_pct * 100).toFixed(0)}%); strikes tied at the edge are included.`
        : `±${(p.floor_pct * 100).toFixed(0)}% already holds ${p.min_strikes}+ strikes with OI, so the window is not widened.`);
  }

  return { symbol, update, noteFailure, refresh, render };
}

const referenceOiSkewMeters = {};
let _refSkewRefresh = null;

function startReferenceOiSkewMeters(refreshSeconds = (typeof REFRESH === 'number' ? REFRESH : 60)) {
  for (const symbol of REFERENCE_OI_SKEW_SYMBOLS) {
    if (!document.getElementById(`${symbol.toLowerCase()}-oi-skew-meter`)) continue;
    referenceOiSkewMeters[symbol] = referenceOiSkewMeters[symbol] || createReferenceOiSkewMeter(symbol);
  }
  const meters = Object.values(referenceOiSkewMeters);
  for (const m of meters) m.refresh();
  if (_refSkewRefresh || typeof setInterval !== 'function' || !meters.length) return;
  _refSkewRefresh = setInterval(() => { for (const m of meters) m.refresh(); }, refreshSeconds * 1000);
  _ensureOiSkewAgeTicker();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { REFERENCE_OI_SKEW_SYMBOLS, createReferenceOiSkewMeter,
    ADAPTIVE_OI_SKEW_PARAMS, ADAPTIVE_OI_SKEW_SYMBOLS, computeAdaptiveOiSkew };
} else if (typeof document !== 'undefined') {
  startReferenceOiSkewMeters();
}
