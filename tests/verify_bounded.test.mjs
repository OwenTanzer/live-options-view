// tests/verify_bounded.test.mjs -- verify must stay inside a Worker CPU budget.
//
// Production verify returned Cloudflare 1102 (CPU limit) against 38 accounts,
// 1,484 sessions and 32 markers. The cause was the source snapshot: every entry
// was kept in the checkpoint, which grew past 200 KB, so each bounded call paid
// to parse and rewrite the whole array four times. The snapshot is now a
// constant-size hash chain. These tests hold the line: the checkpoint stays
// small for a production-shaped source, the digest is deterministic across a
// restart, and verify/stabilize still agree through finalize.

import test from 'node:test';
import assert from 'node:assert/strict';
import { makeStorage, FakeKV } from './helpers/d1_shim.mjs';
import { handleMigration } from '../backend/migrate.js';
import { createStorage } from '../backend/storage.js';

const TOKEN = 'migration-token';
const USERS = 38;
const SESSIONS = 1484;
const MARKERS = 32;

/**
 * Build an account whose serialized record reaches `targetBytes`, with the
 * realistic option-trade fields the production accounts carry. The old fixture
 * used twenty tiny trades (~5 KB); production has 38 accounts totalling
 * 7,457,764 bytes, a largest of 1,139,632 and a first of 333,634 with 576
 * trades, so size is part of the regression surface.
 */
function userRecordOfSize(name, targetBytes) {
  const record = {
    username: name, salt: 'c2FsdA==', hash: 'aGFzaA==', iterations: 100000,
    balance_cash: 10000, starting_balance: 10000, version: 3, is_bot: true, alias: name,
    strategy_id: 'spy_sma200_double_or_out', paper_margin_limit: 2, paper_financing_rate: 0.06,
    createdAt: '2026-10-01T13:30:00.000Z',
    trades: [],
  };
  let k = 0;
  while (JSON.stringify(record).length < targetBytes) {
    const strike = 600 + (k % 40);
    record.trades.push({
      execution_request_id: `req-${name}-${k}`,
      sym: `SPY261120C00${String(strike).padStart(4, '0')}000`,
      underlying: 'SPY', instrument_type: 'option', side: k % 2 ? 'sell' : 'buy',
      qty: 1, price: 4.25, strike, type: 'call', exp: '2026-11-20',
      multiplier: 100, ts: '2026-10-01T14:00:00.000Z',
      order_id: `ord-${name}-${k}`, status: 'filled', username: name,
      note: 'paper fill', quote_ts: '2026-10-01T13:59:59.000Z',
    });
    k += 1;
  }
  return record;
}

function userRecord(name) {
  return userRecordOfSize(name, 4096);
}

function harness() {
  const d1 = makeStorage();
  const kv = new FakeKV();
  const env = {
    DB: d1, USERS: kv, SESSIONS: kv, MIGRATION_TOKEN: TOKEN,
    CUTOVER_DRAIN_SECONDS: '0', CUTOVER_STABLE_SECONDS: '0',
  };
  const call = async (phase, extra = {}) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST',
      headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phase, ...extra }),
    }), env);
    return { status: response.status, body: await response.json() };
  };
  const harnessEnv = () => env;
  const checkpointBytes = (name) => {
    const row = d1.raw('SELECT value FROM migration_state WHERE name = ?', name)[0];
    return row ? row.value.length : 0;
  };
  return { d1, kv, call, checkpointBytes, env, harnessEnv };
}

const PRODUCTION_SIZES = [1_139_632, 333_634];

async function seed(kv, { sizes = [], baseline = 60_000, sessions = SESSIONS } = {}) {
  for (let i = 0; i < USERS; i++) {
    const name = `crassus_bot_${i}`;
    const target = sizes[i] ?? baseline;
    await kv.put(`user:${name}`, JSON.stringify(userRecordOfSize(name, target)));
    if (i < MARKERS) await kv.put(`bot:${name}`, JSON.stringify({ username: name }));
  }
  for (let i = 0; i < sessions; i++) {
    await kv.put(`sess:token-${i}`, JSON.stringify({
      username: `crassus_bot_${i % USERS}`,
      createdAt: new Date(Date.now() - 60_000).toISOString(),
    }));
  }
}

/** Drive a phase to completion while recording the largest checkpoint it wrote. */
async function drain(call, checkpointBytes, phase, extra = {}, limit = 400) {
  let maxCheckpoint = 0;
  let last;
  for (let i = 0; i < limit; i++) {
    last = await call(phase, extra);
    assert.equal(last.status, 200, `${phase} must not fault`);
    maxCheckpoint = Math.max(maxCheckpoint, checkpointBytes(`phase:${phase}`));
    if (last.body.done) return { body: last.body, maxCheckpoint, calls: i + 1 };
  }
  throw new Error(`${phase} did not finish within ${limit} calls`);
}

