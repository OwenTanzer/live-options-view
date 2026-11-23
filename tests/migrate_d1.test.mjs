// tests/migrate_d1.test.mjs -- the KV -> D1 cutover machinery (#208).
//
// Real SQL (SQLite via node:sqlite) behind a D1-shaped adapter, and a fake KV
// namespace with Workers KV's list/cursor shape. What is exercised here is what
// an operator would actually rely on: bounded per-request work, resumable
// cursors, parity that is not fooled by equal counts, conflict detection at
// equal versions, deletion and revocation convergence under the fence, and a
// rollback that refuses to promise what exhausted KV writes cannot deliver.

import test from 'node:test';
import assert from 'node:assert/strict';
import { makeStorage, FakeKV } from './helpers/d1_shim.mjs';
import {
  handleMigration, canonicalJson, recordDigest, cutoverFenceActive,
} from '../backend/migrate.js';
import { hashSessionToken } from '../backend/storage.js';
import { SESSION_TTL_SECONDS } from '../backend/common.js';

const TOKEN = 'migration-token';

function userRecord(username, version, balance, extra = {}) {
  return {
    username, salt: 'c2FsdA==', hash: 'aGFzaA==', iterations: 100000,
    balance_cash: balance, trades: [{ execution_request_id: `req-${username}-${version}` }],
    createdAt: '2026-11-01T00:00:00.000Z', version, is_bot: true, alias: username,
    strategy_id: 'reddit_sentiment_qqq', ...extra,
  };
}

function harness({ drain = '0', stable = '0' } = {}) {
  const d1 = makeStorage();
  const kv = new FakeKV();
  const env = {
    DB: d1, USERS: kv, SESSIONS: kv, MIGRATION_TOKEN: TOKEN,
    // Tests compress the drain window; production defaults to 75s.
    CUTOVER_DRAIN_SECONDS: drain,
    CUTOVER_STABLE_SECONDS: stable,
  };
  const call = async (phase, extra = {}) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST',
      headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phase, ...extra }),
    }), env);
    return { status: response.status, body: await response.json() };
  };
  const putUser = async (name, record) => kv.put(`user:${name}`, JSON.stringify(record));
  const insertD1 = (name, record, version) => d1.db.prepare(
    'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?)',
  ).run(name.toLowerCase(), JSON.stringify(record), version, new Date().toISOString());
  const userCount = () => d1.raw('SELECT COUNT(*) AS n FROM users')[0].n;
  const sessionCount = () => d1.raw('SELECT COUNT(*) AS n FROM sessions')[0].n;
  return { d1, kv, env, call, putUser, insertD1, userCount, sessionCount };
}

/** Drive a phase to completion with bounded calls, returning the last body. */
async function drain(call, phase, extra = {}, limit = 50) {
  let last;
  for (let i = 0; i < limit; i++) {
    last = await call(phase, extra);
    if (last.status !== 200 || last.body.done) return last;
  }
  throw new Error(`${phase} did not finish within ${limit} bounded calls`);
}

test('the route is invisible without the token', async () => {
  const { env } = harness();
  const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
    method: 'POST', body: JSON.stringify({ phase: 'users' }),
  }), env);
  assert.equal(response.status, 404);
  const unconfigured = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
    method: 'POST', headers: { 'X-Migration-Token': TOKEN },
    body: JSON.stringify({ phase: 'users' }),
  }), { ...env, MIGRATION_TOKEN: undefined });
  assert.equal(unconfigured.status, 404);
  assert.equal(await cutoverFenceActive({ ...env, DB: undefined }), false);
});

test('copy is bounded per request and resumes with an exact cursor', async () => {
  const { kv, call, userCount } = harness();
  for (let i = 0; i < 45; i++) {
    await kv.put(`user:bot_${String(i).padStart(3, '0')}`, JSON.stringify(userRecord(`bot_${i}`, 1, i)));
  }
  const first = await call('users', { max_records: 20 });
  assert.equal(first.status, 200);
  assert.ok(first.body.processed_this_call > 0 && first.body.processed_this_call <= 20,
    `bounded by the record budget, got ${first.body.processed_this_call}`);
  assert.equal(first.body.done, false);
  assert.equal(first.body.continuation, true);
  assert.ok(first.body.queries_this_call <= 40, 'query budget respected');

  const second = await call('users', { max_records: 20 });
  assert.ok(second.body.processed_this_call > 0);
  assert.equal(second.body.done, false);

  const third = await call('users', { max_records: 20 });
  assert.equal(third.body.done, true);
  assert.equal(userCount(), 45);
});

