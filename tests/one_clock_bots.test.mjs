import test from 'node:test';
import assert from 'node:assert/strict';
import { readBotLedgerPage,handleOneClockBots } from '../backend/one-clock-bots.js';
import { botLedgerSource,botInventory } from '../site/shared/one-clock-bot-source.js';
import { createClock,createSourceRegistry } from '../site/shared/one-clock.js';
import { sessionInstant } from '../site/shared/one-clock-session.js';
const session='2026-11-09',T=sessionInstant(session,9,30),bot='crassus_test';
const scope={bot,session,instrument:'QQQ',cutoff:T+3600000};
function fixture(count=105) {
  const trades=[],objects=new Map(),calls=[];
  for(let i=0;i<count;i++) {
    const id=`00000000-0000-4000-8000-${String(i).padStart(12,'0')}`;
    const t={execution_request_id:id,execution_id:id,username:bot,status:'filled',ts:new Date(T+(i-1)*1000).toISOString(),
      sym:'QQQ261113C00660000',underlying:'QQQ',instrument_type:'option',side:'buy',qty:1,price:2,type:'call',strike:660,exp:'2026-11-13',multiplier:100,private:'never expose'};
    trades.push(t);objects.set(`paper-trades/requests/${id}.json`,{text:JSON.stringify(t),uploaded:new Date(T+i*1000),etag:String(i)});
  }
  const bucket={async get(key){calls.push(key);const r=objects.get(key);return r?{...r,size:Buffer.byteLength(r.text),text:async()=>r.text}:null;}};
  return {record:{username:bot,is_bot:true,trades},bucket,objects,calls};
}
function adapter(db,onCoverage=()=>{}) {
  return botLedgerSource({getBot:()=>bot,onCoverage,fetchImpl:async(url)=>{
    const q=new URL(url,'https://test/').searchParams;
    const result=await readBotLedgerPage(db.bucket,db.record,{...scope,cutoff:Number(q.get('cutoff')),offset:Number(q.get('offset')),version:q.get('version')});
    return Response.json(result,{status:result.restart?409:200});
  }});
}
test('more than 100 fills page without truncation and preserve overnight inventory / partial exits',async()=>{
  const db=fixture();const first=db.record.trades[0];first.ts=new Date(T-86400000).toISOString();first.qty=10;
  db.objects.get(`paper-trades/requests/${first.execution_request_id}.json`).text=JSON.stringify(first);
  db.objects.get(`paper-trades/requests/${first.execution_request_id}.json`).uploaded=new Date(T-86399000);
  const last=db.record.trades.at(-1);last.side='sell';last.qty=4;
  db.objects.get(`paper-trades/requests/${last.execution_request_id}.json`).text=JSON.stringify(last);
  let coverage;const source=adapter(db,c=>coverage=c);
  const rows=await source.read({...scope,signal:new AbortController().signal});
  assert.equal(rows.length,105);assert.equal(coverage.pages,3);assert.equal(coverage.complete,true);
  assert.equal(db.calls.length,105);assert.equal(JSON.stringify(rows).includes('never expose'),false);
  const inventory=botInventory(rows,{session,complete:true});
  assert.equal(inventory.opening[0].qty,10);assert.equal(inventory.current[0].qty,109);
});
test('publication gates fill rows and inventory; rewind cannot reveal a future terminal body',async()=>{
  const db=fixture(2);let bodyReads=0;
  const get=db.bucket.get;db.bucket.get=async key=>{const object=await get(key);const read=object.text;object.text=()=>{bodyReads++;return read();};return object;};
  let result=await readBotLedgerPage(db.bucket,db.record,{...scope,cutoff:T});
  assert.equal(result.rows.length,1);assert.equal(bodyReads,1);
  result=await readBotLedgerPage(db.bucket,db.record,{...scope,cutoff:T+1000});assert.equal(result.rows.length,2);
  result=await readBotLedgerPage(db.bucket,db.record,{...scope,cutoff:T});assert.equal(result.rows.length,1);
  assert.equal(result.rows[0].availableAt,T);
});
test('missing settlement receipts, mismatched accounts and changed payloads invalidate the book',async()=>{
  for(const kind of ['missing','account','payload','settlement','oversized']) {
    const db=fixture(1),t=db.record.trades[0],key=`paper-trades/requests/${t.execution_request_id}.json`,object=db.objects.get(key);
    if(kind==='missing')db.objects.delete(key);
    if(kind==='account')object.text=JSON.stringify({...t,username:'another_user'});
    if(kind==='payload')object.text=JSON.stringify({...t,qty:2});
    if(kind==='settlement')t.execution_request_id='settle:QQQ:2026-11-09';
    if(kind==='oversized')object.text=' '.repeat(65537);
    const result=await readBotLedgerPage(db.bucket,db.record,scope);
    assert.equal(result.rows.length,0,kind);assert.equal(result.coverage.complete,false,kind);
    assert.deepEqual(botInventory(result.rows,{session,complete:false}),{opening:null,current:null});
  }
});
test('continuations bind to the eligible ledger while future appended entries cannot change replay',async()=>{
  const db=fixture(),first=await readBotLedgerPage(db.bucket,db.record,scope);
  const future={...db.record.trades[0],ts:new Date(scope.cutoff+1000).toISOString()};db.record.trades.push(future);
  const second=await readBotLedgerPage(db.bucket,db.record,{...scope,offset:first.next,version:first.version});assert.equal(second.restart,undefined);
  db.record.trades[0].price=3;
  assert.deepEqual(await readBotLedgerPage(db.bucket,db.record,{...scope,offset:first.next,version:first.version}),{restart:true});
});
test('unknown instrument is a coverage gap and midnight publication is inclusively visible',async()=>{
  const db=fixture(1),t=db.record.trades[0];
  db.record.trades.push({ts:new Date(T-86400000).toISOString(),side:'buy',qty:10,price:2});
  assert.equal((await readBotLedgerPage(db.bucket,db.record,scope)).coverage.complete,false);
  db.record.trades.pop();
  t.ts=new Date(T-86400000).toISOString();
  const object=db.objects.get(`paper-trades/requests/${t.execution_request_id}.json`);
  object.text=JSON.stringify(t);object.uploaded=new Date(sessionInstant(session,0));
  const result=await readBotLedgerPage(db.bucket,db.record,scope);
  assert.equal(botInventory(result.rows,{session,complete:true}).opening[0].qty,1);
});
test('route validates scope before storage and rejects private/hidden accounts without reading receipts',async()=>{
  const forbidden=new Proxy({},{get(){throw new Error('must not access storage');}});
  for(const query of ['bot=../private','bot=crassus_test&session=2026-11-09&instrument=QQQ&cutoff=1',new URLSearchParams({...scope,offset:40})]) {
    const req=new Request('https://test/api/one-clock/bots?'+query);assert.equal((await handleOneClockBots(req,forbidden,new URL(req.url))).status,400);
  }
  const db=fixture(1);let receiptReads=0;
  const env={PAPER_TRADES:{get:async key=>{if(key==='system/bot-membership-v1.json')return {json:async()=>({schema_version:1,ready:true,members:[bot]})};receiptReads++;throw new Error('private');}},
    DB:{prepare:()=>({bind:()=>({first:async()=>({record:JSON.stringify({...db.record,strategy_id:'reddit_sentiment_qqq_hidden'})})})})}};
  const req=new Request('https://test/api/one-clock/bots?'+new URLSearchParams(scope));
  assert.equal((await handleOneClockBots(req,env,new URL(req.url))).status,404);assert.equal(receiptReads,0);
});
test('shared clock cancels paged ledger response on scope changes',async()=>{
  const db=fixture(1);let release,coverageCalled=false;
  const source=adapter(db,()=>coverageCalled=true),read=source.read;
  source.read=async state=>{await new Promise(resolve=>release=resolve);return read(state);};
  const registry=createSourceRegistry();registry.register(source);
  const clock=createClock({registry,initial:{...scope,mode:'replay',origin:'recorded'}});
  const pending=clock.read('bot-ledger');await Promise.resolve();clock.select({...clock.snapshot(),cutoff:T});release();
  assert.deepEqual(await pending,{discarded:true});assert.equal(coverageCalled,false);
});
