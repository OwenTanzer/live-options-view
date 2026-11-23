// tests/auth_logout.test.mjs -- /api/logout regression (#208, PR216 review).
//
// handleLogout used SESSION_COOKIE without importing it, so every logout threw
// ReferenceError and the route could not revoke a session. This exercises the
// real routes against D1 with tripwire KV namespaces: login, read, logout,
// re-read, cookie clearing, token revocation, and a no-cookie logout that must
// still answer 204.

import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../backend/index.js';
import { withD1 } from './helpers/test_env.mjs';
import { createStorage, hashSessionToken } from '../backend/storage.js';
import { SESSION_COOKIE } from '../backend/common.js';

function tripwire(binding) {
  const fail = () => { throw new Error(`KV ${binding} touched by an auth route`); };
  return new Proxy({}, { get: fail });
}

function makeEnv() {
  // D1 plus deliberately hostile KV namespaces: any residual KV access fails the
  // test rather than silently working.
  return withD1({ USERS: tripwire('USERS'), SESSIONS: tripwire('SESSIONS') });
}

function call(env, path, { method = 'GET', body, cookie } = {}) {
  return worker.fetch(new Request(`https://worker.test${path}`, {
    method,
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  }), env);
}

function sessionCookie(response) {
  const header = response.headers.get('Set-Cookie');
  assert.ok(header, 'response must set a session cookie');
  const match = new RegExp(`${SESSION_COOKIE}=([^;]*)`).exec(header);
  return { header, token: match?.[1] ?? '' };
}

test('logout clears the cookie and revokes the token in D1', async () => {
  const env = makeEnv();
  const storage = createStorage(env);
  const credentials = { username: 'logout_fixture', password: 'fixture-password' };

  const registered = await call(env, '/api/register', { method: 'POST', body: credentials });
  assert.equal(registered.status, 201);
  const { token } = sessionCookie(registered);
  assert.ok(token.length > 0);
  assert.equal((await storage.sessions.get(token)).username, 'logout_fixture');

  const cookie = `${SESSION_COOKIE}=${token}`;
  const read = await call(env, '/api/me', { cookie });
  assert.equal(read.status, 200);
  assert.equal((await read.json()).username, 'logout_fixture');

  const loggedOut = await call(env, '/api/logout', { method: 'POST', cookie });
  assert.equal(loggedOut.status, 204);

  // The clearing cookie must expire the session cookie for the browser.
  const cleared = loggedOut.headers.get('Set-Cookie');
  assert.ok(cleared, 'logout must clear the cookie');
  assert.match(cleared, new RegExp(`${SESSION_COOKIE}=;`));
  assert.match(cleared, /Max-Age=0/);

  // The token is gone from D1, not merely hidden from the browser.
  assert.equal(await storage.sessions.get(token), null);
  assert.equal((await storage.sessions.count()), 0);
  const hash = await hashSessionToken(token);
  assert.equal(env.DB.raw('SELECT * FROM sessions WHERE token_hash = ?', hash).length, 0);

  // Re-reading with the old cookie is unauthenticated.
  const reread = await call(env, '/api/me', { cookie });
  assert.equal(reread.status, 401);

  // A second logout with the revoked cookie is still a clean 204.
  assert.equal((await call(env, '/api/logout', { method: 'POST', cookie })).status, 204);
});

test('logout without a cookie answers 204 and still clears', async () => {
  const env = makeEnv();
  const response = await call(env, '/api/logout', { method: 'POST' });
  assert.equal(response.status, 204);
  const cleared = response.headers.get('Set-Cookie');
  assert.ok(cleared);
  assert.match(cleared, new RegExp(`${SESSION_COOKIE}=;`));
});

test('logging out one session does not revoke a second live session', async () => {
  const env = makeEnv();
  const credentials = { username: 'logout_two', password: 'fixture-password' };
  const first = await call(env, '/api/register', { method: 'POST', body: credentials });
  const second = await call(env, '/api/login', { method: 'POST', body: credentials });
  const firstToken = sessionCookie(first).token;
  const secondToken = sessionCookie(second).token;
  assert.notEqual(firstToken, secondToken);

  assert.equal((await call(env, '/api/logout', {
    method: 'POST', cookie: `${SESSION_COOKIE}=${firstToken}`,
  })).status, 204);
  assert.equal((await call(env, '/api/me', { cookie: `${SESSION_COOKIE}=${firstToken}` })).status, 401);
  assert.equal((await call(env, '/api/me', { cookie: `${SESSION_COOKIE}=${secondToken}` })).status, 200);
});
