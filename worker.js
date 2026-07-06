const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";

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
async function handlePaperTrade(request, env) {
  let body;
  try {
    body = await request.json();
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
  const key = `paper-trades/${day}/${now.toISOString().replace(/[:.]/g, '-')}_${installId}_${accountId}.json`;

  await env.PAPER_TRADES.put(key, JSON.stringify({ ...body, received_at: now.toISOString() }), {
    httpMetadata: { contentType: 'application/json' },
  });

  return new Response(null, { status: 204 });
}
