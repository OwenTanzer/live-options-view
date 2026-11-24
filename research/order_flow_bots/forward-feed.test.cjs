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
      provenance:{contributing:{bid_ts:new Date(t).toISOString(),ask_ts:new Date(t).toISOString()},freshness:'live',price_basis:'bid_ask_midpoint',price:650.5,collector_received_at:new Date(t).toISOString()}},
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
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];
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
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];
  poll(f,open,events);
  for(let i=2;i<=6;i++)events.push(event(open+i*1000,i));
  poll(f,open+8000,events);
  events.push(event(open+60000,7));
  const bad=poll(f,open+60000,events).frames[0];
  assert.deepEqual(bad.eligibleSlots,[]);
  assert.equal(bad.frame.coverageReason,'incomplete minute: projection suffix lost');
  assert.equal(bad.frame.coverageDiagnostics.events[0].example.currentPrints,6);
  events.push(event(open+120000,8));
  assert.ok(poll(f,open+120000,events).frames[0].eligibleSlots.includes(10));
  const other={...chain(open+120000),expiration:'2026-11-24'};
  assert.equal(f.observe({open,close,receivedAt:open+120000,chainReceivedAt:open+120000,chain:other}).quote,null);
});
test('late provider print cannot revise prior frame or count in following minute',()=>{
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];
  poll(f,open,events);events.push(event(open+60001,2));poll(f,open+60005,events,open+60000);
  events.push(event(open+59000,3),event(open+120000,4));
  const [d]=poll(f,open+120000,events).frames;
  assert.deepEqual(d.eligibleSlots,[]);assert.equal(d.frame.contracts[0].buy,4);
  assert.deepEqual(d.frame.coverageDiagnostics.reasons,['ladder print arrived after its decision']);
  assert.equal(d.frame.coverageDiagnostics.events[0].example.seq,3);
});

test('opening chain and underlying freeze before flow is published',()=>{
  const f=new ForwardFeed({settleMs:0});
  const before=open-44000;
  const r=f.observe({open,close,flow:null,chain:chain(before),chainReceivedAt:before+100,receivedAt:open+200});
  const opening=r.frames[0].frame;
  assert.equal(opening.t,open);assert.equal(opening.complete,false);assert.equal(opening.openingReferenceComplete,true);
  assert.equal(opening.chainTime,before);assert.equal(opening.contracts[0].openOi,1234);
  assert.equal(f.observe({open,close,receivedAt:open+400}).frames.length,0);
});
test('confirmed volume and BBO continue flow-only slots with missing chain; an opening packet alone never enables clearing-OI slot',()=>{
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];
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
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];poll(f,open,events);
  const flow=projection(events);
  flow.session.contracts[symbols[0]].B=200;
  f.observe({open,close,receivedAt:open+2000,chain:chain(open),chainReceivedAt:open,flow});
  events.push(event(open+60000,2));
  const [d]=poll(f,open+60000,events).frames;
  assert.deepEqual(d.eligibleSlots,[]);
});

test('midpoint becomes usable only once both side receipts preceded the boundary',()=>{
  const boundary=open+60000;
  function closeMinute(bid,ask) {
    const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];
    poll(f,open,events);events.push(event(boundary,2));
    const receivedAt=boundary+2000;
    const candidate=chain(receivedAt-10),m=candidate.underlying_market;
    candidate.underlying_price=m.spot=m.provenance.price=650.75;
    m.spot_ts=m.provenance.collector_received_at=new Date(boundary-1000).toISOString();
    m.provenance.contributing={bid_ts:bid,ask_ts:ask};
    return f.observe({open,close,flow:projection(events),chain:candidate,chainReceivedAt:receivedAt,receivedAt});
  }
  const before=new Date(boundary-1000).toISOString();
  const straddle=closeMinute(before,new Date(boundary+1000).toISOString());
  assert.equal(straddle.frames[0].frame.spot,650.5);
  assert.equal(straddle.quote.t,boundary+1000);
  assert.equal(straddle.quote.oldest,boundary-1000);
  const fractional=closeMinute(before,new Date(boundary).toISOString().replace('.000Z','.000500Z'));
  assert.equal(fractional.frames[0].frame.spot,650.5);
  assert.equal(fractional.quote.t,boundary+1);
  for(const [bid,ask] of [[before,null],[null,before],[before,'n/a']]) {
    const unproven=closeMinute(bid,ask);
    assert.equal(unproven.quote,null);assert.equal(unproven.frames[0].frame.spot,650.5);
  }
  const positive=closeMinute(before,new Date(boundary-100).toISOString());
  assert.equal(positive.frames[0].frame.spot,650.75);
  assert.equal(positive.frames[0].frame.snapshotTime,boundary-100);
  assert.equal(positive.quote.oldest,boundary-1000);
});

test('earlier provider timestamp cannot backdate receipt of last trade',()=>{
  const f=new ForwardFeed({settleMs:0}),events=[event(open,1)],boundary=open+60000;
  poll(f,open,events);events.push(event(boundary,2));
  const later=boundary+850, sampled=later+500;
  const candidate=chain(sampled),market=candidate.underlying_market;
  const arrival=new Date(later).toISOString(), atExchange=new Date(boundary-700).toISOString();
  candidate.underlying_price=market.spot=market.provenance.price=650.75;
  market.spot_ts=market.provenance.collector_received_at=arrival;
  market.provenance.price_basis='last_trade';
  market.provenance.provider_observed_at=atExchange;
  market.provenance.contributing={last_ts:arrival,last_observed_at:atExchange};
  const result=f.observe({open,close,receivedAt:sampled,chainReceivedAt:sampled,flow:projection(events),chain:candidate});
  assert.equal(result.quote.t,later);assert.equal(result.quote.oldest,boundary-700);
  assert.equal(result.frames[0].frame.spot,650.5);
});

