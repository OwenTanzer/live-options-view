const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";
const ALLOWED_ORIGINS = ['https://options.moopertonic.net', 'http://localhost:8787'];
const MAX_BODY_BYTES = 16 * 1024; // a trade record is a few hundred bytes; this leaves ample headroom

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
  const key = request.headers.get('X-Paper-Trade-Key');
  if (!env.PAPER_TRADE_KEY || key !== env.PAPER_TRADE_KEY) {
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
  if (!body || typeof body !== 'object' || !body.sym || !body.side || !body.qty || body.price == null) {
    return new Response('Missing required trade fields', { status: 400 });
  }

  const now = new Date();
  const day = now.toISOString().slice(0, 10).replace(/-/g, '');
  const installId = String(body.install_id || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '');
  const accountId = String(body.account_id || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '');
  const rand = crypto.randomUUID().slice(0, 8);
  const key = `paper-trades/${day}/${now.toISOString().replace(/[:.]/g, '-')}_${installId}_${accountId}_${rand}.json`;

  await env.PAPER_TRADES.put(key, JSON.stringify({ ...body, received_at: now.toISOString() }), {
    httpMetadata: { contentType: 'application/json' },
  });

  return new Response(null, { status: 204 });
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
