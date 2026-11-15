// tests/settlement_cron_outcome.test.mjs (E2) -- the settlement Cron Trigger
// logs a structured outcome for every invocation: "invoked", then exactly one
// of "completed" (settled / no_op + reason) or "error" (re-thrown). These
// tests pin the legitimate no-op paths, the error path, the counts-only
// summary and that nothing private reaches the log -- and that settlement
// itself behaves exactly as before.
//
// Run: NODE_OPTIONS=--experimental-sqlite node --test tests/settlement_cron_outcome.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';

import worker, { settleAllBots, BOT_INDEX_KEY } from '../backend/index.js';
import { createCronOutcomeLog, classifyError, CRON_EVENT, ERROR_MESSAGES } from '../backend/cron_outcome.js';
import { createStorage } from '../backend/storage.js';
import { withD1 } from './helpers/test_env.mjs';

const BOT = 'crassus_private_name';
const CRON = '*/15 12-23 * * MON-FRI';
const SCHEDULED_MS = Date.parse('2026-11-13T21:15:00Z');

function indexBucket(members, ready = true) {
  const value = JSON.stringify({ schema_version: 1, ready, members: [...members].sort() });
  return {
    async get(key) {
      assert.equal(key, BOT_INDEX_KEY);
      return { etag: '1', json: async () => JSON.parse(value) };
    },
    async put() { throw new Error('the cron sweep never writes the bot index'); },
  };
}

function botRecord(trades, extra = {}) {
  return {
    username: BOT, is_bot: true, salt: 'S', hash: 'H', iterations: 100000,
    balance_cash: 54321, starting_balance: 50000, trades,
    createdAt: '2026-11-01T00:00:00Z', version: 1, ...extra,
  };
}

// An expired, deep-ITM short call (exp 2026-07-17) and an unexpired one.
const EXPIRED_SHORT = {
  execution_request_id: 'orig-1', sym: 'QQQ260717C00600000', side: 'sell', qty: 1,
  price: 5, strike: 600, type: 'call', exp: '2026-07-17',
  instrument_type: 'option', multiplier: 100, ts: '2026-07-17T12:00:00Z',
};
const FUTURE_LONG = {
  execution_request_id: 'orig-2', sym: 'QQQ991231C00600000', side: 'buy', qty: 1,
  price: 5, strike: 600, type: 'call', exp: '2099-12-31',
  instrument_type: 'option', multiplier: 100, ts: '2026-11-13T15:00:00Z',
};

async function envWith(records, { ready = true } = {}) {
  const env = withD1({ PAPER_TRADES: indexBucket(records.map(r => r.username), ready) });
  const storage = createStorage(env);
  for (const record of records) {
    await storage.users.insert(record.username, record);
    await storage.botMarkers.put(record.username, { username: record.username });
  }
  return env;
}

async function readRecord(env) {
  return createStorage(env).users.get(BOT);
}

// Capture console.log / console.error lines emitted during fn().
async function captureLogs(fn) {
  const lines = [];
  const realLog = console.log, realError = console.error;
  console.log = (...args) => lines.push({ level: 'log', text: args.join(' ') });
  console.error = (...args) => lines.push({ level: 'error', text: args.join(' ') });
  try {
    const value = await fn();
    return { lines, value };
  } finally {
    console.log = realLog;
    console.error = realError;
  }
}

function outcomeRecords(lines) {
  return lines.map(l => { try { return { ...JSON.parse(l.text), level: l.level }; } catch { return null; } })
    .filter(r => r?.event === CRON_EVENT);
}

async function withFetch(impl, fn) {
  const real = globalThis.fetch;
  globalThis.fetch = impl;
  try { return await fn(); } finally { globalThis.fetch = real; }
}

async function runScheduled(env) {
  const waits = [];
  const { lines } = await captureLogs(async () => {
    await worker.scheduled({ cron: CRON, scheduledTime: SCHEDULED_MS }, env, { waitUntil: p => waits.push(p) });
    for (const p of waits) await p.catch(() => {});
  });
  return { waits, lines, records: outcomeRecords(lines) };
}

