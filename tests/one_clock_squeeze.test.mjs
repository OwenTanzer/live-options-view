import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {advanceSourceCatalog} from '../backend/one-clock-source-catalog.js';
import {squeezeCatalog} from '../backend/one-clock-catalog-sources.js';
import {publicIngestionSources} from '../backend/one-clock-ingestion-sources.js';
import {ingestPublicSource,ingestedAsOf} from '../backend/one-clock-ingestion.js';
import { readSqueezePublications,handleOneClockSqueeze } from '../backend/one-clock-squeeze.js';
import { squeezeSource } from '../site/shared/one-clock-squeeze-source.js';
import { sessionInstant } from '../site/shared/one-clock-session.js';
import { createClock,createSourceRegistry,readingAsOf } from '../site/shared/one-clock.js';
import { evaluateEvents } from '../site/shared/one-clock-events.js';
const session='2026-11-09',T=sessionInstant(session,9,30),prefix='squeeze-scanner/v1/scheduled/';
const hash=s=>createHash('sha256').update(s).digest('hex');
const scope=(cutoff=T+5000,instrument='ABC')=>({session,instrument,cutoff});
function fixture({ticker='ABC',listed=true,status='complete',latest='advanced',run='11111111-1111-4111-8111-111111111111',started=T-60000,finished=T,published=T+2000,receiptAt=T+2500}={}) {
  const attempt='22222222-2222-4222-8222-222222222222',base=prefix+'runs/'+run;
  const key=prefix+`publication-attempts/${run}/${attempt}/finished.json`;
  const results=JSON.stringify(listed?[{ticker,comparison_eligible:true,ranks:{combined:1},scores:{combined:.7,factor:.8,options:.6},private:'must-not-leak'}]:[]);
  const manifest=JSON.stringify({schema_version:1,run_id:run,started_at:new Date(started).toISOString(),finished_at:new Date(finished).toISOString(),status,
    scoring:{version:'baseline-60-40-v1'},files:{'results.json':{sha256:hash(results),bytes:Buffer.byteLength(results)}}});
  const objects=new Map([[base+'/results.json',{text:results,at:finished+500}],[base+'/manifest.json',{text:manifest,at:finished+1000}],
    [base+'/COMMITTED.json',{text:JSON.stringify({run_id:run,manifest_sha256:hash(manifest)}),at:finished+1500}],
    [key.replace('finished.json','started.json'),{text:JSON.stringify({at:new Date(finished+100).toISOString(),run_id:run}),at:finished+100}],
    [key,{text:JSON.stringify({at:new Date(published).toISOString(),run_id:run,archive:base,archive_verified:true,latest}),at:receiptAt}]]);
  const calls=[];
  const bucket={async list(){calls.push('list');return {objects:[...objects].map(([key,r])=>({key,uploaded:new Date(r.at)})),truncated:false};},
    async get(key){calls.push(key);const r=objects.get(key);return r?{size:Buffer.byteLength(r.text),uploaded:new Date(r.at),etag:hash(r.text),text:async()=>r.text}:null;}};
  return {objects,bucket,key,base,calls};
}
test('shortlist uses proven successful publication, verifies digests and strips other report fields',async()=>{
  const db=fixture();
  assert.equal((await readSqueezePublications(db.bucket,scope(T+2499))).rows.length,0);
  const result=await readSqueezePublications(db.bucket,scope(T+2500));
  assert.equal(result.rows.length,1);assert.equal(result.rows[0].availableAt,T+2500);
  assert.equal(result.rows[0].value.listed,true);assert.equal(result.rows[0].value.rank,1);
  assert.equal(JSON.stringify(result).includes('must-not-leak'),false);assert.equal(result.coverage.publicationComplete,true);
  assert.equal(result.rows[0].details.digestVerification,'manifest-and-results');
});
test('absence requires a qualified published run; unavailable is not an empty shortlist',async()=>{
  const empty=await readSqueezePublications(fixture({listed:false,status:'empty'}).bucket,scope());
  assert.equal(empty.rows[0].value.listed,false);
  for(const latest of ['unchanged','not_eligible'])assert.equal((await readSqueezePublications(fixture({latest}).bucket,scope())).rows.length,0);
  const db=fixture();db.objects.delete(db.key);
  const missing=await readSqueezePublications(db.bucket,scope());assert.equal(missing.rows.length,0);assert.equal(missing.coverage.reason,'unresolved-publication-attempt');
});
test('publication with missing/mismatched retained evidence fails closed rather than falling back',async()=>{
  for(const mutation of ['digest','owner','future','bad-score','missing']) {
    const db=fixture(),m=db.objects.get(db.base+'/manifest.json');
    if(mutation==='digest')m.text+=' ';
    if(mutation==='owner')db.objects.get(db.key).text=db.objects.get(db.key).text.replace('"archive_verified":true','"archive_verified":false');
    if(mutation==='future')db.objects.get(db.base+'/results.json').at=T+6000;
    if(mutation==='missing')db.objects.delete(db.base+'/COMMITTED.json');
    if(mutation==='bad-score') {
      const r=db.objects.get(db.base+'/results.json');r.text=r.text.replace('0.7','7');
      const obj=JSON.parse(m.text);obj.files['results.json']={sha256:hash(r.text),bytes:Buffer.byteLength(r.text)};m.text=JSON.stringify(obj);
      db.objects.get(db.base+'/COMMITTED.json').text=JSON.stringify({run_id:obj.run_id,manifest_sha256:hash(m.text)});
    }
    const result=await readSqueezePublications(db.bucket,scope());
    assert.equal(result.rows.length,0,mutation);assert.equal(result.coverage.publicationComplete,false,mutation);
  }
});
test('list and publication byte budgets fail explicitly, and missing metadata is not inferred',async()=>{
  const db=fixture();db.bucket.list=async()=>({objects:[],truncated:true,cursor:'more'});
  let result=await readSqueezePublications(db.bucket,scope());assert.equal(result.coverage.lists,2);assert.equal(result.coverage.reason,'publication-index-bound');
  db.bucket.list=async()=>({objects:[{key:db.key}],truncated:false});
  result=await readSqueezePublications(db.bucket,scope());assert.equal(result.coverage.reason,'publication-time-unavailable');
  const large=fixture();large.objects.get(large.base+'/results.json').text=' '.repeat(1048577);
  await assert.rejects(readSqueezePublications(large.bucket,scope()),/byte bound/);
});
test('route rejects arbitrary paths and invalid scope before any storage access',async()=>{
  const env=new Proxy({},{get(){throw new Error('no storage');}});
  for(const q of [new URLSearchParams({...scope(),path:'private'}),new URLSearchParams({...scope(),instrument:'../private'}),new URLSearchParams({...scope(),cutoff:1})]) {
    const req=new Request('https://test/api/one-clock/squeeze?'+q);assert.equal((await handleOneClockSqueeze(req,env,new URL(req.url))).status,400);
  }
});
test('same evaluator counts first shortlist appearance once and rewinds before publication',async()=>{
  const db=fixture(),rows=(await readSqueezePublications(db.bucket,scope())).rows;
  const rule={id:'appearance',version:1,name:'First published shortlist appearance',instruments:['ABC'],combine:'all',transition:'once-per-session',missingInput:'reset',
    conditions:[{source:'shortlist',field:'listed',op:'==',value:true,maxAgeMs:86400000}],outcomes:[]};
  const run=cutoff=>evaluateEvents({rule,observations:rows,selection:{...scope(cutoff),origin:'recorded'},sessionClose:sessionInstant(session,16)});
  assert.equal(run(T+2499).length,0);assert.equal(run(T+2500).length,1);assert.equal(run(T+5000).length,1);
  assert.equal(run(T+2500)[0].triggeredAt,T+2500);
});
test('overlapping scan finishes and delayed terminal receipts obey producer started/run pointer order',async()=>{
  for(const delayed of [false,true]) {
    const a=fixture({started:T,finished:T+3000,published:T+4000,receiptAt:T+(delayed?10000:4500)});
    const b=fixture({run:'33333333-3333-4333-8333-333333333333',listed:false,status:'empty',started:T+1000,finished:T+2000,published:T+5000,receiptAt:T+5500});
    for(const [key,value] of b.objects)a.objects.set(key,value);
    const selection={...scope(T+11000),origin:'recorded'},result=await readSqueezePublications(a.bucket,selection);
    assert.equal(result.coverage.publicationComplete,true);
    assert.equal(readingAsOf(result.rows,selection,{maxAgeMs:86400000}).row.value.listed,false);
    assert.equal(result.rows.length,delayed?1:2);
  }
});
test('started-only, failed-after-pointer and unknown terminal state cannot certify an old shortlist',async()=>{
  for(const kind of ['started','failed','unknown']) {
    const a=fixture(),b=fixture({run:'33333333-3333-4333-8333-333333333333',listed:false,started:T+500,finished:T+1000,published:T+3000,receiptAt:T+3500});
    if(kind!=='unknown')b.objects.delete(b.key);
    if(kind==='failed')b.objects.set(b.key.replace('finished.json','failed.json'),{text:JSON.stringify({error_type:'WriteError'}),at:T+3500});
    if(kind==='unknown')b.objects.get(b.key).text=b.objects.get(b.key).text.replace('advanced','unrecognized');
    for(const [key,value] of b.objects)a.objects.set(key,value);
    const result=await readSqueezePublications(a.bucket,scope());assert.equal(result.rows.length,0,kind);assert.equal(result.coverage.publicationComplete,false,kind);
  }
});
test('equal publication milliseconds collapse to the producer winner, not UUID sort order',async()=>{
  const a=fixture({run:'99999999-9999-4999-8999-999999999999',started:T-2000,receiptAt:T+2500});
  const b=fixture({started:T-1000,listed:false,status:'empty',receiptAt:T+2500});
  for(const [key,value] of b.objects)a.objects.set(key,value);
  const selection={...scope(),origin:'recorded'},result=await readSqueezePublications(a.bucket,selection);
  assert.equal(result.rows.length,1);
  assert.equal(readingAsOf(result.rows,selection,{maxAgeMs:86400000}).row.value.listed,false);
});
test('shared shortlist transport deduplicates and discards late scope responses',async()=>{
  let release,called=false,calls=0;const db=fixture(),registry=createSourceRegistry();
  registry.register(squeezeSource({onCoverage:()=>called=true,fetchImpl:async()=>{calls++;await new Promise(resolve=>release=resolve);return Response.json(await readSqueezePublications(db.bucket,scope()));}}));
  const clock=createClock({registry,initial:{...scope(),mode:'replay',origin:'recorded'}});
  const pending=clock.read('shortlist');assert.equal(clock.read('shortlist'),pending);await Promise.resolve();
  clock.select({...clock.snapshot(),cutoff:T});release();assert.deepEqual(await pending,{discarded:true});assert.equal(calls,1);assert.equal(called,false);
});

