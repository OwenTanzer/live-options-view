import test from 'node:test';
import assert from 'node:assert/strict';
import { handleKalshi, kalshiTick, settleKalshiMarket } from '../backend/kalshi.js';
import { ACCOUNT, CREDIT_ORIGIN, SITE, OWNER_ID } from '../backend/kalshi-access.js';
import { kalshiFee, kalshiSnapshot } from '../backend/kalshi-model.js';
import { fetchMarket, displayMarket, effectiveFee } from '../backend/kalshi-source.js';
import { quoteEvent, compareEvent } from '../backend/prediction-model.js';
import { archive, getArchive, budget, pruneArchive } from '../backend/kalshi-store.js';
import { createStorage } from '../backend/storage.js';
import worker from '../backend/index.js';
import { fixture, envFixture, network, request, COOKIE, TICKER } from './helpers/kalshi_fixture.mjs';

async function scenario(fn) {
  const f = fixture(), env = envFixture(), saved = globalThis.fetch;
  globalThis.fetch = network(f);
  try { await fn(env, f); } finally { globalThis.fetch = saved; env.DB.db.close(); }
}
const intent = (changes = {}) => ({ execution_request_id: crypto.randomUUID(), market: TICKER, side: 'buy', outcome: 'YES', qty: 3, ...changes });
const order = (env, b) => handleKalshi(request('paper', b), env);

