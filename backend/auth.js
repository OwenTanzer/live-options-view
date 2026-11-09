// backend/auth.js (B4) -- auth: password hashing, session start, and the
// /api/register, /api/login, /api/logout, /api/me routes. Moved verbatim
// from worker.js; the entry re-exports the public names. The session-cookie
// and KV helpers live in backend/common.js; bot-membership sync (retried on
// register/login) lives in backend/bots.js.

import {
  BOT_USERNAME_RE, SESSION_TTL_SECONDS, STARTING_BALANCE, STRATEGY_ID_RE,
  checkOrigin, clearedSessionCookieHeader, jsonResponse, parseCookies,
  readJsonBody, requireSession, sessionCookieHeader, sessionKey, userKey,
} from './common.js';
import { syncBotMembership } from './bots.js';

const PBKDF2_ITERATIONS = 100_000;
export const USERNAME_RE = /^[A-Za-z0-9_]{3,20}$/;
export const MIN_PASSWORD_LEN = 8;
export const MAX_PASSWORD_LEN = 256;
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
export async function handleRegister(request, env) {
  const originError = checkOrigin(request);
  if (originError) return originError;
  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;
  const { username, password, alias, strategy_id, paper_margin_limit, paper_financing_rate } = bodyResult.body || {};

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
  if (isBot && paper_margin_limit !== undefined &&
      (typeof paper_margin_limit !== 'number' || !Number.isFinite(paper_margin_limit) ||
       paper_margin_limit < 1 || paper_margin_limit > 4)) {
    return jsonResponse({ error: 'paper_margin_limit must be a number from 1 to 4' }, 400);
  }
  if (isBot && paper_financing_rate !== undefined &&
      (typeof paper_financing_rate !== 'number' || !Number.isFinite(paper_financing_rate) ||
       paper_financing_rate < 0 || paper_financing_rate > 1)) {
    return jsonResponse({ error: 'paper_financing_rate must be a number from 0 to 1' }, 400);
  }

  if (typeof username !== 'string' || !(isBot ? BOT_USERNAME_RE : USERNAME_RE).test(username)) {
    return jsonResponse({ error: `Username must be 3-${isBot ? 40 : 20} characters: letters, numbers, underscore` }, 400);
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
    ...(isBot ? {
      is_bot: true, alias: alias || username, strategy_id: strategy_id || null,
      ...(paper_margin_limit !== undefined ? { paper_margin_limit } : {}),
      ...(paper_financing_rate !== undefined ? { paper_financing_rate } : {}),
    } : {}),
  };
  await env.USERS.put(key, JSON.stringify(record));
  // Index after the record exists, so the roster can never point at a
  // username that has no account behind it.
  if (isBot) {
    try { await syncBotMembership(env, username); }
    catch { return jsonResponse({ error: 'Bot membership update failed; login to retry' }, 503); }
  }

  return startSession(request, env, record, 201);
}
export async function handleLogin(request, env) {
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

  if (record.is_bot === true) {
    try { await syncBotMembership(env, record.username); }
    catch { return jsonResponse({ error: 'Bot membership update failed; retry login' }, 503); }
  }

  return startSession(request, env, record, 200);
}
export async function handleLogout(request, env) {
  const cookies = parseCookies(request);
  const token = cookies[SESSION_COOKIE];
  if (token) await env.SESSIONS.delete(sessionKey(token));
  return new Response(null, { status: 204, headers: { 'Set-Cookie': clearedSessionCookieHeader(request) } });
}
export async function handleMe(request, env) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Not logged in' }, 401);
  return jsonResponse(
    { username: session.username, balance_cash: session.record.balance_cash, trades: session.record.trades,
      account_closed: session.record.account_closed === true,
      closed_at: session.record.closed_at ?? null, closure_reason: session.record.closure_reason ?? null },
    200,
  );
}
