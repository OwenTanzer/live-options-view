import test from 'node:test';
import assert from 'node:assert/strict';
import { observation, instant, selectAsOf, readingAsOf, createSourceRegistry, createClock } from '../site/shared/one-clock.js';
import { sourceConformance, T, selection } from './helpers/one_clock_conformance.mjs';

const fixture = (changes = {}) => ({ source: 'fixture', id: 'r1', revision: 1,
  eventAt: T, availableAt: T, origin: 'recorded', evidence: 'fixture://r1',
  instrument: 'QQQ', session: '2026-11-09', value: 1, ...changes });
sourceConformance('canonical envelope', observation, fixture);

function registry(read) {
  const sources = createSourceRegistry();
  sources.register({ id: 'fixture', version: '1', owner: 'test', maxAgeMs: 150000, read });
  return sources;
}
const initial = { ...selection, mode: 'replay' };

test('invalid timestamps and absent provenance fail at the adapter boundary', () => {
  for (const time of ['2026-11-09', '2026-11-09T14:33:54', T / 1000, NaN, Infinity, null]) {
    assert.throws(() => instant(time));
  }
  assert.equal(instant('2026-11-09T09:33:54-05:00'), T);
  for (const changes of [{ origin: undefined }, { revision: 0 }, { evidence: '' }, { availableAt: T - 1 }]) {
    assert.throws(() => observation(fixture(changes)));
  }
});

test('retained input and projected values cannot be mutated by a consumer', () => {
  const input = fixture({ value: { price: 1 } });
  const row = observation(input);
  input.value.price = 3;
  assert.equal(row.value.price, 1);
  assert.throws(() => { row.value.price = 2; });
  const rows = selectAsOf([row], selection);
  assert.throws(() => rows.push(row));
});

test('delivery order cannot choose a different revision; conflicting identities fail', () => {
  const first = fixture();
  const correction = fixture({ revision: 2, value: 2, availableAt: T + 1 });
  const at = { ...selection, cutoff: T + 1 };
  assert.deepEqual(selectAsOf([first, correction], at), selectAsOf([correction, first], at));
  assert.throws(() => selectAsOf([first, fixture({ value: 99 })], at), /conflicting/);
  // Same identity in different streams is not a duplicate.
  assert.equal(selectAsOf([first, fixture({ source: 'other' })], at).length, 2);
});

test('a retraction supersedes its earlier value only at its availability boundary', () => {
  const rows = [fixture(), fixture({ revision: 2, value: null, availableAt: T + 1 })];
  assert.equal(readingAsOf(rows, selection, { maxAgeMs: 10 }).state, 'fresh');
  assert.equal(readingAsOf(rows, { ...selection, cutoff: T + 1 }, { maxAgeMs: 10 }).state, 'unavailable');
});

test('unproven publication remains unavailable, including in reconstructed mode', () => {
  const rows = [fixture({ availableAt: null }), fixture({ origin: 'reconstructed', availableAt: null })];
  for (const origin of ['recorded', 'reconstructed']) {
    assert.equal(readingAsOf(rows, { ...selection, origin }, { maxAgeMs: 1 }).row, null);
  }
});

test('one request serves multiple panels, and only eligible records reach consumers', async () => {
  let calls = 0;
  const clock = createClock({ initial, registry: registry(async () => {
    calls++;
    return [fixture(), fixture({ id: 'future', eventAt: T + 1, availableAt: T + 1 })];
  }) });
  const [a, b] = await Promise.all([clock.read('fixture'), clock.read('fixture')]);
  assert.equal(calls, 1);
  assert.equal(a, b);
  assert.deepEqual(a.rows.map(row => row.id), ['r1']);
  assert.equal(a.selection.cutoff, T);
  clock.dispose();
});

test('session switch discards an old live response even when transport ignores abort', async () => {
  let finish;
  let signal;
  const clock = createClock({ initial: { ...initial, mode: 'live' }, now: () => T,
    registry: registry(input => { signal = input.signal; return new Promise(resolve => { finish = resolve; }); }) });
  const response = clock.read('fixture');
  await Promise.resolve();
  clock.select({ ...initial, session: '2026-11-10' });
  assert.equal(signal.aborted, true);
  finish([fixture()]);
  assert.deepEqual(await response, { discarded: true });
  clock.dispose();
});

test('selection before a queued read prevents even starting the old transport', async () => {
  let calls = 0;
  const clock = createClock({ initial, registry: registry(async () => { calls++; return []; }) });
  const response = clock.read('fixture');
  clock.select({ ...initial, cutoff: T + 1 });
  assert.deepEqual(await response, { discarded: true });
  assert.equal(calls, 0);
});

test('live ticks retain subscriptions; entering replay stops them and freezes time', () => {
  let time = T;
  let stopped = 0;
  const clock = createClock({ initial: { ...initial, mode: 'live' }, now: () => time, registry: registry(async () => []) });
  clock.ownLiveSubscription(() => { stopped++; });
  time += 1000;
  assert.equal(clock.tick().cutoff, time);
  assert.equal(stopped, 0);
  clock.select(initial);
  assert.equal(stopped, 1);
  time += 1000;
  assert.equal(clock.tick().cutoff, T);
  clock.ownLiveSubscription(() => { stopped++; });
  assert.equal(stopped, 2);
  clock.dispose();
});

test('failed requests can retry; failed selections preserve the existing view', async () => {
  let calls = 0;
  const clock = createClock({ initial, registry: registry(async () => {
    if (++calls === 1) throw new Error('offline');
    return [fixture()];
  }) });
  await assert.rejects(clock.read('fixture'), /offline/);
  assert.equal((await clock.read('fixture')).rows.length, 1);
  const before = clock.snapshot();
  assert.throws(() => clock.select({ ...initial, cutoff: 'invalid' }));
  assert.deepEqual(clock.snapshot(), before);
  clock.dispose();
  assert.throws(() => clock.read('fixture'), /disposed/);
});

test('adapters cannot impersonate another source or register twice', async () => {
  const sources = registry(async () => [fixture({ source: 'another' })]);
  assert.throws(() => sources.register({ id: 'fixture' }), /duplicate/);
  assert.throws(() => sources.get('unknown'), /unknown/);
  const clock = createClock({ initial, registry: sources });
  await assert.rejects(clock.read('fixture'), /another source/);
});