function assertEnvelope(records) {
  assert.equal(records.length, 2, 'exactly one invoked and one terminal record');
  const [first, last] = records;
  assert.equal(first.phase, 'invoked');
  assert.ok(['completed', 'error'].includes(last.phase));
  assert.equal(first.run_id, last.run_id, 'the terminal record matches its invocation');
  for (const r of records) {
    assert.equal(r.cron, CRON);
    assert.equal(r.scheduled_time, '2026-11-13T21:15:00.000Z');
    assert.ok(!Number.isNaN(Date.parse(r.at)), 'every record is timestamped');
  }
  assert.ok(typeof last.duration_ms === 'number' && last.duration_ms >= 0);
  return last;
}

function assertNothingPrivate(lines) {
  const all = lines.map(l => l.text).join('\n');
  for (const secret of [BOT, '54321', 'QQQ260717C00600000', '"S"', 'orig-1']) {
    assert.ok(!all.includes(secret), `log output must not contain ${secret}`);
  }
}

test('legitimate no-op: bots with no expired positions (e.g. flattened 0DTE)', async () => {
  const env = await envWith([botRecord([FUTURE_LONG])]);
  const before = await readRecord(env);
  const { records, lines } = await withFetch(async () => { throw new Error('a no-op run must not fetch marks'); },
    () => runScheduled(env));
  const done = assertEnvelope(records);
  assert.equal(done.phase, 'completed');
  assert.equal(done.outcome, 'no_op');
  assert.equal(done.no_op_reason, 'no_expired_positions');
  assert.equal(done.accounts_indexed, 1);
  assert.equal(done.accounts_with_expired, 0);
  assert.equal(done.positions_settled, 0);
  assert.deepEqual(await readRecord(env), before, 'nothing is written');
  assertNothingPrivate(lines);
});

test('legitimate no-op: empty bot index', async () => {
  const env = await envWith([]);
  const { records } = await runScheduled(env);
  const done = assertEnvelope(records);
  assert.equal(done.outcome, 'no_op');
  assert.equal(done.no_op_reason, 'no_bot_accounts');
  assert.equal(done.accounts_indexed, 0);
});

test('legitimate no-op: closed bot accounts are ineligible', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT], { account_closed: true })]);
  const { records } = await runScheduled(env);
  const done = assertEnvelope(records);
  assert.equal(done.no_op_reason, 'no_expired_positions');
  assert.equal(done.accounts_ineligible, 1);
});

test('legitimate no-op: same-day expiration before the 16:15 ET cutoff', async () => {
  const env = await envWith([botRecord([{ ...EXPIRED_SHORT }])]);
  const summary = await withFetch(async () => { throw new Error('pre-cutoff must not fetch'); },
    () => settleAllBots(env, new Date('2026-07-17T20:14:00Z')));
  assert.equal(summary.no_op_reason, 'no_expired_positions');
  assert.equal(summary.as_of, '2026-07-17');
  assert.equal((await readRecord(env)).trades.length, 1);
});

test('no-op while the cutover fence is armed: no sweep is scheduled', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT])]);
  await env.DB.prepare('INSERT INTO migration_state (name, value) VALUES (?, ?)')
    .bind('fence', JSON.stringify({ armed: true })).run();
  const { records, waits, lines } = await runScheduled(env);
  assert.equal(waits.length, 0, 'settlement is still skipped while fenced');
  const done = assertEnvelope(records);
  assert.equal(done.outcome, 'no_op');
  assert.equal(done.no_op_reason, 'cutover_fence');
  assert.ok(lines.some(l => l.text.includes('settlement_skipped_cutover_fence')), 'the existing skip line is kept');
});

test('pending short with no mark: nothing_settleable, obligation left pending', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT])]);
  const { records } = await withFetch(async () => ({ ok: false, text: async () => '' }), () => runScheduled(env));
  const done = assertEnvelope(records);
  assert.equal(done.outcome, 'no_op');
  assert.equal(done.no_op_reason, 'nothing_settleable');
  assert.equal(done.accounts_with_expired, 1);
  assert.equal(done.marks_missing, 1);
  assert.equal(done.accounts_unchanged, 1);
  const record = await readRecord(env);
  assert.equal(record.trades.length, 1, 'a missing mark still does not forgive the short');
  assert.equal(record.balance_cash, 54321);
});

