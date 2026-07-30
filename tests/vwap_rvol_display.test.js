// Proves formatVwapRvol() (docs/shared.js) -- the pure formatting/state
// function both the options-chain header and the QQQ price-strip tile call
// (see docs/index.html's renderHeatmap/updateQqqVwapRvolLine). Kept as a
// pure, DOM-free function specifically so it's testable this simple way,
// like TickerStateStore/LiveQuotePoller above it in shared.js -- no need for
// bots_panel.test.js's heavier comment-banner-extraction + DOM-shim pattern,
// since none of this logic lives inline in index.html's <script>.
const assert = require('node:assert/strict');
const { formatVwapRvol } = require('../docs/shared.js');

const NOW = Date.parse('2026-07-30T14:32:00Z');

function um(overrides = {}) {
  return {
    symbol: 'QQQ',
    spot: 402.0, spot_ts: '2026-07-30T14:32:00Z',
    vwap: 400.0, vwap_ts: '2026-07-30T14:32:00Z', vwap_session_date: '2026-07-30',
    price_vs_vwap_abs: 2.0, price_vs_vwap_pct: 0.5,
    session_volume: 41823400, session_volume_ts: '2026-07-30T14:32:00Z',
    rvol: { status: 'ok', multiple: 1.42, bucket_label: '10:30',
            baseline_volume: 700000, baseline_days_used: 10,
            baseline_lookback_days: 20, baseline_updated_through: '2026-07-29' },
    source: 'dxlink', freshness: 'live',
    ...overrides,
  };
}

// -- unavailable: no underlying_market at all (older payload, or collector
// hasn't published one yet) -----------------------------------------------
{
  const result = formatVwapRvol(null, NOW);
  assert.equal(result.state, 'fallback', 'missing underlying_market renders as fallback state');
  assert.match(result.text, /unavailable/i);
}

// -- closed: the observation is clearly a leftover from a prior session ---
{
  const stale = um({ spot_ts: '2026-07-29T20:00:00Z', vwap_ts: '2026-07-29T20:00:00Z' });
  const result = formatVwapRvol(stale, NOW);
  assert.equal(result.state, 'closed');
  assert.match(result.text, /session closed/i);
}

// -- ok, live: full VWAP + RVOL rendered ------------------------------------
{
  const result = formatVwapRvol(um(), NOW);
  assert.equal(result.state, 'live');
  assert.match(result.text, /VWAP 400\.00 \(\+0\.50%\)/);
  assert.match(result.text, /RVOL 1\.42×/);
}

// -- negative price_vs_vwap_pct renders without a double-negative sign -----
{
  const result = formatVwapRvol(um({ price_vs_vwap_pct: -0.75, vwap: 405.0 }), NOW);
  assert.match(result.text, /VWAP 405\.00 \(-0\.75%\)/);
}

// -- stale: freshness reported by the collector as not-live -----------------
{
  const result = formatVwapRvol(um({ freshness: 'stale' }), NOW);
  assert.equal(result.state, 'stale');
  // Still shows real numbers when they're merely stale, not "unavailable" --
  // staleness is a styling concern (dimmed), not a data-hiding one.
  assert.match(result.text, /VWAP 400\.00/);
}

// -- rvol insufficient_history: shown as "warming up," not a fabricated
// multiple or a blank field --------------------------------------------------
{
  const result = formatVwapRvol(um({ rvol: { ...um().rvol, status: 'insufficient_history', multiple: null } }), NOW);
  assert.match(result.text, /RVOL warming up/i);
  assert.doesNotMatch(result.text, /RVOL null/);
}

// -- rvol no_data: shown as an explicit dash, not a fabricated multiple ------
{
  const result = formatVwapRvol(um({ rvol: { status: 'no_data', multiple: null } }), NOW);
  assert.match(result.text, /RVOL —/);
}

// -- vwap itself unavailable (e.g. still warming up server-side) -- shown
// as an explicit dash, not a fabricated price --------------------------------
{
  const result = formatVwapRvol(um({ vwap: null, vwap_ts: null }), NOW);
  assert.match(result.text, /VWAP —/);
}

// -- vwap known but price_vs_vwap_pct missing -- shows the bare price, not a
// fabricated "+0.00%" (can't happen from the collector today since both are
// always computed together, but the formatter shouldn't guess if it did) ----
{
  const result = formatVwapRvol(um({ price_vs_vwap_pct: null }), NOW);
  assert.match(result.text, /VWAP 400\.00/);
  assert.doesNotMatch(result.text, /%/);
}

console.log('PASS vwap/rvol display formatting -- unavailable/closed/live/stale/insufficient_history/no_data all render explicitly');
