import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {advanceOneClockResearch} from '../backend/one-clock-research.js';
import {advanceNativePublications,nativeScopeRecords,collectorPublication,markPendingNativeEvidence} from '../backend/one-clock-publication-sources.js';
import {registerObservedRule,observedEventsAsOf} from '../backend/one-clock-observer.js';
import {selectAsOf} from '../site/shared/one-clock.js';
import {handoffSource} from '../backend/one-clock-handoff.js';
// Exact Python-produced bytes, shared with the producer's regeneration test.
// Keeping the fixture static lets the API ship before watched producer code.
const fixtures=JSON.parse(fs.readFileSync(new URL('./fixtures/one_clock_publications.json',import.meta.url),'utf8'));
const T=Date.parse('2026-11-20T15:00:00Z'),session='2026-11-20';
async function setup(t){
  const DB=makeStorage();t.after(()=>DB.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql','0011_one_clock_publications.sql','0012_one_clock_handoff.sql'])await DB.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
  const bucket={visible:0,gets:0,async get(key){this.gets++;const i=fixtures.findIndex(f=>f.key===key);if(i<0||i>=this.visible)return null;const raw=Buffer.from(fixtures[i].wire,'base64');return {size:raw.length,uploaded:new Date(T+i*60000+1000),arrayBuffer:async()=>raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength)};},async list(){throw new Error('native ingestion must not list');}};
  return {DB,bucket,env:{DB,PAPER_TRADES:bucket,ONE_CLOCK_OBSERVER:'enabled',ONE_CLOCK_PUBLICATIONS:'enabled'}};
}
const read=(DB,cutoff,extra={})=>nativeScopeRecords(DB,collectorPublication,{session,instrument:'QQQ',cutoff,...extra});

test('real Python publisher bytes enter JS consumer with both time gates, corrections and stable collection',async t=>{
  const {DB,bucket,env}=await setup(t);await advanceNativePublications(env,{now:T-1000});
  bucket.visible=2;await advanceNativePublications(env,{now:T+62000});
  let data=await read(DB,T+62000);assert.equal(data.records.length,2);assert.ok(data.records.every(r=>r.collection==='prospective'));assert.equal(data.coverage.indexComplete,true);assert.equal(data.coverage.final,false);
  assert.equal(await read(DB,T+61999),null);
  bucket.visible=3;await advanceNativePublications(env,{now:T+122000});
  for(const [cutoff,expected] of [[T+62000,701],[T+122000,698]]) {
    data=await read(DB,cutoff);const rows=selectAsOf(data.records.map(r=>JSON.parse(r.payload)),{session,instrument:'QQQ',origin:'recorded',cutoff});assert.equal(rows.at(-1).value.spot,expected);
  }
  bucket.visible=4;await advanceNativePublications(env,{now:T+182000});data=await read(DB,T+182000);
  assert.equal(selectAsOf(data.records.map(r=>JSON.parse(r.payload)),{session,instrument:'QQQ',origin:'recorded',cutoff:T+182000}).at(-1).value,null);
  await advanceNativePublications(env,{now:T+86400000});assert.deepEqual((await read(DB,T+86400000)).records.map(r=>r.collection),['prospective','prospective','backfill','backfill']);
});

test('initial catch-up remains backfill; current transaction failure cannot reuse old tail success',async t=>{
  const {DB,bucket,env}=await setup(t);bucket.visible=2;await advanceNativePublications(env,{now:T+62000});
  const data=await read(DB,T+62000);assert.ok(data.records.every(r=>r.collection==='backfill'));
  const failed=await read(DB,T+62001,{currentProgress:{stream:'collector-chain/QQQ',reason:'transaction-failed'}});
  assert.equal(failed.coverage.indexComplete,false);assert.equal(failed.coverage.pending,1);assert.equal(failed.records.length,2);
  const query=DB.queries.find(q=>q.includes('SELECT coverage FROM one_clock_publication_status'));
  const plan=DB.raw('EXPLAIN QUERY PLAN '+query,'collector-chain/QQQ',T+62000).map(r=>r.detail).join('\n');
  assert.match(plan,/SEARCH.*one_clock_publication_status_cutoff/);assert.doesNotMatch(plan,/TEMP B-TREE|SCAN/);
});

test('scheduled native ingestion runs with zero active accounts and disabled feature touches no storage',async t=>{
  const {DB,bucket,env}=await setup(t);bucket.visible=2;
  assert.deepEqual(await advanceOneClockResearch(env,{now:T+62000}),[]);assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_publication_records')[0].n,2);assert.ok(bucket.gets<=192);
  assert.deepEqual(await advanceNativePublications({ONE_CLOCK_PUBLICATIONS:'disabled'}),[]);
});

