// Exercises the Squeeze tab's fetch/render logic from docs/index.html (OA-191).
//
// Same rationale as tests/bots_panel.test.js: the panel's code lives in
// index.html's inline script, extracted by its section markers and evaluated
// against a minimal DOM/fetch shim, so this runs the shipped source rather
// than a copy that can drift. If this file starts failing with "could not
// locate", that's why.
//
// Rewritten for PR #105 review: findings 1/2 added the latest-attempt.json
// fetch and acquisition-time freshness; finding 3 removed the
// localStorage-based New-badge tracking outright; finding 4 is why every
// fixture below runs through an injected `squeezeNow` instead of the
// system clock -- these fixed-date fixtures must assert the same way no
// matter what day the suite actually runs on.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { formatSqueezeScanStatus, describeSqueezeAcquisitionFailure } = require('../docs/shared.js');

const html = fs.readFileSync(path.join(__dirname, '..', 'docs', 'index.html'), 'utf8');
const START = '// ── short-squeeze scanner panel (OA-191)';
const END = '// ── tab switching';
const from = html.indexOf(START);
const to = html.indexOf(END, from);
assert.ok(from !== -1 && to !== -1, 'could not locate the squeeze-panel section in docs/index.html');
const source = html.slice(from, to);

// ── DOM + collaborator shim ───────────────────────────────────────────────────
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, textContent: '', innerHTML: '', dataset: {}, classList: { toggle() {} } };
  return els[id];
}

// Fixed "now" for every fixture below (PR #105 finding 4) -- overridden per
// test via `nowOverride` rather than ever falling through to Date.now().
let nowOverride = Date.parse('2026-09-23T14:05:00Z');

// fetch responses keyed by which pointer file the URL is for; set per test.
let latestResponse = null; // { ok, status, json } or an Error, for latest.json
let attemptResponse = null; // same shape, for latest-attempt.json

const scope = {
  document: { getElementById: el },
  fetch: async (url) => {
    const isAttempt = url.includes('latest-attempt.json');
    const response = isAttempt ? attemptResponse : latestResponse;
    if (response instanceof Error) throw response;
    return response;
  },
  R2: 'https://example-r2.test',
  escapeHtml: (s) => String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  )),
  formatSqueezeScanStatus, describeSqueezeAcquisitionFailure,
};

const names = Object.keys(scope);
// The extracted source defines its own `squeezeNow()` (reading the real
// Date.now()); redeclaring it below shadows that shipped definition with
// one reading a test-controlled variable instead -- the whole point of
// finding 4's fix being an injectable clock in the first place.
const factory = new Function(...names, `${source}
  let __nowOverride = ${nowOverride};
  function squeezeNow() { return __nowOverride; }
  return { fetchSqueezeLatest, renderSqueezePanel,
    get squeezePointer(){return squeezePointer}, set squeezePointer(v){squeezePointer=v},
    get squeezeAttemptPointer(){return squeezeAttemptPointer}, set squeezeAttemptPointer(v){squeezeAttemptPointer=v},
    get squeezeError(){return squeezeError}, set squeezeError(v){squeezeError=v},
    set now(v){__nowOverride=v} };`);
const panel = factory(...names.map(n => scope[n]));

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}
const notFound = jsonResponse({}, 404);

const candidate = (ticker, combined, overrides = {}) => ({
  ticker, company: `${ticker} Inc`, price: 10.0,
  scores: { factor: 0.6, options: 0.5, combined, momentum: 5.0 },
  ...overrides,
});

(async () => {

// ── a populated, successful scan renders rows and status, clears errors ────
{
  panel.squeezeError = 'stale error from a previous tick';
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T13:58:00Z', finished_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('AAA', 0.7), candidate('BBB', 0.5)],
  });
  attemptResponse = notFound; // no separate failed attempt on record
  await panel.fetchSqueezeLatest();

  assert.equal(panel.squeezeError, null, 'a successful fetch clears the prior error');
  const out = els['squeeze-tbody'].innerHTML;
  assert.match(out, /AAA/);
  assert.match(out, /BBB/);
  assert.match(out, /0\.700/);
  assert.match(els['squeeze-status'].textContent, /Scan/);
  assert.equal(els['squeeze-status'].dataset.state, 'live');
  assert.equal(els['squeeze-warning'].textContent, '', 'no attempt-failure warning when the latest attempt is just "not found"');
}

// ── PR #105 finding 1: a newer failed attempt surfaces a warning banner
// alongside (not instead of) the last successful table ──────────────────────
{
  attemptResponse = jsonResponse({ status: 'partial', started_at: '2026-09-23T17:00:00Z' });
  await panel.fetchSqueezeLatest();
  assert.match(els['squeeze-warning'].textContent, /did not complete: partial/);
  // The last successful table is still shown, not blanked by the warning.
  assert.match(els['squeeze-tbody'].innerHTML, /AAA/);
}

