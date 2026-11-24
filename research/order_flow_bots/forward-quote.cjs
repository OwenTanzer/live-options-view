'use strict';

const url = 'https://options.moopertonic.net/api/live-quotes?symbols=QQQ';
const maxQuoteAgeMs = 15_000;

// The producer's bid_ts and ask_ts are receipt times of each side. Its
// aggregate quote_ts can also advance on a last trade, even with stale sides.
function parseQqqQuote(payload, receivedAt) {
  if (!Number.isSafeInteger(receivedAt) || payload?.health?.state !== 'live' ||
      !Array.isArray(payload.quotes) || payload.quotes.length !== 1) return null;
  const row = payload.quotes[0];
  if (row?.symbol !== 'QQQ' || row.instrument_class !== 'equity' ||
      row.kind !== 'ticker' || row.source !== 'dxlink') return null;
  const {bid, ask} = row;
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask < bid) return null;
  const sideTime = value => typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value)
    ? Date.parse(value) : NaN;
  const bidAt = sideTime(row.bid_ts);
  const askAt = sideTime(row.ask_ts);
  if (![bidAt, askAt].every(t => Number.isFinite(t) && t <= receivedAt && receivedAt-t <= maxQuoteAgeMs))
    return null;
  // A new order cannot fill until both displayed sides postdate its decision.
  return {symbol:'QQQ', t:Math.min(bidAt, askAt), spot:(bid+ask)/2, bid, ask, bidAt, askAt, receivedAt};
}

async function fetchQqqQuote({fetchImpl = fetch, now = Date.now} = {}) {
  const response = await fetchImpl(url, {signal:AbortSignal.timeout(5000), redirect:'error',
    headers:{Accept:'application/json', 'Cache-Control':'no-cache'}});
  if (!response.ok) throw new Error('QQQ quote endpoint returned HTTP '+response.status);
  const payload = await response.json();
  return parseQqqQuote(payload, now());
}

module.exports = {parseQqqQuote, fetchQqqQuote};
