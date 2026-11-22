import { setupBtcOwner, seedBtcOwnerSession } from './helpers/btc_owner_fixture.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { withD1, seedAccounts, seedSession } from './helpers/test_env.mjs';
import { createStorage } from '../backend/storage.js';
import { SESSION_COOKIE, computeBookFromTrades } from '../backend/common.js';
import { handlePaperTrade } from '../backend/paper-trade.js';
import { exactBtcQuote, btcSession, settleBtcAccount, handleBtcOptions } from '../backend/btc-options.js';
import { handleLiveQuotes, isPublicBrowserDataKey } from '../backend/browser-data.js';
import { validateTradeIntent } from '../backend/execution.js';
import { handleSettle } from '../backend/settlement.js';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';
import { handleBtcOwnerLink } from '../backend/btc-owner-link.js';
import { CREDIT_ORIGIN, OWNER_ID } from '../backend/kalshi-access.js';

const now = Date.parse('2026-11-21T18:00:00Z');
const sym = 'BTCOPT:' + 'a'.repeat(40);
const contract = { contract_key: sym, instrument_type: 'futures_option', product: '/BTC',
  series: 'monthly', exchange: 'CME', multiplier: 5, type: 'call', strike: 90000,
  provider_symbol: './BTCZ6 BTCZ6 261127C90000', streamer_symbol: './BTCZ26C90000:XCME',
  future_symbol: '/BTCZ6', future_month: '2026-11', future_expiry: '2026-11-27', exp: '2026-11-27',
  expires_at: '2026-11-27T16:00:00Z', stops_trading_at: '2026-11-27T16:00:00Z' };
const stamp = new Date(now).toISOString();
function payload(c = contract) { return { session: 'open', entry_keys: [sym], contracts: [c], quotes: [{
  symbol: sym, contract: c, source: 'fixture-dxlink', bid: 99, ask: 100, bid_size: 2, ask_size: 1,
  bid_ts: stamp, ask_ts: stamp, bid_received_ts: stamp, ask_received_ts: stamp, available: true,
}] }; }
function intent(side = 'buy', extra = {}) { return { execution_request_id: randomUUID(), sym,
  instrument_type: 'futures_option', side, qty: 1, ...extra }; }
class Bucket {
  data = new Map(); seq = 0; failFinal = false;
  async get(key) { const row = this.data.get(key); return row ? { etag: row.etag, json: async () => JSON.parse(row.body) } : null; }
  async put(key, body, options = {}) {
    const old = this.data.get(key), c = options.onlyIf;
    if ((c?.etagDoesNotMatch === '*' && old) || (c?.etagMatches && old?.etag !== c.etagMatches)) return null;
    if (this.failFinal && JSON.parse(body).status === 'filled') { this.failFinal = false; throw Error('fixture final failure'); }
    const row = { etag: String(++this.seq), body }; this.data.set(key, row); return row;
  }
}
async function harness(c = contract) {
  const bucket = new Bucket();
  const env = withD1({ PAPER_TRADES: bucket, LIVE_QUOTE_ORIGIN: 'https://fixture.invalid', LIVE_QUOTE_KEY: 'fixture',
    BTC_OPTIONS_ENTRIES: '1' });
  await seedAccounts(env, [{ username: 'fixture', trades: [], balance_cash: 10000 }]);
  await seedSession(env, 'fixture-session', 'fixture');
  setupBtcOwner(env);
  await seedBtcOwnerSession(env, 'fixture-session', 'fixture');
  let market = payload(c), clock = now;
  const oldFetch = global.fetch, OldDate = Date;
  global.Date = class extends OldDate { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } };
  global.fetch = async (url, options) => {
    assert.equal(new URL(url).pathname, '/btc-options');
    assert.equal(options.redirect, 'manual'); // Supported by workerd; never forward the private origin key.
    return Response.json(market);
  };
  const order = body => handlePaperTrade(new Request('https://options.moopertonic.net/api/paper-trade', {
    method: 'POST', headers: { Cookie: `${SESSION_COOKIE}=fixture-session`, 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }), env);
  return { env, bucket, order, market, advance(ms) { clock += ms; const stamp = new Date(clock).toISOString(); Object.assign(market.quotes[0], { bid_ts: stamp, ask_ts: stamp, bid_received_ts: stamp, ask_received_ts: stamp }); }, store: createStorage(env), restore() { global.fetch = oldFetch; global.Date = OldDate; } };
}

