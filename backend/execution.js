// backend/execution.js (B4) -- execution: trade-intent validation, the R2
// reservation-lease machinery (reserve/renew/finalize + expiry), and the
// exact-quote pricers (share + contract). Moved verbatim from worker.js;
// the entry re-exports the public names. handlePaperTrade itself lives in
// backend/paper-trade.js and imports these; quoteProviderErrorResponse and
// exactExecutionQuote/exactContractQuote are exported for it, not public.

import { ISO_DATE, jsonResponse } from './common.js';

const MAX_QUOTE_AGE_MS = 15 * 1000;
const MAX_QUOTE_FUTURE_MS = 30 * 1000;
const TRADEABLE_SHARE_SYMBOLS = new Set([
  'QQQ', 'SPY', 'USO', 'SMH', 'IGV', 'META', 'GOOGL', 'AMZN', 'TSLA', 'MU', 'SPCX', 'AAPL',
]);
const MAX_STRING_LEN = 64;
export const EXECUTION_RESERVATION_LEASE_MS = 30 * 1000;
// Schema check for a trade record before it's written to R2. This log is
// meant to become cross-strategy analysis data, so loose validation here
// (accepting non-numeric prices, negative quantities, arbitrary side/type
// strings, unbounded text fields) is how the dataset silently rots — a
// malformed record parses fine as JSON but breaks downstream analysis with
// no visible failure in the UI. Returns an error string, or null if valid.
export function validateTradeIntent(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';

  if (typeof body.execution_request_id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.execution_request_id)) {
    return 'execution_request_id must be a UUID';
  }
  if (typeof body.sym !== 'string' || !body.sym.trim() || body.sym.length > MAX_STRING_LEN) {
    return 'sym must be a non-empty string';
  }
  const instrumentType = body.instrument_type ?? 'option';
  if (instrumentType !== 'option' && instrumentType !== 'share') {
    return 'instrument_type must be "option" or "share"';
  }
  if (instrumentType === 'share' && !TRADEABLE_SHARE_SYMBOLS.has(body.sym)) {
    return 'share symbol is not in the tradeable equity list';
  }
  if (body.side !== 'buy' && body.side !== 'sell') {
    return 'side must be "buy" or "sell"';
  }
  if (!Number.isInteger(body.qty) || body.qty <= 0 || body.qty > 100_000) {
    return 'qty must be a positive integer';
  }

  const orderType = body.order_type ?? 'market';
  if (orderType !== 'market' && orderType !== 'limit') {
    return 'order_type must be "market" or "limit"';
  }
  if (orderType === 'market' && body.limit_price !== undefined) {
    return 'limit_price is only allowed for limit orders';
  }
  if (orderType === 'limit') {
    if (typeof body.limit_price !== 'number' || !Number.isFinite(body.limit_price) ||
        body.limit_price <= 0 || body.limit_price > 1_000_000) {
      return 'limit_price must be a positive finite number';
    }
    if (Math.abs(body.limit_price * 100 - Math.round(body.limit_price * 100)) > 1e-7) {
      return 'limit_price must use increments of 0.01';
    }
  }

  return null;
}
export function executionIntentMatches(trade, intent) {
  return trade?.execution_request_id === intent.execution_request_id &&
    trade?.sym === intent.sym &&
    (trade?.instrument_type ?? 'option') === (intent.instrument_type ?? 'option') &&
    trade?.side === intent.side &&
    trade?.qty === intent.qty &&
    (trade?.order_type ?? 'market') === (intent.order_type ?? 'market') &&
    (trade?.limit_price ?? null) === (intent.limit_price ?? null);
}
export function executionPriceForOrder(quote, intent) {
  const price = intent.side === 'buy' ? quote.ask : quote.bid;
  if ((intent.order_type ?? 'market') !== 'limit') return { price };

  if (intent.side === 'buy' && price > intent.limit_price) {
    return { error: `Buy limit $${intent.limit_price.toFixed(2)} is below current ask $${price.toFixed(2)}` };
  }
  if (intent.side === 'sell' && price < intent.limit_price) {
    return { error: `Sell limit $${intent.limit_price.toFixed(2)} is above current bid $${price.toFixed(2)}` };
  }
  return { price };
}
export function existingExecutionResponse(outcome, intent) {
  if (!executionIntentMatches(outcome, intent)) {
    return jsonResponse({ error: 'execution_request_id conflicts with a different trade intent' }, 409);
  }
  return jsonResponse(outcome, outcome.status === 'rejected' ? (outcome.http_status ?? 409) : 200);
}
export async function reserveExecutionRequest(bucket, key, reservation) {
  const stored = await bucket.put(key, JSON.stringify(reservation), {
    onlyIf: { etagDoesNotMatch: '*' },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: { executionId: reservation.execution_request_id, reservedAt: reservation.ts },
  });
  if (stored) return { outcome: reservation, created: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution reservation missing after conditional write');
  return { outcome: await existing.json(), created: false, etag: existing.etag };
}
export function executionReservationExpired(
  reservation,
  nowMs = Date.now(),
  leaseMs = EXECUTION_RESERVATION_LEASE_MS,
) {
  if (reservation?.status !== 'pending') return false;
  const reservedAt = Date.parse(reservation.ts);
  return !Number.isFinite(reservedAt) || nowMs - reservedAt >= leaseMs;
}
export async function renewExecutionReservation(bucket, key, reservation, reservationEtag, renewedAt) {
  const renewed = Object.freeze({ ...reservation, status: 'pending', ts: renewedAt });
  const stored = await bucket.put(key, JSON.stringify(renewed), {
    onlyIf: { etagMatches: reservationEtag },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: {
      executionId: renewed.execution_request_id,
      reservedAt: renewed.ts,
    },
  });
  if (stored) return { outcome: renewed, updated: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution reservation missing after conditional renewal');
  return { outcome: await existing.json(), updated: false, etag: existing.etag };
}
export async function finalizeExecutionRequest(bucket, key, terminalOutcome, reservationEtag) {
  const stored = await bucket.put(key, JSON.stringify(terminalOutcome), {
    onlyIf: { etagMatches: reservationEtag },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: {
      executionId: terminalOutcome.execution_request_id,
      executedAt: terminalOutcome.ts,
    },
  });
  if (stored) return { outcome: terminalOutcome, updated: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution outcome missing after conditional write');
  return { outcome: await existing.json(), updated: false, etag: existing.etag };
}
export async function quoteProviderErrorResponse(response) {
  const status = response.status >= 400 && response.status <= 599 ? response.status : 503;
  let upstream = {};
  try {
    upstream = await response.json();
  } catch (error) {
    // Keep the public error stable even if the provider returns non-JSON.
  }
  const headers = {};
  const retryAfter = response.headers.get('Retry-After');
  if (retryAfter) headers['Retry-After'] = retryAfter;
  return jsonResponse(
    { error: upstream.error || 'Fresh quote unavailable', provider_status: status },
    status,
    headers,
  );
}
export function exactExecutionQuote(payload, intent, nowMs) {
  if ((intent.instrument_type ?? 'option') === 'share') {
    return exactShareQuote(payload, intent.sym, intent.side, nowMs);
  }
  return exactContractQuote(payload, intent.sym, intent.side, nowMs);
}
export function exactShareQuote(payload, symbol, side, nowMs) {
  const row = Array.isArray(payload?.quotes)
    ? payload.quotes.find(candidate => candidate.symbol === symbol)
    : null;
  if (!row) return { error: 'Exact share quote not found', status: 409 };
  if (row.instrument_class !== 'equity') {
    return { error: 'Symbol is not a tradeable equity', status: 409 };
  }
  const bidMs = Date.parse(row.bid_ts);
  const askMs = Date.parse(row.ask_ts);
  const sideMs = side === 'buy' ? askMs : bidMs;
  if (!Number.isFinite(sideMs)) return { error: `Share quote has no valid ${side} timestamp`, status: 503 };
  const age = nowMs - sideMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: 'Share quote is stale', status: 409 };
  }
  const bid = row.bid;
  const ask = row.ask;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask <= 0 || bid > ask) {
    return { error: 'Exact share quote is invalid', status: 409 };
  }
  return {
    bid, ask, strike: null, type: null, exp: null,
    multiplier: 1,
    bidTs: Number.isFinite(bidMs) ? new Date(bidMs).toISOString() : null,
    askTs: Number.isFinite(askMs) ? new Date(askMs).toISOString() : null,
    quoteTs: new Date(sideMs).toISOString(),
  };
}
function exactContractQuote(payload, symbol, side, nowMs) {
  const identity = /^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(symbol);
  if (!identity) return { error: 'Invalid OCC contract identity', status: 409 };
  const row = Array.isArray(payload?.quotes)
    ? payload.quotes.find(candidate => candidate.symbol === symbol)
    : null;
  if (!row) return { error: 'Exact contract quote not found', status: 409 };
  const bidMs = Date.parse(row.bid_ts);
  const askMs = Date.parse(row.ask_ts);
  const sideMs = side === 'buy' ? askMs : bidMs;
  if (!Number.isFinite(sideMs)) return { error: `Quote has no valid ${side} timestamp`, status: 503 };
  const age = nowMs - sideMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: `${side === 'buy' ? 'Ask' : 'Bid'} quote is stale`, status: 409 };
  }
  const bid = row.bid;
  const ask = row.ask;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask < 0 || bid > ask) {
    return { error: 'Exact contract quote is invalid', status: 409 };
  }
  const strike = Number(row.strike);
  if (strike !== Number(identity[6]) / 1000 || row.type !== (identity[5] === 'C' ? 'call' : 'put') ||
      row.exp !== `20${identity[2]}-${identity[3]}-${identity[4]}`) {
    return { error: 'Quote metadata does not match OCC contract', status: 409 };
  }
  if (!Number.isFinite(strike) || strike <= 0 || !['call', 'put'].includes(row.type) ||
      typeof row.exp !== 'string' || !ISO_DATE.test(row.exp)) {
    return { error: 'Exact contract metadata is invalid', status: 409 };
  }
  return {
    bid, ask, strike, type: row.type, exp: row.exp,
    multiplier: 100,
    bidTs: Number.isFinite(bidMs) ? new Date(bidMs).toISOString() : null,
    askTs: Number.isFinite(askMs) ? new Date(askMs).toISOString() : null,
    quoteTs: new Date(sideMs).toISOString(),
  };
}
