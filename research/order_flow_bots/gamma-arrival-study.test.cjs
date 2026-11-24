'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {at,frameAt,runArm,fillOpportunity,confirmedMs,stats}=require('./gamma-arrival-study.cjs');
const MIN=60000,open=Date.parse('2026-11-24T14:30:00Z');
const ladder=Array.from({length:8},(_,i)=>['C','P'].map(type=>({symbol:type+i,strike:97+i,type}))).flat();
function fixtures() {
  const quotes=Array.from({length:20},(_,i)=>{const t=open+i*MIN-10;return {at:t,bidAt:t,askAt:t,bid:100,ask:100.02};});
  const sums={buy:3,sell:0,unknown:0,buyPremium:300,sellPremium:0,unknownPremium:0,printCount:1,unknownCount:0};
  const rowsBySymbol=new Map(ladder.map(c=>[c.symbol,[{at:open+1000,...sums},{at:open+62000,...sums,buy:8,buyPremium:1300,printCount:2}]]));
  const gamma=new Map(ladder.map(c=>[c.symbol,0.1]));
  const chains=quotes.map(q=>({at:q.at+10,captureMs:q.at+10,key:'k'+q.at,gamma}));
  return {tape:{rowsBySymbol,quotes,gaps:[],last:open+20*MIN},chains};
}
test('use receipt and publication upper bound, including exact boundary and millisecond ties',()=>{
  assert.equal(confirmedMs('2026-11-24T14:30:00.000001Z'),open+1);
  assert.equal(confirmedMs('2026-11-24T14:30:00.000000Z'),open);
  assert.equal(at([{at:1,k:1},{at:2,k:2},{at:2,k:3}],2).k,3);
  const {tape,chains}=fixtures();
  const f=frameAt(open+61000,open,tape,chains,ladder);
  assert.equal(f.contracts[0].flow.B,3);
  assert.equal(f.contracts[0].buy,3);
  const g=frameAt(open+62000,open,tape,chains,ladder);
  assert.equal(g.contracts[0].flow.B,3);
  assert.equal(g.contracts[0].buy,0);
  assert.equal(frameAt(open+62001,open,tape,chains,ladder).contracts[0].buy,5);
  const next={at:open+61000,bidAt:open+61000,askAt:open+61000,bid:102,ask:104};
  tape.quotes.push(next);tape.quotes.sort((a,b)=>a.at-b.at);
  assert.equal(frameAt(next.at,open,tape,chains,ladder).spot,(100+100.02)/2);
  assert.equal(frameAt(next.at+1,open,tape,chains,ladder).spot,103);
  assert.equal(frameAt(open+MIN-1,open,tape,chains,ladder).chainKey,chains[0].key);
});
test('rolling strategy arms can disagree when later prices break the preceding range',()=>{
  const {tape,chains}=fixtures(),t=open+16*MIN;
  tape.quotes.push({at:t+5000,bidAt:t+5000,askAt:t+5000,bid:100.1,ask:100.12});
  tape.quotes.sort((a,b)=>a.at-b.at);
  // Keep the same phase of each prior rolling window fully quote-covered.
  const a=runArm(t,open,tape,chains,ladder),b=runArm(t+5001,open,tape,chains,ladder);
  assert.equal(a.rules.find(r=>r.id===1).signal,null);
  assert.equal(b.rules.find(r=>r.id===1).signal,'long');
  tape.gaps.push(t+5001);
  assert.equal(runArm(t+5001,open,tape,chains,ladder).eligible,false);
});
test('reject stale underlying and exclude same-time or out-of-window fill opportunities',()=>{
  const {tape,chains}=fixtures();
  assert.equal(runArm(open+16000,open,tape,chains,ladder).eligible,false);
  const rows=[{t:10,received_at:12},{t:11,received_at:20},{t:20,received_at:21}];
  assert.equal(fillOpportunity(rows,10,19),undefined);
  assert.equal(fillOpportunity(rows,10,20),rows[1]);
  assert.deepEqual(stats([]),{n:0,min:null,median:null,p90:null,max:null});
});
