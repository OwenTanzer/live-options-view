import { instant, observation, observationKey, selectAsOf } from './one-clock.js';

const OPS = { '>=': (a, b) => a >= b, '>': (a, b) => a > b, '<=': (a, b) => a <= b,
  '<': (a, b) => a < b, '==': (a, b) => a === b };
const FIELD = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$/;
const text = (v, name) => { if (typeof v !== 'string' || !v.trim()) throw new Error(`${name} is required`); };
const integer = (v, name, min = 0) => { if (!Number.isSafeInteger(v) || v < min) throw new Error(`${name} is invalid`); };
function fieldName(value) {
  if (!FIELD.test(value) || value.split('.').some(p => ['__proto__', 'prototype', 'constructor'].includes(p))) throw new Error('invalid field');
}
function field(value, name) {
  return name.split('.').reduce((obj, key) => obj && Object.hasOwn(obj, key) ? obj[key] : undefined, value);
}
function numeric(value) { return typeof value === 'number' && Number.isFinite(value); }
function immutable(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(immutable); Object.freeze(value); }
  return value;
}

export function eventRule(input) {
  const rule = structuredClone(input);
  for (const name of ['id', 'name']) text(rule[name], name);
  integer(rule.version, 'version', 1);
  if (!Array.isArray(rule.instruments) || !rule.instruments.length) throw new Error('choose instruments');
  rule.instruments.forEach(v => text(v, 'instrument'));
  if (!['all', 'any'].includes(rule.combine)) throw new Error('choose all or any conditions');
  if (!['rising', 'once-per-session'].includes(rule.transition)) throw new Error('invalid transition');
  if (!['hold', 'reset'].includes(rule.missingInput)) throw new Error('choose missing-input policy');
  if (!Array.isArray(rule.conditions) || !rule.conditions.length || rule.conditions.length > 20) throw new Error('one to twenty conditions required');
  rule.conditions.forEach(c => {
    text(c.source, 'condition source'); fieldName(c.field);
    if (!Object.hasOwn(OPS, c.op)) throw new Error('invalid comparison');
    if (!numeric(c.value) && typeof c.value !== 'boolean' && typeof c.value !== 'string') throw new Error('invalid comparison value');
    if (c.op !== '==' && !numeric(c.value)) throw new Error('ordered comparisons require numbers');
    if (c.relativeToAnchor !== undefined && typeof c.relativeToAnchor !== 'boolean') throw new Error('invalid anchor policy');
    integer(c.maxAgeMs, 'condition age');
  });
  if (!Array.isArray(rule.outcomes) || rule.outcomes.length > 8) throw new Error('zero to eight outcomes required');
  const ids = new Set();
  rule.outcomes.forEach(o => {
    text(o.id, 'outcome id'); text(o.source, 'outcome source'); fieldName(o.field);
    if (ids.has(o.id)) throw new Error('duplicate outcome id');
    ids.add(o.id);
    if (!['return', 'change', 'value'].includes(o.measure)) throw new Error('invalid measure');
    integer(o.horizonMs, 'horizon', 1); integer(o.toleranceMs, 'tolerance'); integer(o.maxAgeMs, 'baseline age');
  });
  return immutable(rule);
}

// No arbitrary code or expression evaluation: rules are data. The immutable
// version is also the prospective definition; editing means a new version.
export function saveRule(versions, input) {
  const existing = versions.filter(v => v.id === input.id);
  const next = existing.reduce((max, v) => Math.max(max, v.version), 0) + 1;
  return eventRule({ ...input, version: next });
}

function evidence(row) {
  return row ? { source: row.source, id: row.id, revision: row.revision,
    eventAt: row.eventAt, availableAt: row.availableAt, evidence: row.evidence } : null;
}

