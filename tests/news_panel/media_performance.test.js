/* Media performance panel honesty tests (Node, no DOM, no network).
 *
 * Run (offline, after the execution-risk blocker is cleared):
 *   node tests/news_panel/media_performance.test.js
 *
 * The panel is render-only and never fetches. These tests cover the
 * pure helpers (format / honesty label / return class) and the DOM
 * rendering with a minimal fake document — every honesty state must
 * render truthfully and never as zero.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const panel = require(path.join(__dirname, '..', '..', 'site', 'js', 'media-performance-panel.js'));

// Minimal fake document: createElement returns a node with
// textContent, className, children, and appendChild.
function fakeDocument() {
  function makeNode(tag) {
    const node = {
      tagName: tag,
      className: '',
      textContent: '',
      children: [],
      appendChild(child) { this.children.push(child); return child; },
    };
    return node;
  }
  return { createElement: makeNode };
}

function findText(node, needle) {
  if (node.textContent && node.textContent.includes(needle)) return true;
  return (node.children || []).some((c) => findText(c, needle));
}

function collectText(node, out) {
  if (node.textContent) out.push(node.textContent);
  (node.children || []).forEach((c) => collectText(c, out));
  return out;
}

test('formatReturn never renders a missing return as 0.00%', () => {
  assert.equal(panel.formatReturn(null), null);
  assert.equal(panel.formatReturn(undefined), null);
  assert.equal(panel.formatReturn(0.831024), '+0.83%');
  assert.equal(panel.formatReturn(-1.5), '\u22121.50%');
});

test('honestyLabel: fresh, stale base, pending, hindsight', () => {
  assert.equal(panel.honestyLabel({ status: 'matched', hindsight: false, base_stale: false }), 'fresh');
  assert.equal(panel.honestyLabel({ status: 'matched', hindsight: false, base_stale: true }), 'stale base');
  assert.equal(panel.honestyLabel({ status: 'missing', missing_reason: 'not_yet_available', instrument_id: 'eq.nvda' }), 'pending');
  assert.equal(panel.honestyLabel({ status: 'missing', missing_reason: 'no_series', instrument_id: 'eq.nvda' }), 'no coverage');
  assert.equal(panel.honestyLabel({ status: 'missing', missing_reason: 'unknown mapping', instrument_id: null }), 'unknown mapping');
  assert.equal(panel.honestyLabel({ status: 'hindsight', hindsight: true }), 'hindsight');
});

test('returnClass reserves red/green for gain/loss only', () => {
  assert.equal(panel.returnClass(1.0, 'matched'), 'mp-gain');
  assert.equal(panel.returnClass(-1.0, 'matched'), 'mp-loss');
  assert.equal(panel.returnClass(null, 'missing'), 'mp-neutral');
  assert.equal(panel.returnClass(1.0, 'missing'), 'mp-neutral');
});

test('rendered card leads with the move, not the provenance', () => {
  const doc = fakeDocument();
  const row = {
    item_id: 'tc-nvda-001', revision_id: 'rev-nvda-001',
    title: 'NVIDIA beats on data-center revenue as NVDA guides higher',
    outlet: 'TechCrunch', event_at: '2026-11-09T10:05:00Z',
    instrument_id: 'eq.nvda', base_stale: false,
    performance: { horizons: {
      '5m': { horizon: '5m', status: 'matched', honesty_label: 'fresh', return_pct: 0.831024, return_bps: 83.1 },
      '1h': { horizon: '1h', status: 'matched', honesty_label: 'fresh', return_pct: 3.3241, return_bps: 332.41 },
    } },
  };
  const card = panel.renderPerformanceCard(doc, row);
  const text = collectText(card, []).join(' ');
  assert.match(text, /\+3\.32%/);
  assert.match(text, /fresh/);
  assert.doesNotMatch(text, /0\.00%/);
  // The move is in the header (first child), the title is after it.
  assert.equal(card.children[0].tagName, 'header');
});

test('missing forward price renders as not yet available, never zero', () => {
  const doc = fakeDocument();
  const row = {
    item_id: 'tc-nvda-001', revision_id: 'rev-nvda-001',
    title: 'NVIDIA beats', outlet: 'TechCrunch', event_at: '2026-11-09T10:05:00Z',
    instrument_id: 'eq.nvda',
    performance: { horizons: {
      '1d': { horizon: '1d', status: 'missing', honesty_label: 'pending', return_pct: null, return_bps: null },
    } },
  };
  const card = panel.renderPerformanceCard(doc, row);
  const text = collectText(card, []).join(' ');
  assert.match(text, /pending|not yet available/);
  assert.doesNotMatch(text, /0\.00%/);
});

test('unknown mapping renders without a ticker or a move', () => {
  const doc = fakeDocument();
  const row = {
    item_id: 'tc-unmapped-001', revision_id: 'rev-unmapped-001',
    title: 'A startup raises a round', outlet: 'TechCrunch',
    event_at: '2026-11-09T10:05:00Z', instrument_id: null, performance: null,
  };
  const card = panel.renderPerformanceCard(doc, row);
  const text = collectText(card, []).join(' ');
  assert.match(text, /performance not yet available/);
  assert.doesNotMatch(text, /\+[0-9]+\.[0-9]+%/);
});

test('market-closed / no-forward-price renders as not yet available', () => {
  const doc = fakeDocument();
  const row = {
    item_id: 'tc-nvda-001', revision_id: 'rev-nvda-001',
    title: 'NVIDIA beats', outlet: 'TechCrunch', event_at: '2026-11-09T10:05:00Z',
    instrument_id: 'eq.nvda',
    performance: { horizons: {
      '5m': { horizon: '5m', status: 'missing', honesty_label: 'not yet available', return_pct: null, return_bps: null },
    } },
  };
  const card = panel.renderPerformanceCard(doc, row);
  assert.ok(findText(card, 'not yet available'));
  assert.doesNotMatch(collectText(card, []).join(' '), /0\.00%/);
});
