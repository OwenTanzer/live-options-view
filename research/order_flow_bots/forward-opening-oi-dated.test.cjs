'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {parseAlpaca,matchPublicSeries,readPublic}=require('./forward-opening-oi-dated.cjs');
const {openPaper,registerSession,applyFrame,applyQuote}=require('./forward-paper.cjs');
const {ForwardFeed}=require('./forward-feed.cjs');
const day='2026-11-24',settled='2026-11-23',open=Date.parse(day+'T14:30:00Z'),close=open+390*60000;
const sym='QQQ261124C00637000';
const reference={open,session:day,settledSession:settled,openingSpot:640.15,version:1,key:'intraday/20261124/opening-oi/v1/0001.json',
  sha256:'c'.repeat(64),receivedAt:open+20,bySymbol:{[sym]:4},strikes:[637000]};
const row={symbol:sym,underlying_symbol:'QQQ',root_symbol:'QQQ',size:'100',expiration_date:day,
  strike_price:'637',type:'call',open_interest_date:settled,open_interest:'4'};
const alpaca=(rows=[row])=>Buffer.from(JSON.stringify({option_contracts:rows,next_page_token:null}));
const archive=Buffer.from(['# QQQ', 'Open interest settled 2026-11-23 vs 2026-11-20.',
  '## Expiration 2026-11-24','','| Strike | Type | Prior OI | Settled OI | Change |','|---:|---|---:|---:|---:|',
  '| 637 | call | 6 | 4 | -2 |',''].join('\n'));
const line='QQQ\t\t2026\t11\t24\t637\t000\tC P\t4\t0\t180000000';
const occ=Buffer.from(['Series Search Results for QQQ','ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit',line,''].join('\n'));
test('dated source must identify the exact standard series, settlement, quantity and unambiguous identity',()=>{
  assert.equal(parseAlpaca(alpaca(),reference,open+30).bySymbol[sym],4);
  for(const changes of [{root_symbol:'QQQ1'},{size:'10'},{type:'put'},{strike_price:'638'},
    {open_interest_date:day},{open_interest_date:null},{open_interest:'3'}])
    assert.deepEqual(parseAlpaca(alpaca([{...row,...changes}]),reference,open+30).bySymbol,{});
  assert.throws(()=>parseAlpaca(alpaca([row,row]),reference,open+30));
  assert.throws(()=>parseAlpaca(Buffer.from('{"option_contracts":[],"next_page_token":"more"}'),reference,open+30));
  const match=(raw,dir)=>matchPublicSeries(raw,dir,reference,open+30);
  assert.equal(match(archive,occ).bySymbol[sym],4);
  assert.equal(match(Buffer.from(archive.toString().replace('2026-11-23','2026-11-18')),occ),null);
  assert.equal(match(archive,Buffer.concat([occ,Buffer.from(line.replace('\t24\t','\t23\t'))])),null);
  assert.deepEqual(match(archive,Buffer.concat([occ,Buffer.from(line.replace('QQQ','4QQQ'))])).bySymbol,{});
  assert.deepEqual(match(archive,Buffer.from(occ.toString().replace('C P\t4','C P\t3'))).bySymbol,{});
});
test('a stale public report costs only one GET and never qualifies',async()=>{
  let calls=0;
  const result=await readPublic(reference,async()=>{calls++;return new Response(archive.toString().replace('2026-11-23','2026-11-18'));},()=>open+50);
  assert.equal(result,null);assert.equal(calls,1);
});

