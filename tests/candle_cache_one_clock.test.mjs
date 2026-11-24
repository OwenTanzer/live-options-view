import test from 'node:test';
import assert from 'node:assert/strict';

import {
  barsToObservations,
  selectCandlesAsOf,
  gapReport,
  readPublishedCandles,
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

function fixtureRows(bars) {
  return barsToObservations(file(bars), { origin: 'recorded', available_at: '2026-11-24T00:12:32Z' });
}

test('barsToObservations stamps the One Clock envelope', () => {
  const rows = fixtureRows(minuteBars(3));
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
  const rows = barsToObservations(file(minuteBars(1)), { origin: 'synthetic', revision: 1, available_at: '2026-11-24T00:12:32Z' });
  assert.equal(rows[0].origin, 'synthetic');
});

test('no generated_at means availableAt null, and the row is ineligible', () => {
  const rows = barsToObservations(file(minuteBars(1), { generated_at: undefined }));
  assert.deepEqual(rows, []);
  const selected = selectAsOf(rows, { session: '2026-11-23', instrument: 'AAPL', cutoff: Date.now() });
  assert.deepEqual(selected, []);
});

test('selectCandlesAsOf uses the real core: rewind restores the earlier revision', () => {
  const old = fixtureRows(minuteBars(1))[0];
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
  const rows = fixtureRows(minuteBars(2));
  const result = selectCandlesAsOf(rows, {
    session: '2026-11-23', instrument: 'AAPL', cutoff: Date.parse('2026-11-24T00:12:32Z'),
  }, { maxAgeMs: 60_000 });
  assert.equal(result.reading.state, 'stale');
  assert.equal(result.rows.length, 2);
});

test('gapReport labels a missing minute as a price gap, honestly', () => {
  const bars = minuteBars(10);
  bars.splice(4, 1);
  const rows = fixtureRows(bars);
  const selected = selectCandlesAsOf(rows, {
    session: '2026-11-23', instrument: 'AAPL', cutoff: Date.parse('2026-11-24T00:12:32Z'),
  }, { maxAgeMs: 10 ** 12 });
  assert.equal(selected.rows.length, 9);
  assert.ok(selected.labels.includes('reaction unmeasurable · price gap'));
});

test('gapReport labels a late-starting session as coverage partial', () => {
  const rows = fixtureRows(minuteBars(10, 900)); // 10:00 ET start
  const report = gapReport(rows, '2026-11-23', [Date.parse('2026-11-23T14:30Z'),Date.parse('2026-11-23T21:00Z')]);
  assert.ok(report.some((entry) => entry.label === 'coverage partial'));
});

test('published AAPL archive becomes visible at its acknowledgement time', async () => {
  const {execFileSync} = await import('node:child_process');
  const output = execFileSync('python3', ['-c', `
import base64, json
from datetime import datetime, timezone
from unittest.mock import patch
from market_data.single_stock_historical_backfill.provenance import build_provenance
from market_data.single_stock_historical_backfill.publish import publish_symbol
import sys
sys.path.insert(0, "tests")
from tests_stock_archive_store import FakeR2Client
store = FakeR2Client()
file = json.loads(input())
with patch("market_data.single_stock_historical_backfill.publish.datetime") as clock:
    clock.now.return_value = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)
    publish_symbol("AAPL", {"1m":file}, build_provenance("AAPL", {"1m":{}}),
               config={"TIINGO_R2_BUCKET":"fixture"}, client=store)
for key in ["nasdaq/candles/AAPL/1m.json", "nasdaq/candles/AAPL/provenance.json"]:
    print(base64.b64encode(store.objects[key]).decode())
`], {input:JSON.stringify(file(minuteBars(2))),encoding:'utf8'});
  const [interval, sidecar] = output.trimEnd().split('\n');
  const bytes = Buffer.from(interval, 'base64');
  const receipt = JSON.parse(Buffer.from(sidecar, 'base64'));
  const rows = await readPublishedCandles(bytes, receipt);
  assert.equal(rows.length,2);
  const cutoff = Date.parse(receipt.files['1m'].available_at);
  const selection = {session:'2026-11-23',instrument:'AAPL',cutoff};
  assert.equal(selectAsOf(rows,{...selection,cutoff:cutoff-1}).length,0);
  assert.equal(selectAsOf(rows,selection).length,2);
  assert.deepEqual(await readPublishedCandles(bytes,null),[]);
  assert.deepEqual(await readPublishedCandles(Buffer.concat([bytes,Buffer.from(' ')]),receipt),[]);
});

test('daily history from 2000 does not block a later session or its own session', () => {
  const bars = ['2000-01-03','2026-11-23'].map(day => ({
    ...minuteBars(1)[0], t:day+'T00:00:00Z',
  }));
  const rows = barsToObservations(file(bars,{interval:'1d'}),{
    origin:'recorded',available_at:'2026-11-24T01:00:00Z',
  });
  for (const session of ['2000-01-03','2026-11-23']) {
    const result = selectCandlesAsOf(rows, {instrument:'AAPL',session,cutoff:Date.parse('2026-11-24T02:00Z')},{maxAgeMs:60000});
    assert.equal(result.rows.length,1);
    assert.equal(result.rows[0].session,session);
  }
});

test('complete early close has no partial label; missing closing minute does', () => {
  const bars = minuteBars(210).map(row=>({...row,t:row.t.replace('11-23','11-27')}));
  const rows = barsToObservations(file(bars),{origin:'recorded',available_at:'2026-11-28T00:00Z'});
  const hours=[Date.parse('2026-11-27T14:30Z'),Date.parse('2026-11-27T18:00Z')];
  assert.deepEqual(gapReport(rows,'2026-11-27',hours),[]);
  assert.equal(gapReport(rows.slice(0,-1),'2026-11-27',hours)[0].label,'coverage partial');
});