test('side freshness/size, identity, session calendar and one-contract limits', () => {
  assert.equal(exactBtcQuote(payload(), intent(), now).multiplier, 5);
  for (const patch of [{ ask: 0 }, { ask_size: 0 }, { ask_size: null }, { ask_ts: null },
    { ask_ts: '2026-11-21T17:59:00Z' }, { ask_received_ts: '2026-11-21T17:59:00Z' },
    { ask_ts: '2026-11-21T18:00:02Z' }, { bid: 101 }, { available: false }]) {
    const p = payload(); Object.assign(p.quotes[0], patch); assert.ok(exactBtcQuote(p, intent(), now).error, JSON.stringify(patch));
  }
  const oneSided = payload(); oneSided.quotes[0].bid = null; oneSided.quotes[0].bid_ts = null;
  assert.equal(exactBtcQuote(oneSided, intent(), now).error, undefined);
  assert.ok(exactBtcQuote(oneSided, intent('sell'), now).error);
  for (const patch of [{ series: 'weekly' }, { multiplier: 100 }, { future_expiry: '2026-12-31' }]) {
    assert.ok(exactBtcQuote(payload({ ...contract, ...patch }), intent(), now).error);
  }
  assert.ok(validateTradeIntent(intent('buy', { qty: 2 })));
  assert.equal(btcSession(Date.parse('2026-11-21T08:00:00Z')), 'maintenance');
  assert.equal(btcSession(Date.parse('2026-11-21T10:00:00Z')), 'open');
  assert.equal(btcSession(Date.parse('2026-11-23T22:01:00Z')), 'maintenance');
  assert.equal(btcSession(Date.parse('2026-11-23T22:02:00Z')), 'open');
  assert.equal(btcSession(Date.parse('2026-07-20T21:01:00Z')), 'maintenance');
});

test('private collector redirects fail closed without following another origin', async () => {
  const h = await harness();
  try {
    let calls = 0;
    global.fetch = async (url, options) => {
      calls++;
      assert.equal(options.redirect, 'manual');
      assert.equal(new URL(url).origin, h.env.LIVE_QUOTE_ORIGIN);
      return new Response(null, { status: 302, headers: { Location: 'https://other.invalid/redirect' } });
    };
    const response = await handleBtcOptions(new Request('https://options.moopertonic.net/api/btc-options', {
      headers: { Cookie: `${SESSION_COOKIE}=fixture-session` },
    }), h.env);
    assert.equal(response.status, 503);
    assert.equal((await h.order(intent())).status, 503);
    assert.equal(calls, 2);
    assert.equal((await h.store.users.get('fixture')).trades.length, 0);
  } finally { h.restore(); }
});

test('real collector wire scenarios enforce changed-side clocks and provenance', () => {
  for (const s of btcClockScenarios()) {
    const row = s.payload.quotes[0];
    for (const action of ['buy', 'sell']) {
      const result = exactBtcQuote(s.payload, { sym: row.symbol, side: action }, s.now);
      assert.equal(!result.error, s[action], `${s.name}/${action}: ${result.error}`);
      if (s[action]) {
        const side = action === 'buy' ? 'ask' : 'bid';
        assert.equal(result.quote_clock_basis, 'observed_side_change');
        assert.equal(result.quote_source_ts, null);
        assert.equal(result.quoteTs, row[side + '_change'].at);
        assert.equal(result.source_quote[side + '_ts'], null);
      }
    }
  }
});

test('fallback rejects forged, mismatched, stale and future evidence', () => {
  const s = btcClockScenarios().find(s => s.name === 'ask_price');
  const edits = [
    q => { q.ask_change = null; },
    q => { q.ask_change.schema = 'receipt'; },
    q => { q.stream_epoch = 'b'.repeat(32); },
    q => { q.ask_change.previous_price = q.ask; q.ask_change.previous_size = q.ask_size; },
    q => { q.ask_change.price += 1; },
    q => { q.ask_change.size += 1; },
    q => { q.ask_change.sequence = q.ask_change.previous_sequence; },
    q => { q.ask_change.baseline_sequence = 0; },
    q => { q.ask_change.baseline_at = q.ask_change.at; },
    q => { q.ask_change.previous_received_at = q.ask_change.at; },
    q => { q.ask_change.at = new Date(s.now + 2000).toISOString(); },
    q => { q.ask_received_ts = new Date(s.now - 16000).toISOString(); },
    q => { q.ask_received_ts = new Date(s.now + 2000).toISOString(); },
    q => { q.ask_ts = new Date(s.now - 16000).toISOString(); },
    q => { q.ask_ts = new Date(s.now + 2000).toISOString(); },
    q => { q.ask_ts = 'invalid'; },
  ];
  for (const edit of edits) {
    const p = structuredClone(s.payload); edit(p.quotes[0]);
    assert.ok(exactBtcQuote(p, { sym: p.quotes[0].symbol, side: 'buy' }, s.now).error, String(edit));
  }
});

