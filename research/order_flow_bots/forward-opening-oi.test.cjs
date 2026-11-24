'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {createOpeningOiReader,parseReference} = require('./forward-opening-oi.cjs');
const evaluateB = require('./strategies-b.cjs');

const session='2026-11-23';
const open=Date.parse(session+'T14:30:00Z');
const bounds={session,open,close:open+390*60000};
const makeArgs = () => ({
  bounds,key:'intraday/20261123/opening-oi/v1/0001.json',version:1,lastModified:new Date(open+40000).toUTCString(),receivedAt:open+42000,
  raw:JSON.stringify({schema:'opening-oi.v1',instrument:'QQQ',version:1,session,settledSession:'2026-11-20',
    openAt:new Date(open).toISOString(),capturedAt:new Date(open+37626).toISOString(),snapshotKey:'intraday/20261123/snapshot_093037626144.csv',
    openingSpot:655.98,legs:[{strike:655,call:20,put:null}]})
});

test('observed opening reference requires original session, receipt, immutable revisions and genuine OI quantities',()=>{
  const args=makeArgs();
  const valid=parseReference(args);
  assert.equal(valid.clearingVerified,false);
  assert.equal(valid.bySymbol.QQQ261123C00655000,20);
  assert.ok(!Object.hasOwn(valid.bySymbol,'QQQ261123P00655000'));
  assert.match(valid.sha256,/^[a-f0-9]{64}$/);
  assert.throws(()=>parseReference({...args,receivedAt:open+37000}));
  assert.throws(()=>parseReference({...args,lastModified:undefined}));
  assert.throws(()=>parseReference({...args,raw:args.raw.replace('2026-11-20','2026-11-22')}));
  assert.throws(()=>parseReference({...args,raw:args.raw.replace('"call":20','"call":-1')}));
  const revision={...args,version:2,key:args.key.replace('0001','0002'),previous:valid,
    raw:args.raw.replace('"version":1','"version":2').replace('"put":null','"put":17')};
  assert.equal(parseReference(revision).bySymbol.QQQ261123P00655000,17);
  assert.throws(()=>parseReference({...revision,raw:revision.raw.replace('"call":20','"call":21')}));
});

test('wait for real publication and throttle missing packets without delaying the main feed',async()=>{
  let now=open-1, requests=0;
  const args=makeArgs();
  async function fetchImpl(url) {
    assert.equal(url,'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'+args.key);
    requests++;
    if(requests===1) return {ok:false,status:404};
    return {ok:true,status:200,text:async()=>args.raw,headers:{get:key=>key==='last-modified'?args.lastModified:null}};
  }
  const reader=createOpeningOiReader(bounds,fetchImpl,()=>now);
  assert.equal(await reader(),null);
  now=open; assert.equal(await reader(),null);
  now=open+22500; assert.equal(await reader(),null);
  assert.equal(requests,1);
  now=open+52000;
  assert.equal((await reader()).receivedAt,now);
});

test('a received official opening reference can recover slot nine after a missing opening observation',()=>{
  const c={type:'P',strike:650,symbol:'QQQ261123P00650000',buyPremium:100,sellPremium:0,unknownPremium:0,buy:1,sell:0,unknown:0};
  const base={session,t:open+60000,complete:true,snapshotTime:open,spot:651,contracts:[c],openingReferenceComplete:false};
  const now={...base,t:base.t+60000,spot:650.5,officialOpeningReference:{spot:650.9,bySymbol:{[c.symbol]:123}}};
  const result=frames=>evaluateB(frames).find(r=>r.id===9);
  assert.equal(result([base,now]).signal,'short');
  assert.equal(result([base,{...now,officialOpeningReference:null}]).signal,null);
  assert.equal(result([base,{...now,officialOpeningReference:{spot:650.9,bySymbol:{}}}]).signal,null);
});

test('two strikes with stale underlying observations cannot qualify a parity signal',()=>{
  const frames=[0,1].map((m)=>({t:open+60000*m,complete:true,contracts:[650,651].flatMap(strike=>['C','P'].map(type=>({
    strike,type,time:open+60000*m-1000,price:1+(type==='C'?m*0.2:0),openOi:10}))),
    spot:650.5,snapshotTime:open+60000*m-6000}));
  const parity=evaluateB(frames).find(r=>r.id===11);
  assert.equal(parity.signal,null);
});
