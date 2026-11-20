// Truth layer (Nov 2026): one shared vocabulary for "how much can this
// reading be trusted right now", rendered the same way on every page.
//
// The site already computes honest states (live / stale / session closed /
// thin / refresh failed ...) but each panel words and colours them its own
// way, usually inside a sentence at the same weight as the methodology. The
// truth layer splits that into two depths:
//   1. a short state chip in the line of sight: glyph + word + observation age
//      ("● live · 42s", "◐ stale · 12m", "◐ thin · 1 strike");
//   2. the methodology, unchanged, one interaction away in an
//      "About this reading" <details> drawer (markup, not this module).
// Nothing is deleted; caveats move to the right depth.
//
// Rules (Critical Design Theory):
//  - one meaning per colour channel: tone is about trust, never direction,
//    so chips never use the call/put or gain/loss colours;
//  - colour is always paired with a glyph and a word;
//  - every chip says when the observation was made; brightness guides
//    attention, but an earlier observation stays legible.
//
// Classic script like the other site/shared modules: page globals in the
// browser, module.exports under Node for tests.

const TRUTH_STATES = Object.freeze({
  live:         { label: 'live',            tone: 'ok',    glyph: '●' },
  stale:        { label: 'stale',           tone: 'warn',  glyph: '◐' },
  closed:       { label: 'session closed',  tone: 'warn',  glyph: '◐' },
  thin:         { label: 'thin',            tone: 'warn',  glyph: '◐' },
  partial:      { label: 'partial',         tone: 'warn',  glyph: '◐' },
  capped:       { label: 'capped',          tone: 'warn',  glyph: '◐' },
  gap:          { label: 'gap',             tone: 'warn',  glyph: '◐' },
  pending:      { label: 'pending',         tone: 'muted', glyph: '○' },
  insufficient: { label: 'insufficient',    tone: 'muted', glyph: '○' },
  no_data:      { label: 'no data',         tone: 'muted', glyph: '○' },
  unavailable:  { label: 'unavailable',     tone: 'muted', glyph: '○' },
});

// Compact observation age: "4s", "12m", "2h 5m", "3d". Null for unknown or
// future (clock-skewed) times -- an unknown age is said in words, not guessed.
function formatTruthAge(observedMs, nowMs) {
  if (observedMs == null || !Number.isFinite(observedMs) || !Number.isFinite(nowMs)) return null;
  const s = Math.floor((nowMs - observedMs) / 1000);
  if (s < -30) return null;
  if (s < 60) return `${Math.max(0, s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

// state: a TRUTH_STATES key (unknown keys degrade to "unavailable").
// opts.observedMs / opts.nowMs: when the reading was observed; adds the age.
// opts.note: a short qualifier shown after the label ("1 strike", "refresh failed").
// opts.why: longer plain-language reason for the tooltip / accessible name.
function describeTruth(state, opts = {}) {
  const key = Object.prototype.hasOwnProperty.call(TRUTH_STATES, state) ? state : 'unavailable';
  const def = TRUTH_STATES[key];
  const age = formatTruthAge(opts.observedMs, opts.nowMs);
  const parts = [def.label];
  if (opts.note) parts.push(String(opts.note));
  parts.push(age != null ? age : (opts.observedMs === undefined ? null : 'age unknown'));
  const text = parts.filter(Boolean).join(' · ');
  const spoken = `Reading ${def.label}`
    + (opts.note ? `, ${opts.note}` : '')
    + (age != null ? `, observed ${age} ago` : opts.observedMs === undefined ? '' : ', observation time unknown')
    + (opts.why ? `. ${opts.why}` : '');
  return { state: key, tone: def.tone, glyph: def.glyph, label: def.label, age, text, spoken };
}

// Writes a describeTruth() result into a chip element. Missing elements are
// ignored so panels (and their test shims) without a chip keep working.
function renderTruthChip(el, truth) {
  if (!el || !truth) return;
  el.textContent = `${truth.glyph} ${truth.text}`;
  if (el.dataset) {
    el.dataset.truth = truth.state;
    el.dataset.tone = truth.tone;
  }
  if (typeof el.setAttribute === 'function') {
    el.setAttribute('aria-label', truth.spoken);
    el.setAttribute('title', truth.spoken);
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { TRUTH_STATES, formatTruthAge, describeTruth, renderTruthChip };
}
