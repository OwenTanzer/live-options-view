const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";
const ALLOWED_ORIGINS = ['https://options.moopertonic.net', 'http://localhost:8787'];
const MAX_BODY_BYTES = 16 * 1024; // a trade record is a few hundred bytes; this leaves ample headroom
const MAX_QUOTE_AGE_MS = 15 * 1000;
const MAX_QUOTE_FUTURE_MS = 30 * 1000;
const MAX_LIVE_QUOTE_SYMBOLS = 100;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Same-origin passthrough to the R2 bucket, fetched edge-to-edge by
    // Cloudflare rather than over the client's own network. Exists so
    // diag.html can compare "direct to the r2.dev subdomain" against "same
    // origin as the page" — if a client's network/DNS/carrier treats the two
    // hostnames differently, this tells them apart instead of leaving it a
    // guess. Not used by the main app yet.
    if (url.pathname.startsWith("/r2-proxy/")) {
      const key = url.pathname.slice("/r2-proxy/".length);
      const upstream = await fetch(`${R2_ORIGIN}/${key}${url.search}`, { cf: { cacheTtl: 0 } });
      const proxied = new Response(upstream.body, upstream);
      proxied.headers.set("Cache-Control", "no-store");
      proxied.headers.set("Access-Control-Allow-Origin", "*");
      return proxied;
    }

    if (request.method === 'POST' && url.pathname === '/api/paper-trade') {
      return handlePaperTrade(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/live-quotes') {
      return handleLiveQuotes(url, env);
    }

    const response = await env.ASSETS.fetch(request);

    // HTML documents (the single-file app + diagnostic page) must never be
    // cached — a stale cached copy silently keeps serving old JS against a
    // live backend indefinitely, with no user-visible signal that anything
    // is wrong. Everything else (none currently — this is a single-file
    // static site) can use normal caching.
    if (url.pathname === "/" || url.pathname.endsWith(".html")) {
      const noCache = new Response(response.body, response);
      noCache.headers.set("Cache-Control", "no-store");
      return noCache;
    }

    return response;
  }
}

async function handleLiveQuotes(url, env) {
  const symbols = parseRequestedSymbols(url.searchParams.get('symbols'));
  if (!symbols) {
    return jsonResponse({ error: `Request 1-${MAX_LIVE_QUOTE_SYMBOLS} valid symbols` }, 400);
  }
  try {
    const upstream = await fetchLiveQuotes(symbols, env);
    const response = new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'no-store',
      },
    });
    const retryAfter = upstream.headers.get('Retry-After');
    if (retryAfter) response.headers.set('Retry-After', retryAfter);
    return response;
  } catch (error) {
    return jsonResponse({ error: 'Live quote service unavailable' }, 503);
  }
}