test('dated OI drives a causal turnover order using corroborated quantities despite conflicting chain OI',()=>{
  const db=openPaper(':memory:');
  try {
    registerSession(db,{session:day,open,close});
    const symbols=[];
    for(let strike=637;strike<=644;strike++)
      for(const type of ['C','P']) symbols.push('QQQ261124'+type+String(strike*1000).padStart(8,'0'));
    const dated=parseAlpaca(alpaca(),reference,open+250);
    const feed=new ForwardFeed({settleMs:0}),rows=[];
    const chain=t=>({symbol:'QQQ',date:day,expiration:day,timestamp:new Date(t).toISOString(),underlying_price:640.15,
      underlying_market:{symbol:'QQQ',source:'dxlink',freshness:'live',spot:640.15,spot_ts:new Date(t).toISOString(),
        provenance:{contributing:{bid_ts:new Date(t).toISOString(),ask_ts:new Date(t).toISOString()},
          freshness:'live',price_basis:'bid_ask_midpoint',price:640.15,collector_received_at:new Date(t).toISOString()}},
      rows:symbols.map(OptionSymbol=>({OptionSymbol,Gamma:.01,OpenInterest:999}))});
    function observe(minute,size,putSize=0) {
      const t=open+minute*60000;
      rows.push({symbol:sym,t,seq:rows.length+1,size,side:'buy',price:1});
      if(putSize) rows.push({symbol:symbols[1],t,seq:rows.length+1,size:putSize,side:'buy',price:1});
      const contracts=Object.fromEntries(symbols.map(s=>[s,{B:0,S:0,U:0,prints:0}]));
      for(const event of rows){contracts[event.symbol].B+=event.size;contracts[event.symbol].prints++;}
      const flow={cap:100,rows:[...rows],session:{day,first:open,last:t,contracts,opening:contracts,gaps:[],prints:rows.length,rebuilt:{status:'rebuilt'}}};
      const args={open,close,receivedAt:t,chainReceivedAt:t,chain:chain(t),flow,openingOi:{...reference,dated}};
      const envelope=feed.observe(args).frames[0];
      const result=applyFrame(db,envelope,t);
      return {envelope,rule:result.decisions.find(d=>d.id===3)};
    }
    assert.equal(observe(0,1).envelope.eligibleSlots.includes(3),false);
    assert.equal(observe(1,1).envelope.eligibleSlots.includes(3),false);
    const fired=observe(2,3);
    assert.ok(fired.envelope.eligibleSlots.includes(3));
    assert.equal(fired.rule.signal,'long');
    assert.equal(fired.envelope.frame.contracts[0].openOi,999);
    assert.equal(fired.envelope.frame.contracts[0].verifiedOpenOi,4);
    assert.equal(db.prepare('SELECT status FROM trades WHERE id=3').get().status,'pending');
    const decision=open+120000;
    const quote=t=>({symbol:'QQQ',spot:640.15,bid:640.14,ask:640.16,t,bidAt:t,askAt:t,receivedAt:t});
    applyQuote(db,quote(decision),decision);
    assert.equal(db.prepare('SELECT status FROM trades WHERE id=3').get().status,'pending');
    applyQuote(db,quote(decision+1),decision+1);
    assert.equal(db.prepare('SELECT entry_price FROM trades WHERE id=3').get().entry_price,640.16);
    const uncertain=structuredClone(fired.envelope.frame);
    const conflicting=uncertain.contracts[1]; conflicting.cumBuy=10;
    const history=[JSON.parse(db.prepare('SELECT body FROM frames WHERE t=?').get(open+60000).body),uncertain];
    const skipped=require('./strategies-a.cjs')(history).find(x=>x.id===3);
    assert.equal(skipped.signal,null);assert.match(skipped.reason,/unverified/);
    const exit=decision+15*60000;
    applyQuote(db,quote(exit),exit);
    assert.equal(db.prepare('SELECT status FROM trades WHERE id=3').get().status,'closed');
  } finally { db.close(); }
});

test('reader propagates a dated receipt and never re-requests a fully verified packet',async()=>{
  const {createOpeningOiReader}=require('./forward-opening-oi.cjs');
  let now=open+1000, calls=[];
  const packet={schema:'opening-oi.v1',version:1,instrument:'QQQ',session:day,settledSession:settled,
    openingSpot:640.15,openAt:new Date(open).toISOString(),capturedAt:new Date(open+20).toISOString(),
    snapshotKey:'intraday/20261124/snapshot_093000020000.csv',legs:[{strike:637,call:4,put:0}]};
  const md=Buffer.from(archive.toString()+'| 637 | put | 1 | 0 | -1 |\n');
  const fetchImpl=async url=>{
    calls.push(String(url));
    if(String(url).includes('/opening-oi/')) return new Response(JSON.stringify(packet),{headers:{'Last-Modified':new Date(open+500).toUTCString()}});
    if(String(url).endsWith('/QQQ.md')) return new Response(md);
    if(String(url).includes('marketdata.theocc.com')) return new Response(occ);
    throw Error('unexpected URL');
  };
  const read=createOpeningOiReader({session:day,open,close},fetchImpl,()=>now,{});
  const first=await read();
  assert.equal(first.dated,undefined);
  now+=100;
  const second=await read();
  assert.equal(second.dated.source,'dated-archive-and-occ-series');
  assert.equal(second.dated.bySymbol[sym],4);
  assert.equal(second.dated.receivedAt,now);
  now+=20*60000;
  assert.equal((await read()).dated.receivedAt,second.dated.receivedAt);
  assert.equal(calls.length,3);
});
