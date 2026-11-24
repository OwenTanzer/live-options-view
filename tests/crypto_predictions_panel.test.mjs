// Crypto predictions panel tests: the R2-first as-of estimate read
// in site/js/crypto-predictions.js (docs/specs/CRYPTO_PREDICTIONS.md)
// and its rendering in site/js/crypto-panel.js (docs/specs/
// CRYPTO_SNAPSHOT.md, "Predictions read path").
//
// The prediction file (crypto/predictions/<COIN>.json) already holds
// One Clock observation rows, so the module validates them through
// the real selectAsOf / readingAsOf — never an estimate as a live
// price, and never a passed estimate shown as the live next-hour
// estimate: passed predictions are retained as historical
// evidence, and the read path reports "no live estimate" once
// their target hour has passed. Estimates are
// labelled ESTIMATE ONLY / RESEARCH ONLY wherever they are shown.
// Plain node:assert; the panel half drives the shipped classic
// script with fakes, like tests/crypto_panel.test.js does.
// Run: node tests/crypto_predictions_panel.test.mjs
// (Joins tests/ when the patch is applied on top of PR #389.)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  MAX_AGE_MS, ORIGIN, PREDICTIONS_BASE_PATH, SESSION, SOURCE,
  predictionPath, readPredictionsAsOf, toObservations,
} from '../site/js/crypto-predictions.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const panelSource = fs.readFileSync(
  path.join(__dirname, '../site/js/crypto-panel.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');

const NOW = Date.parse('2026-11-24T00:45:00Z');
const GENERATED_AT = '2026-11-24T00:32:44Z';

function estimateValue(targetStart, targetEnd, price = 61192.0) {
  return {
    estimated_price: price, direction: 'flat', pct_move: 0.003,
    range_low: price - 300, range_high: price + 300, confidence: 0.743,
    last_close: 61189.97,
    data_window: { bars_1h: 168, returns: 24,
      first_bar: '2026-11-17T00:00:00Z', last_bar: '2026-11-23T23:00:00Z',
      hours_24h: 24, hours_7d: 168 },
    basis: ['last-hour momentum', '24h move', '7d move',
      'hourly volatility', 'last-hour range'],
    signals: { momentum_1h_pct: 0.01, move_24h_pct: -0.5, move_7d_pct: -1.2,
      hourly_volatility_pct: 0.49, last_hour_range_pct: 0.74,
      btc_correlation: null },
    target_start: targetStart, target_end: targetEnd,
  };
}

function predictionRow(targetStart, targetEnd, revision = 1, value = undefined) {
  return {
    source: SOURCE, id: `BTC|1h|${targetStart}`, instrument: 'BTC',
    session: SESSION, evidence: 'crypto/predictions/BTC.json',
    origin: ORIGIN, revision,
    eventAt: Date.parse(targetStart), valueAt: Date.parse(targetStart),
    availableAt: Date.parse(GENERATED_AT),
    value: value === undefined ? estimateValue(targetStart, targetEnd) : value,
    provider: SOURCE, horizon: '1h',
  };
}

function predictionFile() {
  // One passed hour (value row retained as historical evidence)
  // and one live hour. No retraction rows are published.
  return {
    schema_version: 1, dataset: 'crypto_predictions', symbol: 'BTC',
    horizon: '1h', generated_at: GENERATED_AT, coverage: 'current',
    reason: null, observations_count: 2,
    observations: [
      predictionRow('2026-11-23T23:00:00Z', '2026-11-24T00:00:00Z'),
      predictionRow('2026-11-24T00:00:00Z', '2026-11-24T01:00:00Z'),
    ],
  };
}

const response = (value) => ({ ok: true, status: 200, json: async () => value });
const notFound = { ok: false, status: 404, json: async () => ({}) };

// --- paths and constants ---------------------------------------------------
assert.equal(PREDICTIONS_BASE_PATH, 'crypto/predictions/',
  'predictions live under crypto/predictions/');
assert.equal(predictionPath('BTC'), 'crypto/predictions/BTC.json',
  'the R2 prediction path is crypto/predictions/<COIN>.json');
assert.equal(SESSION, 'continuous',
  'prediction observations use the continuous 24/7 session');
assert.equal(SOURCE, 'crypto_predictions', 'the adapter id is the producer');
assert.equal(ORIGIN, 'synthetic',
  'predictions are synthetic observations, never recorded prices');
assert.equal(MAX_AGE_MS, 2 * 60 * 60 * 1000,
  'the prediction staleness bound matches the snapshot panel (2h)');

