// tests/no_runtime_kv.test.mjs -- post-cutover cleanup guard (#208).
//
// The normal deployment must not bind Workers KV, must not mount the migration
// route, and must not reach any module that reads env.USERS/env.SESSIONS. The
// legacy namespaces stay in the account for the offline recovery Worker, which
// is a separate entry with its own config and is never pointed at user traffic.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import worker from '../backend/index.js';
import recovery from '../backend/recovery.js';
import { makeStorage, FakeKV } from './helpers/d1_shim.mjs';
import { withD1 } from './helpers/test_env.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (relative) => fs.readFileSync(path.join(ROOT, relative), 'utf8');

const TOKEN = 'recovery-token';

test('all serving and staged configurations exclude KV bindings', () => {
  for (const name of ['wrangler.assets.toml', 'wrangler.api.toml', 'wrangler.preview.toml']) {
    if (!fs.existsSync(path.join(ROOT, name))) continue;
    assert.doesNotMatch(read(name), /\[\[(?:env\.[^.]+\.)?kv_namespaces\]\]/, name);
  }
});

test('One Clock modules have no storage or recovery dependencies', () => {
  const files = fs.readdirSync(path.join(ROOT, 'site/shared')).filter(name => name.startsWith('one-clock') && /\.m?js$/.test(name));
  assert.ok(files.length > 0);
  for (const name of files) {
    const source = read(path.join('site/shared', name));
    assert.doesNotMatch(source, /(?:env\.(?:USERS|SESSIONS)|\bKVNamespace\b|(?:from|import).*\b(?:storage|recovery|migrate)\b)/, name);
  }
});

function tripwire(binding) {
  const fail = () => { throw new Error(`KV ${binding} reached from the normal Worker`); };
  return new Proxy({}, { get: fail });
}

function post(handler, env, pathName, body, token = TOKEN) {
  return handler.fetch(new Request(`https://worker.test${pathName}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { 'X-Migration-Token': token } : {}),
    },
    body: JSON.stringify(body),
  }), env);
}

test('the production config binds no KV namespaces', () => {
  const config = read('wrangler.toml');
  assert.ok(!config.includes('[[kv_namespaces]]'), 'no KV namespace block in production config');
  assert.ok(!/binding\s*=\s*"USERS"/.test(config), 'no USERS binding');
  assert.ok(!/binding\s*=\s*"SESSIONS"/.test(config), 'no SESSIONS binding');
  assert.match(config, /\[\[d1_databases\]\]/, 'D1 remains the store');
});

test('the normal Worker graph does not import the migration module', () => {
  const entry = read('backend/index.js');
  assert.ok(!entry.includes("from './migrate.js'"), 'index must not import migrate.js');
  assert.ok(!entry.includes('handleMigration'), 'index must not mount handleMigration');
  assert.ok(!entry.includes('/api/admin/migrate'), 'index must not route the migration endpoint');
});

test('only the offline recovery path references the KV bindings', () => {
  const files = fs.readdirSync(path.join(ROOT, 'backend')).filter(name => name.endsWith('.js'));
  const allowed = new Set(['storage.js', 'migrate.js', 'fence.js']);
  const offenders = [];
  for (const name of files) {
    if (allowed.has(name)) continue;
    const source = read(path.join('backend', name));
    if (/env\.(USERS|SESSIONS)\b/.test(source)) offenders.push(name);
  }
  assert.deepEqual(offenders, [], `runtime modules touching KV: ${offenders.join(', ')}`);
});

test('the normal Worker answers the migration route as retired', async () => {
  const env = withD1({
    USERS: tripwire('USERS'), SESSIONS: tripwire('SESSIONS'),
    ASSETS: { async fetch() { return new Response('not found', { status: 404 }); } },
  });
  const response = await post({ fetch: worker.fetch.bind(worker) }, env, '/api/admin/migrate', { phase: 'status' });
  // The route is not mounted, so this falls through to the site fallback.
  assert.equal(response.status, 404);
  assert.ok(!(await response.clone().text()).includes('checkpoints'),
    'the retired route must not answer as the migration API');
});

test('the normal Worker still authenticates on D1 with no KV bound', async () => {
  const env = withD1({ USERS: tripwire('USERS'), SESSIONS: tripwire('SESSIONS') });
  const registered = await worker.fetch(new Request('https://worker.test/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'cleanup_fixture', password: 'fixture-password' }),
  }), env);
  assert.equal(registered.status, 201);
  const cookie = /session=([^;]+)/.exec(registered.headers.get('Set-Cookie'))[1];
  const me = await worker.fetch(new Request('https://worker.test/api/me', {
    headers: { Cookie: `session=${cookie}` },
  }), env);
  assert.equal(me.status, 200);
});

test('the recovery entry mounts only the authenticated migration route', async () => {
  const d1 = makeStorage();
  const kv = new FakeKV();
  const env = {
    DB: d1, USERS: kv, SESSIONS: kv, MIGRATION_TOKEN: TOKEN,
    CUTOVER_DRAIN_SECONDS: '0', CUTOVER_STABLE_SECONDS: '0',
  };

  const unauthorised = await post({ fetch: recovery.fetch.bind(recovery) }, env,
    '/api/admin/migrate', { phase: 'status' }, null);
  assert.equal(unauthorised.status, 404);

  const status = await post({ fetch: recovery.fetch.bind(recovery) }, env,
    '/api/admin/migrate', { phase: 'status' });
  assert.equal(status.status, 200);
  assert.ok((await status.json()).checkpoints, 'recovery exposes the migration phases');

  const me = await recovery.fetch(new Request('https://worker.test/api/me'), env);
  assert.equal(me.status, 404, 'ordinary user routes are unavailable on recovery');
});

test('the recovery config is the only place KV namespaces are bound', () => {
  const config = read('wrangler.recovery.toml');
  assert.match(config, /\[\[kv_namespaces\]\]/);
  assert.match(config, /binding\s*=\s*"USERS"/);
  assert.match(config, /binding\s*=\s*"SESSIONS"/);
  assert.match(config, /main\s*=\s*"backend\/recovery\.js"/);
  assert.ok(!config.includes('[triggers]'), 'the recovery Worker runs no cron');
});