test('observed execution clock and full change evidence survive D1 reload', async () => {
  const s = btcClockScenarios().find(s => s.name === 'ask_price');
  const h = await harness(s.payload.quotes[0].contract);
  try {
    h.advance(s.now - now);
    Object.assign(h.market, s.payload);
    const response = await h.order(intent('buy', { sym: s.payload.quotes[0].symbol }));
    assert.equal(response.status, 201, await response.clone().text());
    const entry = (await createStorage(h.env).users.get('fixture')).trades[0];
    assert.equal(entry.quote_clock_basis, 'observed_side_change');
    assert.equal(entry.quote_source_ts, null);
    assert.equal(entry.quote_ts, s.payload.quotes[0].ask_change.at);
    assert.deepEqual(entry.source_quote, s.payload.quotes[0]);
    assert.equal(entry.cash_delta, -502.5);
  } finally { h.restore(); }
});

for (const type of ['call', 'put']) test(`${type}: persisted 500/600 round trip; duplicates and concurrent closes`, async () => {
  const h = await harness({ ...contract, type });
  try {
    const buy = intent();
    const opened = await h.order(buy); assert.equal(opened.status, 201, await opened.clone().text());
    assert.equal((await opened.json()).balance_cash, 9497.5);
    assert.equal((await h.order(buy)).status, 200);
    const entry = (await h.store.users.get('fixture')).trades[0];
    assert.equal(entry.cash_delta, -502.5); assert.equal(entry.source_quote.ask_size, 1);
    assert.equal(entry.quote_ts, stamp); assert.equal(entry.contract.type, type);
    h.advance(5000);
    h.market.quotes[0].bid = 120; h.market.quotes[0].ask = 121;
    const outcomes = await Promise.all([h.order(intent('sell')), h.order(intent('sell'))]);
    assert.deepEqual(outcomes.map(r => r.status).sort(), [201, 400]);
    const reloaded = await createStorage(h.env).users.get('fixture');
    assert.equal(reloaded.trades.length, 2); assert.equal(reloaded.balance_cash, 10095);
    assert.equal(Date.parse(reloaded.trades[1].quote_ts) - Date.parse(reloaded.trades[0].quote_ts), 5000);
    const b = computeBookFromTrades(reloaded.trades)[sym];
    assert.equal(b.pos, 0); assert.equal(b.realized_gross, 100); assert.equal(b.realized, 95); assert.equal(b.fees, 5);
  } finally { h.restore(); }
});

test('partial finalization recovery, limits, insufficient cash and entry-only disable', async () => {
  const h = await harness();
  try {
    assert.equal((await h.order(intent('sell'))).status, 400);
    assert.equal((await h.order(intent('buy', { order_type: 'limit', limit_price: 99 }))).status, 409);
    await h.store.users.mutate('fixture', r => ({ record: { ...r, balance_cash: 501 } }));
    assert.equal((await h.order(intent())).status, 400);
    await h.store.users.mutate('fixture', r => ({ record: { ...r, balance_cash: 10000 } }));
    const buy = intent('buy', { order_type: 'limit', limit_price: 100 });
    h.bucket.failFinal = true;
    assert.equal((await h.order(buy)).status, 503);
    assert.equal((await h.order(buy)).status, 200);
    assert.equal((await h.store.users.get('fixture')).trades.length, 1);
    h.env.BTC_OPTIONS_ENTRIES = '0';
    assert.equal((await h.order(intent())).status, 403);
    assert.equal((await h.order(intent('sell'))).status, 201);
  } finally { h.restore(); }
});

