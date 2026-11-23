import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResults} from '../backend/one-clock-results.js';
import {demoRules} from '../site/shared/one-clock-demo.js';
const T=Date.parse('2026-11-20T21:00:00Z');
async function fixture(t) {
  const DB=makeStorage();t.after(()=>DB.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])await DB.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
  const env={DB,ONE_CLOCK_OBSERVER:'enabled'},storage=createStorage(env);
  for(const name of ['alice','other']){await storage.users.insert(name,{username:name,version:0});await storage.sessions.create(name+'-token',name,3600);}
  for(const [id,session,instrument,namespace,at] of [['a','2026-11-19','QQQ','alice',T],['b','2026-11-20','SPY','alice',T],['secret','2026-11-20','QQQ','other',T],['future','2026-11-20','QQQ','alice',T+1]]){
    const event={id,ruleId:'r',ruleVersion:1,session,instrument,origin:'recorded',triggeredAt:T-100000000,outcomes:[]},payload=JSON.stringify(event);
    await DB.prepare('INSERT INTO one_clock_events(namespace,event_id,session,instrument,origin,revision,payload,state) VALUES(?,?,?,?,?,?,?,?)').bind('account:'+namespace,id,session,instrument,'recorded',1,payload,'active').run();
    await DB.prepare('INSERT INTO one_clock_event_history(namespace,event_id,revision,recorded_at,batch_id,state,payload) VALUES(?,?,?,?,?,?,?)').bind('account:'+namespace,id,1,at,'fixture','active',payload).run();
  }
  const call=(user='alice',query={},method='GET')=>handleOneClockResults(new Request('https://options.moopertonic.net/api/one-clock/results?'+new URLSearchParams({from:'2026-11-19',to:'2026-11-20',instrument:'ALL',cutoff:String(T),...query}),{method,headers:user?{Cookie:'session='+user+'-token'}:{}}),env);
  return {DB,call};
}
test('range results authorize before reading and preserve namespace, instrument and receipt cutoff',async t=>{
  const {call}=await fixture(t);
  assert.equal((await call(null)).status,401);assert.equal((await call('alice',{},'POST')).status,405);
  const response=await call(),body=await response.json();assert.equal(response.status,200);
  assert.deepEqual(body.events.map(e=>e.id),['a','b']);assert.equal(body.session,'2026-11-19/2026-11-20');
  assert.deepEqual((await (await call('alice',{instrument:'QQQ'})).json()).events.map(e=>e.id),['a']);
  assert.equal((await (await call('alice',{cutoff:String(T-1)})).json()).events.length,0);
  assert.deepEqual((await (await call('other')).json()).events.map(e=>e.id),['secret']);
  for(const query of [{from:'2026-10-20'},{from:'2026-11-21'},{instrument:'private'},{cutoff:'NaN'},{unknown:'x'}])assert.equal((await call('alice',query)).status,400);
});
test('later retractions do not rewrite a previously visible range',async t=>{
  const {DB,call}=await fixture(t),payload=DB.raw("SELECT payload FROM one_clock_events WHERE event_id='a'")[0].payload;
  await DB.prepare("UPDATE one_clock_events SET state='retracted',revision=2 WHERE event_id='a'").run();
  await DB.prepare('INSERT INTO one_clock_event_history(namespace,event_id,revision,recorded_at,batch_id,state,payload) VALUES(?,?,?,?,?,?,?)').bind('account:alice','a',2,T+1000,'correction','retracted',payload).run();
  assert.deepEqual((await (await call()).json()).events.map(e=>e.id),['a','b']);
  assert.deepEqual((await (await call('alice',{cutoff:String(T+1000)})).json()).events.map(e=>e.id),['b','future']);
});
test('range evaluation status distinguishes unattempted rules at each cutoff and excludes other accounts',async t=>{
  const {DB,call}=await fixture(t),rule=demoRules[0];
  await DB.prepare('INSERT INTO one_clock_rules(namespace,rule_id,version,registered_at,payload) VALUES(?,?,?,?,?)').bind('account:alice',rule.id,1,T-7200000,JSON.stringify(rule)).run();
  await DB.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)').bind('account:alice',rule.id,1,T-3600000).run();
  await DB.prepare('INSERT INTO one_clock_evaluation_history(namespace,session,instrument,rule_id,version,attempted_at,state) VALUES(?,?,?,?,?,?,?)').bind('account:alice','2026-11-20','QQQ',rule.id,1,T-10000,'evaluated').run();
  assert.equal((await (await call('alice',{cutoff:String(T-11000)})).json()).evaluation[0].state,'pending');
  assert.equal((await (await call()).json()).evaluation[0].state,'evaluated');
  assert.equal((await (await call('alice',{cutoff:String(T-3600001)})).json()).evaluation.length,0);
  assert.equal((await (await call('other')).json()).evaluation.length,0);
});
test('result byte preflight rejects an oversized payload before reading its body',async t=>{
  const {DB,call}=await fixture(t);
  await DB.prepare("UPDATE one_clock_event_history SET payload=? WHERE event_id='a'").bind(JSON.stringify({padding:'x'.repeat(4*1024*1024)})).run();
  DB.queries.length=0;
  const response=await call();assert.equal(response.status,400);assert.match((await response.json()).error,/byte/);
  assert.ok(!DB.queries.some(sql=>sql.startsWith('SELECT h.* FROM one_clock_events')));
});

test('a prior empty level study remains pending while newer coverage is partial',async t=>{
  const {DB,call}=await fixture(t),session='2026-11-20',instrument='QQQ';
  const rule={...demoRules[0],conditions:[{source:'level-spot'}],outcomes:[]};
  await DB.prepare('INSERT INTO one_clock_rules VALUES(?,?,?,?,?)').bind('account:alice',rule.id,1,T-7200000,JSON.stringify(rule)).run();
  await DB.prepare('INSERT INTO one_clock_activations VALUES(?,?,?,?,NULL)').bind('account:alice',rule.id,1,T-3600000).run();
  await DB.prepare("INSERT INTO one_clock_evaluation_history VALUES(?,?,?,?,1,?,'evaluated',NULL)").bind('account:alice',session,instrument,rule.id,T-10000).run();
  for(const [generation,at,pending] of [[1,T-10000,0],[2,T,55]]) {
    const source='calibrated-oi-levels';
    await DB.prepare('INSERT INTO one_clock_ingestion_history VALUES(?,?,?,?,?,?)').bind(source,session,instrument,generation,at,
      JSON.stringify({source,session,instrument,indexComplete:true,pending,updatedAt:at})).run();
  }
  const read=async cutoff=>(await call('alice',{from:session,to:session,instrument,cutoff:String(cutoff)})).json();
  assert.equal((await read(T-1)).evaluation[0].state,'evaluated');
  const partial=await read(T);
  assert.deepEqual(partial.events,[]);
  assert.equal(partial.evaluation[0].state,'pending');
});
