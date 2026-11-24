// Candle-cache One Clock adapter (ESM).
//
// The panel side of the candle-cache backend. Unlike the Python side
// (which mirrors the selector — see
// market_data/candle_cache_backend/selector.py), this module reuses
// the real One Clock core: it imports selectAsOf / readingAsOf /
// createClock from ../shared/one-clock.js, the same module the
// backend publication contract imports
// (backend/one-clock-publication-contract.js). Verified on master
// 2026-11-24: site/shared/one-clock.js exports instant, observation,
// observationKey, selectAsOf, readingAsOf, createSourceRegistry and
// createClock; the envelope fields are source, id, revision, session,
// instrument, origin (recorded / reconstructed / synthetic), evidence,
// eventAt, availableAt (null means unknown, never substituted),
// value (null is a retraction) and optional valueAt / details. The
// selector requires eventAt and availableAt at or before the cutoff,
// one origin cohort, and picks the highest eligible revision per
// identity; readingAsOf reports fresh / stale / unavailable with
// ageMs and publicationAgeMs.
//
// This module does NOT touch One Clock's QQQ/SPY/IBIT fields: it only
// ever produces source "candle-cache" rows. It is not wired into the
// NASDAQ-100 panel yet — site/js/nasdaq-panel.js arrives in PR #391,
// which is unmerged, and the panel itself is a classic script, so
// the wiring (a module script or dynamic import, after #391 merges)
// is a follow-up, not part of this PR. Until then this module is
// exercised by tests/candle_cache_one_clock.test.mjs.
//
// An adapter read fetches one interval file plus its provenance
// sidecar from the cache base (R2 public origin by default, or a
// local preview), converts the stored bars to observations (eventAt
// = the bar's own start; availableAt = the file's generated_at, or
// the provenance envelope's available_at when present; origin /
// revision from the provenance envelope, defaulting to recorded / 1),
// and lets the shared clock select as of its cutoff. A missing file
// is unavailable, never a substituted price; a gap inside coverage
// is labelled "reaction unmeasurable · price gap" and a file that
// does not span the requested window is "coverage partial".
import { selectAsOf, readingAsOf } from '../shared/one-clock.js';

export const CANDLE_CACHE_SOURCE = 'candle-cache';
export const CANDLE_CACHE_VERSION = '1';
export const CANDLE_CACHE_OWNER = 'candle-cache-backend';

export const GAP_LABEL = 'reaction unmeasurable · price gap';
export const PARTIAL_LABEL = 'coverage partial';

const ORIGINS = new Set(['recorded', 'reconstructed', 'synthetic']);

/** Parse an ISO-8601 UTC bar time to epoch milliseconds. */
export function barTimeMs(t) {
  const ms = Date.parse(t);
  if (!Number.isSafeInteger(ms)) throw new TypeError(`invalid bar time: ${t}`);
  return ms;
}

/** The NYSE session date (YYYY-MM-DD) of a bar's event time. */
export function sessionForMs(ms) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(ms));
}

/**
 * Convert stored bars to One Clock observations.
 * `file` is a parsed interval file (PR #393 shape); `envelope` is the
 * per-file entry from the provenance sidecar's envelope.files, if any.
 * availableAt is the file's generated_at (or the envelope's
 * available_at); it is never filled from the bar time or the fetch
 * time. Rows with no proven availability carry availableAt: null and
 * are ineligible at every cutoff.
 */
export function barsToObservations(file, envelope = {}) {
  const origin = envelope.origin ?? 'recorded';
  if (!ORIGINS.has(origin)) throw new TypeError(`unknown origin: ${origin}`);
  const revision = envelope.revision ?? 1;
  const availableAt = envelope.available_at ?? file.generated_at ?? null;
  const availableMs = availableAt === null ? null : barTimeMs(availableAt);
  const evidence = `nasdaq/candles/${file.symbol}/${file.interval}.json`;
  return file.bars.map((bar) => {
    const eventAt = barTimeMs(bar.t);
    const value = { open: bar.open, high: bar.high, low: bar.low, close: bar.close };
    for (const key of ['volume', 'adj_close', 'adj_volume', 'bars']) {
      if (key in bar) value[key] = bar[key];
    }
    return {
      source: CANDLE_CACHE_SOURCE,
      id: `${file.symbol}:${file.interval}:${bar.t}`,
      revision,
      session: sessionForMs(eventAt),
      instrument: file.symbol,
      origin,
      evidence,
      eventAt,
      availableAt: availableMs,
      value,
    };
  });
}

/**
 * Select cached bars as of a cutoff, using the real One Clock core.
 * Returns { rows, reading, gaps, labels }: rows are the selected
 * observations in event order, reading is readingAsOf's
 * { state, row, ageMs, publicationAgeMs }, and gaps/labels are the
 * honest hole labels (see gapReport).
 */
