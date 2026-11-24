'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {ForwardFeed}=require('./forward-feed.cjs');
const open=Date.parse('2026-11-23T14:30:00Z'),close=open+390*60000, day='2026-11-23';
const symbols=[];
for(let k=647;k<=654;k++) for(const type of ['C','P']) symbols.push('QQQ261123'+type+String(k*1000).padStart(8,'0'));
function chain(t) {
  return {symbol:'QQQ',date:day,expiration:day,timestamp:new Date(t).toISOString(),underlying_price:650.5,
    underlying_market:{symbol:'QQQ',source:'dxlink',freshness:'live',spot:650.5,spot_ts:new Date(t).toISOString(),
      provenance:{freshness:'live',price_basis:'bid_ask_midpoint',price:650.5,collector_received_at:new Date(t).toISOString()}},
    rows:symbols.map(OptionSymbol=>({OptionSymbol,Gamma:.01,OpenInterest:1234}))};
}
function event(t,seq,side='buy') {return {symbol:symbols[0],t,seq,size:2,side,price:1.25};}
function projection(events,cap=3) {
  const contracts=Object.fromEntries(symbols.map(s=>[s,{B:0,S:0,U:0,prints:0}]));
  for(const r of events) {const c=contracts[r.symbol];c[{buy:'B',sell:'S',unknown:'U'}[r.side]]+=r.size;c.prints++;}
  return {cap,rows:events.slice(-cap),session:{day,first:Math.min(...events.map(e=>e.t)),last:Math.max(...events.map(e=>e.t)),
    contracts,opening:contracts,gaps:[],prints:events.length,rebuilt:{status:'rebuilt'}}};
}
function poll(f,t,events,snapshot=t) {
  return f.observe({open,close,receivedAt:t,chainReceivedAt:t,chain:chain(snapshot),flow:projection(events)});
}
test('fills one minute across overlapping capped projections, deducts post-boundary flow and keeps actual publication time',()=>{
  const f=new ForwardFeed(),events=[event(open,1)];
  assert.equal(poll(f,open,events).frames.length,1);
  events.push(event(open+5000,2),event(open+4000,3,'sell'));poll(f,open+6000,events);
  events.push(event(open+14000,4));poll(f,open+16000,events);
  events.push(event(open+59000,5),event(open+60500,6));
  const [decision]=poll(f,open+61000,events,open+60000).frames;
  assert.equal(decision.receivedAt,open+61000);
  assert.deepEqual(decision.eligibleSlots.slice().sort((a,b)=>a-b),[1,2,4,5,6,7,8,9,10,11]);
  const c=decision.frame.contracts[0];
  assert.equal(c.time,open+59000);assert.equal(c.buy,6);assert.equal(c.sell,2);
  assert.equal(c.buyPremium,750);assert.equal(c.cumBuy,8);assert.equal(c.cumSell,2);
});
test('cap loss fails closed, then continuous fresh windows recover; wrong-session quotes are ignored',()=>{
  const f=new ForwardFeed(),events=[event(open,1)];
  poll(f,open,events);
  for(let i=2;i<=6;i++)events.push(event(open+i*1000,i));
  poll(f,open+8000,events);
  events.push(event(open+60000,7));
  assert.deepEqual(poll(f,open+60000,events).frames[0].eligibleSlots,[]);
  events.push(event(open+120000,8));
  assert.ok(poll(f,open+120000,events).frames[0].eligibleSlots.includes(10));
  const other={...chain(open+120000),expiration:'2026-11-24'};
  assert.equal(f.observe({open,close,receivedAt:open+120000,chainReceivedAt:open+120000,chain:other}).quote,null);
});
test('late provider print cannot revise prior frame or count in following minute',()=>{
  const f=new ForwardFeed(),events=[event(open,1)];
  poll(f,open,events);events.push(event(open+60001,2));poll(f,open+60005,events,open+60000);
  events.push(event(open+59000,3),event(open+120000,4));
  const [d]=poll(f,open+120000,events).frames;
  assert.deepEqual(d.eligibleSlots,[]);assert.equal(d.frame.contracts[0].buy,4);
});

test('opening chain and underlying freeze before flow is published',()=>{
  const f=new ForwardFeed();
  const before=open-44000;
  const r=f.observe({open,close,flow:null,chain:chain(before),chainReceivedAt:before+100,receivedAt:open+200});
  const opening=r.frames[0].frame;
  assert.equal(opening.t,open);assert.equal(opening.complete,false);assert.equal(opening.openingReferenceComplete,true);
  assert.equal(opening.chainTime,before);assert.equal(opening.contracts[0].openOi,1234);
  assert.equal(f.observe({open,close,receivedAt:open+400}).frames.length,0);
});
test('confirmed volume and BBO continue flow-only slots with missing chain; an opening packet alone never enables clearing-OI slot',()=>{
  const f=new ForwardFeed(),events=[event(open,1)];
  poll(f,open,events);
  const evidence={session:day,settledSession:'2026-11-20',key:'intraday/20261123/opening-oi/v1/0001.json',
    sha256:'a'.repeat(64),version:1,open,openingSpot:650.5,receivedAt:open+1000,
    bySymbol:Object.fromEntries(symbols.map(s=>[s,10]))};
  let final;
  for(let i=1;i<=4;i++){
    const t=open+i*60000;events.push(event(t,1+i));
    [final]=f.observe({open,close,receivedAt:t,chain:null,openingOi:evidence,flow:projection(events),
      spotQuote:{symbol:'QQQ',bid:650,ask:651,bidAt:t-20,askAt:t-15,receivedAt:t}}).frames;
    assert.equal(final.frame.oiVerified,false);assert.ok(!final.eligibleSlots.includes(3));
  }
  assert.equal(final.frame.chainTime,null);assert.ok(final.eligibleSlots.includes(10));
  assert.ok(final.eligibleSlots.includes(9));assert.equal(final.frame.officialOpeningReference.spot,650.5);
  for(const id of [1,4,5])assert.ok(!final.eligibleSlots.includes(id));
});
test('a changed cumulative projection with identical count cannot be a continuous observation',()=>{
  const f=new ForwardFeed(),events=[event(open,1)];poll(f,open,events);
  const flow=projection(events);
  flow.session.contracts[symbols[0]].B=200;
  f.observe({open,close,receivedAt:open+2000,chain:chain(open),chainReceivedAt:open,flow});
  events.push(event(open+60000,2));
  const [d]=poll(f,open+60000,events).frames;
  assert.deepEqual(d.eligibleSlots,[]);
});
