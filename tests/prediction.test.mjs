import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { marketKey, positionKey, quoteEvent, compareEvent } from '../backend/prediction-model.js';
import { predictionNamespace, handlePrediction, resolvePredictionRecord, settlePredictions } from '../backend/prediction.js';
import { handlePaperTrade } from '../backend/paper-trade.js';
import { computeBookFromTrades } from '../backend/common.js';
import { Collector, replay, fixed } from '../market_data/prediction/collector.mjs';
import { withD1, seedAccounts, seedSession } from './helpers/test_env.mjs';
import { createStorage } from '../backend/storage.js';
import worker from '../backend/index.js';

const now = Date.now(), iso = n => new Date(n).toISOString();
import { fixture } from './fixtures/prediction.mjs';

class Bucket {
  data = new Map(); version = 0; fail = false;
  async get(key) { const item = this.data.get(key); return item ? { etag: item.etag, json: async () => JSON.parse(item.body), text: async () => item.body } : null; }
  async put(key, body, opts = {}) {
    if (this.fail) throw Error('outage');
    const old = this.data.get(key);
    if (opts.onlyIf?.etagDoesNotMatch === '*' && old || opts.onlyIf?.etagMatches && old?.etag !== opts.onlyIf.etagMatches) return null;
    const item = { body, etag: String(++this.version) }; this.data.set(key, item); return { etag: item.etag };
  }
}
async function setup() {
  const bucket = new Bucket(), m = fixture(now);
  const env = withD1({ PAPER_TRADES: new Bucket(), PREDICTION_DATA: bucket, ENVIRONMENT: 'preview', ISOLATION: 'disposable', PREDICTION_ENVIRONMENT: 'fixtures', PREDICTION_DISPLAY: 'enabled', PREDICTION_ENTRIES: 'enabled' });
  await bucket.put('prediction/v1/fixtures/current.json', JSON.stringify({ schema: 'prediction.catalog.v1', markets: [m] }));
  await seedAccounts(env, [{ username: 'tester', trades: [], balance_cash: 100 }]);
  await seedSession(env, 'token', 'tester');
  const order = async (side = 'buy', outcome = 'YES', qty = 3, id = randomUUID()) => {
    const response = await handlePaperTrade(new Request('http://localhost:8787/api/paper-trade', { method: 'POST', headers: { Cookie: 'session=token', Origin: 'http://localhost:8787', 'Content-Type': 'application/json' }, body: JSON.stringify({ execution_request_id: id, sym: positionKey(m.rules, outcome), instrument_type: 'prediction', side, qty }) }), env);
    return { status: response.status, body: await response.json() };
  };
  return { env, m, order, record: () => createStorage(env).users.get('tester') };
}
function resolution(m, result = 'YES', revision = 1) {
  return { id: `official-${revision}`, revision, official: true, source: 'synthetic://official', result, market_key: marketKey(m.rules), rule_version: m.rules.version, receive_ts: m.rules.cutoff, exchange_ts: m.rules.cutoff };
}
test('actual authenticated ledger: buy 3 YES, replay, resolve, restart, correction', async () => {
  const { env, m, order, record } = await setup(); const id = randomUUID();
  const fill = await order('buy', 'YES', 3, id);
  assert.equal(fill.status, 201); assert.equal(fill.body.price, .45); assert.equal(fill.body.balance_cash, 98.65);
  assert.equal((await order('buy', 'YES', 3, id)).status, 200);
  assert.equal((await order('buy', 'NO', 3, id)).status, 409);
  assert.equal((await record()).trades.length, 1);
  const when = new Date(m.rules.cutoff);
  await env.PREDICTION_DATA.put('prediction/v1/fixtures/resolutions.json', JSON.stringify([resolution(m)]));
  await settlePredictions(env, when); await settlePredictions({ ...env }, when);
  assert.equal((await record()).balance_cash, 101.65); assert.equal((await record()).trades.length, 2);
  await env.PREDICTION_DATA.put('prediction/v1/fixtures/resolutions.json', JSON.stringify([resolution(m), resolution(m, 'NO', 2)]));
  await settlePredictions(env, when); await settlePredictions(env, when);
  assert.equal((await record()).balance_cash, 98.65); assert.equal((await record()).trades.length, 3);
  assert.equal(computeBookFromTrades((await record()).trades)[positionKey(m.rules, 'YES')].pos, 0);
});
test('NO purchase, early exit, competing closes, fees and insufficient funds', async () => {
  const { env, m, order, record } = await setup();
  m.rules.fee_units = 100;
  await env.PREDICTION_DATA.put('prediction/v1/fixtures/current.json', JSON.stringify({ schema: 'prediction.catalog.v1', markets: [m] }));
  assert.equal((await order('buy', 'NO')).body.balance_cash, 98.17);
  const closes = await Promise.all([order('sell', 'NO'), order('sell', 'NO')]);
  assert.equal(closes.filter(r => r.status === 201).length, 1);
  assert.equal((await record()).balance_cash, 99.79);
  await createStorage(env).users.mutate('tester', r => ({ record: { ...r, balance_cash: 0 }, result: {} }));
  assert.equal((await order()).status, 400);
});
test('crash after account mutation recovers the original fill without a new debit', async () => {
  const { env, order, record } = await setup();
  const put = env.PAPER_TRADES.put.bind(env.PAPER_TRADES);
  let failed = false;
  env.PAPER_TRADES.put = async (key, body, opts) => {
    if (!failed && JSON.parse(body).status === 'filled') { failed = true; throw Error('Lost finalization'); }
    return put(key, body, opts);
  };
  const id = randomUUID();
  assert.equal((await order('buy', 'YES', 3, id)).status, 503);
  env.PREDICTION_ENTRIES = 'disabled';
  assert.equal((await order('buy', 'YES', 3, id)).status, 200);
  assert.equal((await record()).trades.length, 1); assert.equal((await record()).balance_cash, 98.65);
});
test('partial exit then loss pays only held quantity; closed account still resolves', async () => {
  const { env, m, order, record } = await setup();
  await order(); await order('sell', 'YES', 1);
  await createStorage(env).users.mutate('tester', r => ({ record: { ...r, account_closed: true }, result: {} }));
  await env.PREDICTION_DATA.put('prediction/v1/fixtures/resolutions.json', JSON.stringify([resolution(m, 'NO')]));
  await settlePredictions(env, new Date(m.rules.cutoff));
  const r = await record(); assert.equal(r.trades.at(-1).qty, 2); assert.equal(r.trades.at(-1).price, 0);
  assert.equal(r.balance_cash, 99.05); assert.equal(r.account_closed, true);
});
test('weekend cron settles human event holdings without invoking the equity roster', async () => {
  const { env, m, order, record } = await setup(); await order();
  await env.PREDICTION_DATA.put('prediction/v1/fixtures/resolutions.json', JSON.stringify([resolution(m)]));
  const saturday = new Date(m.rules.cutoff);
  saturday.setUTCDate(saturday.getUTCDate() + (6 - saturday.getUTCDay() + 7) % 7);
  const pending = [];
  await worker.scheduled({ scheduledTime: saturday.getTime(), cron: '*/15 * * * *' }, env, { waitUntil(p) { pending.push(p); } });
  await Promise.all(pending);
  assert.equal((await record()).balance_cash, 101.65);
});
test('void refunds entry cost, delayed/disputed resolution stays pending; old positions preserved', async () => {
  const { m, order, record } = await setup(); await order();
  const r = await record(); r.trades.push({ sym: 'QQQ270101C00100000', price: 1, qty: 1, side: 'buy' });
  assert.deepEqual(resolvePredictionRecord(r, [], now).record, r);
  assert.deepEqual(resolvePredictionRecord(r, [{ ...resolution(m), official: false }], now + 60000).record, r);
  const result = resolvePredictionRecord(r, [resolution(m, 'VOID')], now + 60000);
  assert.equal(result.record.balance_cash, 100);
  assert.equal(computeBookFromTrades(result.record.trades).QQQ270101C00100000.pos, 1);
});
test('book failures all produce zero ledger fills', async () => {
  for (const mutate of [m => m.book.no = [], m => m.book.exchange_ts = iso(now - 20000), m => m.book.gap = true,
    m => m.book.yes[0][0] = 6000, m => m.book.no[0][1] = 2, m => m.status = 'halted',
    m => m.rules.cutoff = iso(now - 1), m => m.book.receive_ts = iso(now + 60000)]) {
    const { env, m, order, record } = await setup(); mutate(m);
    await env.PREDICTION_DATA.put('prediction/v1/fixtures/current.json', JSON.stringify({ schema: 'prediction.catalog.v1', markets: [m] }));
    const r = await order(); assert.equal(r.status, 409, JSON.stringify(r)); assert.equal((await record()).trades.length, 0);
  }
});
test('gates isolate fixtures, auth protects feed/archive, entry rollback permits exits', async () => {
  const { env, order } = await setup();
  assert.throws(() => predictionNamespace({ ...env, ENVIRONMENT: 'production' }));
  assert.throws(() => predictionNamespace({ ...env, PREDICTION_ENVIRONMENT: 'production' }));
  for (const path of ['markets', 'archive', 'portfolio']) {
    const url = new URL(`http://localhost:8787/api/prediction/${path}`);
    assert.equal((await handlePrediction(new Request(url), env, url)).status, 401);
  }
  await order(); env.PREDICTION_ENTRIES = 'disabled';
  assert.equal((await order()).status, 409); assert.equal((await order('sell')).status, 201);
});
test('equivalent comparison intervals and look-ahead / quarterly mismatch rejection', () => {
  const m = fixture(now); const r = m.rules;
  const options = { ...r, settlement_basis: 'spot', basis: 0, expiry: r.window_end, quote_units: 'USD', discount_factor: 1,
    exchange_ts: iso(now), receive_ts: iso(now), lower: { strike: 99000, bid: 1500, ask: 1600, size: 3 }, upper: { strike: 101000, bid: 600, ask: 700, size: 3 } };
  const spot = { underlying: 'BTC', index: r.index, currency: 'USD', exchange_ts: iso(now), receive_ts: iso(now), price: 100000 };
  const o = compareEvent(m, options, spot, now);
  assert.equal(o.eligible, true); assert.deepEqual(o.option_interval, [.4, .5]); assert.deepEqual(o.event_interval, [.4, .45]);
  assert.equal(compareEvent(m, { ...options, expiry: '2027-03-31T00:00:00Z', settlement_basis: 'futures' }, spot, now).eligible, false);
  assert.equal(compareEvent(m, { ...options, receive_ts: iso(now + 1) }, spot, now).eligible, false);
});
test('archive retry, deterministic replay, sequence gap recovery and restart suppression', async () => {
  const m = fixture(now), bucket = new Bucket(), c = new Collector({ bucket, enabled: true });
  const envelope = { market_key: marketKey(m.rules), exchange_ts: iso(now), receive_ts: iso(now) };
  const rules = { ...envelope, id: 'rules', kind: 'rules', rules: m.rules };
  const snapshot = { ...envelope, id: 'snapshot', kind: 'snapshot', sequence: 1, payload: { orderbook_fp: { yes_dollars: [['0.4000', '3.00']], no_dollars: [['0.5500', '3.00']] } } };
  c.accept(rules); c.accept(snapshot); c.accept(snapshot);
  const events = structuredClone(c.pending);
  assert.deepEqual(replay(events).catalog(), c.state.catalog());
  c.accept({ ...envelope, id: 'gap', kind: 'delta', sequence: 3, side: 'yes', price_units: 4000, size_delta: 1 });
  assert.match(quoteEvent(c.state.catalog().markets[0], 'YES', 'buy', 1, now).error, /unsynchronized/);
  await c.ingest({ ...envelope, id: 'disconnect', kind: 'disconnect' }, async () => ({ ...snapshot, id: 'resnapshot', sequence: 10 }));
  assert.equal(c.status(now).markets[0].synchronized, true);
  bucket.fail = true; await assert.rejects(c.flush()); assert.ok(c.pending.length);
  bucket.fail = false; const digest = await c.flush(); assert.match(digest, /^[a-f0-9]{64}$/);
  const restored = new Collector({ bucket, enabled: true }); await restored.restoreArchive();
  assert.equal(restored.status().markets[0].synchronized, false);
  assert.equal(restored.status().markets[0].last_good_sequence, 10);
  assert.equal(replay(events, now - 1).markets.size, 0);
  assert.throws(() => fixed('0.00001', 10000)); assert.throws(() => fixed('1.5', 1));
  assert.throws(() => new Collector({ bucket, enabled: true, environment: 'production' }));
});