// --- module: validation and as-of reads ------------------------------------
{
  const { observations } = toObservations(predictionFile(), 'BTC');
  assert.equal(observations.length, 2, 'the fixture file holds two rows');
  assert.ok(observations.every((r) => r.revision === 1),
    'every row is a revision-1 value row: no retractions are published');
  assert.ok(observations.every((r) => r.origin === 'synthetic'),
    'every validated row is synthetic');

  // A recorded row in a prediction file is malformed, never served.
  const bad = predictionFile();
  bad.observations[0] = { ...bad.observations[0], origin: 'recorded' };
  assert.throws(() => toObservations(bad, 'BTC'), /not a synthetic/,
    'a recorded row must not pass as a prediction');
  const wrongCoin = predictionFile();
  wrongCoin.observations[0] = { ...wrongCoin.observations[0], instrument: 'ETH' };
  assert.throws(() => toObservations(wrongCoin, 'BTC'), /not BTC/,
    'a row for another coin must not pass');

  // Live estimate as of 00:45: the 00:00 prediction is fresh; the
  // retained 23:00 hour is history, not the live estimate.
  const live = await readPredictionsAsOf({
    fetchFn: async () => response(predictionFile()),
    coin: 'BTC', cutoff: NOW,
  });
  assert.equal(live.state, 'fresh', 'the live estimate reads fresh');
  assert.equal(live.reading.row.id, 'BTC|1h|2026-11-24T00:00:00Z',
    'the reading is the 00:00 target hour, not the retained 23:00 one');
  assert.equal(live.reading.row.value.estimated_price, 61192.0);

  // A cutoff before the live prediction was available sees
  // nothing: unavailable, never a stale estimate. (The 23:00 row
  // is not selectable yet either — its availableAt is 00:32.)
  const early = await readPredictionsAsOf({
    fetchFn: async () => response(predictionFile()),
    coin: 'BTC', cutoff: Date.parse('2026-11-24T00:15:00Z'),
  });
  assert.equal(early.state, 'unavailable',
    'before availability, no estimate is shown');
  assert.match(early.reason, /no live estimate/,
    'the reason says there is no live estimate yet');

  // A cutoff after the target hour, with the prediction retained
  // as historical evidence, is unavailable as a *live* estimate —
  // the past estimate is never shown as the next hour's — but
  // its value is still there to be judged: the reading row
  // carries the estimate, and the reason names the retained
  // history and its target hour.
  const retainedOnly = {
    ...predictionFile(),
    observations: [
      predictionRow('2026-11-24T00:00:00Z', '2026-11-24T01:00:00Z'),
    ],
  };
  const past = await readPredictionsAsOf({
    fetchFn: async () => response(retainedOnly),
    coin: 'BTC', cutoff: Date.parse('2026-11-24T01:30:00Z'),
  });
  assert.equal(past.state, 'unavailable',
    'a passed estimate is not shown as the live next-hour estimate');
  assert.equal(past.reading.row.id, 'BTC|1h|2026-11-24T00:00:00Z',
    'the reading row is still the retained prediction');
  assert.equal(past.reading.row.value.estimated_price, 61192.0,
    'the retained prediction keeps its value for judging');
  assert.match(past.reason, /retained as historical evidence/,
    'the reason names the retained history');
  assert.match(past.reason, /2026-11-24T00:00:00Z/,
    'the reason names the target hour');

  // 404 and malformed files are unavailable with a reason.
  const missing = await readPredictionsAsOf({
    fetchFn: async () => notFound, coin: 'BTC', cutoff: NOW,
  });
  assert.equal(missing.state, 'unavailable');
  assert.match(missing.reason, /no predictions published yet/);
  const malformed = await readPredictionsAsOf({
    fetchFn: async () => response({ observations: 'nope' }),
    coin: 'BTC', cutoff: NOW,
  });
  assert.equal(malformed.state, 'unavailable');
}

// --- page wiring -----------------------------------------------------------
assert.ok(html.includes('id="crypto-predictions"'),
  'the predictions section must exist');
assert.ok(html.includes('js/crypto-predictions.js'),
  'the predictions module must be loaded');
assert.ok(html.includes('Next hour estimate'),
  'the section must be headed as an estimate');

