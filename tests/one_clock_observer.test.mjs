import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { makeStorage } from './helpers/d1_shim.mjs';
import { registerObservedRule, observeBatch, observedEventsAsOf } from '../backend/one-clock-observer.js';
import { evaluateEvents } from '../site/shared/one-clock-events.js';
import { demoRows, demoRules, demoStart, demoClose, demoSelection } from '../site/shared/one-clock-demo.js';

async function setup(t, namespace='test') {
  const db=makeStorage(); t.after(()=>db.db.close());
  await db.exec(fs.readFileSync(new URL('../backend/migrations/0004_one_clock.sql',import.meta.url),'utf8'));
  for(const rule of demoRules) await registerObservedRule(db,{ namespace,rule,registeredAt:demoStart });
  return db;
}
const at = cutoff=>({...demoSelection,cutoff});
function batch(source, cutoff, extra={}) {
  return {namespace:'test',batchId:`${source}:${cutoff}`,source,cursor:`cursor:${cutoff}`,
    selection:at(cutoff),sessionClose:demoClose,collection:'replay',
    observations:demoRows.filter(r=>r.source===source && r.availableAt<=cutoff),...extra};
}
const read=(db,cutoff=demoClose+1000,namespace='test')=>observedEventsAsOf(db,{namespace,selection:at(cutoff)});

test('durable observer and replay yield identical triggers and outcomes after restart',async t=>{
  const db=await setup(t);
  for(const source of ['price','shortlist','skew']) await observeBatch(db,batch(source,demoClose+1000));
  // No in-memory state is retained by the observer: a new call uses D1 alone.
  const actual=await read(db);
  const expected=demoRules.flatMap(rule=>evaluateEvents({rule,observations:demoRows,selection:at(demoClose+1000),sessionClose:demoClose}));
  assert.equal(actual.length,expected.length);
  for(const event of expected) {
    const saved=actual.find(e=>e.ruleId===event.ruleId && e.triggeredAt===event.triggeredAt);
    assert.ok(saved);
    assert.deepEqual(saved.outcomes,event.outcomes);
    assert.deepEqual(saved.inputEvidence,event.inputEvidence);
    assert.equal(saved.collection,'replay');
  }
  const receipt=await observeBatch(db,batch('skew',demoClose+1000));
  assert.equal(receipt.duplicate,true);
  assert.deepEqual(await read(db),actual);
});

test('failure between event write and checkpoint advancement rolls back the entire batch',async t=>{
  const db=await setup(t);
  const request=batch('skew',demoStart+301000);
  db.flags.failAfterRuns=3;
  await assert.rejects(observeBatch(db,request),/simulated D1/);
  for(const table of ['one_clock_state','one_clock_observations','one_clock_events','one_clock_event_history','one_clock_checkpoints','one_clock_receipts']) {
    assert.equal(db.raw(`SELECT COUNT(*) AS n FROM ${table}`)[0].n,0,table);
  }
  db.flags.failAfterRuns=null;
  await observeBatch(db,request);
  assert.equal((await read(db,demoStart+301000)).length,1);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_checkpoints')[0].n,1);
  assert.equal((await observeBatch(db,request)).duplicate,true);
});

test('late outcome correction has its own observer availability and rewinds to pending',async t=>{
  const db=await setup(t);
  const trigger=demoStart+301000;
  for(const source of ['price','skew']) await observeBatch(db,batch(source,trigger));
  assert.equal((await read(db,trigger))[0].outcomes[0].state,'pending');
  await observeBatch(db,batch('price',demoStart+1261000));
  const later=await read(db,demoStart+1261000);
  assert.equal(later[0].outcomes[0].state,'measured');
  assert.equal(later[0].observerRevision,2);
  assert.equal((await read(db,trigger))[0].outcomes[0].state,'pending');
  assert.equal((await read(db,trigger-1)).length,0);
});

test('same identity with different evidence fails without advancing its checkpoint',async t=>{
  const db=await setup(t);
  const request=batch('skew',demoStart+301000);
  await observeBatch(db,request);
  await assert.rejects(observeBatch(db,{...request,observations:[]}),/batch identity/);
  const changed={...request,batchId:'changed',observations:[{...request.observations[0],value:{imbalance:99}}]};
  await assert.rejects(observeBatch(db,changed),/conflicting immutable/);
  assert.equal(db.raw('SELECT generation FROM one_clock_state')[0].generation,1);
  await assert.rejects(registerObservedRule(db,{namespace:'test',rule:{...demoRules[0],name:'Overwrite'},registeredAt:demoStart}),/immutable rule/);
});

test('as-of reads enforce namespace, source cohort and session scope',async t=>{
  const db=await setup(t);
  await observeBatch(db,batch('skew',demoStart+301000));
  assert.deepEqual(await read(db,demoClose,'another-owner'),[]);
  assert.deepEqual(await observedEventsAsOf(db,{namespace:'test',selection:{...at(demoClose),origin:'recorded'}}),[]);
  assert.deepEqual(await observedEventsAsOf(db,{namespace:'test',selection:{...at(demoClose),session:'another-day'}}),[]);
  await assert.rejects(observeBatch(db,batch('price',demoClose,{observations:[demoRows[0]]})),/outside authorized/);
});

test('backfill never upgrades to prospective on repeat delivery',async t=>{
  const db=await setup(t);
  await observeBatch(db,batch('skew',demoStart+301000,{collection:'backfill'}));
  await observeBatch(db,batch('skew',demoStart+302000,{collection:'prospective'}));
  assert.equal((await read(db,demoStart+302000))[0].collection,'backfill');
});

test('concurrent generation is rejected atomically and can retry from current state',async t=>{
  const db=await setup(t);
  let injected=false;
  const realBatch=db.batch.bind(db);
  db.batch=async statements=>{
    if(!injected) {
      injected=true;
      await db.prepare('INSERT INTO one_clock_state(namespace,generation,cutoff) VALUES(?,?,?)').bind('test',1,demoStart+301000).run();
    }
    return realBatch(statements);
  };
  const request=batch('skew',demoStart+301000);
  await assert.rejects(observeBatch(db,request),/generation conflict/);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_observations')[0].n,0);
  assert.equal((await observeBatch(db,request)).generation,2);
});

test('observation is independent of KV bindings and rejects future input or rewind',async t=>{
  const db=await setup(t);
  const env=new Proxy({DB:db},{get(target,key){ if(key!=='DB') throw new Error('unexpected binding'); return target.DB; }});
  const request=batch('skew',demoStart+301000);
  await observeBatch(env.DB,request);
  await assert.rejects(observeBatch(env.DB,batch('skew',demoStart+1000)),/cannot rewind/);
  await assert.rejects(observeBatch(env.DB,batch('skew',demoStart+302000,{observations:[demoRows.find(r=>r.source==='skew'&&r.eventAt>demoStart+302000)]})),/not arrived/);
});