test('an interrupted copy resumes without loss or an unbounded rescan', async () => {
  const { d1, kv, call } = harness();
  for (let i = 0; i < 25; i++) {
    await kv.put(`user:bot_${String(i).padStart(3, '0')}`, JSON.stringify(userRecord(`bot_${i}`, 1, i)));
  }
  d1.flags.failAfterRuns = 12;
  await assert.rejects(() => call('users', { max_records: 20 }));
  const checkpoint = JSON.parse(d1.raw("SELECT value FROM migration_state WHERE name = 'phase:users'")[0].value);
  assert.equal(checkpoint.done, false);
  assert.ok(checkpoint.offset > 0 || checkpoint.cursor !== null, 'progress is checkpointed');

  d1.flags.failAfterRuns = null;
  const finished = await drain(call, 'users', { max_records: 20 });
  assert.equal(finished.body.done, true);
  assert.equal(d1.raw('SELECT COUNT(*) AS n FROM users')[0].n, 25);
});

test('verify compares identity and content, not counts', async () => {
  const { kv, call, insertD1 } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 3, 100)));
  // Equal counts, different account: D1 has beta, KV has alpha.
  insertD1('beta', userRecord('beta', 3, 100), 3);

  assert.equal((await call('fence', { action: 'arm' })).status, 200);
  const verified = await drain(call, 'verify');
  assert.equal(verified.body.missing, 1, 'alpha is missing from D1');
  assert.equal(verified.body.extra, 1, 'beta does not exist in KV');
  assert.equal(verified.body.mismatch_total, 2);

  // Redaction: counts only, never a username.
  const serialised = JSON.stringify(verified.body);
  assert.ok(!serialised.includes('alpha'));
  assert.ok(!serialised.includes('beta'));

  const finalized = await call('finalize');
  assert.equal(finalized.status, 409);
  assert.ok(finalized.body.missing_phases.includes('sessions'),
    'finalize fails closed on the phases that have not run yet');
});

test('verify detects an altered record at the same version', async () => {
  const { kv, call, insertD1 } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 3, 100)));
  insertD1('alpha', userRecord('alpha', 3, 999999), 3);

  await call('fence', { action: 'arm' });
  const verified = await drain(call, 'verify');
  assert.equal(verified.body.mismatch, 1);
  assert.equal(verified.body.missing, 0);
  assert.equal(verified.body.extra, 0);
});

test('finalize refuses until every required phase is complete', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  // sessions, markers, reconcile and verify are missing.
  const finalized = await call('finalize');
  assert.equal(finalized.status, 409);
  for (const phase of ['sessions', 'bot_markers', 'reconcile', 'verify', 'stabilize']) {
    assert.ok(finalized.body.missing_phases.includes(phase), phase);
  }
});

test('reconcile and verify refuse to run before the fence is armed', async () => {
  const { call } = harness();
  assert.equal((await call('verify')).status, 409);
  assert.equal((await call('reconcile')).status, 409);
});

test('the full path finalizes only with parity and the fence armed', async () => {
  const { kv, call, userCount } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 2, 10)));
  await kv.put('user:beta', JSON.stringify(userRecord('beta', 4, 20)));
  await kv.put('bot:alpha', JSON.stringify({ username: 'alpha' }));
  await kv.put('sess:token-1', JSON.stringify({ username: 'alpha', createdAt: new Date(Date.now() - 60_000).toISOString() }));

  assert.equal((await call('finalize')).status, 409, 'fence first');
  assert.equal((await call('fence', { action: 'arm' })).status, 200);
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  const verified = await drain(call, 'verify');
  assert.equal(verified.body.mismatch_total, 0);
  await drain(call, 'stabilize');
  const finalized = await call('finalize');
  assert.equal(finalized.status, 200);
  assert.equal(finalized.body.status, 'finalized');
  assert.equal(userCount(), 2);

  const status = await call('status');
  assert.ok(status.body.cutover.finalized_at);
  assert.equal(status.body.cutover.epoch, status.body.fence.epoch);
  assert.equal(status.body.checkpoints['phase:verify'].mismatch_total, 0);
});