export function evaluateEvents({ rule: input, observations, selection, sessionClose, activationWindows }) {
  const rule = eventRule(input);
  const cutoff = instant(selection.cutoff);
  const close = instant(sessionClose);
  if (!rule.instruments.includes(selection.instrument)) return [];
  const rows = observations.map(observation).filter(r => r.session === selection.session &&
    r.instrument === selection.instrument && r.origin === selection.origin &&
    r.availableAt !== null && r.availableAt <= cutoff && r.eventAt <= cutoff);
  // Validate duplicate revision contents once, then advance a source index by
  // availability. Do not rescan/clone the full archive per condition per tick.
  const finalRows = selectAsOf(rows, selection);
  const arrivals = new Map();
  for (const row of rows) {
    if (!arrivals.has(row.availableAt)) arrivals.set(row.availableAt, []);
    arrivals.get(row.availableAt).push(row);
  }
  // Freshness changes even when no feed publishes. Evaluate integer-millisecond
  // expiry boundaries so an unrelated stream cannot change reset semantics.
  const boundaries = new Set(arrivals.keys());
  for (const row of rows) for (const condition of rule.conditions) {
    if (row.source !== condition.source) continue;
    const expires = row.eventAt + condition.maxAgeMs + 1;
    if (Number.isSafeInteger(expires) && expires > row.availableAt && expires <= cutoff) boundaries.add(expires);
  }
  const bySource = new Map();
  const latest = new Map();
  const compare = (a, b) => a.eventAt - b.eventAt || a.availableAt - b.availableAt || observationKey(a).localeCompare(observationKey(b));
  function ingest(row) {
    if (!bySource.has(row.source)) bySource.set(row.source, new Map());
    const records = bySource.get(row.source);
    const key = observationKey(row);
    const prior = records.get(key);
    if (prior && prior.revision >= row.revision) return;
    records.set(key, row);
    const newest = latest.get(row.source);
    if (!newest || compare(row, newest) >= 0) latest.set(row.source, row);
    else if (newest === prior) latest.set(row.source, [...records.values()].reduce((a, b) => compare(a, b) >= 0 ? a : b));
  }
  function readingAt(source, at, maxAgeMs) {
    const row = latest.get(source) ?? null;
    return { row, state: !row || row.value === null ? 'unavailable' : at - row.eventAt > maxAgeMs ? 'stale' : 'fresh' };
  }
  const anchors = new Map();
  const events = [];
  let bullish = false;
  let active = null;
  let previousSignature = null;
  for (const at of [...boundaries].sort((a, b) => a - b)) {
    if (at > close) break; // Late publication may score an outcome, not create a post-close trigger.
    for (const row of arrivals.get(at) ?? []) ingest(row);
    const values = rule.conditions.map(c => {
      const reading = readingAt(c.source, at, c.maxAgeMs);
      const raw = reading.row ? field(reading.row.value, c.field) : undefined;
      const anchorKey = JSON.stringify([c.source, c.field]);
      if (reading.state === 'fresh' && numeric(raw) && !anchors.has(anchorKey)) anchors.set(anchorKey, { value: raw, evidence: evidence(reading.row) });
      const anchor = anchors.get(anchorKey);
      const value = c.relativeToAnchor && numeric(raw) ? raw - anchor?.value : raw;
      const known = reading.state === 'fresh' && value !== undefined && value !== null &&
        (c.op === '==' || numeric(value));
      return { known, matches: known && OPS[c.op](value, c.value), value,
        anchor: c.relativeToAnchor ? anchor?.value : null,
        anchorEvidence: c.relativeToAnchor ? anchor?.evidence ?? null : null, row: reading.row };
    });
    const matches = rule.combine === 'all' ? values.every(v => v.known && v.matches) : values.some(v => v.known && v.matches);
    // Three-valued logic: false AND unknown is false; true OR unknown is true.
    const known = matches || (rule.combine === 'all' ? values.some(v => v.known && !v.matches) : values.every(v => v.known));
    if (!known) {
      if (rule.missingInput === 'reset') { bullish = false; active = null; }
      continue;
    }
    if (!matches) { bullish = false; active = null; previousSignature = null; continue; }
    const signature = JSON.stringify(values.map(v => v.row ? [observationKey(v.row), v.row.revision] : null));
    // Anchors and condition state retain the whole session's eligible history.
    // Activation admits a rising edge; it neither invents an edge nor spends
    // the once-per-session allowance on an inactive occurrence.
    const admitted = activationWindows === undefined || activationWindows.some(w => at >= w.started_at && (w.ended_at === null || at < w.ended_at));
    if (admitted && !bullish && (rule.transition !== 'once-per-session' || events.length === 0)) {
      const id = JSON.stringify([rule.id, rule.version, selection.origin, selection.session, selection.instrument, at, signature]);
      const baselines = Object.fromEntries(rule.outcomes.map(o => {
        const reading = readingAt(o.source, at, o.maxAgeMs);
        const value = reading.row ? field(reading.row.value, o.field) : undefined;
        return [o.id, { value: reading.state === 'fresh' && numeric(value) ? value : null, evidence: evidence(reading.row) }];
      }));
      active = { id, ruleId: rule.id, ruleVersion: rule.version, session: selection.session,
        instrument: selection.instrument, origin: selection.origin, triggeredAt: at,
        inputEvidence: values.map(v => ({ ...evidence(v.row), value: v.value, anchor: v.anchor, anchorEvidence: v.anchorEvidence })),
        baselines, matchingObservations: 0 };
      events.push(active);
    }
    if (active && signature !== previousSignature) active.matchingObservations++;
    previousSignature = signature;
    bullish = true;
  }
  return immutable(events.map(event => ({ ...event,
    outcomes: rule.outcomes.map(o => measureOutcome(event, o, finalRows, selection, close)),
  })));
}

