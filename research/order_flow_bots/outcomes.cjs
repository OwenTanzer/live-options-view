'use strict';

const MINUTE = 60_000;
const MAX_SPOT_AGE = 2 * MINUTE;

function milliseconds(value) {
  if (Number.isFinite(value)) return value;
  if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/i.test(value)) return null;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}

// The replay builder writes a receipt at the END of a measured outage. The
// display model and live feed use epoch-millisecond start/end pairs instead.
function gaps(replay) {
  return (replay.coverage.gaps || []).map(entry => {
    if (!entry) return { start: null, end: null };
    if ('receipt' in entry || 'outage_seconds' in entry) {
      const end = milliseconds(entry.receipt);
      const duration = entry.outage_seconds;
      const start = end !== null && Number.isFinite(duration) && duration >= 0
        ? end - Math.round(duration * 1000) : null;
      return { start, end };
    }
    const start = milliseconds(entry.start), end = milliseconds(entry.end);
    if (start !== null && end !== null && end < start) return { start: null, end: null };
    return { start, end };
  });
}

function anchorOutcome(t, horizon, replay, snapshots, outages) {
  const due = t + horizon * MINUTE;
  const row = { session: replay.session, t, horizon, due, start: null, end: null,
    spotReturnBps: null, sampledRangeBps: null, downsideExcursionBps: null, missingReason: null };
  const missing = reason => ({ ...row, missingReason: reason });
  if (t < replay.coverage.open || due > replay.coverage.close) return missing('outside regular session');
  const start = snapshots.findIndex(s => s.t > t);
  const end = snapshots.findIndex(s => s.t >= due);
  if (start < 0 || end < 0) return missing('missing forward endpoint');
  if (start >= end) return missing('no distinct forward observations');
  const first = snapshots[start], last = snapshots[end];
  if (first.t - t > MAX_SPOT_AGE || last.t - due > MAX_SPOT_AGE) return missing('forward endpoint outside tolerance');
  if (outages.some(g => (g.start === null || g.start <= last.t) && (g.end === null || g.end >= t))) {
    return missing('outage intersects outcome window');
  }
  let low = first.spot, high = first.spot;
  for (let i = start; i <= end; i++) {
    const s = snapshots[i];
    if (!Number.isFinite(s.spot) || s.spot <= 0) return missing('invalid forward spot');
    if (i > start && s.t - snapshots[i - 1].t > MAX_SPOT_AGE) return missing('missing interior spot interval');
    low = Math.min(low, s.spot);
    high = Math.max(high, s.spot);
  }
  return { ...row, start: first.t, end: last.t, spotReturnBps: (last.spot / first.spot - 1) * 1e4,
    sampledRangeBps: (high - low) / first.spot * 1e4, downsideExcursionBps: (first.spot - low) / first.spot * 1e4 };
}

function anchors(rows, replay) {
  const snapshots = replay.snapshots.filter(s => s.t >= replay.coverage.open && s.t <= replay.coverage.close)
    .sort((a, b) => a.t - b.t);
  const outages = gaps(replay), byAnchor = new Map();
  for (const row of rows) {
    const key = row.t + '/' + row.horizon;
    if (!byAnchor.has(key)) byAnchor.set(key, anchorOutcome(row.t, row.horizon, replay, snapshots, outages));
  }
  return [...byAnchor.values()];
}

function labels(rows, replay, anchorRows = anchors(rows, replay)) {
  const keyed = new Map(anchorRows.map(a => [a.t + '/' + a.horizon, a]));
  return rows.filter(row => row.episode).map(row => {
    const a = keyed.get(row.t + '/' + row.horizon);
    const range = row.signal === 'range';
    let valueBps = null;
    if (a.missingReason === null) valueBps = range ? a.sampledRangeBps :
      a.spotReturnBps * (row.signal === 'long' ? 1 : -1);
    return { id: row.id, t: row.t, label: range ? 'sampled_range_bps' : 'signed_spot_return_bps',
      start: a.start, end: a.end, valueBps };
  });
}

module.exports = { gaps, anchors, labels };
