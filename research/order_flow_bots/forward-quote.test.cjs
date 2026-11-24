'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {parseQqqQuote, fetchQqqQuote} = require('./forward-quote.cjs');

const now = Date.parse('2026-11-24T15:30:00Z');
const ago = ms => new Date(now-ms).toISOString();
const qqq = {symbol:'QQQ', instrument_class:'equity', kind:'ticker', source:'dxlink',
  bid:651.32, ask:651.38, bid_ts:ago(800), ask_ts:ago(1200)};
const payload = (row = qqq, state = 'live') => ({health:{state}, quotes:[row]});

test('orders see the observed spread and the older of the two receipt times', () => {
  const got = parseQqqQuote(payload(),now);
  assert.equal(got.t,now-1200);
  assert.equal(got.bidAt,now-800);
  assert.equal(got.askAt,now-1200);
  assert.equal(got.spot,651.35);
  assert.equal(got.bid,651.32);
  assert.equal(got.ask,651.38);
});

test('reject fallback, mixed ages, invalid sides, disconnected feeds and future times', () => {
  for (const changes of [{bid_ts:null,ask_ts:null,quote_ts:ago(1)}, {ask_ts:ago(15001)},
    {bid_ts:ago(-1)}, {bid_ts:'2026-11-24T15:29:59'}, {bid:0}, {bid:652}, {ask:null},
    {symbol:'SPY'}, {instrument_class:'option'}, {source:'provider-fallback'}]) {
    assert.equal(parseQqqQuote(payload({...qqq,...changes}),now),null);
  }
  assert.equal(parseQqqQuote(payload(qqq,'offline'),now),null);
  assert.equal(parseQqqQuote({health:{state:'live'},quotes:[qqq,qqq]},now),null);
});

test('one shared request uses local receipt time after reading the body; failures propagate', async () => {
  let calls = 0, parsed = false;
  const fetchImpl = async (url,options) => {
    calls++;
    assert.match(url,/\/api\/live-quotes\?symbols=QQQ$/);
    assert.ok(options.signal instanceof AbortSignal);
    return {ok:true, json:async () => { parsed=true; return payload(); }};
  };
  const result = await fetchQqqQuote({fetchImpl,now:()=>{ assert.ok(parsed); return now; }});
  assert.equal(calls,1);
  assert.equal(result.receivedAt,now);
  await assert.rejects(fetchQqqQuote({fetchImpl:async()=>({ok:false,status:503})}),/503/);
});