function fetchLiveQuotes(symbols, env) {
  if (!env.LIVE_QUOTE_ORIGIN || !env.LIVE_QUOTE_KEY) {
    throw new Error('Live quote service is not configured');
  }
  const upstreamUrl = new URL('/live-quotes', env.LIVE_QUOTE_ORIGIN);
  upstreamUrl.searchParams.set('symbols', symbols.join(','));
  return fetch(upstreamUrl, {
    headers: { 'X-Live-Quote-Key': env.LIVE_QUOTE_KEY },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
}

function parseRequestedSymbols(value) {
  if (!value) return null;
  const symbols = [...new Set(value.split(',').map(symbol => symbol.trim().replace(/\s/g, '')).filter(Boolean))];
  if (!symbols.length || symbols.length > MAX_LIVE_QUOTE_SYMBOLS) return null;
  if (symbols.some(symbol => symbol.length > 64 || !/^[A-Za-z0-9._/:+-]+$/.test(symbol))) return null;
  return symbols;
}

// Records one paper-trading fill per client-generated request id under
// paper-trades/requests/. The deterministic key makes retries idempotent and
// avoids read-modify-write races between unrelated browsers/accounts.
//
// This is a write-capable public endpoint, so it's guarded by a shared token,
// an Origin allowlist, a Cloudflare rate limiting rule (configured at the
// zone level, not in this file), and a hard body-size cap. None of these are
// real auth — the token lives in public client JS (docs/index.html) and the
// Origin header can be forged by any non-browser client — they just raise the
// bar past casual/scripted abuse. Fine for a private tool with no sensitive
// data at stake; would need real auth if that ever changes.
async function handlePaperTrade(request, env) {
  const authKey = request.headers.get('X-Paper-Trade-Key');
  if (!env.PAPER_TRADE_KEY || authKey !== env.PAPER_TRADE_KEY) {
    return new Response('Forbidden', { status: 403 });
  }

  const origin = request.headers.get('Origin');
  if (origin && !ALLOWED_ORIGINS.includes(origin)) {
    return new Response('Forbidden', { status: 403 });
  }

  const contentLength = Number(request.headers.get('Content-Length') || 0);
  if (contentLength > MAX_BODY_BYTES) {
    return new Response('Payload too large', { status: 413 });
  }

  let raw;
  try {
    raw = await readLimited(request, MAX_BODY_BYTES);
  } catch (e) {
    return new Response('Payload too large', { status: 413 });
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    return new Response('Invalid JSON', { status: 400 });
  }
  const validationError = validateTradeIntent(body);
  if (validationError) {
    return new Response(validationError, { status: 400 });
  }

  const executionId = body.execution_request_id;
  const key = `paper-trades/requests/${executionId}.json`;
  try {
    const existing = await env.PAPER_TRADES.get(key);
    if (existing) return existingExecutionResponse(await existing.json(), body);
  } catch (error) {
    return jsonResponse({ error: 'Execution state unavailable' }, 503);
  }

  let quotePayload;
  try {
    const quoteResponse = await fetchLiveQuotes([body.sym], env);
    if (!quoteResponse.ok) return quoteProviderErrorResponse(quoteResponse);
    quotePayload = await quoteResponse.json();
  } catch (error) {
    return jsonResponse({ error: 'Fresh quote unavailable' }, 503);
  }
  const quoteReceivedAt = new Date();

  const quoteResult = exactContractQuote(quotePayload, body.sym, body.side, quoteReceivedAt.getTime());
  if (quoteResult.error) {
    return jsonResponse({ error: quoteResult.error }, quoteResult.status);
  }

  const executedAt = new Date();
  const trade = Object.freeze({
    execution_id: executionId,
    execution_request_id: executionId,
    ts: executedAt.toISOString(),
    quote_received_ts: quoteReceivedAt.toISOString(),
    quote_ts: quoteResult.quoteTs,
    bid_ts: quoteResult.bidTs,
    ask_ts: quoteResult.askTs,
    sym: body.sym,
    strike: quoteResult.strike,
    type: quoteResult.type,
    exp: quoteResult.exp,
    side: body.side,
    qty: body.qty,
    bid: quoteResult.bid,
    ask: quoteResult.ask,
    price: body.side === 'buy' ? quoteResult.ask : quoteResult.bid,
    account_id: body.account_id,
    account_name: body.account_name,
    install_id: body.install_id,
  });

  try {
    const stored = await env.PAPER_TRADES.put(key, JSON.stringify(trade), {
      onlyIf: { etagDoesNotMatch: '*' },
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { executionId, executedAt: executedAt.toISOString() },
    });
    if (!stored) {
      // Another request with the same client-generated identity won the
      // conditional create. R2 is strongly consistent, so return that exact
      // canonical fill rather than this request's independently fetched quote.
      const existing = await env.PAPER_TRADES.get(key);
      if (!existing) throw new Error('Canonical execution missing after conditional write');
      return existingExecutionResponse(await existing.json(), body);
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution could not be recorded' }, 503);
  }

  return jsonResponse(trade, 201);
}

const MAX_STRING_LEN = 64;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// Schema check for a trade record before it's written to R2. This log is
// meant to become cross-strategy analysis data, so loose validation here
// (accepting non-numeric prices, negative quantities, arbitrary side/type
// strings, unbounded text fields) is how the dataset silently rots — a
// malformed record parses fine as JSON but breaks downstream analysis with
// no visible failure in the UI. Returns an error string, or null if valid.
function validateTradeIntent(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';

  if (typeof body.execution_request_id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.execution_request_id)) {
    return 'execution_request_id must be a UUID';
  }
  if (typeof body.sym !== 'string' || !body.sym.trim() || body.sym.length > MAX_STRING_LEN) {
    return 'sym must be a non-empty string';
  }
  if (body.side !== 'buy' && body.side !== 'sell') {
    return 'side must be "buy" or "sell"';
  }
  if (!Number.isInteger(body.qty) || body.qty <= 0 || body.qty > 100_000) {
    return 'qty must be a positive integer';
  }
  for (const field of ['account_id', 'account_name', 'install_id']) {
    if (typeof body[field] !== 'string' || !body[field].trim() || body[field].length > MAX_STRING_LEN) {
      return `${field} must be a non-empty string under ${MAX_STRING_LEN} chars`;
    }
  }

  return null;
}

function executionIntentMatches(trade, intent) {
  return trade?.execution_request_id === intent.execution_request_id &&
    trade?.sym === intent.sym &&
    trade?.side === intent.side &&
    trade?.qty === intent.qty &&
    trade?.account_id === intent.account_id &&
    trade?.account_name === intent.account_name &&
    trade?.install_id === intent.install_id;
}

function existingExecutionResponse(trade, intent) {
  if (!executionIntentMatches(trade, intent)) {
    return jsonResponse({ error: 'execution_request_id conflicts with a different trade intent' }, 409);
  }
  return jsonResponse(trade, 200);
}

async function quoteProviderErrorResponse(response) {
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

function exactContractQuote(payload, symbol, side, nowMs) {
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
  if (!Number.isFinite(strike) || strike <= 0 || !['call', 'put'].includes(row.type) ||
      typeof row.exp !== 'string' || !ISO_DATE.test(row.exp)) {
    return { error: 'Exact contract metadata is invalid', status: 409 };
  }
  return {
    bid, ask, strike, type: row.type, exp: row.exp,
    bidTs: Number.isFinite(bidMs) ? new Date(bidMs).toISOString() : null,
    askTs: Number.isFinite(askMs) ? new Date(askMs).toISOString() : null,
    quoteTs: new Date(sideMs).toISOString(),
  };
}

function jsonResponse(value, status, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...extraHeaders },
  });
}

// Reads the request body up to `limit` bytes, aborting the stream and
// throwing if exceeded. Content-Length alone isn't trustworthy (absent on
// chunked requests, or simply lied about), so this is the real enforcement.
async function readLimited(request, limit) {
  if (!request.body) return '';
  const reader = request.body.getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > limit) {
      await reader.cancel();
      throw new Error('Payload too large');
    }
    chunks.push(value);
  }
  const buf = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    buf.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(buf);
}