test('a same-version conflict is detected and blocks finalize', async () => {
  const { kv, call, insertD1 } = harness();
  insertD1('alpha', userRecord('alpha', 3, 999), 3);
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 3, 111)));

  const copied = await call('users');
  assert.equal(copied.body.conflicts, 1, 'same version, different content is a conflict');
  assert.equal(copied.body.written, 0);

  await call('fence', { action: 'arm' });
  await drain(call, 'reconcile');
  await drain(call, 'verify');
  const finalized = await call('finalize');
  assert.equal(finalized.status, 409);
});

test('a d1-ahead record means a write outside the single-writer window', async () => {
  const { kv, call, insertD1 } = harness();
  insertD1('alpha', userRecord('alpha', 5, 500), 5);
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 2, 200)));
  const copied = await call('users');
  assert.equal(copied.body.d1_ahead, 1);
  assert.equal(copied.body.written, 0);
});

test('an account deleted in KV is deleted in D1 under the fence', async () => {
  const { kv, call, userCount } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await drain(call, 'users');
  assert.equal(userCount(), 1);

  await kv.delete('user:alpha');       // settlement liquidation in the legacy store
  await call('fence', { action: 'arm' });
  await drain(call, 'users', { restart: true });
  const reconciled = await drain(call, 'reconcile');
  assert.equal(reconciled.body.deleted, 1);
  assert.equal(userCount(), 0);
});

test('a revoked session is not resurrected by a repeated copy', async () => {
  const { kv, call, sessionCount } = harness();
  const created = new Date(Date.now() - 60_000).toISOString();
  await kv.put('sess:legacy-token', JSON.stringify({ username: 'alpha', createdAt: created }));
  await drain(call, 'sessions');
  assert.equal(sessionCount(), 1);

  await kv.delete('sess:legacy-token');   // logout or settlement revocation
  await call('fence', { action: 'arm' });
  await drain(call, 'sessions', { restart: true });
  const reconciled = await drain(call, 'reconcile');
  assert.equal(reconciled.body.deleted, 1);
  assert.equal(sessionCount(), 0);
});

test('sessions verify catches a wrong owner and a wrong expiry', async () => {
  const { d1, kv, call } = harness();
  const created = new Date(Date.now() - 60_000).toISOString();
  await kv.put('sess:tok', JSON.stringify({ username: 'alpha', createdAt: created }));
  await drain(call, 'sessions');
  const hash = await hashSessionToken('tok');
  d1.db.prepare('UPDATE sessions SET username = ?, expires_at = ? WHERE token_hash = ?')
    .run('beta', 1, hash);

  await call('fence', { action: 'arm' });
  const verified = await drain(call, 'verify');
  assert.equal(verified.body.mismatch, 1);
});

test('reverse refuses to promise a rollback while KV writes are exhausted', async () => {
  const { d1, kv, call, insertD1 } = harness();
  insertD1('alpha', userRecord('alpha', 7, 700), 7);
  const failingKv = {
    get: (...args) => kv.get(...args),
    put: async () => { throw new Error('KV put() limit exceeded for the day'); },
    delete: (...args) => kv.delete(...args),
    list: (...args) => kv.list(...args),
  };
  const env = { DB: d1, USERS: failingKv, SESSIONS: failingKv, MIGRATION_TOKEN: TOKEN };
  const callFailing = async (phase, extra = {}) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST', headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phase, ...extra }),
    }), env);
    return { status: response.status, body: await response.json() };
  };

  const probe = await callFailing('reverse_preflight');
  assert.equal(probe.body.available, false);

  const refused = await callFailing('reverse_users');
  assert.equal(refused.status, 409);
  assert.match(refused.body.recovery, /D1 is the rollback floor/);
  assert.equal(d1.raw('SELECT COUNT(*) AS n FROM users')[0].n, 1, 'D1 data untouched by a refused reverse');

  const forced = await callFailing('reverse_users', { force: true });
  assert.equal(forced.status, 503);
  assert.equal(forced.body.error, 'kv_write_failed_midway');
  assert.equal(d1.raw('SELECT COUNT(*) AS n FROM users')[0].n, 1, 'a failed reverse never discards D1 rows');
});