test('void conserves fixed-point premium across unequal entry prices', () => {
  const m = fixture(now), sym = positionKey(m.rules, 'YES');
  const entry = { sym, instrument_type: 'prediction', multiplier: 1, side: 'buy', prediction: { rules: m.rules, outcome: 'YES' } };
  const record = { balance_cash: 98.63, trades: [{ ...entry, price: .45, qty: 1 }, { ...entry, price: .46, qty: 2 }] };
  const result = resolvePredictionRecord(record, [resolution(m, 'VOID')], Date.parse(m.rules.cutoff));
  assert.equal(result.record.balance_cash, 100);
  assert.equal(result.record.trades.at(-1).payout_units, 13700);
});
test('series rollover retains delisted held rules and replays weekend closure', () => {
  const m = fixture(now), next = fixture(now); next.rules.market = 'BTC-NEXT';
  const events = [m, next].map((m, i) => ({ id: `rules-${i}`, kind: 'rules', rules: m.rules,
    market_key: marketKey(m.rules), exchange_ts: iso(now), receive_ts: iso(now) }));
  events.push({ id: 'closed', kind: 'status', status: 'delisted', market_key: marketKey(m.rules), exchange_ts: iso(now), receive_ts: iso(now) });
  const state = replay(events); assert.equal(state.markets.size, 2);
  assert.equal(state.markets.get(marketKey(m.rules)).status, 'delisted');
  assert.equal(state.markets.get(marketKey(m.rules)).rules.version, '1');
  const revised = structuredClone(events[0]); revised.id = 'revised'; revised.rules.version = '2'; revised.rules.text += ' Clarification.';
  state.apply(revised); assert.equal(state.markets.get(marketKey(m.rules)).rule_history.length, 2);
  assert.deepEqual(replay([...events, revised]).catalog(), state.catalog());
});
