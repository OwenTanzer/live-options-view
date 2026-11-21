import test from 'node:test';
import assert from 'node:assert/strict';
import { readArchivedChain,handleOneClockChain,projectChainCsv } from '../backend/one-clock-chain.js';
import { archivedChainSource } from '../site/shared/one-clock-chain-source.js';
import { createClock,createSourceRegistry } from '../site/shared/one-clock.js';
import { sessionInstant,sessionAt,snapshotInstant } from '../site/shared/one-clock-session.js';
const session='2026-11-09',T=sessionInstant(session,9,30),prefix='intraday/20261109/';
const csvKey=prefix+'snapshot_093000000000.csv',metaKey=csvKey.replace('.csv','.provenance.json');
const csv='Strike,Type,OpenInterest,Expiration,UnderlyingPrice,OptionSymbol,Bid,Ask,PRIVATE\n660,call,100,2026-11-09,661,QQQ261109C00660000,1,2,secret\n660,put,50,2026-11-09,661,QQQ261109P00660000,1,2,secret';
const meta={schema_version:'snapshot_provenance.v1',snapshot_key:csvKey,symbol:'QQQ',constructed_at:new Date(T).toISOString(),
  latest_published_at:new Date(T+1000).toISOString(),publication:{status:'published'},private:'secret',
  market_readings:[{schema_version:'market_reading.v1',reading_id:'reference_oi_skew',symbol:'QQQ',
    published_at:new Date(T+1000).toISOString(),observed_at:new Date(T).toISOString(),publication:{status:'published'},
    interpretation:{kind:'reference',account_alias:'must-not-leak'},status:{code:'ok'},freshness:{status:'live'},
    calculation:{version:'reference_oi_skew.v1',parameters:{near_money_pct:0.02,secret:'private'}},
    values:{imbalance_ratio:1/3,near_money_call_oi:100,near_money_put_oi:50,band_strikes:1,private:'secret'}}]};
function storage(changes={}) {
  const records=new Map([[csvKey,{text:csv,uploaded:T+500,etag:'csv1'}],[metaKey,{text:JSON.stringify(meta),uploaded:T+1500,etag:'meta1'}]]);
  for(const [key,value] of Object.entries(changes)) records.set(key,{...records.get(key),...value});
  const calls=[];
  return {calls,records,async list(args){calls.push(['list',args]);return {objects:[...records.keys()].map(key=>({key})),truncated:false};},
    async get(key){calls.push(['get',key]);const r=records.get(key);if(!r)return null;return {size:Buffer.byteLength(r.text),uploaded:new Date(r.uploaded),etag:r.etag,text:async()=>r.text};}};
}
const scope=(cutoff=T+2000,instrument='QQQ')=>({session,instrument,cutoff});
test('New York archive conversion handles summer/winter and validates dates',()=>{
  assert.equal(new Date(T).toISOString(),'2026-11-09T14:30:00.000Z');
  assert.equal(new Date(sessionInstant('2026-07-09',9,30)).toISOString(),'2026-07-09T13:30:00.000Z');
  assert.equal(snapshotInstant(csvKey,session),T);assert.equal(sessionAt(T),session);
  assert.throws(()=>sessionInstant('2026-02-30',9,30));
});
test('archive gates publication AND current CSV/sidecar versions and exposes only market fields',async()=>{
  const db=storage();
  assert.equal((await readArchivedChain(db,scope(T+1499))).rows.length,0);
  const result=await readArchivedChain(db,scope());
  assert.equal(result.rows.length,1);const row=result.rows[0];
  assert.equal(row.availableAt,T+1500);assert.equal(row.value.rows.length,2);
  assert.equal(row.value.market_readings[0].values.imbalance_ratio,1/3);
  assert.equal(JSON.stringify(result).includes('secret'),false);
  assert.equal(JSON.stringify(result).includes('must-not-leak'),false);
  assert.equal(row.details.digestVerification,'not-verified');
  const corrected=storage({[csvKey]:{uploaded:T+5000,etag:'csv2'}});
  assert.equal((await readArchivedChain(corrected,scope(T+4999))).rows.length,0);
  assert.notEqual((await readArchivedChain(corrected,scope(T+5000))).rows[0].id,row.id);
});
test('unpublished, unknown, mismatched and absent provenance never become historical evidence',async()=>{
  for(const patch of [{publication:{status:'archived_unpublished'}},{latest_published_at:null},{symbol:'SPY'},{snapshot_key:'private/data.csv'},{schema_version:'unknown'}]) {
    assert.equal((await readArchivedChain(storage({[metaKey]:{text:JSON.stringify({...meta,...patch})}}),scope())).rows.length,0);
  }
  const db=storage();db.records.delete(metaKey);
  assert.equal((await readArchivedChain(db,scope())).rows.length,0);
  assert.equal(db.calls.filter(c=>c[0]==='get').length,0);
});
test('reader bounds listing and object bytes, without sampling an incomplete index',async()=>{
  const db=storage();db.list=async()=>({objects:[{key:metaKey}],truncated:true,cursor:'more'});
  const result=await readArchivedChain(db,scope());
  assert.equal(result.coverage.reason,'archive-index-bound');assert.equal(result.coverage.lists,2);
  assert.equal(result.rows.length,0);assert.equal(db.calls.length,0);
  const large=storage();large.get=async()=>({size:1024*1024+1});
  await assert.rejects(readArchivedChain(large,scope()),/byte bound/);
});
test('route rejects unauthorized key/path scope before touching storage and uses no KV',async()=>{
  const env=new Proxy({PAPER_TRADES:storage()},{get(target,key){assert.equal(key,'PAPER_TRADES');return target[key];}});
  for(const query of ['session=2026-11-09&instrument=QQQ&cutoff=1','session=2026-11-09&instrument=SECRET&cutoff='+T,'session=2026-11-09&instrument=QQQ&cutoff='+T+'&key=private']) {
    const req=new Request('https://example.test/api/one-clock/chain?'+query);
    const response=await handleOneClockChain(req,env,new URL(req.url));assert.equal(response.status,400);
  }
  const req=new Request('https://example.test/api/one-clock/chain?'+new URLSearchParams(scope()));
  const response=await handleOneClockChain(req,env,new URL(req.url));assert.equal(response.status,200);
  assert.equal((await response.json()).rows.length,1);
  assert.equal(response.headers.get('Cache-Control'),'no-store');
});
test('shared adapter reuses one request across readers and discards late responses after rewind',async()=>{
  const registry=createSourceRegistry();let calls=0,release;
  registry.register(archivedChainSource({fetchImpl:async()=>{calls++;await new Promise(resolve=>{release=resolve;});return Response.json(await readArchivedChain(storage(),scope()));}}));
  const clock=createClock({registry,initial:{...scope(),mode:'replay',origin:'recorded'}});
  const a=clock.read('chain'),b=clock.read('chain');assert.equal(a,b);await Promise.resolve();
  clock.select({...scope(T),mode:'replay',origin:'recorded'});release();
  assert.deepEqual(await a,{discarded:true});assert.equal(calls,1);
});
test('CSV projection rejects malformed numeric data rather than treating absence as zero',()=>{
  assert.throws(()=>projectChainCsv(csv.replace('call,100','call,'),'QQQ',session));
  assert.throws(()=>projectChainCsv(csv.replace('660,call','-1,call'),'QQQ',session));
});