test('reverse writes only when D1 is newer, once KV writes are available', async () => {
  const { kv, call, insertD1 } = harness();
  insertD1('alpha', userRecord('alpha', 5, 500), 5);
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 2, 200)));
  await kv.put('user:gone', JSON.stringify(userRecord('gone', 1, 10)));

  const probe = await call('reverse_preflight');
  assert.equal(probe.body.available, true);
  const reversed = await call('reverse_users');
  assert.equal(reversed.status, 200);
  assert.equal(reversed.body.written, 1);
  assert.equal(JSON.parse(await kv.get('user:alpha')).version, 5);

  const again = await call('reverse_users', { restart: true });
  assert.equal(again.body.written, 0);
});

test('disarming after finalize is allowed but annotated', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  await drain(call, 'verify');
  await drain(call, 'stabilize');
  assert.equal((await call('finalize')).status, 200);
  const disarmed = await call('fence', { action: 'disarm' });
  assert.equal(disarmed.status, 200);
  assert.match(disarmed.body.note, /switch revision is live/);
});

test('canonical digests are stable under key order and sensitive to content', async () => {
  const a = { username: 'alpha', balance_cash: 1, nested: { b: 2, a: 1 } };
  const b = { nested: { a: 1, b: 2 }, balance_cash: 1, username: 'alpha' };
  assert.equal(canonicalJson(a), canonicalJson(b));
  assert.equal(await recordDigest(a), await recordDigest(b));
  assert.notEqual(await recordDigest(a), await recordDigest({ ...a, balance_cash: 2 }));
});

