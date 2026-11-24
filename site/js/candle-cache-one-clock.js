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

const sessionDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
});
export function sessionForMs(ms) { return sessionDate.format(new Date(ms)); }

export function barsToObservations(file, envelope = {}) {
  if (!envelope.origin || !envelope.available_at) return [];
  const origin = envelope.origin;
  if (!ORIGINS.has(origin)) throw new TypeError(`unknown origin: ${origin}`);
  const revision = envelope.revision ?? 1;
  const availableAt = envelope.available_at;
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
      session: ['1d', '1w'].includes(file.interval) ? bar.t.slice(0, 10) : sessionForMs(eventAt),
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
export function selectCandlesAsOf(observations, selection, { maxAgeMs, sessionHours } = {}) {
  if (!Number.isSafeInteger(maxAgeMs) || maxAgeMs < 0) {
    throw new TypeError('maxAgeMs must be an explicit non-negative integer');
  }
  const rows = selectAsOf(observations.filter(row => row.session === selection.session), selection);
  const reading = readingAsOf(rows, selection, { maxAgeMs });
  const gaps = gapReport(rows.filter(row => row.value !== null), selection.session, sessionHours);
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
export function gapReport(rows, session, sessionHours) {
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
    if (sessionHours && (ordered[0].eventAt > sessionHours[0] ||
        ordered.at(-1).eventAt + step * 60000 < sessionHours[1])) {
      report.push({ label: PARTIAL_LABEL, from: fmt(ordered[0].eventAt), to: fmt(ordered.at(-1).eventAt), bars: ordered.length });
    }
  }
  return report;
}

/** Read only the acknowledged interval bytes named in the publication receipt. */
export async function readPublishedCandles(bytes, receipt) {
  if (receipt?.schema !== 'snapshot_provenance.v1' ||
      receipt.dataset !== 'single_stock_candles' || receipt.publication?.status !== 'published') return [];
  const file = JSON.parse(new TextDecoder().decode(bytes));
  const entry = receipt.files?.[file.interval];
  if (file.dataset !== receipt.dataset || file.symbol !== receipt.symbol ||
      !entry?.available_at || entry.key !== `nasdaq/candles/${file.symbol}/${file.interval}.json`) return [];
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hex = Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('');
  if (hex !== entry.sha256) return [];
  return barsToObservations(file, {origin: 'recorded', available_at: entry.available_at});
}
