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
    if (existing) return jsonResponse(await existing.json(), 200);
  } catch (error) {
    return jsonResponse({ error: 'Execution state unavailable' }, 503);
  }

  let quotePayload;
  try {
    const quoteResponse = await fetchLiveQuotes([body.sym], env);
    if (!quoteResponse.ok) throw new Error(`quote source returned HTTP ${quoteResponse.status}`);
    quotePayload = await quoteResponse.json();
  } catch (error) {
    return jsonResponse({ error: 'Fresh quote unavailable' }, 503);
  }
  const quoteReceivedAt = new Date();

  const quoteResult = exactContractQuote(quotePayload, body.sym, quoteReceivedAt.getTime());
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
    await env.PAPER_TRADES.put(key, JSON.stringify(trade), {
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { executionId, executedAt: executedAt.toISOString() },
    });
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

function exactContractQuote(payload, symbol, nowMs) {
  const row = Array.isArray(payload?.quotes)
    ? payload.quotes.find(candidate => candidate.symbol === symbol)
    : null;
  if (!row) return { error: 'Exact contract quote not found', status: 409 };
  const quoteMs = Date.parse(row.quote_ts);
  if (!Number.isFinite(quoteMs)) return { error: 'Quote has no valid timestamp', status: 503 };
  const age = nowMs - quoteMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: 'Quote is stale', status: 409 };
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
  return { bid, ask, strike, type: row.type, exp: row.exp, quoteTs: new Date(quoteMs).toISOString() };
}

function jsonResponse(value, status) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
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
