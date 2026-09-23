// Exercises the Squeeze tab's fetch/render logic from docs/index.html (OA-191).
//
// Same rationale as tests/bots_panel.test.js: the panel's code lives in
// index.html's inline script, extracted by its section markers and evaluated
// against a minimal DOM/fetch/storage shim, so this runs the shipped source
// rather than a copy that can drift. If this file starts failing with
// "could not locate", that's why.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { formatSqueezeScanStatus, trackFirstSeenToday } = require('../docs/shared.js');

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
  if (!els[id]) els[id] = { id, textContent: '', innerHTML: '', dataset: {} };
  return els[id];
}

let store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = v; },
};

let fetchResponse = null; // set per-test: { ok, status, json: async () => ... } or throws
const scope = {
  document: { getElementById: el },
  fetch: async () => {
    if (fetchResponse instanceof Error) throw fetchResponse;
    return fetchResponse;
  },
  localStorage,
  R2: 'https://example-r2.test',
  escapeHtml: (s) => String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  )),
  formatSqueezeScanStatus, trackFirstSeenToday,
};

const names = Object.keys(scope);
const factory = new Function(...names, `${source}\nreturn { fetchSqueezeLatest, renderSqueezePanel, squeezeTodayKey,
  get squeezePointer(){return squeezePointer}, set squeezePointer(v){squeezePointer=v},
  get squeezeError(){return squeezeError}, set squeezeError(v){squeezeError=v} };`);
const panel = factory(...names.map(n => scope[n]));

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const candidate = (ticker, combined, overrides = {}) => ({
  ticker, company: `${ticker} Inc`, price: 10.0,
  scores: { factor: 0.6, options: 0.5, combined, momentum: 5.0 },
  ...overrides,
});

(async () => {

// ── a populated scan renders rows, status, and clears any prior error ───────
{
  store = {};
  panel.squeezeError = 'stale error from a previous tick';
  fetchResponse = jsonResponse({
    status: 'complete', published_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('AAA', 0.7), candidate('BBB', 0.5)],
  });
  await panel.fetchSqueezeLatest();

  assert.equal(panel.squeezeError, null, 'a successful fetch clears the prior error');
  const out = els['squeeze-tbody'].innerHTML;
  assert.match(out, /AAA/);
  assert.match(out, /BBB/);
  assert.match(out, /0\.700/);
  assert.match(els['squeeze-status'].textContent, /Scan published/);
  assert.equal(els['squeeze-status'].dataset.state, 'live');
  // First run of the day: both candidates are flagged New.
  assert.equal((out.match(/squeeze-new-badge/g) || []).length, 2);
}

// ── a later same-day fetch only flags the newcomer ───────────────────────────
{
  fetchResponse = jsonResponse({
    status: 'complete', published_at: '2026-09-23T17:00:00Z',
    candidates: [candidate('AAA', 0.7), candidate('CCC', 0.4)],
  });
  await panel.fetchSqueezeLatest();
  const out = els['squeeze-tbody'].innerHTML;
  const ccc = out.slice(out.indexOf('CCC') - 10, out.indexOf('CCC') + 200);
  const aaa = out.slice(out.indexOf('AAA') - 10, out.indexOf('AAA') + 200);
  assert.match(ccc, /squeeze-new-badge/, 'a genuinely new ticker is flagged New');
  assert.doesNotMatch(aaa, /squeeze-new-badge/, 'a ticker already seen today is not re-flagged New');
}

// ── a fetch error keeps the last-rendered table instead of blanking it ──────
{
  const beforeError = els['squeeze-tbody'].innerHTML;
  fetchResponse = new Error('network down');
  await panel.fetchSqueezeLatest();
  assert.equal(els['squeeze-tbody'].innerHTML, beforeError, 'the last good table survives a fetch failure');
  assert.match(els['squeeze-status'].textContent, /scanner unavailable — network down/);
  assert.equal(els['squeeze-status'].dataset.state, 'stale');
}

// ── recovery after an error re-populates normally ────────────────────────────
{
  fetchResponse = jsonResponse({
    status: 'complete', published_at: '2026-09-23T18:00:00Z',
    candidates: [candidate('DDD', 0.9)],
  });
  await panel.fetchSqueezeLatest();
  assert.equal(panel.squeezeError, null);
  assert.match(els['squeeze-tbody'].innerHTML, /DDD/);
}

// ── a 404 (nothing published yet) is not an error state ──────────────────────
{
  fetchResponse = jsonResponse({}, 404);
  await panel.fetchSqueezeLatest();
  assert.equal(panel.squeezeError, null, 'no run published yet is not a fetch error');
  assert.equal(els['squeeze-status'].textContent, 'No scan published yet');
  assert.equal(els['squeeze-tbody'].innerHTML, '<tr><td colspan="6" class="squeeze-empty">No eligible candidates in the latest scan.</td></tr>');
}

// ── a genuinely empty scan renders the explicit empty state, not a blank table
{
  fetchResponse = jsonResponse({ status: 'empty', published_at: '2026-09-23T14:00:00Z', candidates: [] });
  await panel.fetchSqueezeLatest();
  assert.match(els['squeeze-tbody'].innerHTML, /No eligible candidates/);
  assert.match(els['squeeze-status'].textContent, /no eligible candidates/);
}

// ── a hostile ticker must not execute (defense in depth; finviz screener
// output isn't attacker-controlled, but this must not regress silently) ──────
{
  fetchResponse = jsonResponse({
    status: 'complete', published_at: '2026-09-23T14:00:00Z',
    candidates: [candidate('<img src=x onerror=alert(1)>', 0.5)],
  });
  await panel.fetchSqueezeLatest();
  const out = els['squeeze-tbody'].innerHTML;
  assert.equal(out.includes('<img src=x'), false, 'a hostile ticker must be escaped, not injected');
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
}

console.log('PASS squeeze scanner panel fetch, render, New-badge tracking, and error handling');

})();
