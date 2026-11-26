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
    input:{snapshot_key:csvKey,snapshot_key_status:'valid',snapshot_timestamp:new Date(T).toISOString()},
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
test('public chain route returns a late revision only once its receipt is available',async()=>{
  const later=sessionInstant('2026-11-10',9,30),db=storage({[metaKey]:{uploaded:later}});
  for(const cutoff of [later-1,later]){
    const url=new URL('https://example.test/api/one-clock/chain?'+new URLSearchParams(scope(cutoff)));
    const response=await handleOneClockChain(new Request(url),{PAPER_TRADES:db},url);
    assert.equal(response.status,200);
    assert.equal((await response.json()).rows.length,cutoff===later?1:0);
  }
});
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
  const env=new Proxy({PAPER_TRADES:storage()},{get(target,key){assert.ok(['PAPER_TRADES','ONE_CLOCK_PUBLICATIONS'].includes(key));return target[key];}});
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
  for(const changed of [csv.replace('QQQ261109C','SPY261109C'),csv.replace('QQQ261109C','QQQ261113C'),
    csv.replace('QQQ261109C','QQQ261109P'),csv.replace('C00660000','C00661000'),csv.replace('QQQ261109C00660000','malformed')]) {
    assert.throws(()=>projectChainCsv(changed,'QQQ',session),/contract identity/);
  }
});

test('source digest is a bounded hash or null, never an arbitrary public payload',async()=>{
  for(const digest of [{private_ledger:'secret'},'secret','a'.repeat(100000)]) {
    const result=await readArchivedChain(storage({[metaKey]:{text:JSON.stringify({...meta,market_readings_digest:digest})}}),scope());
    assert.equal(result.rows[0].details.sourceDigest,null);
    assert.equal(JSON.stringify(result).includes('secret'),false);
  }
});

test('published skew must identify the exact eligible parent snapshot',async()=>{
  for(const patch of [{symbol:'SPY'},{input:null},{input:{...meta.market_readings[0].input,snapshot_key:'another.csv'}},
    {input:{...meta.market_readings[0].input,snapshot_timestamp:new Date(T-60000).toISOString()}}]) {
    const changed={...meta,market_readings:[{...meta.market_readings[0],...patch}]};
    const result=await readArchivedChain(storage({[metaKey]:{text:JSON.stringify(changed)}}),scope());
    assert.equal(result.rows.length,1);assert.deepEqual(result.rows[0].value.market_readings,[]);
  }
});

test('IBIT retains its source-selected upcoming expiry rather than imposing zero DTE',async()=>{
  const db=storage(),ibitCsv=csvKey.replace('intraday/','intraday/ibit/'),ibitMeta=ibitCsv.replace('.csv','.provenance.json');
  db.records.set(ibitCsv,{text:csv.replaceAll('2026-11-09','2026-11-13').replaceAll('QQQ261109','IBIT261113'),uploaded:T+500,etag:'ibitcsv'});
  db.records.set(ibitMeta,{text:JSON.stringify({...meta,symbol:'IBIT',snapshot_key:ibitCsv,market_readings:[]}),uploaded:T+1500,etag:'ibitmeta'});
  const result=await readArchivedChain(db,scope(T+2000,'IBIT'));
  assert.equal(result.rows.length,1);assert.equal(result.rows[0].value.expiration,'2026-11-13');
  assert.equal(result.rows[0].session,'2026-11-09');
  db.records.get(ibitCsv).text+='\n660,call,1,2026-11-20,661,IBIT261120C00660000,1,2,secret';
  assert.equal((await readArchivedChain(db,scope(T+2000,'IBIT'))).rows.length,0);
});

