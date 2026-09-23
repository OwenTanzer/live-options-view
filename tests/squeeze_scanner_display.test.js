// Proves formatSqueezeScanStatus()/trackFirstSeenToday() (docs/shared.js) --
// the pure formatting/tracking functions docs/index.html's squeeze panel
// (OA-191) calls to render the short-squeeze scanner's published
// latest.json pointer. Mirrors tests/steo_calibration_display.test.js's
// structure: pure, DOM-free functions, testable the simple direct-require way.
const assert = require('node:assert/strict');
const { formatSqueezeScanStatus, trackFirstSeenToday } = require('../docs/shared.js');

// -- formatSqueezeScanStatus --------------------------------------------------

// -- no pointer at all: no run has ever been published -----------------------
{
  const result = formatSqueezeScanStatus(null);
  assert.equal(result.text, 'No scan published yet');
  assert.equal(result.state, 'fallback');
}

// -- a fresh, complete scan ----------------------------------------------------
{
  const now = Date.parse('2026-09-23T14:05:00Z'); // 10:05am ET
  const pointer = { status: 'complete', published_at: '2026-09-23T14:00:00Z' };
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /Scan published/);
  assert.doesNotMatch(result.text, /stale/);
  assert.equal(result.state, 'live');
}

// -- an old scan is flagged stale, not silently shown as current -------------
{
  const now = Date.parse('2026-09-23T20:05:00Z');
  const pointer = { status: 'complete', published_at: '2026-09-23T14:00:00Z' }; // just over 6h old
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /stale/);
  assert.equal(result.state, 'stale');
}

// -- a genuinely empty scan (no eligible candidates) is not an error ---------
{
  const now = Date.parse('2026-09-23T14:05:00Z');
  const pointer = { status: 'empty', published_at: '2026-09-23T14:00:00Z' };
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /no eligible candidates/);
  // Still flagged as a state that reads visually distinct from a healthy
  // populated scan -- there is nothing to rank right now.
  assert.equal(result.state, 'stale');
}

// -- a missing/malformed published_at doesn't crash, and reads as stale ------
{
  const result = formatSqueezeScanStatus({ status: 'complete', published_at: null });
  assert.match(result.text, /unknown time/);
  assert.equal(result.state, 'stale');
}

// -- trackFirstSeenToday -------------------------------------------------------

// -- first run of the day: every ticker is new --------------------------------
{
  const tracked = trackFirstSeenToday(['AAA', 'BBB'], null, '2026-09-23');
  assert.deepEqual([...tracked.newlySeen].sort(), ['AAA', 'BBB']);
  assert.deepEqual(tracked.tickers, { AAA: true, BBB: true });
  assert.equal(tracked.dateKey, '2026-09-23');
}

// -- a later same-day run: only the newcomer is flagged New ------------------
{
  const morning = trackFirstSeenToday(['AAA', 'BBB'], null, '2026-09-23');
  const noon = trackFirstSeenToday(['AAA', 'CCC'], morning, '2026-09-23');
  assert.deepEqual([...noon.newlySeen], ['CCC']);
  // AAA and BBB (BBB dropped off the shortlist, but still remembered as
  // seen today) both remain recorded so a later reappearance of BBB
  // wouldn't be wrongly re-flagged New within the same day.
  assert.deepEqual(noon.tickers, { AAA: true, BBB: true, CCC: true });
}

// -- a new calendar day resets everything, even a previously-seen ticker -----
{
  const yesterday = trackFirstSeenToday(['AAA'], null, '2026-09-22');
  const today = trackFirstSeenToday(['AAA'], yesterday, '2026-09-23');
  assert.deepEqual([...today.newlySeen], ['AAA'], 'a new day treats every ticker as freshly seen');
  assert.deepEqual(today.tickers, { AAA: true });
}

// -- an empty candidate list on a previously-populated day changes nothing ---
{
  const morning = trackFirstSeenToday(['AAA'], null, '2026-09-23');
  const emptyNoon = trackFirstSeenToday([], morning, '2026-09-23');
  assert.equal(emptyNoon.newlySeen.size, 0);
  assert.deepEqual(emptyNoon.tickers, { AAA: true }, 'seen-today memory survives a run with no candidates');
}

console.log('PASS squeeze scanner panel status formatting and first-seen tracking');