function measureOutcome(event, outcome, rows, selection, close) {
  const targetAt = event.triggeredAt + outcome.horizonMs;
  const deadline = Math.min(targetAt + outcome.toleranceMs, close);
  const cutoff = instant(selection.cutoff);
  const base = { id: outcome.id, targetAt, value: null, measuredAt: null, availableAt: null, evidence: null };
  if (targetAt > close) return { ...base, state: cutoff >= close ? 'missing' : 'pending', reason: cutoff >= close ? 'session-closed-before-horizon' : null };
  const baseline = event.baselines[outcome.id].value;
  if (outcome.measure !== 'value' && (baseline === null || (outcome.measure === 'return' && baseline === 0))) {
    return { ...base, state: 'missing', reason: 'trigger-baseline-unavailable' };
  }
  const eligible = rows.filter(r => r.source === outcome.source);
  const row = eligible.find(r => r.eventAt >= targetAt && r.eventAt <= deadline && numeric(field(r.value, outcome.field)));
  if (!row) return { ...base, state: cutoff > deadline ? 'missing' : 'pending', reason: cutoff > deadline ? 'no-qualified-endpoint' : null };
  const endpoint = field(row.value, outcome.field);
  const value = outcome.measure === 'return' ? (endpoint / baseline - 1) * 100 : outcome.measure === 'change' ? endpoint - baseline : endpoint;
  return { ...base, state: 'measured', value, measuredAt: row.eventAt, availableAt: row.availableAt, evidence: evidence(row), reason: null };
}

export function aggregateEvents(events, outcomeId) {
  const outcomes = events.map(e => e.outcomes.find(o => o.id === outcomeId)).filter(Boolean);
  const values = outcomes.filter(o => o.state === 'measured').map(o => o.value).sort((a, b) => a - b);
  const n = values.length;
  return Object.freeze({ occurrences: events.length, sessions: new Set(events.map(e => e.session)).size,
    measured: n, pending: outcomes.filter(o => o.state === 'pending').length,
    missing: outcomes.filter(o => o.state === 'missing').length,
    mean: n ? values.reduce((a, b) => a + b, 0) / n : null,
    median: n ? (values[Math.floor(n / 2)] + values[Math.floor((n - 1) / 2)]) / 2 : null,
    values: Object.freeze(values),
  });
}