// --- panel: estimate-only rendering ----------------------------------------
// The classic panel script, driven with fakes like
// tests/crypto_panel.test.js: a fake window.CryptoPredictions
// returns the module-shaped result, and the render target is a
// plain element stub.
const elements = {};
const el = (id) => elements[id] ||= {
  textContent: '', innerHTML: '', dataset: {},
  classList: { toggle() {} },
};
function loadPanel(fakeModule) {
  for (const key of Object.keys(elements)) delete elements[key];
  let expiry;
  let poll;
  const scope = {
    document: { getElementById: el },
    BROWSER_DATA: '/browser-data',
    escapeHtml: (s) => String(s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    fetch: async () => { throw new Error('no fetch in this harness'); },
    window: { CryptoPredictions: fakeModule },
    setTimeout: (callback, delay) => { expiry = { callback, delay }; return 1; },
    clearTimeout: () => {},
    setInterval: callback => { poll = callback; return 1; },
  };
  const panel = new Function(...Object.keys(scope), panelSource + `
  let clock = Date.parse('2026-11-24T00:45:00Z');
  function cryptoNow() { return clock; }
  return { fetchCryptoPredictions, renderCryptoPredictions, loadCryptoPanel,
    set now(value) { clock = value; },
    get prediction() { return cryptoPrediction; },
    get predictionError() { return cryptoPredictionError; } };
`)(...Object.values(scope));
  return Object.assign(panel, { expiry: () => expiry, poll: () => poll });
}

function fakeResult(state, rowValue) {
  return {
    state,
    rows: [],
    reading: rowValue === undefined ? null : {
      state, row: rowValue === null ? { value: null } : {
        id: 'BTC|1h|2026-11-24T00:00:00Z',
        availableAt: Date.parse(GENERATED_AT),
        value: estimateValue('2026-11-24T00:00:00Z', '2026-11-24T01:00:00Z'),
      },
      ageMs: 45 * 60 * 1000, publicationAgeMs: 12 * 60 * 1000,
    },
    reason: state === 'unavailable'
      ? 'no live estimate for this hour — the latest prediction (target 2026-11-24T00:00:00Z–2026-11-24T01:00:00Z) is retained as historical evidence' : null,
  };
}

{
  // A live estimate renders with the estimate-only labelling and
  // every field the spec names — and no live-price language.
  const panel = loadPanel({
    readPredictionsAsOf: async () => fakeResult('fresh', true),
  });
  await panel.fetchCryptoPredictions();
  const rendered = el('crypto-predictions').innerHTML;
  assert.match(rendered, /ESTIMATE ONLY/, 'estimates are labelled estimate-only');
  assert.match(rendered, /RESEARCH ONLY/, 'estimates are labelled research-only');
  assert.match(rendered, /not a live price/, 'the label says it is not a live price');
  assert.match(rendered, /\$61,192/, 'the estimated price is shown');
  assert.match(rendered, /flat/, 'the direction is shown');
  assert.match(rendered, /\+0\.00%/, 'the percent move is shown');
  assert.match(rendered, /\$60,892/, 'the range low is shown');
  assert.match(rendered, /\$61,492/, 'the range high is shown');
  assert.match(rendered, /74%/, 'the confidence is shown');
  assert.match(rendered, /168 1h bars/, 'the data window is shown');
  assert.match(rendered, /last-hour momentum/, 'the basis is shown');
  assert.match(rendered, /00:00 UTC/, 'the estimate/target times are shown');
  assert.match(rendered, /01:00 UTC/, 'the target hour end is shown');
  assert.equal(panel.expiry().delay, 15 * 60 * 1000);
  panel.now = Date.parse('2026-11-24T01:00:00Z');
  panel.expiry().callback();
  assert.match(el('crypto-predictions').innerHTML, /No live next-hour estimate/);
  assert.doesNotMatch(el('crypto-predictions').innerHTML, /\$61,192/);
  await Promise.resolve();

  // A passed prediction renders as "no live estimate" — and
  // the estimated price appears nowhere in the section, even
  // though the prediction itself is retained in the file.
  const passed = loadPanel({
    readPredictionsAsOf: async () => fakeResult('unavailable', null),
  });
  await passed.fetchCryptoPredictions();
  const gone = el('crypto-predictions').innerHTML;
  assert.match(gone, /No live next-hour estimate/,
    'a passed prediction is not shown as the live estimate');
  assert.match(gone, /retained as historical evidence/,
    'the reason names the retained history');
  assert.doesNotMatch(gone, /\$61,192/, 'no estimated price is shown for a passed hour');

  // No predictions module: explicit state, no invented numbers.
  const nomodule = loadPanel(null);
  await nomodule.fetchCryptoPredictions();
  assert.match(el('crypto-predictions').innerHTML, /predictions module not loaded/);

  // The live panel is untouched: the snapshot render, hero and
  // table code are still present and unmodified in shape.
  assert.ok(panelSource.includes('function renderCryptoPanel'),
    'the snapshot renderer must remain');
  assert.ok(panelSource.includes('function cryptoRowHtml'),
    'the price table renderer must remain');
  assert.ok(panelSource.includes("fetchCryptoPredictions();"),
    'loadCryptoPanel must call the predictions fetch');
  assert.ok(!panelSource.includes('estimated_price ='),
    'the panel must never compute an estimate itself');
}

let refreshes = 0;
const refreshing = loadPanel({ readPredictionsAsOf: async () => {
  refreshes++;
  return fakeResult('unavailable', null);
} });
refreshing.loadCryptoPanel();
await Promise.resolve();
assert.equal(refreshes, 1);
refreshing.poll()();
await Promise.resolve();
assert.equal(refreshes, 2);

console.log('crypto predictions panel tests passed');
