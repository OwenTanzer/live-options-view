import assert from 'node:assert/strict';
import {model,flowResponse,alkalusFlow,ALKALUS} from '../site/shared/intraminute.js';
const minute=1794580560000;
const row=(kind,offset,rest)=>({kind,at:minute+offset,...rest});
const quote=(at,mid)=>row('quote',at,{bid:mid-.01,ask:mid+.01});
const print=(seq,type,size,label,basis,strike)=>row('option',80,{seq,type,size,label,basis,strike,symbol:'QQQ-'+type+strike});
const rows=[quote(50,670),print(1,'C',10,'buy','q1',670),print(2,'P',3,'sell','q5',670),
 print(3,'P',4,'buy','tick',671),print(1,'C',10,'buy','q1',670),quote(1050,670.5)];
const opts={minute,cutoff:minute+1099,width:100};
const tape=model(rows,opts);
assert.deepEqual(tape.bins[0].C,[10,0,0]);
assert.deepEqual(tape.bins[0].P,[0,3,4]);
assert.deepEqual(flowResponse(tape,0,100,minute,opts.cutoff),{pressure:13,imbalance:1,response:.5});
assert.equal(flowResponse(tape,0,100,minute,minute+1098).response,null);
assert.equal(model(rows,{...opts,strike:670}).unknown,0);
const gap=row('gap',900,{start:minute+400,reason:'stream_reconnect_resumed'});
assert.equal(flowResponse(model([...rows,gap],opts),0,100,minute,opts.cutoff).response,null);
assert.equal(model(rows,{...opts,cutoff:minute+79}).calls,0);
console.log('Intraminute signing, receipts, strike scope, gaps, response and duplicate checks passed');
const carried=row('gap',0,{reason:'stream_disconnect',start:minute});
const resume=row('gap',100,{reason:'stream_reconnect_resumed',start:minute-100});
const recovered=[carried,resume,quote(120,671)];
assert.deepEqual(model(recovered,{...opts,cutoff:minute+50}).gaps,[[minute,minute+50]]);
const after=model(recovered,{...opts,cutoff:minute+125});
assert.equal(after.bins[1].mid,671);
assert.deepEqual(after.gaps,[[minute-100,minute+100]]);
const original=row('stock',5,{t:minute+5,seq:123,flag:'@',price:700,size:2});
const amendments=model([original,{...original,at:minute+10,cancel:true},
  {...original,at:minute+11,correction:true},{...original,at:minute+12,correction:true,price:701}],opts);
assert.equal(amendments.inspected.length,4);
assert.equal(amendments.bins[0].shares,2);
assert.equal(amendments.bins[0].high,700);

// Preliminary Alkalus flow classification: owned 1 s bin only, spread $0.02
// (quote helper), thresholds 1 spread copper / 5 spreads golden, signed
// pressure must agree with the displacement direction.
assert.equal(ALKALUS.COPPER_SPREADS,1);
assert.equal(ALKALUS.GOLDEN_SPREADS,5);
const alkOpts={minute,cutoff:minute+1999,width:1000};
const alkBin=(first,last,prints,extra=[])=>model(
  [quote(10,first),...prints,quote(900,last),...extra],alkOpts);
const buyCall=print(1,'C',10,'buy','q1',670);
// Existing fixture bin: one midpoint, no displacement -> lead.
assert.equal(alkalusFlow(tape,0,100,minute).kind,'lead');
// Golden: +$0.12 is 6 spreads, agreeing call-buy pressure.
const golden=alkalusFlow(alkBin(670,670.12,[buyCall]),0,1000,minute);
assert.equal(golden.kind,'golden');
assert.ok(Math.abs(golden.displacementSpreads-6)<1e-9);
assert.equal(golden.pressure,10);
assert.equal(golden.imbalance,1);
// Copper: +$0.04 is 2 spreads, agreeing call-buy pressure.
assert.equal(alkalusFlow(alkBin(670,670.04,[buyCall]),0,1000,minute).kind,'copper');
// Lead: +$0.01 is 0.5 spreads, and no displacement at all, with flow present.
assert.equal(alkalusFlow(alkBin(670,670.01,[buyCall]),0,1000,minute).kind,'lead');
assert.equal(alkalusFlow(alkBin(670,670,[buyCall]),0,1000,minute).kind,'lead');
// Lead: displacement against the signed flow is non-conversion, not gold/copper.
const buyPut=print(1,'P',10,'buy','q1',670);
assert.equal(alkalusFlow(alkBin(670,670.06,[buyPut]),0,1000,minute).kind,'lead');
// Unclassified: flow with no midpoint, and midpoint move with no flow.
assert.equal(alkalusFlow(model([buyCall],alkOpts),0,1000,minute),null);
assert.equal(alkalusFlow(alkBin(670,670.04,[]),0,1000,minute),null);
// Unclassified: a recorded gap overlaps the bin, so the move is not observed.
const gapInBin=row('gap',500,{start:minute+400,reason:'stream_reconnect_resumed'});
assert.equal(alkalusFlow(alkBin(670,670.04,[buyCall],[gapInBin]),0,1000,minute),null);
// Regression: gold appearing one second after the exit must not reclassify
// the owned bin. Only the later midpoint changes; the classification is identical.
const laterGold=alkBin(670,670.04,[buyCall],[quote(1100,671)]);
const laterDown=alkBin(670,670.04,[buyCall],[quote(1100,669)]);
assert.ok(Math.abs(flowResponse(laterGold,0,1000,minute,alkOpts.cutoff).response-.96)<1e-9);
assert.ok(Math.abs(flowResponse(laterDown,0,1000,minute,alkOpts.cutoff).response+1.04)<1e-9);
assert.deepEqual(alkalusFlow(laterGold,0,1000,minute),alkalusFlow(laterDown,0,1000,minute));
assert.equal(alkalusFlow(laterGold,0,1000,minute).kind,'copper');
console.log('Alkalus flow classification (golden/copper/lead, unclassified, owned-interval-only) passed');