export function selectCandlesAsOf(observations, selection, { maxAgeMs } = {}) {
  if (!Number.isSafeInteger(maxAgeMs) || maxAgeMs < 0) {
    throw new TypeError('maxAgeMs must be an explicit non-negative integer');
  }
  const rows = selectAsOf(observations, selection);
  const reading = readingAsOf(rows, selection, { maxAgeMs });
  const gaps = gapReport(rows, selection.session);
  const labels = [...new Set(gaps.map((gap) => gap.label))].sort();
  if (reading.state === 'unavailable' && !labels.includes('unavailable')) labels.push('unavailable');
  return { rows, reading, gaps, labels: labels.sort() };
}

const INTERVAL_MINUTES = { '1m': 1, '3m': 3, '5m': 5, '9m': 9, '15m': 15, '30m': 30, '1h': 60, '2h': 120, '4h': 240 };

/**
 * Label the holes in a selected observation set. A missing expected
 * bar inside coverage is "reaction unmeasurable · price gap"; a set
 * whose first/last bars do not span the regular session is
 * "coverage partial". Daily/weekly rows get no intraday gap
 * inference (a missing trading day is not inferred without a
 * calendar). An empty selection is "unavailable".
 */
export function gapReport(rows, session) {
  if (!rows.length) return [{ label: 'unavailable', from: null, to: null, bars: 0 }];
  const ordered = [...rows].sort((a, b) => a.eventAt - b.eventAt);
  const report = [];
  const interval = ordered[0].id.split(':')[1];
  const step = INTERVAL_MINUTES[interval];
  const fmt = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
  if (step !== undefined) {
    let expected = ordered[0].eventAt;
    for (const row of ordered) {
      while (expected < row.eventAt) {
        const next = expected + step * 60_000;
        report.push({ label: GAP_LABEL, from: fmt(expected), to: fmt(Math.min(next, row.eventAt)), bars: 1 });
        expected = next;
      }
      expected = Math.max(expected, row.eventAt + step * 60_000);
    }
    const dayParts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short', hour: 'numeric', minute: '2-digit', hour12: false,
    }).formatToParts(new Date(ordered[0].eventAt));
    const firstMinutes = Number(dayParts.find((p) => p.type === 'hour').value) * 60
      + Number(dayParts.find((p) => p.type === 'minute').value);
    const lastParts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short', hour: 'numeric', minute: '2-digit', hour12: false,
    }).formatToParts(new Date(ordered.at(-1).eventAt));
    const lastMinutes = Number(lastParts.find((p) => p.type === 'hour').value) * 60
      + Number(lastParts.find((p) => p.type === 'minute').value);
    const weekday = !['Sat', 'Sun'].includes(dayParts.find((p) => p.type === 'weekday').value);
    if (weekday && (firstMinutes > 9 * 60 + 30 || lastMinutes < 16 * 60 - step)) {
      report.push({ label: PARTIAL_LABEL, from: fmt(ordered[0].eventAt), to: fmt(ordered.at(-1).eventAt), bars: ordered.length });
    }
  }
  return report;
}

/**
 * Build a One Clock source adapter for one cached interval.
 * `fetchJson` is an injected JSON fetcher (fetch in the browser, a
 * fixture reader in tests); `baseUrl` is the cache base, e.g. the
 * public R2 origin. The adapter's read() receives the clock's
 * { session, instrument, mode, origin, cutoff, generation, signal }
 * and returns observation rows; the shared clock applies selectAsOf.
 * A missing file returns [] (unavailable), never a fallback price.
 */
export function createCandleCacheAdapter({ fetchJson, baseUrl, interval, maxAgeMs = 15 * 60 * 1000 }) {
  if (typeof fetchJson !== 'function') throw new TypeError('fetchJson is required');
  if (!baseUrl) throw new TypeError('baseUrl is required');
  if (!interval) throw new TypeError('interval is required');
  return Object.freeze({
    id: CANDLE_CACHE_SOURCE,
    version: CANDLE_CACHE_VERSION,
    owner: CANDLE_CACHE_OWNER,
    maxAgeMs,
    async read({ instrument, signal }) {
      const prefix = `${String(baseUrl).replace(/\/$/, '')}/nasdaq/candles/${instrument}`;
      const file = await fetchJson(`${prefix}/${interval}.json`, { signal }).catch(() => null);
      if (!file) return [];
      const provenance = await fetchJson(`${prefix}/provenance.json`, { signal }).catch(() => null);
      const envelope = provenance?.envelope?.files?.[interval] ?? {};
      return barsToObservations(file, envelope);
    },
  });
}
