// backend/common.js (B4) -- shared foundation for the Worker modules: the
// constants and helpers more than one route group needs (response/body
// helpers, legacy key helpers kept for the offline recovery path, session-cookie
// helpers, the D1 account writer, and the position-book primitive). worker.js
// re-exports the public names so the Worker's 32-export API is unchanged. Moved verbatim
// from worker.js; only the file changed, not the code.

import { createStorage } from './storage.js';

export const R2_ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev";
const ALLOWED_ORIGINS = ['https://options.moopertonic.net', 'http://localhost:8787'];
export const MAX_BODY_BYTES = 16 * 1024; // a trade record is a few hundred bytes; this leaves ample headroom
export const SESSION_COOKIE = 'session';
export const SESSION_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days
export const STARTING_BALANCE = 10_000;
export const BOT_USERNAME_RE = /^[A-Za-z0-9_]{3,40}$/;
export const STRATEGY_ID_RE = /^[a-z0-9_]{1,40}$/;
export const MAX_KV_WRITE_ATTEMPTS = 5;
export const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
export function userKey(username) {
  return `user:${username.toLowerCase()}`;
}
// Legacy membership markers are retained for migration and rollback. The R2
// index holds the same public bot identifiers; account records remain authoritative.
export function botKey(username) {
  return `bot:${username.toLowerCase()}`;
}
export function sessionKey(token) {
  return `sess:${token}`;
}
function isSecureRequest(request) {
  return new URL(request.url).protocol === 'https:';
}
// `Secure` cookies never round-trip over plain http://, which the dev origin
// (http://localhost:8787) already in ALLOWED_ORIGINS relies on -- so the
// flag is conditional on the actual request scheme rather than always on.
export function sessionCookieHeader(request, token, maxAgeSeconds) {
  const secure = isSecureRequest(request) ? '; Secure' : '';
  return `${SESSION_COOKIE}=${token}; HttpOnly${secure}; SameSite=Lax; Path=/; Max-Age=${maxAgeSeconds}`;
}
export function clearedSessionCookieHeader(request) {
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
export async function readJsonBody(request) {
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
export function checkOrigin(request) {
  const origin = request.headers.get('Origin');
  if (origin && !ALLOWED_ORIGINS.includes(origin)) {
    return new Response('Forbidden', { status: 403 });
  }
  return null;
}
// Resolves the session cookie to a live user record. Returns null on any
// break in the chain, including a session that still exists but whose
// account was since deleted (e.g. liquidated by /api/settle from another
// device) -- that account is gone, not just logged out.
//
// Sessions and accounts come from D1 (#208). The session row stores a hash of
// the token, so the cookie value never appears at rest; the lookup hashes what
// the client presented.
export async function requireSession(request, env, storage = null) {
  const cookies = parseCookies(request);
  const token = cookies[SESSION_COOKIE];
  if (!token) return null;
  const store = storage ?? createStorage(env);
  const session = await store.sessions.get(token);
  if (!session) return null;
  const record = await store.users.get(session.username);
  if (!record) return null;
  return { token, username: record.username ?? session.username, record };
}
// Applies `mutate(record)` to an account record and writes the result back.
//
// This used to be KV read-modify-write: read, mutate, put a bumped version,
// then re-read to see whether the write stuck -- a check its own comment called
// best effort. D1 gives a real conditional write, so this is now a single
// UPDATE guarded by the version that was read, with the row count as proof and
// a bounded retry only when somebody else genuinely won the race.
//
// `mutate` returns one of:
//   { error }             -- reject, no write (e.g. insufficient balance)
//   { result }             -- no-op (e.g. idempotent replay), no write
//   { record, result }     -- write `record` (version is added automatically)
export async function withUserRecord(env, username, mutate, storage = null) {
  const store = storage ?? createStorage(env);
  return store.users.mutate(username, mutate);
}
export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
export function computeBookFromTrades(trades) {
  const book = {};
  for (const t of trades) {
    if (t.adjustment_units !== undefined) {
      if (book[t.sym]) book[t.sym].realized += t.adjustment_units / 10000;
      continue;
    }
    const instrumentType = t.instrument_type ?? 'option';
    const multiplier = t.multiplier ?? (instrumentType === 'share' ? 1 : 100);
    const b = book[t.sym] ?? (book[t.sym] = {
      pos: 0, avg: 0, realized: 0,
      underlying: /^([A-Z]+)\d{6}[CP]\d{8}$/.exec(t.sym)?.[1] ?? t.underlying ?? 'QQQ',
      instrument_type: instrumentType, multiplier,
      strike: t.strike, type: t.type, exp: t.exp,
      ...(t.prediction ? { prediction: t.prediction } : {}),
      ...(instrumentType === 'futures_option' ? { contract: t.contract, fees: 0, open_fees: 0, realized_gross: 0 } : {}),
    });
    b.realized -= (t.fee_units ?? 0) / 10000;
    const q = t.side === 'buy' ? t.qty : -t.qty;
    if (instrumentType === 'futures_option') {
      const fee = t.fees ?? 0; b.fees += fee;
      if (q > 0) b.open_fees += fee;
      else {
        const allocated = b.pos > 0 ? b.open_fees * Math.min(-q, b.pos) / b.pos : 0;
        b.open_fees -= allocated; b.realized -= allocated + fee;
        b.realized_gross += (t.price - b.avg) * Math.min(-q, b.pos) * b.multiplier;
      }
    }
    if (b.pos === 0 || Math.sign(b.pos) === Math.sign(q)) {
      b.avg = (b.avg * Math.abs(b.pos) + t.price * Math.abs(q)) / (Math.abs(b.pos) + Math.abs(q));
      b.pos += q;
    } else {
      const closing = Math.min(Math.abs(q), Math.abs(b.pos));
      b.realized += (t.price - b.avg) * closing * b.multiplier * Math.sign(b.pos);
      b.pos += q;
      if (b.pos === 0) b.avg = 0;
      else if (Math.sign(b.pos) === Math.sign(q)) b.avg = t.price;
    }
  }
  return book;
}
export function jsonResponse(value, status, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...extraHeaders },
  });
}
// Reads the request body up to `limit` bytes, aborting the stream and
// throwing if exceeded. Content-Length alone isn't trustworthy (absent on
// chunked requests, or simply lied about), so this is the real enforcement.
export async function readLimited(request, limit) {
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
