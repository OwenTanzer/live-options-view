// Proves formatMomentum() (docs/shared.js) -- the pure formatting/state
// function both the options-chain header and the QQQ price-strip tile call
// (see docs/index.html's renderHeatmap/updateQqqMomentumLine). Mirrors
// tests/vwap_rvol_display.test.js's structure and rationale: pure, DOM-free
// function, testable the simple direct-require way.
const assert = require('node:assert/strict');
const { formatMomentum } = require('../docs/shared.js');

const NOW = Date.parse('2026-07-30T14:32:00Z');

function um(overrides = {}) {
  return {
    symbol: 'QQQ',
    spot: 402.0, spot_ts: '2026-07-30T14:32:00Z',
    momentum: {
      status: 'ok', return_pct: 0.42, lookback_minutes: 60.0,
      anchor_age_minutes: 61.0, sample_count: 30, direction: 'up',
    },
    freshness: 'live',
    ...overrides,
  };
}

// -- unavailable: no underlying_market at all -------------------------------
{
  const result = formatMomentum(null, NOW);
  assert.equal(result.state, 'fallback');
  assert.match(result.text, /unavailable/i);
}

// -- closed: the observation is clearly a leftover from a prior session ----
{
  const stale = um({ spot_ts: '2026-07-29T20:00:00Z' });
  const result = formatMomentum(stale, NOW);
  assert.equal(result.state, 'closed');
  assert.match(result.text, /session closed/i);
}

// -- ok, up direction --------------------------------------------------------
{
  const result = formatMomentum(um(), NOW);
  assert.equal(result.state, 'live');
  assert.match(result.text, /▲/);
  assert.match(result.text, /\+0\.42%/);
  assert.match(result.text, /\(60m\)/);
}

// -- ok, down direction, negative return rendered without a double sign ----
{
  const result = formatMomentum(um({ momentum: { status: 'ok', return_pct: -0.75, lookback_minutes: 60.0, direction: 'down' } }), NOW);
  assert.match(result.text, /▼/);
  assert.match(result.text, /-0\.75%/);
  assert.doesNotMatch(result.text, /--0\.75/);
}

// -- ok, flat direction -------------------------------------------------------
{
  const result = formatMomentum(um({ momentum: { status: 'ok', return_pct: 0.01, lookback_minutes: 60.0, direction: 'flat' } }), NOW);
  assert.match(result.text, /→/);
}

// -- warming_up: shown explicitly, not a fabricated return ------------------
{
  const result = formatMomentum(um({ momentum: { status: 'warming_up' } }), NOW);
  assert.match(result.text, /warming up/i);
  assert.doesNotMatch(result.text, /%/);
}

// -- stale_anchor: shown explicitly, not silently identical to "ok" ---------
{
  const result = formatMomentum(um({ momentum: { status: 'stale_anchor' } }), NOW);
  assert.match(result.text, /stale/i);
}

// -- no_data: explicit dash, not blank ---------------------------------------
{
  const result = formatMomentum(um({ momentum: { status: 'no_data' } }), NOW);
  assert.match(result.text, /—/);
}

// -- stale freshness (collector-reported) -- state reflects it, numbers still shown --
{
  const result = formatMomentum(um({ freshness: 'stale' }), NOW);
  assert.equal(result.state, 'stale');
  assert.match(result.text, /\+0\.42%/);
}

console.log('PASS momentum display formatting -- unavailable/closed/live/stale/up/down/flat/warming_up/stale_anchor/no_data all render explicitly');