test('market replay selects the immutable publication before the compatibility sidecar, verifies bytes and honors retraction',async t=>{
  const {makeStorage}=await import('./helpers/d1_shim.mjs');
  const {readFileSync}=await import('node:fs');
  const {createHash}=await import('node:crypto');
  const DB=makeStorage(),bucket=storage(),stream='collector-chain/QQQ';
  t.after(()=>DB.db.close());
  for(const file of ['0011_one_clock_publications.sql','0014_one_clock_chain_evidence.sql'])
    await DB.exec(readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
  const evidence=[];
  function blob(text,schema,contentType) {
    const wire=Buffer.from(text),sha256=createHash('sha256').update(wire).digest('hex');
    const key='publications/v1/'+stream+'/blobs/'+sha256;
    evidence.push({key,sha256,bytes:wire.length,schema,contentType});
    bucket.records.set(key,{text,uploaded:T+250,etag:sha256});
  }
  blob(csv,'collector.snapshot.csv.v1','text/csv');
  blob(JSON.stringify({symbol:'QQQ',date:session,expiration:session,timestamp:new Date(T).toISOString(),
    snapshot_key:csvKey,underlying_price:661,private:'account-secret',market_readings:[]}),
    'collector.snapshot.payload.v1','application/json');
  const archivedGet=bucket.get;
  bucket.get=async key=>{
    const object=await archivedGet.call(bucket,key);
    if(object)object.arrayBuffer=async()=>new TextEncoder().encode(await object.text()).buffer;
    return object;
  };
  const id='native:'+stream+':'+csvKey;
  async function retain(revision,availableAt,value,refs=evidence){
    const evidenceKey='publications/v1/'+stream+'/entries/'+String(revision).padStart(12,'0')+'.json';
    const data={source:'price',id,revision,session,instrument:'QQQ',eventAt:T,availableAt,evidence:evidenceKey,
      value,details:{originalRecordId:csvKey,sequence:revision,evidence:refs}};
    await DB.prepare('INSERT INTO one_clock_publication_entries VALUES(?,?,?,?,?,?,?)').bind(stream,revision,'test-'+revision,'digest',availableAt,T+5000,'{}').run();
    await DB.prepare('INSERT INTO one_clock_publication_records VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind(stream,'price',id,revision,session,'QQQ',T,availableAt,T+5000,revision,JSON.stringify(data)).run();
  }
  await retain(1,T+1000,{spot:661});await retain(2,T+4000,null);
  await retain(3,T+5000,null,[{schema:'collector.snapshot.withdrawal.v1'}]);
  const env={DB,PAPER_TRADES:bucket,ONE_CLOCK_PUBLICATIONS:'enabled'};
  const request=ms=>{
    const url=new URL('https://example.test/api/one-clock/chain?'+new URLSearchParams(scope(ms)));
    return handleOneClockChain(new Request(url),env,url);
  };
  assert.equal((await (await request(T+999)).json()).rows.length,0);
  const {journalContextAsOf}=await import('../backend/trade-journal-context.js');
  const position={position_id:'fixture',contract:{underlying:'QQQ',occ_symbol:'QQQ261109C00660000'}};
  const journal=await journalContextAsOf(env,position,session,T+1000);
  assert.equal(journal.available_at,T+1000);
  assert.equal(journal.underlying.segments[0].points[0][1],661);
  assert.deepEqual(journal.option.segments[0].points[0].slice(1),[1,2]);
  const first=await (await request(T+1000)).json();
  assert.equal(first.rows[0].value.underlying_price,661);
  assert.equal(first.rows[0].details.digestVerification,'verified');
  assert.equal(first.rows[0].availableAt,T+1000);
  assert.ok(!JSON.stringify(first).includes('account-secret'));
  const stale=(await (await request(T+4000)).json()).rows[0];
  assert.equal(stale.value.underlying_price,null);
  assert.equal(stale.value.rows.length,2);
  assert.ok(stale.value.rows.every(row=>row.UnderlyingPrice===null && row.Bid===1));
  assert.equal((await (await request(T+5000)).json()).rows.length,0);
  const explain=DB.queries.find(s=>s.includes('INDEXED BY one_clock_native_chain_replay'));
  assert.match(DB.raw('EXPLAIN QUERY PLAN '+explain,stream,session,T+1000,13).map(r=>r.detail).join(),/SEARCH.*one_clock_native_chain_replay/);
  const bytes=bucket.records.get(evidence[0].key);bytes.text=bytes.text.replace('661','662');
  assert.equal((await request(T+1000)).status,503);
});

test('window projection admits only the bounded fields and as-of timestamps',async()=>{
  const iso = ms => new Date(ms).toISOString();
  const good={status:'ok',return_pct:1.2,direction:'up',anchor_observed_at:iso(T),secret:'bad'};
  const market={symbol:'QQQ',spot_ts:iso(T),freshness:'live',momentum_windows:{
    observed_at:iso(T),session,readings:{session:good,'5m':{...good,anchor_observed_at:iso(T+1)},private:good}}};
  const read=async m=>(await readArchivedChain(storage({[metaKey]:{text:JSON.stringify({...meta,market:m})}}),scope())).rows[0].value.underlying_market;
  const r=(await read(market)).momentum_windows.readings;
  assert.deepEqual(r.session,{status:'ok',return_pct:1.2,direction:'up',anchor_observed_at:iso(T)});
  assert.equal(r['5m'].return_pct,null);
  assert.equal(r.private,undefined);
  assert.equal(await read({...market,symbol:'SPY'}),undefined);
  assert.equal(await read({...market,spot_ts:iso(T+20)}),undefined);
});