test('verify keeps a constant-size checkpoint on a production-shaped source', async () => {
  const { kv, call, checkpointBytes } = harness();
  await seed(kv, { sizes: PRODUCTION_SIZES });
  // The fixture must actually carry production-sized records, not 20 tiny trades.
  const largest = Math.max(...[...kv.data.entries()]
    .filter(([key]) => key.startsWith('user:'))
    .map(([, value]) => value.length));
  assert.ok(largest >= 1_100_000, `fixture largest account is ${largest} bytes`);
  await call('fence', { action: 'arm' });

  const result = await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  assert.equal(result.body.done, true);
  assert.ok(result.body.source_digest, 'a source digest is produced');
  assert.ok(result.maxCheckpoint < 4096,
    `checkpoint stayed bounded, was ${result.maxCheckpoint} bytes`);
  // The old implementation reached ~206 KB on this shape; anything near that is
  // a regression even if the test host does not enforce the Worker CPU limit.
  assert.ok(result.maxCheckpoint < 8 * 1024, 'no entry array may return to the checkpoint');
});

test('the source digest is deterministic across a restart', async () => {
  const { kv, call, checkpointBytes } = harness();
  await seed(kv, { sessions: 300 });
  await call('fence', { action: 'arm' });

  const first = await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  await call('verify', { restart: true });
  const second = await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  assert.equal(first.body.source_digest, second.body.source_digest,
    're-scanning the same source produces the same digest');
});

test('verify and stabilize agree on the snapshot and finalize succeeds', async () => {
  const { kv, call, checkpointBytes } = harness();
  await seed(kv);
  await call('fence', { action: 'arm' });

  const verified = await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  const stable = await drain(call, checkpointBytes, 'stabilize', { max_records: 20 });
  assert.equal(stable.body.stable, true);
  assert.equal(stable.body.source_digest, verified.body.source_digest,
    'the stabilized snapshot is the one parity was proven against');

  // Complete the rest of the cutover and finalize against the same source.
  await drain(call, checkpointBytes, 'users', { max_records: 20 });
  await drain(call, checkpointBytes, 'sessions', { max_records: 20 });
  await drain(call, checkpointBytes, 'bot_markers', { max_records: 20 });
  await drain(call, checkpointBytes, 'reconcile', { max_records: 20 });
  await call('verify', { restart: true });
  await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  await call('stabilize', { restart: true });
  await drain(call, checkpointBytes, 'stabilize', { max_records: 20 });
  const finalized = await call('finalize');
  assert.equal(finalized.status, 200);
  assert.equal(finalized.body.users, USERS);
  assert.equal(finalized.body.sessions, SESSIONS);
});

test('a late legacy write still fails finalize after the chain change', async () => {
  const { kv, call, checkpointBytes } = harness();
  await seed(kv, { sessions: 300 });
  await call('fence', { action: 'arm' });
  await drain(call, checkpointBytes, 'users', { max_records: 20 });
  await drain(call, checkpointBytes, 'sessions', { max_records: 20 });
  await drain(call, checkpointBytes, 'bot_markers', { max_records: 20 });
  await drain(call, checkpointBytes, 'reconcile', { max_records: 20 });
  await drain(call, checkpointBytes, 'verify', { max_records: 20 });

  await kv.put('user:crassus_bot_0', JSON.stringify({ ...userRecord('crassus_bot_0'), version: 4 }));
  await drain(call, checkpointBytes, 'stabilize', { max_records: 20 });

  const finalized = await call('finalize');
  assert.equal(finalized.status, 409);
  assert.match(finalized.body.error, /different source snapshot|parity/);
});

test('a 352 KB record still verifies when its serialization is not byte-equal', async () => {
  const { kv, call, checkpointBytes, env } = harness();
  const record = userRecordOfSize('crassus_big', 352_473);
  const compact = JSON.stringify(record);
  await kv.put('user:crassus_big', JSON.stringify(record, null, 2));   // legacy raw has whitespace
  await createStorage(env).users.insert('crassus_big', record);        // D1 copy is compact
  await call('fence', { action: 'arm' });

  const result = await drain(call, checkpointBytes, 'verify', { max_records: 20 });
  assert.equal(result.body.done, true);
  assert.equal(result.body.missing, 0);
  assert.equal(result.body.mismatch, 0, 'canonical digests still prove content parity');
  assert.ok(result.body.source_digest);
  assert.ok(compact.length >= 350_000);
});