test('the fence stops mutating routes and the cron sweep, and reads still flow', async () => {
  const { default: worker } = await import('../backend/index.js');
  const { env, call } = harness();
  const post = (path, body) => worker.fetch(new Request(`https://worker.test${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }), env);

  assert.equal((await post('/api/login', { username: 'nobody', password: 'x' })).status, 401,
    'unfenced writes reach the handler');

  await call('fence', { action: 'arm' });
  const blocked = await post('/api/login', { username: 'nobody', password: 'x' });
  assert.equal(blocked.status, 503);
  assert.match(blocked.headers.get('Retry-After'), /\d+/);

  // Reads are never fenced: a session check still answers as a session check.
  const read = await worker.fetch(new Request('https://worker.test/api/me'), env);
  assert.equal(read.status, 401);

  // The cron sweep is a writer too, so it must not run during the window.
  const waits = [];
  await worker.scheduled({ scheduledTime: Date.now() }, env, { waitUntil: p => waits.push(p) });
  assert.equal(waits.length, 0, 'settlement is skipped while fenced');

  await call('fence', { action: 'disarm' });
  assert.equal((await post('/api/login', { username: 'nobody', password: 'x' })).status, 401,
    'writes resume after disarm');
});

test('sessions expire: an expired legacy session is not copied', async () => {
  const { kv, call, sessionCount } = harness();
  const old = new Date(Date.now() - (SESSION_TTL_SECONDS + 3600) * 1000).toISOString();
  await kv.put('sess:stale', JSON.stringify({ username: 'alpha', createdAt: old }));
  const copied = await call('sessions');
  assert.equal(copied.body.written, 0);
  assert.equal(sessionCount(), 0);
});


test('the fence fails closed when its state cannot be read', async () => {
  const { default: worker } = await import('../backend/index.js');
  const post = (env) => worker.fetch(new Request('https://worker.test/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'nobody', password: 'x' }),
  }), env);

  // A throwing binding is an outage, not a reason to reopen writes.
  const broken = { DB: { prepare() { throw new Error('D1 unavailable'); } } };
  assert.equal((await post(broken)).status, 503);

  // A malformed fence row is equally unsafe.
  const d1 = makeStorage();
  d1.db.prepare('INSERT INTO migration_state (name, value) VALUES (?, ?)')
    .run('fence', '{"armed":');
  const malformed = { DB: d1 };
  assert.equal((await post(malformed)).status, 503);

  // An explicit disarm is the only thing that reopens writes; the request then
  // reaches the handler (whatever it answers) instead of being fenced.
  const open = makeStorage();
  open.db.prepare('INSERT INTO migration_state (name, value) VALUES (?, ?)')
    .run('fence', JSON.stringify({ armed: false, epoch: null }));
  assert.equal((await post({ DB: open, USERS: { get: async () => null }, SESSIONS: { get: async () => null } })).status, 401,
    'an unarmed fence does not block the route');
});

test('a pre-arm copy cannot satisfy finalize, and a new epoch invalidates old results', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));

  // Pre-copy before the fence exists...
  await drain(call, 'users');
  // ...then arm: the epoch changes and every phase result is discarded.
  await call('fence', { action: 'arm' });
  const stale = await call('finalize');
  assert.equal(stale.status, 409);
  assert.ok(stale.body.missing_phases.includes('users'));
  assert.ok(stale.body.missing_phases.includes('sessions'));

  // Build a complete, current-epoch result set.
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  await drain(call, 'verify');
  await drain(call, 'stabilize');
  assert.equal((await call('finalize')).status, 200);

  // Re-arm creates a new epoch; the previous verify/stabilize are stale.
  await call('fence', { action: 'disarm' });
  await call('fence', { action: 'arm' });
  const rearmed = await call('finalize');
  assert.equal(rearmed.status, 409);
  assert.ok(rearmed.body.missing_phases.includes('verify'));
});

test('finalize refuses until the drain window has elapsed', async () => {
  const { kv, call } = harness({ drain: '75' });
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  await drain(call, 'verify');
  const early = await call('stabilize');
  assert.ok(early.body.too_early_ms > 0, 'stabilize waits out the drain window');
  assert.equal(early.body.done, false);
  const finalized = await call('finalize');
  assert.equal(finalized.status, 409);
  assert.match(finalized.body.error, /drain window/);
});

test('stabilize detects a late legacy write between two scans', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await call('fence', { action: 'arm' });

  const first = await call('stabilize');
  assert.equal(first.body.stable, false);
  assert.ok(first.body.pass >= 1);

  // A write that slipped in after the fence armed (in-flight request, or KV
  // propagation catching up) changes the digest of the next pass.
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 2, 11)));
  const second = await call('stabilize');
  assert.equal(second.body.stable, false, 'a changed scan is not stable');

  const third = await call('stabilize');
  assert.equal(third.body.stable, true, 'two identical scans are the evidence');
});

test('status exposes counts and epochs only', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await kv.put('sess:secret-token', JSON.stringify({ username: 'alpha', createdAt: new Date().toISOString() }));
  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'verify');

  const status = await call('status');
  const text = JSON.stringify(status.body);
  assert.ok(!text.includes('alpha'), 'no account identity in status');
  assert.ok(!text.includes('secret-token'), 'no session token in status');
  assert.ok(status.body.fence.epoch, 'epoch is exposed because operators need it');
});

test('reverse retries a row whose KV put failed instead of skipping it', async () => {
  const { d1, kv, insertD1 } = harness();
  insertD1('alpha', userRecord('alpha', 5, 500), 5);
  insertD1('beta', userRecord('beta', 5, 600), 5);
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 100)));
  await kv.put('user:beta', JSON.stringify(userRecord('beta', 1, 100)));

  let failAlpha = true;
  const flaky = {
    get: (...args) => kv.get(...args),
    list: (...args) => kv.list(...args),
    delete: (...args) => kv.delete(...args),
    put: async (key, value) => {
      if (key === 'user:alpha' && failAlpha) throw new Error('KV put() limit exceeded for the day');
      return kv.put(key, value);
    },
  };
  const env = { DB: d1, USERS: flaky, SESSIONS: flaky, MIGRATION_TOKEN: TOKEN };
  const call = async (phase, extra = {}) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST', headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phase, ...extra }),
    }), env);
    return { status: response.status, body: await response.json() };
  };

  await call('reverse_preflight');
  const failed = await call('reverse_users');
  assert.equal(failed.status, 503);
  assert.equal(failed.body.error, 'kv_write_failed_midway');
  assert.equal(failed.body.written, 0, 'the failed row was not counted as written');
  const checkpoint = JSON.parse(
    d1.raw("SELECT value FROM migration_state WHERE name = 'phase:reverse_users'")[0].value,
  );
  assert.equal(checkpoint.cursor, null, 'cursor did not advance past the failed row');

  failAlpha = false;
  const resumed = await call('reverse_users');
  assert.equal(resumed.status, 200);
  assert.equal(JSON.parse(await kv.get('user:alpha')).version, 5, 'the failed row is retried');
  assert.equal(JSON.parse(await kv.get('user:beta')).version, 5);
});

test('reverse honours max_records across the whole invocation', async () => {
  const { kv, call, insertD1 } = harness();
  for (let i = 0; i < 9; i++) {
    insertD1(`bot_${i}`, userRecord(`bot_${i}`, 5, 500), 5);
    await kv.put(`user:bot_${i}`, JSON.stringify(userRecord(`bot_${i}`, 1, 100)));
  }
  await call('reverse_preflight');
  const first = await call('reverse_users', { max_records: 3 });
  assert.ok(first.body.scanned <= 3, `whole-invocation bound, got ${first.body.scanned}`);
  assert.equal(first.body.done, false);

  let guard = 0;
  let last = first;
  while (!last.body.done && guard < 10) {
    last = await call('reverse_users', { max_records: 3 });
    guard += 1;
  }
  assert.equal(last.body.done, true);
  for (let i = 0; i < 9; i++) {
    assert.equal(JSON.parse(await kv.get(`user:bot_${i}`)).version, 5);
  }
});

test('REVIEW: a late legacy write after verify cannot ride a stable snapshot', async () => {
  const { d1, kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));

  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  await drain(call, 'verify');            // parity proven against alpha v1

  // The write the fence did not stop: accepted before arm, visible now.
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 2, 11)));

  // Stabilization is satisfied: both scans see the same new source.
  const stable = await drain(call, 'stabilize');
  assert.equal(stable.body.stable, true);

  const finalized = await call('finalize');
  assert.equal(finalized.status, 409, 'D1 is still v1; this must not finalize');
  assert.match(finalized.body.error, /different source snapshot|parity/);

  // Repair path: copy the newer record, reconcile, verify, stabilize, finalize.
  for (const phase of ['users', 'reconcile', 'verify', 'stabilize']) {
    await call(phase, { restart: true });
    await drain(call, phase);
  }
  const repaired = await call('finalize');
  assert.equal(repaired.status, 200);
  const stored = JSON.parse(
    d1.raw('SELECT record FROM users WHERE username = ?', 'alpha')[0].record,
  );
  assert.equal(stored.balance_cash, 11);
  assert.equal(stored.version, 2);
});

test('postcheck detects a legacy write that only became visible after finalize', async () => {
  const { kv, call } = harness();
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));

  assert.equal((await call('postcheck')).status, 200);
  assert.equal((await call('postcheck')).body.error, 'not_finalized');

  await call('fence', { action: 'arm' });
  await drain(call, 'users');
  await drain(call, 'sessions');
  await drain(call, 'bot_markers');
  await drain(call, 'reconcile');
  await drain(call, 'verify');
  await drain(call, 'stabilize');
  assert.equal((await call('finalize')).status, 200);

  const clean = await call('postcheck');
  assert.equal(clean.body.matches, true);
  assert.equal(clean.body.error, null);

  // A write whose visibility arrived after finalize.
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 9, 900)));
  const dirty = await call('postcheck', { restart: true });
  assert.equal(dirty.body.matches, false);
  assert.equal(dirty.body.error, 'legacy_source_changed_after_finalize');
});

test('stabilize is not satisfied by two immediate identical scans when a quiet window is required', async () => {
  const { kv, call } = harness({ stable: '120' });
  await kv.put('user:alpha', JSON.stringify(userRecord('alpha', 1, 10)));
  await call('fence', { action: 'arm' });

  const first = await call('stabilize');
  assert.equal(first.body.stable, false);
  const second = await call('stabilize');
  assert.equal(second.body.stable, false, 'identical but the quiet window has not elapsed');
  assert.ok(second.body.stable_wait_ms > 0);
});