test('mark source throws: counted, obligation left pending', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT])]);
  const summary = await withFetch(async () => { throw new Error('network down'); },
    () => settleAllBots(env, new Date('2026-07-18T12:00:00Z')));
  assert.equal(summary.mark_errors, 1);
  assert.equal(summary.no_op_reason, 'nothing_settleable');
  assert.equal((await readRecord(env)).trades.length, 1);
});

const SECOND_SHORT = { ...EXPIRED_SHORT, execution_request_id: 'orig-3', sym: 'QQQ260717C00605000', strike: 605 };

test('settled outcome: counts match what was written', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT, SECOND_SHORT])]);
  const { records, lines } = await withFetch(
    async () => ({ ok: true, text: async () => 'UnderlyingPrice,Expiration\n610,2026-07-17\n' }),
    () => runScheduled(env));
  const done = assertEnvelope(records);
  assert.equal(done.outcome, 'settled');
  assert.equal(done.no_op_reason, null);
  assert.equal(done.marks_resolved, 1);
  assert.equal(done.accounts_settled, 1);
  assert.equal(done.positions_settled, 2);
  const record = await readRecord(env);
  assert.equal(record.trades.length, 4, 'two originals plus two settlement trades');
  assert.deepEqual(record.trades.slice(2).map(t => t.note),
    ['exercised against (assigned)', 'exercised against (assigned)']);
  assert.equal(record.balance_cash, 54321 - 10 * 100 - 5 * 100, 'settlement arithmetic unchanged');
  assertNothingPrivate(lines);
});

test('insolvent settlement: liquidation is a settled outcome, not a no-op', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT], { balance_cash: 100 })]);
  const { records, lines } = await withFetch(
    async () => ({ ok: true, text: async () => 'UnderlyingPrice,Expiration\n610,2026-07-17\n' }),
    () => runScheduled(env));
  const done = assertEnvelope(records);
  assert.equal(done.outcome, 'settled');
  assert.equal(done.no_op_reason, null);
  assert.equal(done.accounts_liquidated, 1);
  assert.equal(done.accounts_settled, 0);
  const record = await readRecord(env);
  assert.equal(record.account_closed, true, 'liquidation behavior unchanged');
  assert.equal(record.closure_reason, 'insolvent_settlement');
  assertNothingPrivate(lines);
});

test('error: logged with stage, then re-thrown unchanged', async () => {
  const env = await envWith([botRecord([])], { ready: false });
  const { records, waits, lines } = await runScheduled(env);
  assert.equal(waits.length, 1);
  await assert.rejects(waits[0], /migration is not ready/, 'the invocation still fails as before');
  const done = assertEnvelope(records);
  assert.equal(done.phase, 'error');
  assert.equal(done.level, 'error', 'errors go to console.error');
  assert.equal(done.stage, 'settle_all_bots');
  assert.equal(done.error_category, 'bot_index_not_ready');
  assert.equal(done.error_class, 'Error');
  assert.equal(done.error_message, ERROR_MESSAGES.bot_index_not_ready, 'a fixed message, not the error text');
  assert.equal(done.outcome, undefined);
  assertNothingPrivate(lines);
});

// Adversarial: a stored account record that no longer parses. JSON.parse's
// SyntaxError quotes part of the record, which is exactly what must never
// reach the log -- truncation would not stop it.
const SENSITIVE = ['SECRETHASH9f8e7d', 'hunter2-password', 'tok_live_ABC123', 'hunter2', BOT];

test('adversarial: corrupt stored record -- parse error text never logged, original re-thrown', async () => {
  const env = await envWith([botRecord([EXPIRED_SHORT])]);
  // V8 quotes the text around an unexpected token: ..."password":hunter2}"
  const leaky = `{"username":"${BOT}","is_bot":true,"password":hunter2}`;
  env.DB.db.prepare('UPDATE users SET record = ? WHERE username = ?').run(leaky, BOT);
  // Precondition: the raw error really would leak if logged.
  const direct = await createStorage(env).users.get(BOT).then(() => null, e => e);
  assert.ok(direct instanceof SyntaxError, 'the store raises the raw parse error');
  assert.ok(SENSITIVE.some(x => direct.message.includes(x)), 'and its message quotes the record');

  const { records, waits, lines } = await runScheduled(env);
  const thrown = await waits[0].then(() => null, e => e);
  assert.ok(thrown instanceof SyntaxError, 'the original error is re-thrown unchanged');
  assert.equal(thrown.message, direct.message);
  const done = assertEnvelope(records);
  assert.equal(done.phase, 'error');
  assert.equal(done.error_category, 'unclassified');
  assert.equal(done.error_class, 'SyntaxError');
  assert.equal(done.error_message, ERROR_MESSAGES.unclassified);
  const all = lines.map(l => l.text).join('\n');
  for (const secret of SENSITIVE) assert.ok(!all.includes(secret), `log output must not contain ${secret}`);
  assert.ok(!all.includes(direct.message.slice(0, 20)), 'no fragment of the parse error message');
});

