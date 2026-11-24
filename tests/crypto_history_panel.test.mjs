import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {archiveAsOf, coverageCells} from '../site/js/crypto-history.js';

const receipt = Date.parse('2026-11-24T07:00:00Z');
const end = receipt - 3600000;
const row = {source:'crypto_candle_cache', instrument:'BTC', interval:'1h',
  session:'continuous', provider:'coinbase', origin:'recorded', revision:1,
  id:'BTC|1h|hour', evidence:'snapshot#btc-hourly', eventAt:end,
  valueAt:end-3600000, availableAt:receipt, value:{close:100}};
const archive = {available_at:new Date(receipt).toISOString(), coverage:[{symbol:'BTC'}], btc_hourly:[row]};

test('crypto tab exposes historical evidence at archive receipt, including separate market and receipt times', () => {
  const before = archiveAsOf(archive, receipt-1);
  assert.deepEqual(before, {available:false, candles:[], coverage:[]});
  const after = archiveAsOf(archive, receipt);
  assert.equal(after.candles.length, 1);
  assert.deepEqual([after.candles[0].value.close, after.candles[0].eventAt, after.candles[0].availableAt],
    [100, end, receipt]);
  assert.equal(after.coverage.length, 1);
  const late = {...row, availableAt:receipt+1};
  assert.equal(archiveAsOf({...archive, btc_hourly:[late]}, receipt).candles.length, 0);
});

test('archive rejects source substitution, synthetic prices and pre-receipt availability', () => {
  for (const change of [{provider:'tiingo'},{instrument:'ETH'},{origin:'synthetic'},
    {source:'other'},{interval:'1d'},{availableAt:receipt-1}]) {
    assert.throws(() => archiveAsOf({...archive, btc_hourly:[{...row,...change}]}, receipt));
  }
});

test('crypto tab links the shipped archive reader and cutoff control', () => {
  const html = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="crypto-history-cutoff"/);
  assert.match(html, /type="module" src="js\/crypto-history\.js/);
});

test('coverage reports source batch state independently of record count and timestamps', () => {
  const partial = {symbol:'LINK', interval:'1h', provider:'coinbase', kind:'candle', count:5,
    first:'2026-11-23T00:00:00Z', last:'2026-11-23T04:00:00Z', source_batch_done:false};
  assert.equal(coverageCells({...partial, primary:true})[2], 'coinbase (primary)');
  const alternate = coverageCells({...partial, provider:'coingecko', kind:'price_sample',
    primary:false, source_batch_done:null});
  assert.deepEqual(alternate.slice(2, 6), ['coingecko (alternate)', 'Price samples', 5, 'Batch status unavailable']);
  assert.deepEqual(coverageCells(partial).slice(4), [5, 'Batch in progress',
    '2026-11-23 00:00:00', '2026-11-23 04:00:00']);
  assert.deepEqual(coverageCells({...partial, count:0, first:null, last:null, source_batch_done:true}).slice(4),
    [0, 'Batch marked done', '—', '—']);
});