test('account shortlist ingestion uses catalog boundaries, reuses body reads and records null gaps',async t=>{
  const db=makeStorage();
  t.after(()=>db.db.close());
  for(const file of ['0004_one_clock.sql','0005_one_clock_catalog.sql','0010_one_clock_ingestion.sql']){
    await db.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
  }
  const f=fixture({ticker:'QQQ'}), adapter=publicIngestionSources.find(a=>a.outputs.includes('shortlist'));
  const options={session,instrument:'QQQ',now:T+8000};
  f.bucket.list=async({prefix})=>({objects:[...f.objects].filter(([key])=>key.startsWith(prefix))
    .map(([key,r])=>({key,uploaded:new Date(r.at),etag:hash(r.text),size:Buffer.byteLength(r.text)})),truncated:false});
  assert.equal((await ingestPublicSource(db,f.bucket,adapter,options)).reason,'catalog-catching-up');
  await advanceSourceCatalog(db,f.bucket,squeezeCatalog,{now:T+5000});
  f.bucket.list=()=>{throw new Error('unexpected archive rescan');};
  const coverage=await ingestPublicSource(db,f.bucket,adapter,options);
  assert.equal(coverage.through,T+5000);
  assert.equal(coverage.gaps,1);
  assert.equal(coverage.gets,4);
  const rows=(await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:options.now})).map(r=>JSON.parse(r.payload));
  assert.deepEqual(rows.map(r=>[r.eventAt,r.value?.listed??null]),[[T+100,null],[T+2500,true]]);
  assert.equal(rows[1].value.rank,1);
  assert.equal(rows[1].details.publishedAt,T+2500);
  assert.equal((await ingestPublicSource(db,f.bucket,adapter,{...options,now:options.now+1})).gets,0);
});

