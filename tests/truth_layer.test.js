// Truth layer (site/shared/truth-layer.js) and its pilot on the Live tab's
// three OI skew meters: the shared state vocabulary, the chip rendering,
// the meters' wiring, and the page structure that moves methodology into
// "About this reading" drawers without deleting any of it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const T = require('../site/shared/truth-layer.js');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'site', 'index.html'), 'utf8');
let checks = 0;

// ── vocabulary and ages ──────────────────────────────────────────────────────
{
  const NOW = Date.parse('2026-11-20T20:00:00Z');
  assert.equal(T.formatTruthAge(NOW - 4_000, NOW), '4s');
  assert.equal(T.formatTruthAge(NOW - 12 * 60_000, NOW), '12m');
  assert.equal(T.formatTruthAge(NOW - 125 * 60_000, NOW), '2h 5m');
  assert.equal(T.formatTruthAge(NOW - 120 * 60_000, NOW), '2h');
  assert.equal(T.formatTruthAge(NOW - 3 * 86_400_000, NOW), '3d');
  assert.equal(T.formatTruthAge(NOW + 10_000, NOW), '0s', 'small clock skew reads as now');
  assert.equal(T.formatTruthAge(NOW + 120_000, NOW), null, 'a future observation is not given an age');
  assert.equal(T.formatTruthAge(null, NOW), null);
  checks += 8;

  const live = T.describeTruth('live', { observedMs: NOW - 42_000, nowMs: NOW });
  assert.equal(live.text, 'live · 42s');
  assert.equal(live.tone, 'ok');
  assert.match(live.spoken, /^Reading live, observed 42s ago$/);
  const thin = T.describeTruth('thin', { observedMs: NOW - 30_000, nowMs: NOW, note: '1 strike' });
  assert.equal(thin.text, 'thin · 1 strike · 30s');
  assert.equal(thin.tone, 'warn');
  const unknownAge = T.describeTruth('stale', { observedMs: null, nowMs: NOW });
  assert.equal(unknownAge.text, 'stale · age unknown', 'an unknown observation time is said, not guessed');
  const bogus = T.describeTruth('definitely-fine');
  assert.equal(bogus.state, 'unavailable', 'unknown states never read as healthy');
  assert.equal(bogus.text, 'unavailable');
  checks += 8;

  // Trust, never direction: every state pairs a glyph and a word, and no
  // tone is the call/put or gain/loss colour family.
  for (const [k, v] of Object.entries(T.TRUTH_STATES)) {
    assert.ok(v.glyph && v.label, k);
    assert.ok(['ok', 'warn', 'muted'].includes(v.tone), k);
    checks += 2;
  }

  const el = { textContent: '', dataset: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
  T.renderTruthChip(el, thin);
  assert.equal(el.textContent, '◐ thin · 1 strike · 30s');
  assert.equal(el.dataset.truth, 'thin');
  assert.equal(el.dataset.tone, 'warn');
  assert.match(el.attrs['aria-label'], /Reading thin, 1 strike, observed 30s ago/);
  T.renderTruthChip(null, thin); // tolerated
  checks += 4;
}

// ── pilot wiring: QQQ meter renders its chip ─────────────────────────────────
{
  const NOW = Date.parse('2026-11-13T17:35:00Z');
  const payload = {
    symbol: 'QQQ', timestamp: '2026-11-13T17:33:59.190717+00:00',
    market_readings: [{
      reading_id: 'reference_oi_skew', symbol: 'QQQ',
      input: { snapshot_timestamp: '2026-11-13T17:33:59.190717+00:00' },
      calculation: { version: 'reference_oi_skew.v1', parameters: { near_money_pct: 0.02 } },
      values: { near_money_call_oi: 59607, near_money_put_oi: 156520, imbalance_ratio: -0.448, band_strikes: 27 },
      status: { code: 'ok', reason: null }, freshness: { status: 'live' },
    }],
  };
  const els = {};
  const mk = id => (els[id] = { id, textContent: '', title: '', dataset: {}, style: {}, hidden: false,
    attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); } });
  for (const id of ['oi-skew-meter', 'oi-skew-marker', 'oi-skew-track', 'oi-skew-value', 'oi-skew-lean',
    'oi-skew-meta', 'oi-skew-truth', 'oi-skew-bot', 'oi-skew-bot-detail', 'oi-skew-thresholds']) mk(id);
  const ctx = { document: { getElementById: id => els[id] || null },
    ...require('../site/shared/oi-skew.js'), ...T, Date, Math, Number, Intl };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(root, 'site', 'js', 'oi-skew-meter.js'), 'utf8'), ctx);
  ctx.updateOiSkewMeter(payload, NOW, null);
  assert.equal(els['oi-skew-truth'].dataset.truth, 'live');
  assert.equal(els['oi-skew-truth'].textContent, '● live · 1m');
  ctx.noteOiSkewRefreshFailure('HTTP 503', NOW + 10 * 60_000);
  assert.equal(els['oi-skew-truth'].dataset.tone, 'warn');
  assert.match(els['oi-skew-truth'].textContent, /^◐ stale · refresh failed · 11m$/);
  checks += 4;
}

// ── page structure: chips in view, methodology one interaction away ──────────
{
  const hash = f => createHash('sha256').update(fs.readFileSync(path.join(root, 'site', f))).digest('hex').slice(0, 16);
  assert.ok(html.includes(`shared/truth-layer.js?v=${hash('shared/truth-layer.js')}`), 'truth-layer script hash pinned');
  assert.ok(html.indexOf('shared/truth-layer.js?v=') < html.indexOf('js/oi-skew-meter.js?v='),
    'truth layer loads before the meters');
  for (const id of ['oi-skew-truth', 'spy-oi-skew-truth', 'ibit-oi-skew-truth', 'ibit-adaptive-oi-skew-truth']) {
    assert.ok(html.includes(`id="${id}" class="truth-chip"`), id);
  }
  const sections = ['oi-skew-meter', 'spy-oi-skew-meter', 'ibit-oi-skew-meter'].map(id => {
    const start = html.indexOf(`<section id="${id}"`);
    return html.slice(start, html.indexOf('</section>', start));
  });
  for (const sec of sections) {
    const drawer = sec.slice(sec.indexOf('<details class="truth-about">'));
    assert.ok(drawer.startsWith('<details class="truth-about"><summary>About this reading</summary>'));
    assert.match(drawer, /not fresh intraday flow, and not a win probability/,
      'the methodology caveat is kept, inside the drawer');
    const before = sec.slice(0, sec.indexOf('<details class="truth-about">'));
    assert.doesNotMatch(before, /class="oi-skew-caveat">[^<]*(\(call OI|No bot trades|Reference reading only)/,
      'methodology no longer sits inline above the drawer');
  }
  assert.match(sections[0], /<details class="truth-about">[\s\S]*id="oi-skew-thresholds"[\s\S]*id="oi-skew-bot-detail"/);
  assert.match(sections[2], /<details class="truth-about">[\s\S]*id="ibit-oi-skew-detail"/);
  checks += 2 + 4 + sections.length * 3 + 2;
}

console.log(`truth_layer: ${checks} checks passed`);
