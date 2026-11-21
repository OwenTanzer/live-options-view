import test from 'node:test';
import assert from 'node:assert/strict';
import { eventRule, saveRule, evaluateEvents, aggregateEvents } from '../site/shared/one-clock-events.js';
import { demoRules, demoRows, demoStart, demoClose, demoSelection } from '../site/shared/one-clock-demo.js';
const run = (cutoff, rule = demoRules[0], rows = demoRows) => evaluateEvents({ rule, observations: rows,
  selection: { ...demoSelection, cutoff }, sessionClose: demoClose });

test('continuous bullish ticks count once and every field rewinds with the cursor', () => {
  const trigger = demoStart + 5 * 60000 + 1000;
  assert.equal(run(trigger - 1).length, 0);
  const first = run(trigger);
  assert.equal(first.length, 1);
  assert.equal(first[0].matchingObservations, 1);
  assert.equal(first[0].outcomes[0].state, 'pending');
  assert.equal(aggregateEvents(first, 'spot-15m').mean, null);
  const later = run(demoStart + 21 * 60000 + 1000);
  assert.equal(later.length, 1);
  assert.equal(later[0].matchingObservations, 17);
  assert.equal(later[0].outcomes[0].state, 'measured');
  assert.deepEqual(run(trigger), first);
  assert.equal(run(demoClose).length, 2);
  assert.equal(run(demoClose)[1].outcomes[0].state, 'missing');
});

test('endpoint waits for its availability, even after its event-time horizon', () => {
  const trigger = demoStart + 5 * 60000 + 1000;
  const target = trigger + 900000;
  const rows = demoRows.filter(r => r.source !== 'price' || r.eventAt < target);
  rows.push({ ...demoRows.find(r => r.source === 'price'), id: 'late-endpoint',
    eventAt: target + 8000, availableAt: target + 20000, value: { spot: 671 } });
  assert.equal(run(target + 19999, demoRules[0], rows)[0].outcomes[0].state, 'pending');
  const measured = run(target + 20000, demoRules[0], rows)[0].outcomes[0];
  assert.equal(measured.state, 'measured');
  assert.equal(measured.measuredAt, target + 8000);
  assert.equal(measured.availableAt, target + 20000);
  assert.equal(run(target + 19999, demoRules[0], rows)[0].outcomes[0].value, null);
});

test('late qualifying endpoint corrects a missing result without changing trigger identity', () => {
  const trigger = demoStart + 301000;
  const target = trigger + 900000;
  const rows = demoRows.filter(r => r.source !== 'price' || r.eventAt < target);
  rows.push({ ...demoRows.find(r => r.source === 'price'), id: 'backfill',
    eventAt: target + 1000, availableAt: target + 160000, value: { spot: 671 } });
  const absent = run(target + 150001, demoRules[0], rows)[0];
  const corrected = run(target + 160000, demoRules[0], rows)[0];
  assert.equal(absent.outcomes[0].state, 'missing');
  assert.equal(corrected.outcomes[0].state, 'measured');
  assert.equal(absent.id, corrected.id);
});

test('second class uses presence and once-per-session semantics, not skew thresholds', () => {
  const events = run(demoClose, demoRules[1]);
  assert.equal(events.length, 1);
  assert.equal(events[0].triggeredAt, demoStart + 721000);
  assert.equal(events[0].ruleId, 'shortlist-first');
  assert.equal(events[0].outcomes[0].state, 'measured');
});

test('registration is immutable, versioned, portable, and supports tracking without outcomes', () => {
  const saved = saveRule(demoRules, { ...demoRules[0], name: 'Changed threshold' });
  assert.equal(saved.version, 2);
  assert.equal(demoRules[0].version, 1);
  assert.deepEqual(eventRule(JSON.parse(JSON.stringify(saved))), saved);
  assert.throws(() => { saved.conditions[0].value = 99; });
  const events = run(demoClose, { ...demoRules[0], outcomes: [] });
  assert.equal(events.length, 2);
  assert.equal(events[0].outcomes.length, 0);
});

test('duplicate delivery and shuffled ingestion reproduce replay identities and outcomes', () => {
  const expected = run(demoClose);
  assert.deepEqual(run(demoClose, demoRules[0], [...demoRows].reverse()), expected);
  assert.deepEqual(run(demoClose, demoRules[0], [...demoRows, ...demoRows]), expected);
  for (const cutoff of [demoStart + 301000, demoStart + 1261000, demoClose]) {
    const arrived = demoRows.filter(r => r.availableAt <= cutoff);
    assert.deepEqual(run(cutoff, demoRules[0], arrived), run(cutoff));
  }
});