test('observer uses native commits, preserves prior legacy anchor and records source coverage without duplicate archive polling',async t=>{
  const {DB,bucket,env}=await setup(t),namespace='account:fixture';
  const rule={id:'threshold',version:1,name:'threshold',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
  await registerObservedRule(DB,{namespace,rule,registeredAt:T-100000});
  await DB.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)').bind(namespace,rule.id,1,T-100000).run();
  // Retained legacy data before the native cutover remains a session input.
  const legacy={source:'price',id:'legacy',revision:1,session,instrument:'QQQ',origin:'recorded',eventAt:T-60000,availableAt:T-59000,evidence:'fixture',value:{spot:698}};
  await DB.prepare('INSERT INTO one_clock_ingested(source,session,instrument,origin,record_id,revision,event_at,available_at,received_at,collection,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind('price',session,'QQQ','recorded','legacy',1,legacy.eventAt,legacy.availableAt,T-58000,'prospective',JSON.stringify(legacy)).run();
  await advanceNativePublications(env,{now:T-1000});bucket.visible=2;
  let discovered=0;
  const otherAdapter={id:'other-public',version:'1',instruments:['QQQ'],outputs:['volume'],async discover(){discovered++;return {artifacts:[],complete:true};}};
  const result=await advanceOneClockResearch(env,{now:T+62000,sources:[otherAdapter]});assert.ok(result.some(r=>r.state==='evaluated'));assert.ok(discovered>0);
  const events=await observedEventsAsOf(DB,{namespace,selection:{session,instrument:'QQQ',origin:'recorded',cutoff:T+62000}});assert.equal(events.length,1);assert.equal(events[0].collection,'prospective');
  assert.equal(DB.raw("SELECT COUNT(*) n FROM one_clock_ingestion_history WHERE source='collector-chain'")[0].n,1);
  env.ONE_CLOCK_PUBLICATIONS='disabled';
  const paused=await advanceOneClockResearch(env,{now:T+63000});assert.equal(paused[0].state,'paused');
  assert.equal((await observedEventsAsOf(DB,{namespace,selection:{session,instrument:'QQQ',origin:'recorded',cutoff:T+63000}})).length,1);
});

async function retainLegacy(DB,{value=701,revision=4,id='snapshot1',receivedAt=T+62000}={}) {
  const row={source:'price',id,revision,session,instrument:'QQQ',origin:'recorded',eventAt:T+60000,availableAt:T+61000,evidence:'legacy-evidence',value:{spot:value}};
  const record={source:'price',session,instrument:'QQQ',origin:'recorded',record_id:id,revision,event_at:row.eventAt,available_at:row.availableAt,received_at:receivedAt,collection:'prospective',payload:JSON.stringify(row)};
  await DB.prepare('INSERT INTO one_clock_ingested(source,session,instrument,origin,record_id,revision,event_at,available_at,received_at,collection,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind(...Object.values(record)).run();
  return record;
}
const compose=(DB,native,legacy,cutoff)=>handoffSource(DB,collectorPublication,native,legacy,{source:'price',session,instrument:'QQQ',cutoff});

test('consumer handoff freezes legacy identity and membership even when producer started earlier; future correction keeps same ID',async t=>{
  const {DB,bucket,env}=await setup(t),old=await retainLegacy(DB);bucket.visible=2;await advanceNativePublications(env,{now:T+65000});
  const first=await compose(DB,await read(DB,T+65000),[old],T+65000);
  assert.ok(first.some(r=>r.payload===old.payload&&r.collection==='prospective'));
  assert.equal(first.filter(r=>JSON.parse(r.payload).details?.originalRecordId==='snapshot1').length,0);
  const lateLegacy=await retainLegacy(DB,{value:999,revision:5,receivedAt:T+66000});
  bucket.visible=3;await advanceNativePublications(env,{now:T+122000});
  const later=await compose(DB,await read(DB,T+122000),[old,lateLegacy],T+122000),mapped=later.find(r=>r.record_id==='snapshot1'&&r.revision===5);
  assert.equal(JSON.parse(mapped.payload).value.spot,698);assert.equal(mapped.collection,'backfill');assert.ok(!later.some(r=>r.payload===lateLegacy.payload));
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_source_handoffs')[0].n,1);
  assert.equal(DB.raw('SELECT cutover_at FROM one_clock_source_handoffs')[0].cutover_at,T+65000);
});

test('different native baseline is an explicit backfill correction at cutover; failed handoff rolls back',async t=>{
  const {DB,bucket,env}=await setup(t),old=await retainLegacy(DB);bucket.visible=3;await advanceNativePublications(env,{now:T+122000});
  const native=await read(DB,T+122000);DB.flags.failAfterRuns=1;
  await assert.rejects(compose(DB,native,[old],T+122000),/simulated/);assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_source_handoffs')[0].n,0);
  DB.flags.failAfterRuns=null;const result=await compose(DB,native,[old],T+122000),correction=result.find(r=>r.record_id==='snapshot1'&&r.revision===5);
  assert.equal(correction.collection,'backfill');assert.equal(correction.available_at,T+122000);assert.equal(correction.received_at,T+122000);assert.equal(JSON.parse(correction.payload).value.spot,698);
  const values=at=>selectAsOf(result.map(r=>JSON.parse(r.payload)),{session,instrument:'QQQ',origin:'recorded',cutoff:at}).find(r=>r.id==='snapshot1').value.spot;
  assert.equal(values(T+121999),701);assert.equal(values(T+122000),698);
  assert.deepEqual(await compose(DB,native,[old],T+123000),result);
});

test('Results disclose newer evidence awaiting historical reevaluation without rewriting old cutoff status',async t=>{
  const {DB,bucket,env}=await setup(t);bucket.visible=2;await advanceNativePublications(env,{now:T+62000});
  const evaluation=[{session,instrument:'QQQ',rule_id:'r',version:1,attempted_at:T+62000,state:'evaluated',reason:null}];
  assert.equal((await markPendingNativeEvidence(DB,evaluation,T+62000))[0].state,'evaluated');
  bucket.visible=3;await advanceNativePublications(env,{now:T+86400000});
  assert.equal((await markPendingNativeEvidence(DB,evaluation,T+62000))[0].state,'evaluated');
  const pending=(await markPendingNativeEvidence(DB,evaluation,T+86400000))[0];assert.equal(pending.state,'pending');assert.match(pending.reason,/previous evaluation/);assert.equal(pending.newEvidenceAt,T+86400000);
  const sql=DB.queries.find(q=>q.startsWith('SELECT received_at FROM one_clock_publication_records INDEXED'));
  const plan=DB.raw('EXPLAIN QUERY PLAN '+sql,session,'QQQ',T+86400000).map(r=>r.detail).join('\n');assert.match(plan,/SEARCH.*one_clock_publication_record_scope/);assert.doesNotMatch(plan,/TEMP B-TREE|SCAN/);
});

test('concurrent legacy ingestion rejects stale cutover and retry includes its newer retained membership',async t=>{
  const {DB,bucket,env}=await setup(t),old=await retainLegacy(DB);bucket.visible=2;await advanceNativePublications(env,{now:T+65000});
  const native=await read(DB,T+65000),batch=DB.batch.bind(DB);let intercept=true;
  DB.batch=async statements=>{
    if(intercept){intercept=false;await retainLegacy(DB,{id:'late-legacy',receivedAt:T+64000});await DB.prepare('INSERT INTO one_clock_ingestion_work(source,session,instrument,generation,updated_at,coverage) VALUES(?,?,?,?,?,?)').bind('archived-chain',session,'QQQ',1,T+64000,'{}').run();}
    return batch(statements);
  };
  await assert.rejects(compose(DB,native,[old],T+65000),/legacy ingestion advanced/);assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_source_handoffs')[0].n,0);
  const rows=await compose(DB,native,[old],T+65000);assert.ok(rows.some(r=>r.record_id==='late-legacy'));assert.equal(DB.raw('SELECT legacy_generation FROM one_clock_source_handoffs')[0].legacy_generation,1);
});

test('late native evidence for a session before the first commit is consumed by its existing scheduler job',async t=>{
  const {DB,bucket,env}=await setup(t),older='2026-11-19',namespace='account:fixture',now=T+242000;
  const rule={id:'older',version:1,name:'older threshold',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
  await registerObservedRule(DB,{namespace,rule,registeredAt:T-86400000-10000});
  await DB.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)').bind(namespace,rule.id,1,T-86400000-10000).run();
  bucket.visible=5;await advanceNativePublications(env,{now});
  const olderNative=await nativeScopeRecords(DB,collectorPublication,{session:older,instrument:'QQQ',cutoff:now});assert.equal(olderNative.records.length,1);assert.equal(olderNative.records[0].collection,'backfill');
  const oldAttempt={session:older,instrument:'QQQ',rule_id:rule.id,version:1,attempted_at:now-1000,state:'evaluated'};
  assert.equal((await markPendingNativeEvidence(DB,[oldAttempt],now))[0].state,'pending');
  let olderLegacy=0;const adapter={id:'archived-chain',version:'1',instruments:['QQQ'],outputs:['price','skew'],async discover(bucket,selection){if(selection.session===older)olderLegacy++;return {artifacts:[],complete:true};}};
  const result=await advanceOneClockResearch(env,{now:now+1,sources:[adapter]});assert.ok(result.some(r=>r.state==='evaluated'&&r.session===older));assert.equal(olderLegacy,0);
  const attempt=DB.raw('SELECT session,instrument,rule_id,version,attempted_at,state FROM one_clock_evaluation_history WHERE session=?',older)[0];
  assert.equal((await markPendingNativeEvidence(DB,[attempt],now+1))[0].state,'evaluated');
  const receipt=JSON.parse(DB.raw('SELECT coverage FROM one_clock_ingestion_history WHERE source=? AND session=?','collector-chain',older)[0].coverage);assert.equal(receipt.lastPublicationAt,T+241000);
});
