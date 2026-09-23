// Proves formatSqueezeScanStatus()/describeSqueezeAcquisitionFailure()
// (docs/shared.js) -- the pure formatting functions docs/index.html's
// squeeze panel (OA-191) calls to render the short-squeeze scanner's
// published latest.json / latest-attempt.json pointers. Mirrors
// tests/steo_calibration_display.test.js's structure: pure, DOM-free
// functions, testable the simple direct-require way.
//
// PR #105 review drove this rewrite: findings 1 and 2 replaced the
// previous published_at-based freshness check and added the
// attempt-pointer comparison; the previous localStorage-based
// trackFirstSeenToday was removed outright (finding 3) rather than fixed,
// per that review's explicit instruction not to invent browser-local
// history in place of real producer-owned first-seen metadata.
const assert = require('node:assert/strict');
const { formatSqueezeScanStatus, describeSqueezeAcquisitionFailure } = require('../docs/shared.js');

// -- formatSqueezeScanStatus --------------------------------------------------

// -- no pointer at all: no run has ever been published -----------------------
{
  const result = formatSqueezeScanStatus(null);
  assert.equal(result.text, 'No scan published yet');
  assert.equal(result.state, 'fallback');
}

// -- a fresh, complete scan, same calendar day --------------------------------
{
  const now = Date.parse('2026-09-23T14:05:00Z'); // 10:05am ET
  const pointer = { status: 'complete', started_at: '2026-09-23T13:58:00Z', finished_at: '2026-09-23T14:00:00Z' };
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /Scan/);
  assert.doesNotMatch(result.text, /stale/);
  assert.equal(result.state, 'live');
}

// -- an old-but-same-day scan is flagged stale --------------------------------
{
  const now = Date.parse('2026-09-23T20:05:00Z');
  const pointer = { status: 'complete', started_at: '2026-09-23T13:58:00Z', finished_at: '2026-09-23T14:00:00Z' }; // just over 6h old
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /stale/);
  assert.equal(result.state, 'stale');
}

// -- PR #105 finding 2: freshness uses acquisition time, not published_at ---
// A scan finished September 22 but uploaded (published) September 23 must
// not read as fresh just because publication was recent.
{
  const now = Date.parse('2026-09-23T15:00:00Z'); // 11am ET, Sept 23
  const pointer = {
    status: 'complete',
    started_at: '2026-09-22T18:55:00Z', finished_at: '2026-09-22T19:00:00Z', // acquired the evening before
    published_at: '2026-09-23T14:59:00Z', // but uploaded just one minute ago
  };
  const result = formatSqueezeScanStatus(pointer, now);
  assert.equal(result.state, 'stale', 'a prior-day scan is stale regardless of how recently it was published');
  assert.match(result.text, /Sep 22/, 'the scan date is shown explicitly for a prior-day result');
}

// -- a genuinely empty scan (no eligible candidates) is not an error ---------
{
  const now = Date.parse('2026-09-23T14:05:00Z');
  const pointer = { status: 'empty', started_at: '2026-09-23T13:58:00Z', finished_at: '2026-09-23T14:00:00Z' };
  const result = formatSqueezeScanStatus(pointer, now);
  assert.match(result.text, /no eligible candidates/);
  assert.equal(result.state, 'stale');
}

// -- missing acquisition timestamps don't crash, and read as stale ----------
{
  const result = formatSqueezeScanStatus({ status: 'complete', started_at: null, finished_at: null });
  assert.match(result.text, /unknown/);
  assert.equal(result.state, 'stale');
}

// -- describeSqueezeAcquisitionFailure ----------------------------------------

// -- no attempt pointer at all: nothing to warn about ------------------------
{
  assert.equal(describeSqueezeAcquisitionFailure({ status: 'complete', started_at: '2026-09-23T14:00:00Z' }, null), null);
}

// -- the latest attempt succeeded: no warning, regardless of success pointer -
{
  const attempt = { status: 'complete', started_at: '2026-09-23T17:00:00Z' };
  assert.equal(describeSqueezeAcquisitionFailure(null, attempt), null);
  assert.equal(describeSqueezeAcquisitionFailure({ status: 'complete', started_at: '2026-09-23T14:00:00Z' }, attempt), null);
}

// -- PR #105 finding 1: a newer failed attempt must surface a warning -------
// even though the last SUCCESSFUL result (still shown in the table) looks
// perfectly healthy on its own.
{
  const success = { status: 'complete', started_at: '2026-09-23T14:00:00Z' };
  const failedAttempt = { status: 'partial', started_at: '2026-09-23T17:00:00Z' };
  const notice = describeSqueezeAcquisitionFailure(success, failedAttempt);
  assert.match(notice, /did not complete: partial/);
  assert.match(notice, /1:00 PM ET/);
  assert.match(notice, /last successful result/);
}

// -- a failed attempt OLDER than the current success is not re-surfaced -----
// (e.g. the morning run failed, but the noon run succeeded and is what's shown)
{
  const success = { status: 'complete', started_at: '2026-09-23T17:00:00Z' };
  const olderFailedAttempt = { status: 'error', started_at: '2026-09-23T09:00:00Z' };
  assert.equal(describeSqueezeAcquisitionFailure(success, olderFailedAttempt), null);
}

// -- no successful run has ever existed, only a failed attempt --------------
{
  const failedAttempt = { status: 'failed', started_at: '2026-09-23T09:00:00Z' };
  const notice = describeSqueezeAcquisitionFailure(null, failedAttempt);
  assert.match(notice, /No successful scan yet/);
  assert.match(notice, /failed/);
}

console.log('PASS squeeze scanner scan-status and acquisition-failure formatting');
