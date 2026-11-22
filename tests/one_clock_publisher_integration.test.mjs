import {fanoutChanges,snapshotVersion,generationWrites} from '../backend/one-clock-reevaluation.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {advanceOneClockResearch} from '../backend/one-clock-research.js';
import {advanceNativePublications,nativeScopeRecords,collectorPublication,markPendingNativeEvidence} from '../backend/one-clock-publication-sources.js';
import {registerObservedRule,observedEventsAsOf} from '../backend/one-clock-observer.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {selectAsOf} from '../site/shared/one-clock.js';
import {handoffSource} from '../backend/one-clock-handoff.js';
// Exact Python-produced bytes, shared with the producer's regeneration test.
// Keeping the fixture static lets the API ship before watched producer code.
const fixtures=JSON.parse(fs.readFileSync(new URL('./fixtures/one_clock_publications.json',import.meta.url),'utf8'));
const T=Date.parse('2026-11-20T15:00:00Z'),session='2026-11-20';
async function setup(t){
  const DB=makeStorage();t.after(()=>DB.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql','0011_one_clock_publications.sql','0012_one_clock_handoff.sql','0013_one_clock_reevaluation.sql'])await DB.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
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
  const namespace='account:fixture';
  await DB.prepare('INSERT INTO one_clock_generation_receipts(namespace,session,instrument,rule_id,version,generation,committed_at) VALUES(?,?,?,?,?,?,?)').bind(namespace,session,'QQQ','r',1,2,T+62000).run();
  const evaluation=[{session,instrument:'QQQ',rule_id:'r',version:1,attempted_at:T+62000,state:'evaluated',reason:null}];
  assert.equal((await markPendingNativeEvidence(DB,evaluation,T+62000,namespace))[0].state,'evaluated');
  bucket.visible=3;await advanceNativePublications(env,{now:T+86400000});
  assert.equal((await markPendingNativeEvidence(DB,evaluation,T+62000,namespace))[0].state,'evaluated');
  const pending=(await markPendingNativeEvidence(DB,evaluation,T+86400000,namespace))[0];assert.equal(pending.state,'pending');assert.match(pending.reason,/previous evaluation/);assert.equal(pending.desiredGeneration,3);assert.equal(pending.completedGeneration,2);
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
  assert.equal((await markPendingNativeEvidence(DB,[oldAttempt],now,namespace))[0].state,'pending');
  let olderLegacy=0;const adapter={id:'archived-chain',version:'1',instruments:['QQQ'],outputs:['price','skew'],async discover(bucket,selection){if(selection.session===older)olderLegacy++;return {artifacts:[],complete:true};}};
  const result=await advanceOneClockResearch(env,{now:now+1,sources:[adapter]});assert.ok(result.some(r=>r.state==='evaluated'&&r.session===older));assert.equal(olderLegacy,0);
  const attempt=DB.raw('SELECT session,instrument,rule_id,version,attempted_at,state FROM one_clock_evaluation_history WHERE session=?',older)[0];
  assert.equal((await markPendingNativeEvidence(DB,[attempt],now+1,namespace))[0].state,'evaluated');
  const receipt=JSON.parse(DB.raw('SELECT coverage FROM one_clock_ingestion_history WHERE source=? AND session=?','collector-chain',older)[0].coverage);assert.equal(receipt.lastPublicationAt,T+241000);
});
test('a paused version with no scheduler job replays an old session and acknowledges a same-time correction separately',async t=>{
  const {DB,bucket,env}=await setup(t);
  const namespace='account:archived',rule={id:'paused',version:1,name:'Stopped watching',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
  await registerObservedRule(DB,{namespace,rule,registeredAt:T-100000});
  await DB.prepare('INSERT INTO one_clock_activations VALUES(?,?,?,?,?)').bind(namespace,rule.id,1,T-100000,T+120000).run();
  const now=Date.parse('2026-11-25T18:00:00Z'),selection={session,instrument:'QQQ',origin:'recorded',cutoff:now};
  bucket.visible=2;
  const tick=at=>advanceOneClockResearch(env,{now:at,sources:[]});
  const tick1=await tick(now);
  assert.ok(tick1.some(r=>r.state==='evaluated'&&r.session===session),JSON.stringify(tick1));
  for(let i=0;i<3;i++) await tick(now);
  const before=await observedEventsAsOf(DB,{namespace,selection});assert.equal(before.length,1);
  const evaluation=[{session,instrument:'QQQ',rule_id:rule.id,version:1,attempted_at:now,state:'evaluated'}];
  bucket.visible=3;
  await advanceNativePublications(env,{now});
  const [pending]=await markPendingNativeEvidence(DB,evaluation,now,namespace);
  assert.equal(pending.state,'pending');assert.equal(pending.desiredGeneration,3);assert.equal(pending.completedGeneration,2);
  await tick(now+10);
  const [after]=await markPendingNativeEvidence(DB,evaluation,now+10,namespace);
  assert.equal(after.state,'evaluated');assert.equal(after.completedGeneration,3);
  const replaySelection={...selection,cutoff:now+10};
  const replay=await read(DB,replaySelection.cutoff);
  const expected=evaluateEvents({rule,observations:replay.records.map(r=>JSON.parse(r.payload)),selection:replaySelection,
    sessionClose:Date.parse('2026-11-20T21:00:00Z'),activationWindows:[{started_at:T-100000,ended_at:T+120000}]});
  const actual=await observedEventsAsOf(DB,{namespace,selection:replaySelection});
  assert.deepEqual(actual.map(e=>[e.triggeredAt,e.inputEvidence,e.outcomes]),expected.map(e=>[e.triggeredAt,e.inputEvidence,e.outcomes]));
  assert.deepEqual(await observedEventsAsOf(DB,{namespace,selection}),before);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_evaluation_work WHERE namespace=?',namespace)[0].n,0);
});
test('interrupted fanout and a newer correction preserve the first target until the full page is traversed',async t=>{
  const {DB,bucket,env}=await setup(t);
  for(let i=0;i<30;i++) {
    const namespace='account:fixture-'+String(i).padStart(2,'0');
    const rule={id:'r',version:1,name:'Threshold',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
    await registerObservedRule(DB,{namespace,rule,registeredAt:T-100000});
    await DB.prepare('INSERT INTO one_clock_activations VALUES(?,?,?,?,?)').bind(namespace,'r',1,T-100000,T+120000).run();
  }
  bucket.visible=1;
  await advanceNativePublications(env,{now:T+242000});
  assert.equal((await fanoutChanges(DB,T+242000)).state,'indexing');
  await fanoutChanges(DB,T+242000);
  const first=DB.raw('SELECT * FROM one_clock_fanout')[0];
  assert.notEqual(first.cursor,'[]');assert.equal(DB.raw('SELECT COUNT(*) AS n FROM one_clock_historical_rules')[0].n,24);
  bucket.visible=2;
  await advanceNativePublications(env,{now:T+242000});
  DB.flags.failAfterRuns=2;
  await assert.rejects(fanoutChanges(DB,T+242000),/simulated/);
  DB.flags.failAfterRuns=null;
  assert.equal(DB.raw('SELECT cursor FROM one_clock_fanout')[0].cursor,first.cursor);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_historical_rules')[0].n,24);
  await fanoutChanges(DB,T+242000);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_historical_rules WHERE target=1')[0].n,30);
  assert.equal(DB.raw('SELECT target FROM one_clock_fanout')[0].target,2);
  for(let i=0;i<2;i++) await fanoutChanges(DB,T+242000);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_historical_rules WHERE target=1 AND next_target=2')[0].n,30);
  const frozen=await snapshotVersion(DB,session,'QQQ',T+242000,1);
  assert.equal(frozen.sequences['collector-chain/QQQ'],1);
  assert.equal((await snapshotVersion(DB,session,'QQQ',T+242000)).sequences['collector-chain/QQQ'],2);
});

test('a stale worker or failed acknowledgment rolls back the whole generation transaction',async t=>{
  const {DB}=await setup(t),scope={namespace:'account:a',session,instrument:'QQQ'},rules=[{rule_id:'r',version:1}];
  const commit=(gen,tag)=>DB.batch([
    DB.prepare('INSERT INTO one_clock_checkpoints(namespace,source,cursor,updated_at) VALUES(?,?,?,?)').bind(scope.namespace,tag,'1',T),
    ...generationWrites(DB,scope,rules,gen,T)]);
  await commit(2,'accepted');await assert.rejects(commit(1,'stale'),/stale historical evaluation/);
  DB.flags.failAfterRuns=1;
  await assert.rejects(commit(3,'crash'),/simulated/);DB.flags.failAfterRuns=null;
  assert.deepEqual(DB.raw('SELECT source FROM one_clock_checkpoints').map(r=>r.source),['accepted']);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_generation_receipts')[0].n,1);
});

test('migration bootstraps retained entries while concurrent arrivals retain independent sequence and cutoff boundaries',async t=>{
  const {DB,bucket,env}=await setup(t);
  await DB.exec('DROP TRIGGER one_clock_publication_changed');
  bucket.visible=2;await advanceNativePublications(env,{now:T+62000});
  await DB.exec(fs.readFileSync(new URL('../backend/migrations/0013_one_clock_reevaluation.sql',import.meta.url),'utf8'));
  bucket.visible=3;await advanceNativePublications(env,{now:T+122000});
  assert.equal((await fanoutChanges(DB,T+122000)).state,'indexing');
  const early=await snapshotVersion(DB,session,'QQQ',T+62000),late=await snapshotVersion(DB,session,'QQQ',T+122000);
  assert.equal(early.sequences['collector-chain/QQQ'],2);assert.equal(late.sequences['collector-chain/QQQ'],3);
  const plan=DB.raw('EXPLAIN QUERY PLAN '+DB.queries.find(q=>q.startsWith('SELECT c.stream,c.sequence')),session,'QQQ',T+122000,Number.MAX_SAFE_INTEGER).map(r=>r.detail).join(' ');
  assert.doesNotMatch(plan,/SCAN c|SCAN x|TEMP B-TREE/);
});test('a historical rule exceeding capacity backs off while the same tick evaluates the current market session',async t=>{
  const {DB,env,bucket}=await setup(t),now=Date.parse('2026-11-25T18:00:00Z');
  const rule={id:'threshold',version:1,name:'Switches',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
  for(const [namespace,start,end] of [['account:old',T-100000,T+1800000],['account:current',now-1000,null]]) {
    await registerObservedRule(DB,{namespace,rule,registeredAt:start});
    await DB.prepare('INSERT INTO one_clock_activations VALUES(?,?,?,?,?)').bind(namespace,rule.id,1,start,end).run();
  }
  const writes=Array.from({length:900},(_,i)=>{
    const at=T+i*1000,row={source:'price',id:'legacy-'+i,revision:1,session,instrument:'QQQ',origin:'recorded',eventAt:at,availableAt:at,evidence:'fixture://load/'+i,value:{spot:i%2?699:701}};
    return DB.prepare('INSERT INTO one_clock_ingested VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind('price',session,'QQQ','recorded',row.id,1,at,at,at,'backfill',JSON.stringify(row));
  });
  await DB.batch(writes);bucket.visible=1;
  const tick=at=>advanceOneClockResearch(env,{now:at,sources:[]});
  const result=await tick(now);
  assert.ok(result.some(r=>r.state==='evaluated'&&r.session==='2026-11-25'),JSON.stringify(result));
  // A shared budget already spent on current work defers a full-budget retry.
  if(!result.some(r=>r.state==='failed')) await tick(now+900000);
  const pending=DB.raw("SELECT reason,retry_at FROM one_clock_historical_rules WHERE namespace='account:old'")[0];
  assert.match(pending.reason,/capacity/);assert.ok(pending.retry_at>now);
  assert.equal(DB.raw("SELECT COUNT(*) n FROM one_clock_generation_receipts WHERE namespace='account:old'")[0].n,0);
});