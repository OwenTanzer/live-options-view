const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";
const ALLOWED_ORIGINS = ['https://options.moopertonic.net', 'http://localhost:8787'];
const MAX_BODY_BYTES = 16 * 1024; // a trade record is a few hundred bytes; this leaves ample headroom
const MAX_QUOTE_AGE_MS = 15 * 1000;
const MAX_QUOTE_FUTURE_MS = 30 * 1000;
const MAX_LIVE_QUOTE_SYMBOLS = 100;

const SESSION_COOKIE = 'session';
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days
const PBKDF2_ITERATIONS = 100_000;
export const STARTING_BALANCE = 10_000;
export const USERNAME_RE = /^[A-Za-z0-9_]{3,20}$/;
export const STRATEGY_ID_RE = /^[a-z0-9_]{1,40}$/;
export const MIN_PASSWORD_LEN = 8;
export const MAX_PASSWORD_LEN = 256;
const MAX_KV_WRITE_ATTEMPTS = 5;
const SETTLEMENT_ID_PREFIX = 'settle';

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

    if (request.method === 'POST' && url.pathname === '/api/register') {
      return handleRegister(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/login') {
      return handleLogin(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/logout') {
      return handleLogout(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/bots') {
      return handleBots(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-metadata') {
      return handleBotMetadata(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/me') {
      return handleMe(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/paper-trade') {
      return handlePaperTrade(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/settle') {
      return handleSettle(request, env);
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

// ---------------------------------------------------------------------------
// Auth: username/password accounts backed by the USERS KV namespace, opaque
// session tokens backed by SESSIONS. Real auth (unlike the old shared-token
// deterrent below) because a user's balance now needs to follow them across
// devices rather than living in one browser's localStorage.
// ---------------------------------------------------------------------------

function userKey(username) {
  return `user:${username.toLowerCase()}`;
}

// Membership index for the public /api/bots roster. Bot accounts are marked
// here at registration by an operator holding BOT_REGISTRATION_KEY -- the
// roster is driven off this index rather than off a `crassus_` username
// prefix, because usernames are self-chosen: anyone could register
// `crassus_whatever` and publish their own balance. Nothing a client sends
// can put a record in this index.
function botKey(username) {
  return `bot:${username.toLowerCase()}`;
}

function sessionKey(token) {
  return `sess:${token}`;
}

function isSecureRequest(request) {
  return new URL(request.url).protocol === 'https:';
}

// `Secure` cookies never round-trip over plain http://, which the dev origin
// (http://localhost:8787) already in ALLOWED_ORIGINS relies on -- so the
// flag is conditional on the actual request scheme rather than always on.
function sessionCookieHeader(request, token, maxAgeSeconds) {
  const secure = isSecureRequest(request) ? '; Secure' : '';
  return `${SESSION_COOKIE}=${token}; HttpOnly${secure}; SameSite=Lax; Path=/; Max-Age=${maxAgeSeconds}`;
}

function clearedSessionCookieHeader(request) {
  return sessionCookieHeader(request, '', 0);
}

export function parseCookies(request) {
  const header = request.headers.get('Cookie');
  const cookies = {};
  if (!header) return cookies;
  for (const part of header.split(';')) {
    const idx = part.indexOf('=');
    if (idx === -1) continue;
    const name = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (name) cookies[name] = value;
  }
  return cookies;
}

export function randomSaltBase64() {
  return bytesToBase64(crypto.getRandomValues(new Uint8Array(16)));
}

export async function derivePasswordHash(password, saltBase64, iterations = PBKDF2_ITERATIONS) {
  const keyMaterial = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits'],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt: base64ToBytes(saltBase64), iterations },
    keyMaterial,
    256,
  );
  return bytesToBase64(new Uint8Array(bits));
}

function bytesToBase64(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// Workers' WebCrypto has no built-in timing-safe string compare. Both inputs
// here are fixed-length base64 of a 256-bit digest, so the length check
// leaks nothing secret; the byte comparison itself doesn't short-circuit.
export function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function readJsonBody(request) {
  const contentLength = Number(request.headers.get('Content-Length') || 0);
  if (contentLength > MAX_BODY_BYTES) {
    return { error: new Response('Payload too large', { status: 413 }) };
  }
  let raw;
  try {
    raw = await readLimited(request, MAX_BODY_BYTES);
  } catch (e) {
    return { error: new Response('Payload too large', { status: 413 }) };
  }
  try {
    return { body: JSON.parse(raw) };
  } catch (e) {
    return { error: new Response('Invalid JSON', { status: 400 }) };
  }
}

function checkOrigin(request) {
  const origin = request.headers.get('Origin');
  if (origin && !ALLOWED_ORIGINS.includes(origin)) {
    return new Response('Forbidden', { status: 403 });
  }
  return null;
}

async function startSession(request, env, record, status) {
  const token = crypto.randomUUID();
  await env.SESSIONS.put(
    sessionKey(token),
    JSON.stringify({ username: record.username, createdAt: new Date().toISOString() }),
    { expirationTtl: SESSION_TTL_SECONDS },
  );
  return jsonResponse(
    { username: record.username, balance_cash: record.balance_cash, trades: record.trades },
    status,
    { 'Set-Cookie': sessionCookieHeader(request, token, SESSION_TTL_SECONDS) },
  );
}

async function handleRegister(request, env) {
  const originError = checkOrigin(request);
  if (originError) return originError;
  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;
  const { username, password, alias, strategy_id } = bodyResult.body || {};

  // A bot account is only ever created by an operator presenting the shared
  // BOT_REGISTRATION_KEY. A wrong key is rejected outright rather than quietly
  // downgraded to a human registration, so a typo in the setup script fails
  // loudly instead of silently producing an account missing from the roster.
  const botKeyHeader = request.headers.get('X-Bot-Registration-Key');
  let isBot = false;
  if (botKeyHeader !== null) {
    if (!env.BOT_REGISTRATION_KEY || botKeyHeader !== env.BOT_REGISTRATION_KEY) {
      return jsonResponse({ error: 'Invalid bot registration key' }, 403);
    }
    isBot = true;
  }
  if (isBot && alias !== undefined && (typeof alias !== 'string' || alias.length > 40)) {
    return jsonResponse({ error: 'Alias must be a string of at most 40 characters' }, 400);
  }
  // The Worker deliberately does not validate strategy_id against a list of
  // known strategies: the registry lives in the Crassus runtime and grows
  // there (reddit_sentiment_qqq arrived in #22), so a whitelist here would
  // silently reject every new strategy until someone remembered to redeploy
  // the Worker. The runner already refuses to start on an unregistered
  // strategy_id, which is the check that actually matters.
  if (isBot && strategy_id !== undefined &&
      (typeof strategy_id !== 'string' || !STRATEGY_ID_RE.test(strategy_id))) {
    return jsonResponse({ error: 'strategy_id must be 1-40 chars: lowercase letters, numbers, underscore' }, 400);
  }

  if (typeof username !== 'string' || !USERNAME_RE.test(username)) {
    return jsonResponse({ error: 'Username must be 3-20 characters: letters, numbers, underscore' }, 400);
  }
  if (typeof password !== 'string' || password.length < MIN_PASSWORD_LEN || password.length > MAX_PASSWORD_LEN) {
    return jsonResponse({ error: `Password must be ${MIN_PASSWORD_LEN}-${MAX_PASSWORD_LEN} characters` }, 400);
  }

  const key = userKey(username);
  // KV has no conditional-create, so two simultaneous registrations of the
  // same username can both pass this check and the second put() wins --
  // accepted given this app's low-stakes, low-concurrency usage.
  const existing = await env.USERS.get(key);
  if (existing) return jsonResponse({ error: 'Username already taken' }, 409);

  const salt = randomSaltBase64();
  const hash = await derivePasswordHash(password, salt);
  const record = {
    username,
    salt,
    hash,
    iterations: PBKDF2_ITERATIONS,
    balance_cash: STARTING_BALANCE,
    starting_balance: STARTING_BALANCE,
    trades: [],
    createdAt: new Date().toISOString(),
    version: 0,
    ...(isBot ? { is_bot: true, alias: alias || username, strategy_id: strategy_id || null } : {}),
  };
  await env.USERS.put(key, JSON.stringify(record));
  // Index after the record exists, so the roster can never point at a
  // username that has no account behind it.
  if (isBot) await env.USERS.put(botKey(username), JSON.stringify({ username }));

  return startSession(request, env, record, 201);
}

async function handleLogin(request, env) {
  const originError = checkOrigin(request);
  if (originError) return originError;
  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;
  const { username, password } = bodyResult.body || {};

  if (typeof username !== 'string' || typeof password !== 'string') {
    return jsonResponse({ error: 'Invalid username or password' }, 401);
  }

  const raw = await env.USERS.get(userKey(username));
  if (!raw) return jsonResponse({ error: 'Invalid username or password' }, 401);
  const record = JSON.parse(raw);

  const candidateHash = await derivePasswordHash(password, record.salt, record.iterations);
  if (!constantTimeEqual(candidateHash, record.hash)) {
    return jsonResponse({ error: 'Invalid username or password' }, 401);
  }

  return startSession(request, env, record, 200);
}

async function handleLogout(request, env) {
  const cookies = parseCookies(request);
  const token = cookies[SESSION_COOKIE];
  if (token) await env.SESSIONS.delete(sessionKey(token));
  return new Response(null, { status: 204, headers: { 'Set-Cookie': clearedSessionCookieHeader(request) } });
}

async function handleMe(request, env) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Not logged in' }, 401);
  return jsonResponse(
    { username: session.username, balance_cash: session.record.balance_cash, trades: session.record.trades },
    200,
  );
}

// Re-syncs a bot's operator-owned metadata (strategy_id, alias) after
// registration.
//
// The authoritative strategy assignment lives in the Crassus runtime's
// accounts.json, not here -- capturing it once at registration meant that
// moving an existing bot to a new strategy left /api/bots attributing all its
// future performance to the old one, with no way to correct it short of
// abandoning the username. The runner calls this at startup so the roster
// tracks the config that is actually running.
//
// Authenticated with the same operator key as bot registration: this edits the
// public roster's attribution, so it must not be reachable by a logged-in bot
// session, let alone anonymously.
export async function handleBotMetadata(request, env) {
  const key = request.headers.get('X-Bot-Registration-Key');
  if (!env.BOT_REGISTRATION_KEY || key !== env.BOT_REGISTRATION_KEY) {
    return jsonResponse({ error: 'Invalid bot registration key' }, 403);
  }
  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;
  const { username, strategy_id, alias } = bodyResult.body || {};

  if (typeof username !== 'string' || !USERNAME_RE.test(username)) {
    return jsonResponse({ error: 'username must be a valid username' }, 400);
  }
  if (strategy_id !== undefined && strategy_id !== null &&
      (typeof strategy_id !== 'string' || !STRATEGY_ID_RE.test(strategy_id))) {
    return jsonResponse({ error: 'strategy_id must be 1-40 chars: lowercase letters, numbers, underscore' }, 400);
  }
  if (alias !== undefined && (typeof alias !== 'string' || alias.length > 40)) {
    return jsonResponse({ error: 'Alias must be a string of at most 40 characters' }, 400);
  }

  const outcome = await withUserRecord(env, username, (record) => {
    if (!record.is_bot) return { error: 'not_a_bot' };
    const next = { ...record };
    if (strategy_id !== undefined) next.strategy_id = strategy_id;
    if (alias !== undefined) next.alias = alias;
    return { record: next, result: { strategy_id: next.strategy_id, alias: next.alias } };
  });

  if (outcome.error === 'not_found') return jsonResponse({ error: 'No such account' }, 404);
  // A human account is never editable through the operator key -- that would be
  // a path to publishing a real user's balance by flipping them into the roster.
  if (outcome.error === 'not_a_bot') return jsonResponse({ error: 'Not a bot account' }, 409);
  if (outcome.error) return jsonResponse({ error: 'Metadata could not be updated, try again' }, 503);

  // Repair the roster index, idempotently.
  //
  // Registration writes the user record and the `bot:` index as two separate
  // KV puts with no transaction between them. If the second fails, the account
  // exists and is flagged is_bot but is missing from the roster -- and the
  // runner cannot recover by re-registering, because the username is taken, so
  // it logs in instead and this endpoint is the only code that runs again.
  // Writing the index here unconditionally turns the every-startup metadata
  // sync into the repair path for that window.
  await env.USERS.put(botKey(username), JSON.stringify({ username }));

  return jsonResponse({ username, ...outcome.result }, 200);
}

// Public read-only roster of the automated (Crassus) accounts, for the
// Automated tab's side-by-side comparison. Deliberately unauthenticated: these
// are paper-money bots whose whole purpose is to be observed. Two invariants
// hold it safe to expose:
//
//   1. Only accounts in the `bot:` index appear. Human accounts are never in
//      it (see botKey), so no real user's balance is ever published here.
//   2. Only the whitelisted fields below are returned. `salt`, `hash`,
//      `iterations` and session state never leave this function -- a
//      spread-the-record-and-delete-secrets approach would leak any field a
//      later commit adds, so the projection is explicit.
//
// Positions are netted server-side but left unmarked; the client marks them
// against the same live quotes the paper panel already polls, so the roster
// and the single-account view can never disagree about the mark.
export async function handleBots(request, env) {
  const index = await env.USERS.list({ prefix: 'bot:' });
  const usernames = index.keys.map(k => k.name.slice('bot:'.length));

  const bots = [];
  for (const name of usernames) {
    const raw = await env.USERS.get(`user:${name}`);
    if (!raw) continue;              // liquidated out from under the index
    const record = JSON.parse(raw);
    if (!record.is_bot) continue;    // index and record disagree -- trust the record
    const trades = Array.isArray(record.trades) ? record.trades : [];
    bots.push({
      username: record.username,
      alias: record.alias || record.username,
      strategy_id: record.strategy_id ?? null,
      balance_cash: record.balance_cash,
      starting_balance: record.starting_balance ?? STARTING_BALANCE,
      trade_count: trades.length,
      first_trade_ts: trades.length ? trades[0].ts : null,
      last_trade_ts: trades.length ? trades[trades.length - 1].ts : null,
      positions: netPositions(trades),
      created_at: record.createdAt ?? null,
    });
  }

  bots.sort((a, b) => a.alias.localeCompare(b.alias));
  return jsonResponse({ bots, as_of: new Date().toISOString() }, 200);
}

// Projects the account's position book into the roster's wire shape.
//
// This delegates to computeBookFromTrades rather than accumulating its own cost
// basis. An earlier version summed fills on the surviving side, which silently
// diverged from the real book: it never reset on a flat round trip, so a
// contract closed and reopened at a new price reported a blend of both eras.
// The smoke strategy closes and reopens the same ATM contract every cycle, so
// that was wrong within minutes of the first run. computeBookFromTrades already
// handles reductions, flat resets and side flips, and is the same function
// /api/settle books against -- sharing it is what keeps the roster and
// settlement from disagreeing about what a bot holds.
export function netPositions(trades) {
  const book = computeBookFromTrades(trades);
  return Object.entries(book)
    .filter(([, b]) => b.pos !== 0)
    .map(([sym, b]) => ({
      sym,
      strike: b.strike,
      type: b.type,
      exp: b.exp,
      qty: b.pos,
      avg_price: Number(b.avg.toFixed(4)),
    }));
}

// Resolves the session cookie to a live user record. Returns null on any
// break in the chain, including a session that still exists but whose
// account was since deleted (e.g. liquidated by /api/settle from another
// device) -- that account is gone, not just logged out.
async function requireSession(request, env) {
  const cookies = parseCookies(request);
  const token = cookies[SESSION_COOKIE];
  if (!token) return null;
  const sessionRaw = await env.SESSIONS.get(sessionKey(token));
  if (!sessionRaw) return null;
  const session = JSON.parse(sessionRaw);
  const userRaw = await env.USERS.get(userKey(session.username));
  if (!userRaw) return null;
  return { token, username: session.username, record: JSON.parse(userRaw) };
}

// Applies `mutate(record)` to a user's KV record and writes the result back.
// KV has no compare-and-swap, so this is optimistic concurrency: read,
// mutate, write a bumped `version`, then re-read to check the write actually
// stuck before trusting it; retries from a fresh read on a version mismatch.
// Good enough given this app's single-user-at-a-time usage pattern -- not a
// true CAS, and KV's eventual consistency means the confirming re-read is a
// best-effort check, not a guarantee.
//
// `mutate` returns one of:
//   { error }             -- reject, no write (e.g. insufficient balance)
//   { result }             -- no-op (e.g. idempotent replay), no write
//   { record, result }     -- write `record` (version is added automatically)
async function withUserRecord(env, username, mutate) {
  const key = userKey(username);
  for (let attempt = 0; attempt < MAX_KV_WRITE_ATTEMPTS; attempt++) {
    const raw = await env.USERS.get(key);
    if (!raw) return { error: 'not_found' };
    const record = JSON.parse(raw);
    const outcome = mutate(record);
    if (outcome.error) return outcome;
    if (!outcome.record) return outcome;

    const nextVersion = record.version + 1;
    const nextRecord = { ...outcome.record, version: nextVersion };
    await env.USERS.put(key, JSON.stringify(nextRecord));

    const confirmRaw = await env.USERS.get(key);
    const confirmed = confirmRaw ? JSON.parse(confirmRaw) : null;
    if (confirmed && confirmed.version === nextVersion) {
      return { record: nextRecord, result: outcome.result };
    }
    await sleep(10 + Math.floor(Math.random() * 40));
  }
  return { error: 'conflict' };
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Trade execution
// ---------------------------------------------------------------------------

// Records one paper-trading fill per client-generated request id under
// paper-trades/requests/. The deterministic key makes retries idempotent and
// avoids read-modify-write races between unrelated accounts.
//
// Auth is now a real session (see requireSession above), not the shared
// static token this endpoint used to check. The Origin allowlist, a
// Cloudflare rate limiting rule (configured at the zone level, not in this
// file), and the body-size cap remain as defense in depth.
async function handlePaperTrade(request, env) {
  const session = await requireSession(request, env);
  if (!session) return new Response('Forbidden', { status: 401 });

  const originError = checkOrigin(request);
  if (originError) return originError;

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
  const priceResult = executionPriceForOrder(quoteResult, body);
  if (priceResult.error) {
    return jsonResponse({
      error: priceResult.error,
      order_id: executionId,
      status: 'rejected',
    }, 409);
  }
  const price = priceResult.price;
  // Buys debit cash and are capped by balance below; sells credit cash
  // unconditionally -- this book has no margin model for opening a short
  // (mirrors the existing no-margin design elsewhere in the app). The
  // consequence of an uncovered short lands at settlement instead: see
  // handleSettle's insolvency/liquidation path.
  const cashDelta = body.side === 'buy' ? -(price * body.qty * 100) : (price * body.qty * 100);
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
    order_id: executionId,
    order_type: body.order_type ?? 'market',
    limit_price: body.order_type === 'limit' ? body.limit_price : null,
    status: 'filled',
    bid: quoteResult.bid,
    ask: quoteResult.ask,
    price,
    username: session.username,
  });

  const kvOutcome = await withUserRecord(env, session.username, (record) => {
    const existingTrade = record.trades.find(t => t.execution_request_id === executionId);
    if (existingTrade) {
      return { result: { trade: existingTrade, balance_cash: record.balance_cash } };
    }
    if (body.side === 'buy' && -cashDelta > record.balance_cash) {
      return { error: 'insufficient_balance' };
    }
    const balance_cash = record.balance_cash + cashDelta;
    return {
      record: { ...record, balance_cash, trades: [...record.trades, trade] },
      result: { trade, balance_cash },
    };
  });

  if (kvOutcome.error === 'insufficient_balance') {
    return jsonResponse({ error: 'Insufficient balance' }, 400);
  }
  if (kvOutcome.error) {
    return jsonResponse({ error: 'Balance could not be updated, try again' }, 503);
  }

  try {
    const stored = await env.PAPER_TRADES.put(key, JSON.stringify(trade), {
      onlyIf: { etagDoesNotMatch: '*' },
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { executionId, executedAt: executedAt.toISOString() },
    });
    if (!stored) {
      // Another request with the same client-generated identity won the R2
      // conditional create. The KV balance mutation above already applied
      // using this request's own independently fetched quote, which may
      // differ slightly (e.g. timestamps) from the winning R2 record's quote
      // -- an accepted, documented inconsistency for the rare concurrent-
      // duplicate-id case, since KV has no cross-store transaction with R2.
      // The execution_request_id is present in KV either way, so any further
      // retry of this same id is idempotent from here on.
      const existing = await env.PAPER_TRADES.get(key);
      if (!existing) throw new Error('Canonical execution missing after conditional write');
      return existingExecutionResponse(await existing.json(), body);
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution could not be recorded' }, 503);
  }

  return jsonResponse({ ...trade, balance_cash: kvOutcome.result.balance_cash }, 201);
}

// ---------------------------------------------------------------------------
// Settlement: server-side port of docs/index.html's settleExpired()/
// settlementPrice(), now that trades are server-authoritative in KV rather
// than a client-local array. The client still supplies spot_marks (the
// underlying prices it's observed) since this Worker doesn't track
// underlying spot itself -- those values are used only to price settlement,
// never trusted for anything else.
// ---------------------------------------------------------------------------

export function computeBookFromTrades(trades) {
  const book = {};
  for (const t of trades) {
    const b = book[t.sym] ?? (book[t.sym] = { pos: 0, avg: 0, realized: 0, strike: t.strike, type: t.type, exp: t.exp });
    const q = t.side === 'buy' ? t.qty : -t.qty;
    if (b.pos === 0 || Math.sign(b.pos) === Math.sign(q)) {
      b.avg = (b.avg * Math.abs(b.pos) + t.price * Math.abs(q)) / (Math.abs(b.pos) + Math.abs(q));
      b.pos += q;
    } else {
      const closing = Math.min(Math.abs(q), Math.abs(b.pos));
      b.realized += (t.price - b.avg) * closing * 100 * Math.sign(b.pos);
      b.pos += q;
      if (b.pos === 0) b.avg = 0;
      else if (Math.sign(b.pos) === Math.sign(q)) b.avg = t.price;
    }
  }
  return book;
}

// Longs settle worthless -- this book has no exercise capacity, so an ITM
// long must be closed before expiration to realize value. Shorts don't get
// that same free pass: a real counterparty will exercise an ITM option
// against a short seller regardless of what this account can cover, so
// shorts settle at intrinsic value using the caller-supplied spot for that
// expiration.
export function settlementPriceServer(b, spotMarks) {
  if (b.pos > 0) return 0;
  const spot = spotMarks[b.exp];
  if (spot == null) return 0;
  return b.type === 'call' ? Math.max(spot - b.strike, 0) : Math.max(b.strike - spot, 0);
}

export function validateSettleRequest(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';
  if (typeof body.as_of !== 'string' || !ISO_DATE.test(body.as_of)) return 'as_of must be an ISO date string';
  if (!body.spot_marks || typeof body.spot_marks !== 'object' || Array.isArray(body.spot_marks)) {
    return 'spot_marks must be an object';
  }
  for (const [exp, spot] of Object.entries(body.spot_marks)) {
    if (!ISO_DATE.test(exp)) return 'spot_marks keys must be ISO dates';
    if (typeof spot !== 'number' || !Number.isFinite(spot) || spot < 0) return 'spot_marks values must be non-negative numbers';
  }
  return null;
}

async function handleSettle(request, env) {
  const session = await requireSession(request, env);
  if (!session) return new Response('Forbidden', { status: 401 });

  const originError = checkOrigin(request);
  if (originError) return originError;

  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;

  const validationError = validateSettleRequest(bodyResult.body);
  if (validationError) return new Response(validationError, { status: 400 });

  const { as_of, spot_marks } = bodyResult.body;

  const kvOutcome = await withUserRecord(env, session.username, (record) => {
    const book = computeBookFromTrades(record.trades);
    const expired = Object.entries(book).filter(([, b]) => b.pos !== 0 && b.exp && b.exp < as_of);
    const alreadySettled = new Set(record.trades.map(t => t.execution_request_id));
    const pending = expired.filter(([sym, b]) => !alreadySettled.has(`${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`));

    if (!pending.length) {
      return { result: { settled: [], balance_cash: record.balance_cash, liquidated: false } };
    }

    let balance_cash = record.balance_cash;
    const newTrades = [...record.trades];
    const settled = [];

    for (const [sym, b] of pending) {
      const price = settlementPriceServer(b, spot_marks);
      const side = b.pos > 0 ? 'sell' : 'buy';
      const cashDelta = side === 'sell' ? price * Math.abs(b.pos) * 100 : -(price * Math.abs(b.pos) * 100);
      // An ITM short whose payout exceeds what's left in the account is an
      // obligation this book can't meet -- rather than clamp or partially
      // apply it, the whole account is liquidated (see handleSettle below).
      if (balance_cash + cashDelta < 0) {
        return { error: 'insolvent' };
      }
      balance_cash += cashDelta;
      const trade = Object.freeze({
        execution_id: `${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`,
        execution_request_id: `${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`,
        ts: new Date().toISOString(),
        sym, strike: b.strike, type: b.type, exp: b.exp,
        side, qty: Math.abs(b.pos), price,
        note: price > 0 ? 'exercised against (assigned)' : 'expired worthless',
        username: session.username,
      });
      newTrades.push(trade);
      settled.push(trade);
    }

    return {
      record: { ...record, balance_cash, trades: newTrades },
      result: { settled, balance_cash, liquidated: false },
    };
  });

  if (kvOutcome.error === 'insolvent') {
    await env.USERS.delete(userKey(session.username));
    // Drop the roster index entry too, or a liquidated bot leaves a pointer to
    // an account that no longer exists. handleBots tolerates the dangling case,
    // but leaving one behind would slowly turn the roster into a graveyard.
    await env.USERS.delete(botKey(session.username));
    await env.SESSIONS.delete(sessionKey(session.token));
    return jsonResponse(
      { error: 'account_liquidated', reason: 'A settlement obligation exceeded the account balance' },
      410,
      { 'Set-Cookie': clearedSessionCookieHeader(request) },
    );
  }
  if (kvOutcome.error) {
    return jsonResponse({ error: 'Settlement could not be recorded, try again' }, 503);
  }

  return jsonResponse(kvOutcome.result, 200);
}

const MAX_STRING_LEN = 64;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

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