for (const type of ['call', 'put']) for (const final of [89000, 90000, 91000]) test(`expiration ${type} final ${final}: pending, official mark, exactly once`, async () => {
  const h = await harness({ ...contract, type });
  try {
    assert.equal((await h.order(intent())).status, 201);
    const after = Date.parse('2026-11-27T16:01:00Z');
    const pending = await settleBtcAccount(h.env, 'fixture', after);
    assert.deepEqual(pending.result.pending, [sym]);
    const key = `paper-trades/btc-final/${sym.slice(7)}/${contract.exp}.json`;
    const mark = { contract_key: sym, future_symbol: contract.future_symbol, exp: contract.exp,
      value: final, source: 'CME', kind: 'official_final', source_ref: 'fixture-only', published_at: '2026-11-27T16:00:30Z' };
    await h.bucket.put(key, JSON.stringify({ ...mark, kind: 'daily_close' }));
    assert.equal((await settleBtcAccount(h.env, 'fixture', after)).result.settled.length, 0);
    await h.bucket.put(key, JSON.stringify(mark));
    await Promise.all([settleBtcAccount(h.env, 'fixture', after), settleBtcAccount(h.env, 'fixture', after)]);
    const r = await h.store.users.get('fixture');
    assert.equal(r.trades.length, 2);
    assert.equal(r.balance_cash, 9497.5 + Math.max(type === 'call' ? final - 90000 : 90000 - final, 0) * 5);
    assert.deepEqual(r.trades[1].settlement_mark, mark);
  } finally { h.restore(); }
});

test('anonymous/private authorization and alternate public paths', async () => {
  const h = await harness();
  try {
    const url = 'https://options.moopertonic.net/api/btc-options';
    assert.equal((await handleBtcOptions(new Request(url), h.env)).status, 401);
    h.env.BTC_OWNER_LINK = 'disabled';
    assert.equal((await handleBtcOptions(new Request(url, { headers: { Cookie: `${SESSION_COOKIE}=fixture-session` } }), h.env)).status, 403);
    assert.equal((await handleLiveQuotes(new URL('https://options.moopertonic.net/api/live-quotes?symbols=' + sym), h.env)).status, 403);
    assert.equal(isPublicBrowserDataKey('paper-trades/btc-final/a/2026-11-27.json'), false);
    assert.equal(isPublicBrowserDataKey('intraday/btc/latest.json'), false);
  } finally { h.restore(); }
});

test('revoked display access denies fresh quotes and execution even for holders', async () => {
  const h = await harness();
  try {
    assert.equal((await h.order(intent())).status, 201);
    h.env.BTC_OWNER_LINK = 'disabled';
    let calls = 0;
    global.fetch = async () => { calls++; throw Error('No provider call after revocation'); };
    const response = await handleBtcOptions(new Request('https://options.moopertonic.net/api/btc-options', {
      headers: { Cookie: `${SESSION_COOKIE}=fixture-session` },
    }), h.env);
    assert.equal(response.status, 403);
    assert.equal((await h.order(intent('sell'))).status, 403);
    assert.equal(calls, 0);
    assert.equal(computeBookFromTrades((await h.store.users.get('fixture')).trades)[sym].pos, 1);
  } finally { h.restore(); }
});

test('verified GitHub ordinary paper session reads, buys and closes; revoked identity cannot submit or replay', async () => {
  const h = await harness(); let owner = true;
  try {
    await h.env.DB.prepare('DELETE FROM btc_owner_sessions').run();
    global.fetch = async url => String(url).startsWith(CREDIT_ORIGIN)
      ? Response.json({ github_id: owner ? OWNER_ID : OWNER_ID+1 }) : Response.json(h.market);
    const headers = { Cookie: 'session=fixture-session; __Host-cds-session=synthetic_github_0123456789',
      Origin: 'https://options.moopertonic.net', 'Content-Type': 'application/json' };
    const link = await handleBtcOwnerLink(new Request('https://options.moopertonic.net/api/btc-options/owner-link', {
      method: 'POST', headers, body: JSON.stringify({ action: 'github', confirm: true, paper_account: 'fixture' }),
    }), h.env);
    assert.equal(link.status, 200);
    const read = () => handleBtcOptions(new Request('https://options.moopertonic.net/api/btc-options', { headers }), h.env);
    const order = body => handlePaperTrade(new Request('https://options.moopertonic.net/api/paper-trade', {
      method: 'POST', headers, body: JSON.stringify(body),
    }), h.env);
    assert.equal((await read()).status, 200);
    const buy = intent(); assert.equal((await order(buy)).status, 201);
    owner = false; assert.equal((await read()).status, 403);
    assert.equal((await order(buy)).status, 403, 'receipt replay must reauthorize');
    assert.equal((await order(intent('sell'))).status, 403);
    owner = true; assert.equal((await order(intent('sell'))).status, 201);
    assert.equal((await h.store.users.get('fixture')).trades.length, 2);
  } finally { h.restore(); }
});

