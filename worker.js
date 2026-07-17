const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";
const ALLOWED_ORIGINS = ['https://options.moopertonic.net', 'http://localhost:8787'];
const MAX_BODY_BYTES = 16 * 1024; // a trade record is a few hundred bytes; this leaves ample headroom
const MAX_QUOTE_AGE_MS = 2 * 60 * 1000;
const MAX_QUOTE_FUTURE_MS = 30 * 1000;

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

// Records one paper-trading fill per object under paper-trades/YYYYMMDD/ —
// avoids read-modify-write races when multiple browsers/accounts trade at once.
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

  const now = new Date();
  let snapshot;
  try {
    const quoteResponse = await fetch(`${R2_ORIGIN}/intraday/latest.json?_=${now.getTime()}`, {
      cf: { cacheTtl: 0, cacheEverything: false },
      headers: { 'Cache-Control': 'no-cache' },
    });
    if (!quoteResponse.ok) throw new Error(`quote source returned HTTP ${quoteResponse.status}`);
    snapshot = await quoteResponse.json();
  } catch (error) {
    return jsonResponse({ error: 'Fresh quote unavailable' }, 503);
  }

  const quoteResult = exactContractQuote(snapshot, body.sym, now.getTime());
  if (quoteResult.error) {
    return jsonResponse({ error: quoteResult.error }, quoteResult.status);
  }

  const executionId = crypto.randomUUID();
  const trade = Object.freeze({
    execution_id: executionId,
    ts: now.toISOString(),
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

  const day = now.toISOString().slice(0, 10).replace(/-/g, '');
  const installId = String(body.install_id || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '');
  const accountId = String(body.account_id || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '');
  const key = `paper-trades/${day}/${now.toISOString().replace(/[:.]/g, '-')}_${installId}_${accountId}_${executionId}.json`;

  try {
    await env.PAPER_TRADES.put(key, JSON.stringify(trade), {
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { executionId },
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

function exactContractQuote(snapshot, symbol, nowMs) {
  const quoteMs = Date.parse(snapshot?.timestamp);
  if (!Number.isFinite(quoteMs)) return { error: 'Quote has no valid timestamp', status: 503 };
  const age = nowMs - quoteMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: 'Quote is stale', status: 409 };
  }
  const row = Array.isArray(snapshot.rows)
    ? snapshot.rows.find(candidate => candidate.OptionSymbol === symbol)
    : null;
  if (!row) return { error: 'Exact contract quote not found', status: 409 };
  const bid = row.Bid;
  const ask = row.Ask;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask < 0 || bid > ask) {
    return { error: 'Exact contract quote is invalid', status: 409 };
  }
  const strike = Number(row.Strike);
  if (!Number.isFinite(strike) || strike <= 0 || !['call', 'put'].includes(row.Type) ||
      typeof row.Expiration !== 'string' || !ISO_DATE.test(row.Expiration)) {
    return { error: 'Exact contract metadata is invalid', status: 409 };
  }
  return { bid, ask, strike, type: row.Type, exp: row.Expiration, quoteTs: new Date(quoteMs).toISOString() };
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