test('direct published BBO cannot qualify parity when its ask arrived 900 microseconds after the boundary',()=>{
  const {parseQqqQuote}=require('./forward-quote.cjs');
  const t=open+60000,receivedAt=t+1000;
  function apply(askIso) {
    const f=new ForwardFeed({settleMs:0}),events=[event(open,1)];poll(f,open,events);events.push(event(t,2));
    const row={symbol:'QQQ',instrument_class:'equity',kind:'ticker',source:'dxlink',bid:650.8,ask:651,
      bid_ts:new Date(t-2).toISOString(),ask_ts:askIso};
    const spotQuote=parseQqqQuote({health:{state:'live'},quotes:[row]},receivedAt);
    const [decision]=f.observe({open,close,flow:projection(events),spotQuote,receivedAt}).frames;
    return {decision,spotQuote};
  }
  const post=apply(new Date(t).toISOString().replace('.000Z','.000900Z'));
  assert.equal(post.spotQuote.askAt,t);assert.equal(post.spotQuote.askThrough,t+1);
  assert.equal(post.decision.frame.spot,650.5);assert.ok(!post.decision.eligibleSlots.includes(11));
  const good=apply(new Date(t-1).toISOString().replace('.999Z','.999900Z'));
  assert.equal(good.spotQuote.askThrough,t);assert.equal(good.decision.frame.snapshotTime,t);
  assert.equal(good.decision.frame.spot,650.9);assert.ok(good.decision.eligibleSlots.includes(11));
});

test('a later publication can recover a pre-boundary burst without including post-boundary prints',()=>{
  const f=new ForwardFeed(),events=[event(open,1)],t=open+60000;
  poll(f,open,events);
  events.push(event(t+100,2));
  assert.equal(poll(f,t+1000,events,t).frames.length,0);
  events.push(event(t-200,3,'sell'),event(t+6000,4));
  const [settled]=poll(f,t+7000,events,t).frames;
  assert.equal(settled.frame.minuteComplete,true);assert.equal(settled.receivedAt,t+7000);
  assert.equal(settled.frame.contracts[0].sell,2);assert.equal(settled.frame.contracts[0].buy,0);
  assert.equal(settled.frame.contracts[0].cumBuy,2);
  const evidence=settled.frame.coverageDiagnostics.publicationSettlement;
  assert.equal(evidence.firstReceipt,t+1000);assert.equal(evidence.ladderPrintsAdded,1);
  assert.equal(evidence.mode,'advanced watermark');
  // Both post-boundary prints are retained for the following decision.
  events.push(event(t+66000,5));
  const [next]=poll(f,t+67000,events,t+60000).frames;
  assert.equal(next.frame.minuteComplete,true);
  assert.equal(next.frame.contracts[0].buy,4);
});
test('a quiet projection retains the normal decision deadline with bounded, recorded fallback',()=>{
  const f=new ForwardFeed(),events=[event(open,1)],t=open+60000;
  poll(f,open,events);events.push(event(t+100,2));
  assert.equal(poll(f,t+200,events,t).frames.length,0);
  const [result]=poll(f,t+9000,events,t).frames;
  assert.equal(result.frame.coverageDiagnostics.publicationSettlement.mode,'bounded fallback');
  assert.equal(result.receivedAt,t+9000);assert.equal(result.frame.minuteComplete,true);
  assert.equal(poll(f,t+10000,events,t).frames.length,0);
});
test('a late receipt beyond settling remains visible, and real source gaps never become eligible',()=>{
  const f=new ForwardFeed(),events=[event(open,1)],t=open+60000;
  poll(f,open,events);events.push(event(t+5200,2));
  poll(f,t+7000,events,t);
  events.push(event(t-250,3,'sell'),event(t+65050,4));
  const flow=projection(events);flow.session.gaps=[{start:t+1000,end:t+2000}];
  const [bad]=f.observe({open,close,receivedAt:t+66000,chain:chain(t+60000),chainReceivedAt:t+66000,flow}).frames;
  assert.deepEqual(bad.eligibleSlots,[]);
  assert.deepEqual(bad.frame.coverageDiagnostics.reasons,['ladder print arrived after its decision','collector gap overlaps minute']);
  assert.equal(bad.frame.coverageDiagnostics.overlappingGaps[0].start,t+1000);
});

test('regressed or future flow clocks cannot release a pending minute',()=>{
  const f=new ForwardFeed(),events=[event(open,1)],t=open+60000;
  poll(f,open,events);events.push(event(t+100,2));
  const flow=projection(events);
  const read=(decision,body,sampled,requested)=>f.observe({open,close,flow,receivedAt:decision,
    flowReceivedAt:body,flowSampledAt:sampled,flowRequestedAt:requested});
  assert.equal(read(t+1000,t+800,t+900,t+700).frames.length,0);
  assert.equal(read(t+9000,t+700,t+9000,t+9000).frames.length,0);
  assert.equal(read(t+9500,t+800,t+850,t+750).frames.length,0);
  assert.equal(read(t+9700,t+800,t+9900,t+9600).frames.length,0);
  const [d]=read(t+10000,t+800,t+10000,t+9700).frames;
  assert.equal(d.frame.coverageDiagnostics.flowReceivedAt,t+800);
  assert.equal(d.frame.coverageDiagnostics.publicationSettlement.ladderPrintsAdded,0);
});