test('equity insolvency retains the BTC ledger and pending obligation', async () => {
  const h = await harness();
  try {
    assert.equal((await h.order(intent())).status, 201);
    await h.store.users.mutate('fixture', record => ({ record: { ...record, trades: [...record.trades,
      { sym: 'QQQ261120C00001000', side: 'sell', qty: 1, price: 1, strike: 1, type: 'call', exp: '2026-11-20' }] } }));
    const response = await handleSettle(new Request('https://options.moopertonic.net/api/settle', {
      method: 'POST', headers: { Cookie: `${SESSION_COOKIE}=fixture-session`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ as_of: '2026-11-21', spot_marks: { '2026-11-20': 1000000 } }),
    }), h.env);
    assert.equal(response.status, 410);
    const record = await h.store.users.get('fixture');
    assert.equal(record.account_closed, true);
    assert.equal(computeBookFromTrades(record.trades)[sym].pos, 1);
    const outcome = await settleBtcAccount(h.env, 'fixture', Date.parse('2026-11-27T16:01:00Z'));
    assert.deepEqual(outcome.result.pending, [sym]);
  } finally { h.restore(); }
});

test('USDC minimum lot buy, sell and settlement use the same BTC units', async () => {
  const h = await harness();
  h.env.BTC_USDC_DATA_ORIGIN = 'https://linear.invalid';
  const name = 'BTC_USDC-27NOV26-90000-C', key = 'USDC:' + name;
  const spec = {instrument_name:name, kind:'option', is_active:true, quote_currency:'USDC', settlement_currency:'USDC',
    contract_size:1, min_trade_amount:0.01, strike:90000, option_type:'call', expiration_timestamp:Date.parse(contract.expires_at)};
  let size = 0.01;
  global.fetch = async raw => {
    const u = new URL(raw), method = u.pathname.split('/').pop();
    assert.equal(u.origin,'https://linear.invalid');
    const book = {instrument_name:name,state:'open',timestamp:Date.now(),index_price:90000,best_bid_price:950,
      best_ask_price:1000,best_bid_amount:size,best_ask_amount:size};
    const results = {get_instrument:spec,get_order_book:book,get_delivery_prices:{data:[{date:contract.exp,delivery_price:91000}]}};
    assert.ok(method in results);
    return Response.json({result:results[method]});
  };
  try {
    const buy = () => h.order(intent('buy',{sym:key}));
    size = 0.005;
    assert.equal((await buy()).status,409);
    size = 0.01;
    assert.equal((await buy()).status,201);
    let account = await h.store.users.get('fixture');
    assert.equal(account.trades.at(-1).multiplier,0.01);
    assert.equal(account.balance_cash,9989.73);
    assert.equal((await h.order(intent('sell',{sym:key}))).status,201);
    account = await h.store.users.get('fixture');
    assert.ok(Math.abs(account.balance_cash-9998.96)<1e-8);
    assert.equal((await buy()).status,201);
    const settled = await settleBtcAccount(h.env,'fixture',spec.expiration_timestamp+60000);
    assert.equal(settled.result.settled[0].cash_delta,10);
    assert.equal(settled.result.settled[0].multiplier,0.01);
  } finally {h.restore();}
});

test('Micro paper lot is 0.1 BTC through entry and exit', async () => {
  const micro = {...contract,product:'/MBT',multiplier:0.1,future_symbol:'/MBTX6'};
  const h = await harness(micro);
  try {
    assert.equal((await h.order(intent('buy'))).status,201);
    assert.equal((await h.store.users.get('fixture')).balance_cash,9987.5);
    assert.equal((await h.order(intent('sell'))).status,201);
    assert.equal((await h.store.users.get('fixture')).balance_cash,9994.9);
  } finally {h.restore();}
});
