// Tests for site/js/candle-cache-one-clock.js.
//
// Run from the repository root:
//     node --test tests/candle_cache_one_clock.test.mjs
//
// The module under test imports the REAL One Clock core
// (../shared/one-clock.js: selectAsOf / readingAsOf) — these tests
// prove the adapter's observations are valid One Clock rows and the
// shared selector behaves the same way over them as the Python
// mirror does (see tests/test_candle_cache_backend.py).
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  barsToObservations,
  selectCandlesAsOf,
  gapReport,
  createCandleCacheAdapter,
  CANDLE_CACHE_SOURCE,
} from '../site/js/candle-cache-one-clock.js';
import { selectAsOf } from '../site/shared/one-clock.js';

function file(bars, opts = {}) {
  const generated_at = 'generated_at' in opts ? opts.generated_at : '2026-11-24T00:12:32Z';
  const interval = opts.interval ?? '1m';
  return {
    schema_version: 1, dataset: 'single_stock_candles', symbol: 'AAPL',
    interval, base: true, derived_from: null,
    source: 'Tiingo (api.tiingo.com)', bars_count: bars.length,
    first_bar: bars[0]?.t ?? null, last_bar: bars.at(-1)?.t ?? null,
    generated_at, bars,
  };
}

function minuteBars(count, startMinute = 870) {
  const bars = [];
  for (let i = 0; i < count; i += 1) {
    const total = startMinute + i;
    const hh = String(Math.floor(total / 60)).padStart(2, '0');
    const mm = String(total % 60).padStart(2, '0');
    bars.push({ t: `2026-11-23T${hh}:${mm}:00Z`, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i });
  }
  return bars;
}

test('barsToObservations stamps the One Clock envelope', () => {
  const rows = barsToObservations(file(minuteBars(3)));
  assert.equal(rows.length, 3);
  const row = rows[0];
  assert.equal(row.source, CANDLE_CACHE_SOURCE);
  assert.equal(row.id, 'AAPL:1m:2026-11-23T14:30:00Z');
  assert.equal(row.session, '2026-11-23');
  assert.equal(row.instrument, 'AAPL');
  assert.equal(row.origin, 'recorded');
  assert.equal(row.revision, 1);
  assert.equal(row.evidence, 'nasdaq/candles/AAPL/1m.json');
  assert.equal(row.eventAt, Date.parse('2026-11-23T14:30:00Z'));
  assert.equal(row.availableAt, Date.parse('2026-11-24T00:12:32Z'));
  assert.equal(row.value.close, 100.5);
  assert.ok(!('volume' in row.value));
});

test('synthetic envelope origin is carried, never relabelled', () => {
  const rows = barsToObservations(file(minuteBars(1)), { origin: 'synthetic', revision: 1 });
  assert.equal(rows[0].origin, 'synthetic');
});

test('no generated_at means availableAt null, and the row is ineligible', () => {
  const rows = barsToObservations(file(minuteBars(1), { generated_at: undefined }));
  assert.equal(rows[0].availableAt, null);
  const selected = selectAsOf(rows, { session: '2026-11-23', instrument: 'AAPL', cutoff: Date.now() });
  assert.deepEqual(selected, []);
});

test('selectCandlesAsOf uses the real core: rewind restores the earlier revision', () => {
  const old = barsToObservations(file(minuteBars(1)))[0];
  const corrected = { ...old, revision: 2, availableAt: old.availableAt + 20_000, value: { ...old.value, close: 999 } };
  const atCorrection = selectCandlesAsOf([old, corrected], {
    session: '2026-11-23', instrument: 'AAPL', cutoff: old.availableAt + 20_000,
  }, { maxAgeMs: 60_000 });
  assert.equal(atCorrection.rows[0].value.close, 999);
  const beforeCorrection = selectCandlesAsOf([old, corrected], {
    session: '2026-11-23', instrument: 'AAPL', cutoff: old.availableAt + 10_000,
  }, { maxAgeMs: 60_000 });
  assert.equal(beforeCorrection.rows[0].value.close, 100.5);
});

test('selectCandlesAsOf reports stale instead of hiding old bars', () => {
  const rows = barsToObservations(file(minuteBars(2)));
  const result = selectCandlesAsOf(rows, {
    session: '2026-11-23', instrument: 'AAPL', cutoff: Date.parse('2026-11-24T00:12:32Z'),
  }, { maxAgeMs: 60_000 });
  assert.equal(result.reading.state, 'stale');
  assert.equal(result.rows.length, 2);
});

test('gapReport labels a missing minute as a price gap, honestly', () => {
  const bars = minuteBars(10);
  bars.splice(4, 1);
  const rows = barsToObservations(file(bars));
  const selected = selectCandlesAsOf(rows, {
    session: '2026-11-23', instrument: 'AAPL', cutoff: Date.parse('2026-11-24T00:12:32Z'),
  }, { maxAgeMs: 10 ** 12 });
  assert.equal(selected.rows.length, 9);
  assert.ok(selected.labels.includes('reaction unmeasurable · price gap'));
});

test('gapReport labels a late-starting session as coverage partial', () => {
  const rows = barsToObservations(file(minuteBars(10, 900))); // 10:00 ET start
  const report = gapReport(rows, '2026-11-23');
  assert.ok(report.some((entry) => entry.label === 'coverage partial'));
});

test('adapter read returns observations; a missing file is unavailable', async () => {
  const store = new Map([
    ['https://example.invalid/nasdaq/candles/AAPL/1m.json', file(minuteBars(2))],
    ['https://example.invalid/nasdaq/candles/AAPL/provenance.json', {
      envelope: { files: { '1m': { origin: 'recorded', revision: 1, available_at: '2026-11-24T00:12:32Z' } } },
    }],
  ]);
  const fetchJson = async (url) => {
    if (!store.has(url)) throw new Error(`404 ${url}`);
    return store.get(url);
  };
  const adapter = createCandleCacheAdapter({ fetchJson, baseUrl: 'https://example.invalid', interval: '1m' });
  assert.equal(adapter.id, CANDLE_CACHE_SOURCE);
  const rows = await adapter.read({ session: '2026-11-23', instrument: 'AAPL', mode: 'replay', origin: 'recorded', cutoff: Date.now(), generation: 0, signal: undefined });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].source, CANDLE_CACHE_SOURCE);

  const missing = createCandleCacheAdapter({ fetchJson, baseUrl: 'https://example.invalid', interval: '1m' });
  const empty = await missing.read({ session: '2026-11-23', instrument: 'MSFT', mode: 'replay', origin: 'recorded', cutoff: Date.now(), generation: 0, signal: undefined });
  assert.deepEqual(empty, []);
});
