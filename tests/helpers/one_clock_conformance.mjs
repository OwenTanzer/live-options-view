// Run this same suite for each source's normalization function. The fixture
// factory supplies source-native records; normalize must return the envelope.
import test from 'node:test';
import assert from 'node:assert/strict';
import { selectAsOf, readingAsOf } from '../../site/shared/one-clock.js';

export const T = Date.parse('2026-11-09T14:33:54Z');
export const selection = { session: '2026-11-09', instrument: 'QQQ', cutoff: T, origin: 'recorded' };
export function sourceConformance(name, normalize, fixture) {
  test(`${name}: availability, corrections, origin isolation and rewind`, () => {
    const rows = [
      fixture({ id: 'first', revision: 1, eventAt: T, availableAt: T + 1000, value: 1 }),
      fixture({ id: 'first', revision: 2, eventAt: T, availableAt: T + 5000, value: 2 }),
      fixture({ id: 'unproven', eventAt: T, availableAt: null }),
      fixture({ id: 'synthetic', eventAt: T, availableAt: T, origin: 'synthetic' }),
      fixture({ id: 'reconstructed', eventAt: T, availableAt: T, origin: 'reconstructed' }),
      fixture({ id: 'other-session', session: '2026-11-10', eventAt: T, availableAt: T }),
      fixture({ id: 'other-instrument', instrument: 'SPY', eventAt: T, availableAt: T }),
    ].map(normalize);
    const project = cutoff => selectAsOf(rows, { ...selection, cutoff });
    assert.deepEqual(project(T), []);
    assert.equal(project(T + 1000).length, 1);
    assert.equal(project(T + 1000)[0].value, 1);
    assert.equal(project(T + 5000)[0].value, 2);
    assert.equal(project(T + 4999)[0].value, 1);
    assert.deepEqual(project(T), []);
    assert.equal(selectAsOf(rows, { ...selection, origin: 'synthetic' })[0].id, 'synthetic');
    assert.equal(selectAsOf(rows, { ...selection, origin: 'reconstructed' })[0].id, 'reconstructed');
  });
  test(`${name}: deduplication and observation age, not fetch age`, () => {
    const row = normalize(fixture({ eventAt: T, availableAt: T + 1000 }));
    const at = { ...selection, cutoff: T + 2000 };
    assert.equal(selectAsOf([row, row], at).length, 1);
    const reading = readingAsOf([row], at, { maxAgeMs: 1999 });
    assert.equal(reading.state, 'stale');
    assert.equal(reading.ageMs, 2000);
    assert.equal(reading.publicationAgeMs, 1000);
    assert.equal(readingAsOf([row], at, { maxAgeMs: 2000 }).state, 'fresh');
  });
}
