// tests/storage_d1.test.mjs -- the account/session store that replaces KV.
//
// Runs the real schema and the real SQL against Node's SQLite engine (the same
// engine D1 runs), so UNIQUE constraints, conditional UPDATEs and ON CONFLICT
// behaviour are exercised rather than mocked. What is faked is only the network
// wrapper.

import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { makeStorage, FakeD1 } from './helpers/d1_shim.mjs';
import { createStorage, StorageUnavailable, hashSessionToken, accountId } from '../backend/storage.js';

const LEGACY_RECORD = {
  username: 'crassus_luigi',
  salt: 'c2FsdA==',
  hash: 'aGFzaA==',
  iterations: 100000,
  balance_cash: 9740,
  trades: [
    { execution_request_id: 'req-1', sym: 'QQQ260803C00690000', side: 'buy', qty: 1, price: 1.25 },
  ],
  createdAt: '2026-11-01T12:00:00.000Z',
  version: 7,
  is_bot: true,
  alias: 'Luigi',
  strategy_id: 'reddit_sentiment_qqq',
  paper_margin_limit: 2,
  paper_financing_rate: 0.06,
};

function storageWithDb() {
  const d1 = makeStorage();
  return { d1, storage: createStorage({ DB: d1 }) };
}

test('registration is unique under concurrent inserts', async () => {
  const { storage } = storageWithDb();
  const attempts = await Promise.all(
    [1, 2, 3, 4].map(() => storage.users.insert('NewBot', { username: 'NewBot', version: 0 })),
  );
  assert.equal(attempts.filter(r => r === 'created').length, 1);
  assert.equal(attempts.filter(r => r === 'exists').length, 3);
  assert.equal((await storage.users.listUsernames()).length, 1);
});

test('a legacy record round-trips byte-for-byte', async () => {
  const { storage } = storageWithDb();
  assert.equal(await storage.users.insert('crassus_luigi', LEGACY_RECORD), 'created');
  const read = await storage.users.get('Crassus_Luigi');   // case-insensitive identity
  assert.deepEqual(read, LEGACY_RECORD);
  assert.equal(read.hash, LEGACY_RECORD.hash);
  assert.equal(read.balance_cash, 9740);
  assert.deepEqual(read.trades, LEGACY_RECORD.trades);
});

test('concurrent mutations do not lose a write', async () => {
  const { storage } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct', balance_cash: 100, version: 0 });

  // Two read-modify-writes that would race under KV's non-atomic put.
  await Promise.all([
    storage.users.mutate('acct', record => ({
      record: { ...record, balance_cash: record.balance_cash - 10 },
      result: 'debited',
    })),
    storage.users.mutate('acct', record => ({
      record: { ...record, balance_cash: record.balance_cash - 25 },
      result: 'debited',
    })),
  ]);

  const final = await storage.users.get('acct');
  assert.equal(final.balance_cash, 65);       // both debits applied
  assert.equal(final.version, 2);
});

test('a stale version cannot overwrite a newer record', async () => {
  const { storage, d1 } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct', balance_cash: 100, version: 0 });
  await storage.users.mutate('acct', record => ({ record: { ...record, balance_cash: 90 } }));

  // The guarded UPDATE with the old version must change nothing.
  const result = await d1.prepare(
    'UPDATE users SET record = ?, version = ? WHERE username = ? AND version = ?',
  ).bind(JSON.stringify({ balance_cash: 0, version: 1 }), 1, 'acct', 0).run();
  assert.equal(result.meta.changes, 0);
  assert.equal((await storage.users.get('acct')).balance_cash, 90);
});

test('mutate rejects and no-ops keep the record untouched', async () => {
  const { storage } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct', balance_cash: 100, version: 3 });

  const rejected = await storage.users.mutate('acct', () => ({ error: 'insufficient_balance' }));
  assert.deepEqual(rejected, { error: 'insufficient_balance' });
  const noop = await storage.users.mutate('acct', () => ({ result: 'replay' }));
  assert.deepEqual(noop, { result: 'replay' });
  const missing = await storage.users.mutate('ghost', () => ({ result: 'x' }));
  assert.deepEqual(missing, { error: 'not_found' });

  const final = await storage.users.get('acct');
  assert.equal(final.balance_cash, 100);
  assert.equal(final.version, 3);
});

test('sessions store only a hash of the bearer token, and expire', async () => {
  const { storage, d1 } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct' });
  const token = 'token-abc-123';
  await storage.sessions.create(token, 'acct', 60);

  const row = d1.raw('SELECT token_hash, username FROM sessions')[0];
  assert.equal(row.token_hash, await hashSessionToken(token));
  assert.notEqual(row.token_hash, token);

  const live = await storage.sessions.get(token);
  assert.equal(live.username, 'acct');

  const bad = await storage.sessions.get('some-other-token');
  assert.equal(bad, null);

  // Expiry is enforced on read and the row is collected lazily.
  await storage.sessions.create('short', 'acct', -1);
  assert.equal(await storage.sessions.get('short'), null);
  assert.equal(d1.raw('SELECT * FROM sessions WHERE username = ?', 'acct').length, 1);
});

test('logout and settlement revocation remove every session for an account', async () => {
  const { storage, d1 } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct' });
  await storage.sessions.create('t1', 'acct', 60);
  await storage.sessions.create('t2', 'acct', 60);

  await storage.sessions.delete('t1');
  assert.equal(await storage.sessions.get('t1'), null);
  assert.ok(await storage.sessions.get('t2'));

  await storage.sessions.deleteForUser('acct');
  assert.equal(await storage.sessions.get('t2'), null);
  assert.equal(d1.raw('SELECT * FROM sessions').length, 0);
});

test('migration inserts never overwrite a row that already exists', async () => {
  const { storage } = storageWithDb();
  await storage.users.insert('acct', { username: 'acct', balance_cash: 500, version: 4 });

  // A cutover races a live write: the migration copy must not win.
  const outcome = await storage.users.insertIfAbsent(
    'acct', { username: 'acct', balance_cash: 100, version: 1 },
  );
  assert.equal(outcome, 'exists');
  assert.equal((await storage.users.get('acct')).balance_cash, 500);

  const fresh = await storage.users.insertIfAbsent('new', { username: 'new', version: 0 });
  assert.equal(fresh, 'created');
});

test('migration session inserts are idempotent', async () => {
  const { storage } = storageWithDb();
  const hash = await hashSessionToken('legacy-token');
  const expires = Math.floor(Date.now() / 1000) + 3600;
  assert.equal(await storage.sessions.insertHashedIfAbsent(hash, 'acct', expires), 'created');
  assert.equal(await storage.sessions.insertHashedIfAbsent(hash, 'acct', expires), 'exists');
  assert.equal((await storage.sessions.get('legacy-token')).username, 'acct');
});

test('there is no silent KV fallback', () => {
  assert.throws(
    () => createStorage({ USERS: {}, SESSIONS: {} }),
    error => error instanceof StorageUnavailable && /KV fallback is disabled/.test(error.message),
  );
});

test('account identity is the lowercased legacy key', () => {
  assert.equal(accountId('CraSsus_Luigi'), 'crassus_luigi');
  assert.equal(accountId('a'), 'a');
});

test('the schema is created idempotently', () => {
  const d1 = new FakeD1();
  d1.db.exec(FakeD1.schemaSql());
  d1.db.exec(FakeD1.schemaSql());   // deploy-time re-run must not fail
  assert.equal(d1.raw("SELECT name FROM sqlite_master WHERE type='table'").length, 5);
});
