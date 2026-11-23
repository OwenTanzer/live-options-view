// tests/kv_page_cache.test.mjs -- the request-scoped KV page cache (#208).
//
// The migration walks a page one record at a time. Without a cache that is one
// KV `list` per record, and KV's Free plan has a separate, small daily list
// allowance. The adapter memoises page key lists for its own lifetime only:
// created inside each handleMigration call, keyed by binding + prefix + token,
// never holding values, and evicting a failed list so a retry can succeed.

import test from 'node:test';
import assert from 'node:assert/strict';
import { makeStorage, FakeKV } from './helpers/d1_shim.mjs';
import { createLegacyKvSource } from '../backend/storage.js';
import { handleMigration } from '../backend/migrate.js';

const TOKEN = 'migration-token';

function record(name, version = 1) {
  return { username: name, version, balance_cash: 100, trades: [] };
}

function adapter(kv) {
  return createLegacyKvSource({ USERS: kv, SESSIONS: kv });
}

test('repeated reads of the same page issue one list', async () => {
  const kv = new FakeKV();
  for (const name of ['alpha', 'beta', 'gamma']) await kv.put(`user:${name}`, JSON.stringify(record(name)));
  const legacy = adapter(kv);

  const first = await legacy.listUsers();
  const second = await legacy.listUsers();
  const third = await legacy.listUsers();
  assert.equal(kv.listCalls, 1, 'one list for three reads of the same page');
  assert.deepEqual(second, first);
  assert.deepEqual(third, first);
  assert.deepEqual(first.keys, ['user:alpha', 'user:beta', 'user:gamma']);
});

test('different prefixes and page tokens never share a cache entry', async () => {
  const kv = new FakeKV();
  await kv.put('user:alpha', JSON.stringify(record('alpha')));
  await kv.put('sess:token-1', JSON.stringify({ username: 'alpha', createdAt: new Date().toISOString() }));
  await kv.put('bot:alpha', JSON.stringify({ username: 'alpha' }));
  const legacy = adapter(kv);

  const users = await legacy.listUsers();
  const sessions = await legacy.listSessions();
  const markers = await legacy.listBotMarkers();

  assert.deepEqual(users.keys, ['user:alpha']);
  assert.deepEqual(sessions.keys, ['sess:token-1']);
  assert.deepEqual(markers.keys, ['bot:alpha']);
  assert.equal(kv.listCalls, 3, 'three distinct prefixes mean three lists');

  // The same page token under one prefix must not be reused for another.
  await legacy.listUsers({ pageToken: 'token-x' });
  await legacy.listUsers({ pageToken: 'token-y' });
  assert.equal(kv.listCalls, 5, 'two tokens are two listings');
});

test('a new adapter sees a fresh listing; the old one keeps its own cache', async () => {
  const kv = new FakeKV();
  await kv.put('user:alpha', JSON.stringify(record('alpha')));
  const first = adapter(kv);
  const before = await first.listUsers();
  assert.equal(before.keys.length, 1);

  await kv.put('user:beta', JSON.stringify(record('beta')));
  const cached = await first.listUsers();
  assert.equal(cached.keys.length, 1, 'the same request keeps the page it already read');

  const second = adapter(kv);
  const fresh = await second.listUsers();
  assert.equal(fresh.keys.length, 2, 'a new adapter lists again');
  assert.equal(kv.listCalls, 2);
});

test('a failed list is not cached', async () => {
  const kv = new FakeKV();
  await kv.put('user:alpha', JSON.stringify(record('alpha')));
  const legacy = adapter(kv);

  kv.failNextList = true;
  await assert.rejects(() => legacy.listUsers(), /simulated KV list failure/);
  const recovered = await legacy.listUsers();
  assert.deepEqual(recovered.keys, ['user:alpha']);
  assert.equal(kv.listCalls, 2, 'the failure was retried, not remembered');
});

test('the migration copy issues one list per page per request', async () => {
  const d1 = makeStorage();
  const kv = new FakeKV();
  for (let i = 0; i < 30; i++) {
    await kv.put(`user:bot_${String(i).padStart(2, '0')}`, JSON.stringify(record(`bot_${i}`)));
  }
  const env = {
    DB: d1, USERS: kv, SESSIONS: kv, MIGRATION_TOKEN: TOKEN,
    CUTOVER_DRAIN_SECONDS: '0', CUTOVER_STABLE_SECONDS: '0',
  };
  const call = async (body) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST',
      headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }), env);
    return { status: response.status, body: await response.json() };
  };

  const before = kv.listCalls;
  const first = await call({ phase: 'users', max_records: 20 });
  assert.equal(first.status, 200);
  assert.ok(first.body.processed_this_call > 0 && first.body.processed_this_call <= 20);
  assert.equal(kv.listCalls - before, 1,
    '20 records from one page cost one list, not 20');

  const secondBefore = kv.listCalls;
  const second = await call({ phase: 'users', max_records: 20 });
  assert.equal(second.body.done, true);
  assert.equal(kv.listCalls - secondBefore, 1,
    'a new request re-lists the page it needs, once');
});
