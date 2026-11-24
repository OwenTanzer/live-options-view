'use strict';

const url = 'https://options.moopertonic.net/api/live-quotes?symbols=QQQ';
const maxQuoteAgeMs = 15_000;

// Preserve the collector's fractional second without adding a floating-point
// fraction to an epoch timestamp. At this magnitude nanoseconds can round away.
function timeBounds(value) {
  if(typeof value!=='string') return null;
  const match=/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  if(!match) return null;
  const lower=Date.parse(value);
  if(!Number.isSafeInteger(lower)) return null;
  return {lower,upper:lower+(/[1-9]/.test((match[1]??'').slice(3))?1:0)};
}

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
  const bidClock=timeBounds(row.bid_ts),askClock=timeBounds(row.ask_ts);
  if(![bidClock,askClock].every(clock=>clock && clock.upper<=receivedAt &&
       receivedAt-clock.lower<=maxQuoteAgeMs)) return null;
  return {symbol:'QQQ',t:Math.min(bidClock.lower,askClock.lower),spot:(bid+ask)/2,bid,ask,
    bidAt:bidClock.lower,askAt:askClock.lower,bidThrough:bidClock.upper,askThrough:askClock.upper,receivedAt};
}

async function fetchQqqQuote({fetchImpl = fetch, now = Date.now} = {}) {
  const response = await fetchImpl(url, {signal:AbortSignal.timeout(5000), redirect:'error',
    headers:{Accept:'application/json', 'Cache-Control':'no-cache'}});
  if (!response.ok) throw new Error('QQQ quote endpoint returned HTTP '+response.status);
  const payload = await response.json();
  return parseQqqQuote(payload, now());
}

module.exports = {parseQqqQuote, fetchQqqQuote, timeBounds};