// -- once the attempt pointer catches up to a real success, the warning clears
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T17:00:00Z', finished_at: '2026-09-23T17:02:00Z',
    candidates: [candidate('CCC', 0.9)],
  });
  attemptResponse = jsonResponse({ status: 'complete', started_at: '2026-09-23T17:00:00Z' });
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-warning'].textContent, '');
  assert.match(els['squeeze-tbody'].innerHTML, /CCC/);
}

// ── PR #105 finding 3: the first-seen column reads "unavailable" -- no
// client-side guess replaces the not-yet-existent producer field, and this
// is stable across any number of polls of the identical run (it's a pure
// read of row data now, not tracked state) ─────────────────────────────────
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T14:00:00Z', finished_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('DDD', 0.8)],
  });
  attemptResponse = notFound;
  await panel.fetchSqueezeLatest();
  const firstRender = els['squeeze-tbody'].innerHTML;
  assert.match(firstRender, /squeeze-first-seen">unavailable/);

  // A second poll of the exact same run must render byte-identical output.
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-tbody'].innerHTML, firstRender, 'polling the same run again must not change the rendered table');
}

// -- once the producer publishes a first_seen_at field, it's honored --------
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T14:00:00Z', finished_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('EEE', 0.8, { first_seen_at: '2026-09-23T14:00:00Z' })],
  });
  await panel.fetchSqueezeLatest();
  assert.match(els['squeeze-tbody'].innerHTML, /squeeze-first-seen">New/);
}

// ── a fetch error keeps the last-rendered table instead of blanking it ──────
{
  const beforeError = els['squeeze-tbody'].innerHTML;
  latestResponse = new Error('network down');
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-tbody'].innerHTML, beforeError, 'the last good table survives a fetch failure');
  assert.match(els['squeeze-status'].textContent, /scanner unavailable — network down/);
  assert.equal(els['squeeze-status'].dataset.state, 'stale');
}

// ── recovery after an error re-populates normally ────────────────────────────
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T18:00:00Z', finished_at: '2026-09-23T18:00:00Z',
    candidates: [candidate('FFF', 0.9)],
  });
  attemptResponse = notFound;
  await panel.fetchSqueezeLatest();
  assert.equal(panel.squeezeError, null);
  assert.match(els['squeeze-tbody'].innerHTML, /FFF/);
}

// ── neither pointer has ever been published: not an error state ─────────────
{
  latestResponse = notFound;
  attemptResponse = notFound;
  await panel.fetchSqueezeLatest();
  assert.equal(panel.squeezeError, null, 'no run published yet is not a fetch error');
  assert.equal(els['squeeze-status'].textContent, 'No scan published yet');
  assert.equal(els['squeeze-tbody'].innerHTML, '<tr><td colspan="6" class="squeeze-empty">No eligible candidates in the latest scan.</td></tr>');
}

// ── a genuinely empty scan renders the explicit empty state, not a blank table
{
  latestResponse = jsonResponse({ status: 'empty', started_at: '2026-09-23T14:00:00Z', finished_at: '2026-09-23T14:00:00Z', candidates: [] });
  await panel.fetchSqueezeLatest();
  assert.match(els['squeeze-tbody'].innerHTML, /No eligible candidates/);
  assert.match(els['squeeze-status'].textContent, /no eligible candidates/);
}

// ── PR #105 finding 4: assertions must survive the clock moving forward ────
// (this is the regression: the previous version asserted against a fixed
// fixture date compared to the real, unfrozen Date.now())
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T14:00:00Z', finished_at: '2026-09-23T14:00:00Z',
    candidates: [],
  });
  attemptResponse = notFound;
  panel.now = Date.parse('2026-09-23T14:05:00Z'); // 5 minutes later: still fresh
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-status'].dataset.state, 'live');

  panel.now = Date.parse('2026-09-24T09:00:00Z'); // next day: must read stale/prior-day
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-status'].dataset.state, 'stale');
  panel.now = nowOverride; // restore for any later reader of this file
}

// ── a hostile ticker must not execute (defense in depth; finviz screener
// output isn't attacker-controlled, but this must not regress silently) ──────
{
  latestResponse = jsonResponse({
    status: 'complete', started_at: '2026-09-23T14:00:00Z', finished_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('<img src=x onerror=alert(1)>', 0.5)],
  });
  attemptResponse = notFound;
  await panel.fetchSqueezeLatest();
  const out = els['squeeze-tbody'].innerHTML;
  assert.equal(out.includes('<img src=x'), false, 'a hostile ticker must be escaped, not injected');
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
}

console.log('PASS squeeze scanner panel fetch, render, attempt-failure warning, and clock-independent freshness');

})();