test('future corrections do not rewrite a trigger before correction publication', () => {
  const row = demoRows.find(r => r.source === 'skew' && r.id === 'skew-5');
  const corrected = { ...row, revision: 2, availableAt: demoStart + 360000, value: { imbalance: 0.01, strikes: 5 } };
  assert.deepEqual(run(demoStart + 301000, demoRules[0], [...demoRows, corrected]), run(demoStart + 301000));
});

test('origin and instrument isolation prevent fixture contamination', () => {
  const observed = demoRows.map(row => ({ ...row, origin: 'recorded' }));
  assert.deepEqual(run(demoClose, demoRules[0], [...demoRows, ...observed]), run(demoClose));
  assert.deepEqual(run(demoClose, { ...demoRules[0], instruments: ['SPY'] }), []);
});

test('unknown conditions obey explicit hold/reset policy', () => {
  const rows = demoRows.filter(r => !(r.source === 'skew' && r.eventAt > demoStart + 5 * 60000 && r.eventAt < demoStart + 10 * 60000));
  const held = run(demoStart + 11 * 60000, demoRules[0], rows);
  const reset = run(demoStart + 11 * 60000, { ...demoRules[0], missingInput: 'reset' }, rows);
  assert.equal(held.length, 1);
  assert.equal(reset.length, 2);
});

test('invalid rules fail before evaluation', () => {
  for (const changes of [{ version: 0 }, { transition: 'eval' }, { conditions: [] },
    { outcomes: [{ ...demoRules[0].outcomes[0], horizonMs: -1 }] },
    { conditions: [{ ...demoRules[0].conditions[0], field: '__proto__.secret' }] }]) {
    assert.throws(() => eventRule({ ...demoRules[0], ...changes }));
  }
});

test('sparse feeds reset on expiry independently of unrelated arrivals and rewind', () => {
  const rule = { ...demoRules[0], missingInput: 'reset', outcomes: [],
    conditions: [{ source: 'skew', field: 'imbalance', op: '>', value: 0, maxAgeMs: 1000 }] };
  const first = { ...demoRows[0], source: 'skew', id: 'first', eventAt: demoStart,
    availableAt: demoStart, value: { imbalance: 1 } };
  const second = { ...first, id: 'second', eventAt: demoStart + 10000, availableAt: demoStart + 10000 };
  const unrelated = { ...first, source: 'unrelated', id: 'news', eventAt: demoStart + 5000, availableAt: demoStart + 5000 };
  const sparse = [first, second];
  for (const cutoff of [demoStart, demoStart + 1000, demoStart + 1001, demoStart + 9999, demoStart + 10000]) {
    assert.deepEqual(run(cutoff, rule, sparse), run(cutoff, rule, [...sparse, unrelated]));
  }
  assert.equal(run(demoStart + 10000, rule, sparse).length, 2);
  assert.equal(run(demoStart + 9999, rule, sparse).length, 1);
  assert.equal(run(demoStart + 10000, { ...rule, missingInput: 'hold' }, sparse).length, 1);
});

test('relative conditions retain the exact anchor identity and publication evidence', () => {
  const trigger = run(demoStart + 301000)[0];
  const anchored = trigger.inputEvidence.find(e => e.anchor !== null);
  assert.equal(anchored.anchor, 0.12);
  assert.equal(anchored.anchorEvidence.id, 'skew-0');
  assert.equal(anchored.anchorEvidence.availableAt, demoStart + 1000);
  assert.equal(anchored.anchorEvidence.revision, 1);
});

test('summary uses measured denominator, with empty and all-pending states', () => {
  assert.deepEqual(aggregateEvents([], 'spot-15m'), { occurrences: 0, sessions: 0, measured: 0,
    pending: 0, missing: 0, mean: null, median: null, values: [] });
  assert.equal(aggregateEvents(run(demoStart + 301000), 'spot-15m').pending, 1);
  const summary = aggregateEvents(run(demoClose), 'spot-15m');
  assert.equal(summary.occurrences, 2);
  assert.equal(summary.measured, 1);
  assert.equal(summary.missing, 1);
  assert.equal(summary.mean, summary.values[0]);
});
