import test from 'node:test';
import assert from 'node:assert/strict';
import {privateBitcoinPage,handleBitcoinReplay} from '../backend/one-clock-btc.js';
import {withD1,seedAccounts,seedSession} from './helpers/test_env.mjs';
import {setupBtcOwner,seedBtcOwnerSession} from './helpers/btc_owner_fixture.mjs';
import {SESSION_COOKIE} from '../backend/common.js';
const ts='2026-11-22T00:00:01Z', at=Date.parse(ts), uploaded=at+1000;
const key='USDC:BTC_USDC-27NOV26-90000-C';
const id='aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const trade={instrument_type:'futures_option',sym:key,ts,execution_request_id:id};
const receipt={...trade,username:'owner',status:'filled',source_quote:{
  contract:{contract_key:key,provider_symbol:key.slice(5),exchange:'Deribit',series:'linear',instrument_type:'futures_option',
    product:'BTC_USDC',multiplier:1,type:'call',strike:90000,expires_at:'2026-11-27T08:00:00Z'},symbol:key,
  bid:22,ask:23,bid_received_ts:ts,ask_received_ts:'2026-11-22T00:00:03Z'}};
const record={username:'owner',trades:[trade],balance_cash:12345};
function bucket(body=receipt) {
  return {get:async path=>{
    assert.equal(path,'paper-trades/requests/'+id+'.json');
    return {uploaded:new Date(uploaded),size:1234,json:async()=>body};
  }};
}
test('UTC weekend private receipts only become visible at archive publication, with both sides independently bounded',async()=>{
  assert.equal((await privateBitcoinPage(bucket(),record,uploaded-1,0)).rows.length,0);
  const page=await privateBitcoinPage(bucket(),record,uploaded,0);
  assert.deepEqual(page.rows,[{symbol:key,venue:'Deribit',received:new Date(uploaded).toISOString(),bid:'22',ask:'—'}]);
  assert.equal((await privateBitcoinPage(bucket({...receipt,username:'other'}),record,uploaded,0)).gaps,1);
  assert.equal((await privateBitcoinPage(bucket(),record,uploaded+86400000,0)).rows.length,0);
});
test('HTTP read is identity-scoped, private, and independent of live quote or order service',async()=>{
  const env=withD1({PAPER_TRADES:bucket(),LIVE_QUOTE_ORIGIN:'https://fixture.invalid'});
  await seedAccounts(env,[record]);await seedSession(env,'token','owner');setupBtcOwner(env);
  const url='https://options.moopertonic.net/api/one-clock/btc-replay?one_clock_at='+uploaded;
  const read=(cookie,method='GET')=>handleBitcoinReplay(new Request(url,{method,headers:cookie?{Cookie:SESSION_COOKIE+'='+cookie}:{}}),env);
  assert.equal((await read(null)).status,401);
  assert.equal((await read('token')).status,403);
  await seedBtcOwnerSession(env,'token','owner');
  const response=await read('token');
  assert.equal(response.status,200);
  assert.equal(response.headers.get('cache-control'),'private, no-store');
  const html=await response.text();
  assert.ok(html.includes(key)); assert.ok(!html.includes('12345')); assert.ok(!html.includes(id));
  assert.equal((await read('token','POST')).status,405);
});

test('receipt pages detect a changed candidate set and retain CME contract identity',async()=>{
  const cme='BTCOPT:'+'c'.repeat(40);
  const cmeContract={contract_key:cme,instrument_type:'futures_option',product:'/BTC',series:'monthly',
    exchange:'CME',multiplier:5,type:'put',strike:90000,provider_symbol:'CME fixture',streamer_symbol:'BTC:XCME',
    future_symbol:'/BTCZ6',future_expiry:'2026-11-27',exp:'2026-11-27',expires_at:'2026-11-27T16:00:00Z',
    stops_trading_at:'2026-11-27T16:00:00Z'};
  const owner={...record,trades:[{...trade,sym:cme}]};
  const cmeReceipt={...receipt,sym:cme,source_quote:{...receipt.source_quote,contract:cmeContract,symbol:cme}};
  const first=await privateBitcoinPage(bucket(cmeReceipt),owner,uploaded,0);
  assert.equal(first.rows[0].venue,'CME');
  assert.equal((await privateBitcoinPage(bucket(cmeReceipt),{...owner,trades:[]},uploaded,0,first.version)).restart,true);
});