test('official July 2026 fee formula fixtures: centicent rounding, quantity and series multiplier', () => {
  const fee = { type: 'quadratic', multiplier: '1' };
  // Formula examples derived from https://kalshi.com/docs/kalshi-fee-schedule.pdf (2026-07-07).
  for (const [price, qty, units] of [[5000,1,175],[5000,100,17500],[100,1,7],[500,100,3325],[4500,3,520],[9900,1,7]])
    assert.equal(kalshiFee(price, qty, fee), units);
  assert.equal(kalshiFee(5000, 1, { ...fee, multiplier: '2' }), 350);
  assert.equal(kalshiFee(5000, 1, { ...fee, multiplier: '0' }), 0);
  assert.equal(kalshiFee(5000, 1, { ...fee, type: 'quadratic_with_maker_fees' }), 175);
  for (const bad of ['flat','unknown']) assert.throws(() => kalshiFee(5000,1,{...fee,type:bad}));
  assert.throws(() => kalshiFee(5000, 1.5, fee));
});
test('event overrides, cleared overrides, future schedule and unknown multipliers', () => {
  const f = fixture(), row = { id: 'synthetic', event_ticker: f.raw.event_ticker, series_ticker: f.series.ticker,
    scheduled_ts: new Date(Date.now()-1000).toISOString(), fee_type_override: 'quadratic', fee_multiplier_override: 2 };
  f.fees.event_fee_changes.push(row);
  assert.equal(effectiveFee(f.series,f.fees,f.raw,Date.now()).multiplier,'2');
  row.fee_multiplier_override = null; row.fee_type_override = null;
  assert.equal(effectiveFee(f.series,f.fees,f.raw,Date.now()).multiplier,'1');
  row.scheduled_ts = new Date(Date.now()+10000).toISOString();
  assert.equal(effectiveFee(f.series,f.fees,f.raw,Date.now()).next_change,row.scheduled_ts);
  assert.throws(() => effectiveFee(f.series,{...f.fees,cursor:'more'},f.raw,Date.now()));
});
test('all endpoints deny anonymous, ordinary sessions, spoofed identities, other GitHub users and aliases', async () => scenario(async (env,f) => {
  const paths = ['status','markets','portfolio','archive','archive?id=00000000-0000-0000-0000-000000000000'];
  for (const cookie of ['', 'session=ordinary', '__Host-cds-session=short']) for (const p of paths)
    assert.equal((await worker.fetch(request(p,null,cookie),env)).status,401);
  assert.equal(f.requests.length,0);
  for (const id of [OWNER_ID+1, String(OWNER_ID), null]) {
    f.identity = id;
    for (const p of paths) assert.equal((await worker.fetch(request(p),env)).status,401);
    for (const p of ['discover','market','paper']) assert.equal((await worker.fetch(request(p,{}),env)).status,401);
  }
  f.identity = OWNER_ID;
  for (const origin of ['https://live-options-view.otmooper12.workers.dev','https://live-options-api.otmooper12.workers.dev','http://options.moopertonic.net'])
    assert.equal((await worker.fetch(request('portfolio',null,COOKIE,origin),env)).status,404);
  assert.equal((await handleKalshi(new Request(SITE+'/api/kalshi/paper',{method:'POST',headers:{Cookie:COOKIE},body:'{}'}),env)).status,403);
  assert.equal((await handleKalshi(request('status',null,COOKIE+'; '+COOKIE),env)).status,401);
  assert.ok(f.requests.every(r=>r.url===CREDIT_ORIGIN+'/api/private/identity'));
}));
test('owner introspection forwards only opaque session; disabled and preview never fetch market data', async () => scenario(async (env,f) => {
  env.KALSHI_PERSONAL_ENABLED = '0';
  const r = await handleKalshi(request('status',null,COOKIE+'; session=ordinary; __Host-cds-flow=neverforward'),env);
  assert.equal((await r.json()).enabled,false); assert.match(r.headers.get('Cache-Control'),/private, no-store/);
  assert.deepEqual(Object.keys(f.requests[0].options.headers).sort(),['Accept','Cookie']);
  assert.equal(f.requests[0].options.headers.Cookie,COOKIE);
  assert.equal(f.requests[0].options.redirect,'error');
  assert.equal((await handleKalshi(request('market',{market:TICKER}),env)).status,503);
  await kalshiTick(env); assert.equal(f.requests.filter(x=>!x.url.startsWith(CREDIT_ORIGIN)).length,0);
  env.ENVIRONMENT='preview'; assert.equal((await handleKalshi(request('status'),env)).status,404);
}));
test('synthetic full handler flow: discovery, quote, sized fill, restart retry, replay and private ledger', async () => scenario(async (env,f) => {
  const discovery = await (await handleKalshi(request('discover',{}),env)).json();
  assert.equal(discovery.markets.length,1); assert.equal(discovery.markets[0].bid,undefined);
  const detail = await (await handleKalshi(request('market',{market:TICKER}),env)).json();
  assert.equal(detail.quotes.YES.ask,.45); assert.equal(detail.quotes.YES.ask_size,3); assert.equal(detail.exchange_ts,null);
  const b = intent(); const fill = await order(env,b); assert.equal(fill.status,201);
  const d = await fill.json(); assert.equal(d.balance_cash,9998.598); assert.equal(d.trade.fee_units,520);
  assert.equal(d.trade.provenance.book.exchange_ts,null);
  assert.equal(d.trade.provenance.evidence,undefined);
  env.DB.db.prepare("UPDATE kalshi_evidence SET received_at = '2000-01-01' WHERE id = ?").run(d.trade.provenance.archive.id);
  await pruneArchive(env);
  assert.equal((await getArchive(env,d.trade.provenance.archive.id)).market.rules.market,TICKER);
  const networkBefore=f.requests.length;
  assert.equal((await order(env,b)).status,200);
  assert.equal(f.requests.length,networkBefore+1); // Recheck identity, but never requote/redebit.
  assert.equal((await order(env,{...b,qty:2})).status,409);
  const replay = await (await handleKalshi(request('archive?id='+detail.archive.id),env)).json();
  assert.equal(replay.mode,'archived'); assert.equal(replay.evidence.market.rules.market,TICKER);
  const store=createStorage(env); assert.equal((await store.users.get(ACCOUNT)).trades.length,1);
  await store.sessions.create('ordinary',ACCOUNT,60);
  assert.equal((await worker.fetch(new Request(SITE+'/api/me',{headers:{Cookie:'session=ordinary'}}),env)).status,401);
  assert.equal((await worker.fetch(new Request(SITE+'/api/login',{method:'POST',headers:{Origin:SITE},body:JSON.stringify({username:ACCOUNT,password:'any'})}),env)).status,401);
}));
test('stale, future, aged, fractional, crossed, undersized, unsupported books and client prices cannot fill', async () => {
  const mutations = [f=>f.dateOffset=-20000,f=>f.dateOffset=20000,f=>f.age='10',
    f=>f.book.orderbook_fp.no_dollars[0][1]='1.50',f=>f.book.orderbook_fp.yes_dollars[0][0]='0.9000',
    f=>f.book.orderbook_fp.no_dollars=[],f=>f.raw.status='closed',f=>f.series.fee_type='flat',
    f=>f.raw.notional_value_dollars='10.0000',f=>f.raw.is_provisional=true];
  for (const mutate of mutations) await scenario(async(env,f)=>{
    mutate(f); assert.notEqual((await order(env,intent())).status,201);
    assert.equal((await createStorage(env).users.get(ACCOUNT)).trades.length,0);
  });
  await scenario(async env=>{
    assert.equal((await order(env,intent({qty:4}))).status,409);
    assert.equal((await order(env,intent({price:.01}))).status,400);
    assert.equal((await order(env,intent({market:'BTC-ANY'}))).status,503);
  });
});
test('quote expiry hides prices, opposite sides and unsupported comparison remain explicit', async () => scenario(async env=>{
  const evidence=await fetchMarket(env,TICKER),m=evidence.market;
  assert.equal(quoteEvent(m,'NO','buy',2,Date.now()).ask,.6);
  assert.equal(quoteEvent(m,'YES','sell',2,Date.now()).bid,.4);
  assert.equal(displayMarket(evidence,Date.now()+11000).quotes,null);
  assert.equal(compareEvent(m,null,null,Date.now()).eligible,false);
  assert.throws(() => kalshiSnapshot({orderbook_fp:{yes_dollars:[['.40','3']],no_dollars:[]}}));
}));
test('concurrent closes serialize, insufficient cash and entry rollback preserve original fill', async () => scenario(async(env)=>{
  assert.equal((await order(env,intent())).status,201);
  env.KALSHI_ENTRIES='0'; assert.equal((await order(env,intent())).status,409);
  const closes=await Promise.all([order(env,intent({side:'sell'})),order(env,intent({side:'sell'}))]);
  assert.deepEqual(closes.map(x=>x.status).sort(),[201,409]);
  const r=await createStorage(env).users.get(ACCOUNT); assert.equal(r.trades.length,2);
  env.KALSHI_ENTRIES='1'; await createStorage(env).users.mutate(ACCOUNT,r=>({record:{...r,balance_cash:0},result:{}}));
  assert.equal((await order(env,intent())).status,409);
}));
test('official finalized YES/NO only; duplicate settlement and correction retain immutable fills', async () => scenario(async(env,f)=>{
  await order(env,intent());
  const store=createStorage(env), original=(await store.users.get(ACCOUNT)).trades[0];
  // Move the fixture contract cutoff into the past for official settlement tests.
  await store.users.mutate(ACCOUNT,r=>({record:{...r,trades:r.trades.map(t=>({...t,prediction:{...t.prediction,rules:{...t.prediction.rules,cutoff:new Date(Date.now()-10000).toISOString()}}}))},result:{}}));
  f.raw.status='finalized'; f.raw.result='yes'; f.raw.settlement_ts=new Date(Date.now()-1000).toISOString(); f.raw.settlement_value_dollars='1.0000';
  const evidence={receipts:[{data:{market:structuredClone(f.raw)},receive_ts:new Date().toISOString()}]};
  assert.equal((await settleKalshiMarket(env,evidence)).adjustments,1);
  assert.equal((await settleKalshiMarket(env,evidence)).adjustments,0);
  const after=await store.users.get(ACCOUNT); assert.equal(after.balance_cash,10001.598);
  assert.deepEqual(after.trades[0].provenance,original.provenance);
  evidence.receipts[0].data.market.result='no'; evidence.receipts[0].data.market.settlement_value_dollars='0.0000';
  assert.equal((await settleKalshiMarket(env,evidence)).adjustments,1);
  assert.equal((await store.users.get(ACCOUNT)).balance_cash,9998.598);
  evidence.receipts[0].data.market.settlement_value_dollars='0.5000';
  assert.equal((await settleKalshiMarket(env,evidence)).pending,true);
}));
test('private archive integrity, account separation and persistent request budgets', async () => scenario(async env=>{
  const receipt=await archive(env,TICKER,{synthetic:true}); assert.deepEqual(await getArchive(env,receipt.id),{synthetic:true});
  env.DB.db.prepare('UPDATE kalshi_evidence SET payload = ? WHERE id = ?').run('{}',receipt.id);
  await assert.rejects(()=>getArchive(env,receipt.id));
  await budget(env,'test',1,60000); await assert.rejects(()=>budget(env,'test',1,60000));
  assert.equal(env.DB.db.prepare('SELECT COUNT(*) AS n FROM kalshi_evidence WHERE account_id != ?').get(ACCOUNT).n,0);
}));
