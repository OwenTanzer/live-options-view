// Adapter for research/market_readings_series.py's digest-checked output.
// The export, not an arbitrary browser assertion, is the trust boundary. This
// module does not replace digest verification or authorize archive access.
import { observation, instant } from './one-clock.js';

export function marketReadingObservation(row, { session, readingId }) {
  const archiveDate = /(?:^|\/)(\d{4})(\d{2})(\d{2})\/snapshot_[^/]+\.csv$/.exec(row.snapshot_key ?? '');
  if (!archiveDate || `${archiveDate[1]}-${archiveDate[2]}-${archiveDate[3]}` !== session) return null;
  if (row.reading_id !== readingId) return null;
  if (!['ok', 'legacy_no_readings'].includes(row.digest_status)) return null;
  const legacy = row.digest_status === 'legacy_no_readings';
  if (legacy && row.schema_version !== 'pre-market_reading.v1') return null;
  if (!legacy && row.schema_version !== 'market_reading.v1') return null;
  const eventAt = row.observed_at ?? row.snapshot_constructed_at;
  if (eventAt == null) return null;
  // Publication of the containing snapshot is required; a reading timestamp
  // alone cannot establish that an archived-but-unpublished snapshot was live.
  const published = row.publication?.status === 'published' && row.published_at != null;
  const availableAt = published ? Math.max(instant(row.published_at),
    row.reading_published_at == null ? 0 : instant(row.reading_published_at)) : null;
  return observation({
    source: `market-reading:${readingId}`, id: row.snapshot_key, revision: 1,
    session, instrument: row.symbol, origin: 'recorded', eventAt, availableAt,
    evidence: row.snapshot_key,
    value: row.status === 'ok' || legacy ? row.values : null,
    details: {
      schema: row.schema_version, calculationVersion: row.calculation_version,
      observationBasis: row.observed_at == null ? 'snapshot-construction' : 'source-observation',
      sourceObservationProven: row.observed_at != null,
      status: row.status, reason: row.status_reason, freshness: row.freshness,
      units: row.units, parameters: row.parameters, coverage: row.coverage,
      caveats: row.caveats, legacy,
    },
  });
}

export function marketReadingSource({ readingId, load, maxAgeMs }) {
  return {
    id: `market-reading:${readingId}`, version: '1', owner: 'market_data.overview', maxAgeMs,
    async read(context) {
      const rows = await load(context);
      return rows.map(row => marketReadingObservation(row, { session: context.session, readingId })).filter(Boolean);
    },
  };
}