test('a later unchanged attempt restores pointer coverage without refreshing the underlying value',async()=>{
  const f=fixture({ticker:'QQQ'}), second=fixture({ticker:'QQQ',run:'33333333-3333-4333-8333-333333333333',
    started:T+29000,finished:T+30000,published:T+31000,receiptAt:T+32000,latest:'unchanged'});
  for(const [key,object] of second.objects)f.objects.set(key,object);
  const adapter=publicIngestionSources.find(a=>a.outputs.includes('shortlist'));
  const boundaries=[T+100,T+2500,T+30100,T+32000];
  const rows=[];
  for(const eventAt of boundaries){
    const catalog={objects:(await f.bucket.list()).objects.filter(o=>+o.uploaded<=eventAt),truncated:false};
    const selected=await adapter.read(f.bucket,{session,instrument:'QQQ'},{key:'pointer:'+eventAt,eventAt,catalog});
    rows.push(...selected.rows);
  }
  assert.equal(rows[3].value.listed,true);
  assert.equal(rows[3].valueAt,T+2500);
  const selection={session,instrument:'QQQ',origin:'recorded',cutoff:T+33000};
  assert.equal(readingAsOf(rows,selection,{maxAgeMs:10000}).state,'stale');
  const rule={id:'fresh',name:'Fresh shortlist',version:1,instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',
    conditions:[{source:'shortlist',field:'listed',op:'==',value:true,maxAgeMs:10000}],
    outcomes:[{source:'shortlist',id:'later',field:'rank',measure:'value',maxAgeMs:10000,horizonMs:29000,toleranceMs:1000}]};
  const events=evaluateEvents({rule,selection,observations:rows,sessionClose:T+3600000});
  assert.equal(events.length,1);assert.equal(events[0].outcomes[0].state,'missing');
});