test('adversarial: hostile error name/message/shape are all withheld', () => {
  const custom = new Error(`lookup failed for ${BOT} hash=${SENSITIVE[0]}`);
  custom.name = `Leak ${SENSITIVE[2]}`;
  class AccountError extends Error {}
  const subclass = new AccountError(`password=${SENSITIVE[1]}`);
  const lookalike = new Error('Bot membership migration is not ready ' + SENSITIVE[0]);
  for (const thrown of [custom, subclass, lookalike, `raw string ${SENSITIVE[1]}`, { message: SENSITIVE[0], name: 'Error' }, null]) {
    const lines = [];
    const log = createCronOutcomeLog({}, { log: l => lines.push(l), logError: l => lines.push(l) });
    log.error('settle_all_bots', thrown);
    const record = JSON.parse(lines[0]);
    assert.equal(record.error_category, 'unclassified', 'only an exact known message classifies');
    assert.ok(['Error', 'other'].includes(record.error_class));
    assert.equal(record.error_message, ERROR_MESSAGES.unclassified);
    for (const secret of SENSITIVE) assert.ok(!lines[0].includes(secret), `must not contain ${secret}`);
  }
  assert.equal(classifyError(custom).error_class, 'other', 'a renamed error class is not echoed');
  assert.equal(classifyError(subclass).error_class, 'other', 'custom subclasses are not named');
  for (const [message, category] of [['Indexed settlement account unavailable', 'settlement_account_unavailable'],
    ['Invalid bot index mode', 'bot_index_mode_invalid'], ['Invalid bot membership index', 'bot_index_invalid'],
    ['Invalid bot membership', 'bot_index_invalid'], ['Bot closure could not be persisted', 'liquidation_not_persisted']]) {
    assert.equal(classifyError(new Error(message)).error_category, category);
  }
});

test('outcome log: summary is whitelisted, messages bounded, one terminal record', () => {
  const lines = [];
  const clock = [1000, 1250];
  const log = createCronOutcomeLog({ cron: CRON, scheduledTime: SCHEDULED_MS },
    { now: () => clock.shift() ?? 9999, log: l => lines.push(l), logError: l => lines.push(l), runId: 'r1' });
  log.invoked();
  log.completed({ accounts_settled: 2, positions_settled: 3, username: BOT, balance_cash: 54321,
    trades: [EXPIRED_SHORT], no_op_reason: null });
  log.error('late', new Error('x'));   // ignored: already terminal
  assert.equal(lines.length, 2);
  const done = JSON.parse(lines[1]);
  assert.deepEqual(Object.keys(done).sort(), ['accounts_settled', 'at', 'cron', 'duration_ms', 'event',
    'no_op_reason', 'outcome', 'phase', 'positions_settled', 'run_id', 'scheduled_time'].sort());
  assert.equal(done.duration_ms, 250);
  assert.equal(done.outcome, 'settled');

  const errLines = [];
  const errLog = createCronOutcomeLog({}, { log: l => errLines.push(l), logError: l => errLines.push(l) });
  errLog.error('settle_all_bots', new Error('y'.repeat(500)));
  const err = JSON.parse(errLines[0]);
  assert.deepEqual(Object.keys(err).sort(), ['at', 'cron', 'duration_ms', 'error_category', 'error_class',
    'error_message', 'event', 'phase', 'run_id', 'scheduled_time', 'stage'].sort());
  assert.ok(!errLines[0].includes('yyyy'), 'error text is never forwarded');
  for (const fixed of Object.values(ERROR_MESSAGES)) assert.ok(fixed.length <= 60, 'fixed messages are short');
  assert.equal(err.cron, null);
  assert.equal(err.scheduled_time, null);
});
