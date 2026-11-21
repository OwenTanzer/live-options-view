import test from 'node:test';
import assert from 'node:assert/strict';
import { marketReadingObservation, marketReadingSource } from '../site/shared/one-clock-market-readings.js';
import { createSourceRegistry, createClock } from '../site/shared/one-clock.js';
import { T, selection } from './helpers/one_clock_conformance.mjs';

const exported = (changes = {}) => ({
  snapshot_key: 'intraday/20261109/snapshot_093354.csv', symbol: 'QQQ',
  reading_id: 'reference_oi_skew', schema_version: 'market_reading.v1', digest_status: 'ok',
  publication: { status: 'published' }, published_at: T + 1000,
  snapshot_constructed_at: T, observed_at: T, reading_published_at: T + 500,
  status: 'ok', freshness: 'live', values: { imbalance_ratio: 0.342 },
  ...changes,
});
const convert = row => marketReadingObservation(row, { session: selection.session, readingId: 'reference_oi_skew' });

test('reuse verified export and gate on both snapshot and reading publication', async () => {
  const registry = createSourceRegistry();
  registry.register(marketReadingSource({ readingId: 'reference_oi_skew',
    load: async () => [exported(), exported({ digest_status: 'digest_mismatch' })], maxAgeMs: 150000 }));
  const clock = createClock({ registry, initial: { ...selection, mode: 'replay' } });
  assert.equal((await clock.read('market-reading:reference_oi_skew')).rows.length, 0);
  clock.select({ ...selection, mode: 'replay', cutoff: T + 1000 });
  const result = await clock.read('market-reading:reference_oi_skew');
  assert.equal(result.rows.length, 1);
  assert.equal(result.reading.row.value.imbalance_ratio, 0.342);
  assert.equal(convert(exported({ reading_published_at: T + 2000 })).availableAt, T + 2000);
});

test('unpublished and unknown timestamps cannot be repaired from construction time', () => {
  assert.equal(convert(exported({ publication: { status: 'archived_unpublished' } })).availableAt, null);
  assert.equal(convert(exported({ published_at: null })).availableAt, null);
  const fallback = convert(exported({ observed_at: null }));
  assert.equal(fallback.details.observationBasis, 'snapshot-construction');
  assert.equal(fallback.details.sourceObservationProven, false);
  assert.equal(convert(exported({ observed_at: null, snapshot_constructed_at: null })), null);
});

test('digest errors, another reading and unknown schema fail closed', () => {
  for (const changes of [{ digest_status: 'digest_mismatch' }, { digest_status: 'missing_readings' },
    { schema_version: 'unknown' }, { reading_id: 'another' },
    { snapshot_key: 'intraday/20261110/snapshot_093354.csv' }]) assert.equal(convert(exported(changes)), null);
});

test('legacy values stay labelled and non-ok readings cannot appear fresh with a value', () => {
  const legacy = marketReadingObservation(exported({ reading_id: 'legacy_underlying_market',
    schema_version: 'pre-market_reading.v1', digest_status: 'legacy_no_readings' }),
  { session: selection.session, readingId: 'legacy_underlying_market' });
  assert.equal(legacy.details.legacy, true);
  assert.equal(convert(exported({ status: 'no_data' })).value, null);
});
