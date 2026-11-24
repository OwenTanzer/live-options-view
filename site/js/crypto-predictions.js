// crypto predictions — R2-first as-of estimate reads for the crypto panel.
//
// ESM module (loaded by index.html as a module script, like
// js/one-clock-history.js). It imports the real One Clock selector
// — selectAsOf / readingAsOf / observation from
// ../shared/one-clock.js — and serves the prediction files
// (crypto/predictions/<COIN>.json,
// docs/specs/CRYPTO_PREDICTIONS.md) through it. The prediction
// file already holds One Clock observation rows, so this module
// validates them rather than mapping them: a row whose origin is
// not "synthetic", or whose session is not "continuous", is a
// malformed file, never silently served.
//
// Estimates only. A prediction is a computed guess about the next
// hour, labelled ESTIMATE ONLY / RESEARCH ONLY wherever it is
// shown — never a live price, never a forecast to trade on. A
// retracted or expired prediction is not shown as a price: the
// producer publishes a revision-2 null row when the target hour
// passes, and readingAsOf then reports "unavailable".
//
// The classic crypto-panel.js script talks to this module through
// window.CryptoPredictions when it exists; the panel works without
// it (snapshot table only). Node-safe: no top-level window/document
// access, so tests/crypto_predictions_panel.test.mjs can import it
// directly.
import { observation, selectAsOf, readingAsOf } from '../shared/one-clock.js';

export const PREDICTIONS_BASE_PATH = 'crypto/predictions/';
export const SESSION = 'continuous';
export const SOURCE = 'crypto_predictions';
export const ORIGIN = 'synthetic';
// Hourly cadence, same staleness bound as the snapshot panel.
export const MAX_AGE_MS = 2 * 60 * 60 * 1000;

export function predictionPath(coin) {
  return `${PREDICTIONS_BASE_PATH}${coin}.json`;
}

// Validate a prediction file's rows: they must already be One
// Clock observations from the predictions adapter — synthetic
// origin, continuous session, this coin. Anything else is a
// malformed file, not a price to display.
export function toObservations(file, coin) {
  const rows = Array.isArray(file?.observations) ? file.observations : [];
  const observations = [];
  for (const row of rows) {
    const checked = observation(row);
    if (checked.source !== SOURCE) {
      throw new Error(`prediction row has unexpected source: ${checked.source}`);
    }
    if (checked.session !== SESSION || checked.origin !== ORIGIN) {
      throw new Error('prediction row is not a synthetic continuous observation');
    }
    if (checked.instrument !== coin) {
      throw new Error(`prediction row is for ${checked.instrument}, not ${coin}`);
    }
    observations.push(checked);
  }
  return { observations };
}

// R2-first as-of read of one prediction file. fetchFn defaults to
// the global fetch against BROWSER_DATA (the same public-data
// origin the snapshot uses); tests pass a fake. Returns
// { state, rows, reading, reason } where state is readingAsOf's
// fresh / stale / unavailable. A 404, a malformed file, or an
// expired/retracted prediction yields state 'unavailable' with a
// reason — the caller renders that, never an estimate as a price.
export async function readPredictionsAsOf({
  fetchFn, coin, cutoff, maxAgeMs = MAX_AGE_MS,
}) {
  const at = cutoff ?? Date.now();
  const base = typeof BROWSER_DATA !== 'undefined' ? BROWSER_DATA : '';
  const fetchImpl = fetchFn || fetch;
  const url = `${base}/${predictionPath(coin)}?_=${at}`;
  let file;
  try {
    const response = await fetchImpl(url, { cache: 'no-store' });
    if (response.status === 404) {
      return { state: 'unavailable', rows: [], reading: null,
        reason: 'no predictions published yet' };
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    file = await response.json();
  } catch (e) {
    return { state: 'unavailable', rows: [], reading: null,
      reason: e.message || 'fetch failed' };
  }
  if (!file || typeof file !== 'object' || !Array.isArray(file.observations)) {
    return { state: 'unavailable', rows: [], reading: null,
      reason: 'prediction file malformed' };
  }
  let observations;
  try {
    ({ observations } = toObservations(file, coin));
  } catch (e) {
    return { state: 'unavailable', rows: [], reading: null,
      reason: e.message || 'prediction file malformed' };
  }
  const selection = { session: SESSION, instrument: coin, cutoff: at, origin: ORIGIN };
  const rows = selectAsOf(observations, selection);
  const reading = readingAsOf(rows, selection, { maxAgeMs });
  const reason = reading.state === 'unavailable'
    ? (reading.row && reading.row.value === null
      ? 'estimate retracted — its target hour has passed'
      : 'no live estimate for this hour')
    : null;
  return { state: reading.state, rows, reading, reason };
}

export const CryptoPredictions = {
  predictionPath, toObservations, readPredictionsAsOf,
  PREDICTIONS_BASE_PATH, SESSION, SOURCE, ORIGIN, MAX_AGE_MS,
};

if (typeof window !== 'undefined') {
  window.CryptoPredictions = CryptoPredictions;
}
