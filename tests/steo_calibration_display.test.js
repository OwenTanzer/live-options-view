// Proves fmtSteoDelta()/findRevision() (docs/shared.js) -- the pure
// formatting/lookup functions docs/index.html's fetchSteoCalibration() calls
// to render the EIA STEO crude calibration panel. Mirrors
// tests/momentum_display.test.js's structure and rationale: pure, DOM-free
// functions, testable the simple direct-require way.
const assert = require('node:assert/strict');
const { fmtSteoDelta, findRevision } = require('../docs/shared.js');

// -- fmtSteoDelta -------------------------------------------------------------

// -- null/undefined: no revision to diff against yet (first vintage seen) ---
{
  const result = fmtSteoDelta(null);
  assert.equal(result.text, '');
  assert.equal(result.cls, '');
}

// -- positive delta, no unit --------------------------------------------------
{
  const result = fmtSteoDelta(2.36);
  assert.equal(result.text, '+2.36');
  assert.equal(result.cls, 'up');
}

// -- negative delta rendered without a double sign, with a unit suffix ------
{
  const result = fmtSteoDelta(-24.0, '');
  assert.equal(result.text, '-24.00');
  assert.doesNotMatch(result.text, /--/);
  assert.equal(result.cls, 'down');
}

// -- exactly zero: neither up nor down -----------------------------------
{
  const result = fmtSteoDelta(0);
  assert.equal(result.text, '+0.00');
  assert.equal(result.cls, '');
}

// -- findRevision -------------------------------------------------------------

const REVISIONS = [
  { period: '2026-08', brent_delta: -1.2, wti_delta: -1.0, balance_delta: 0.3 },
  { period: '2026-09', brent_delta: -24.0, wti_delta: -22.2, balance_delta: 9.8 },
];

// -- matching period found -----------------------------------------------
{
  const result = findRevision(REVISIONS, '2026-09');
  assert.equal(result.brent_delta, -24.0);
}

// -- no matching period: null, not undefined/throw ------------------------
{
  assert.equal(findRevision(REVISIONS, '2027-01'), null);
}

// -- missing/empty revisions array: null, not a crash ----------------------
{
  assert.equal(findRevision(null, '2026-09'), null);
  assert.equal(findRevision(undefined, '2026-09'), null);
  assert.equal(findRevision([], '2026-09'), null);
}

console.log('PASS eia steo calibration panel formatting -- delta sign/units and revision lookup all render explicitly');
