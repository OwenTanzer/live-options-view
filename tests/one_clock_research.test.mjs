import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResearch,advanceOneClockResearch} from '../backend/one-clock-research.js';
import {registerObservedRule,observeBatch,observedEventsAsOf} from '../backend/one-clock-observer.js';
import {demoRows,demoRules,demoStart,demoClose,demoSelection} from '../site/shared/one-clock-demo.js';
import {sessionAt,sessionInstant} from '../site/shared/one-clock-session.js';
import {equitySession,observerSessions} from '../backend/one-clock-calendar.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
async function setup(t) {
  const DB=makeStorage();t.after(()=>DB.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])await DB.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
  const env={DB,ONE_CLOCK_OBSERVER:'enabled'},storage=createStorage(env);
  for(const name of ['alice','other']) {await storage.users.insert(name,{username:name,version:0});await storage.sessions.create(name+'-token',name,3600);}
  const call=(user,body,query='',origin='https://options.moopertonic.net')=>handleOneClockResearch(new Request('https://options.moopertonic.net/api/one-clock/research'+query,{headers:{...(user?{Cookie:'session='+user+'-token'}:{}),Origin:origin,'Content-Type':'application/json'},...(body?{method:'POST',body:JSON.stringify(body)}:{})}),env);
  return {DB,env,call};
}
const rule={id:'test',version:1,name:'Underlying threshold',instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',conditions:[{source:'price',field:'spot',op:'>=',value:700,maxAgeMs:150000}],outcomes:[]};
test('account authorization, origin, immutable versions and source allowlist precede activation',async t=>{
  const {DB,call}=await setup(t);
  assert.equal((await call(null)).status,401);
  assert.equal((await call('alice',{action:'save',rule},'','https://evil.example')).status,403);
  assert.equal((await call('alice',{action:'save',rule})).status,201);
  assert.equal((await call('alice',{action:'save',rule:{...rule,name:'changed'}})).status,409);
  assert.equal((await call('other',{action:'activate',ruleId:rule.id,version:1})).status,404);
  assert.equal((await call('alice',{action:'save',rule:{...rule,id:'private',conditions:[{...rule.conditions[0],source:'journal'}]}})).status,400);
  assert.equal((await call('alice',{action:'activate',ruleId:rule.id,version:1})).status,200);
  assert.equal(DB.raw('SELECT COUNT(*) AS n FROM one_clock_evaluation_work')[0].n,2);
  assert.equal((await call('other')).status,200);assert.equal((await (await call('other')).json()).rules.length,0);
  assert.equal((await call('alice',{action:'deactivate',ruleId:rule.id,version:1})).status,200);
  assert.ok(DB.raw('SELECT ended_at FROM one_clock_activations')[0].ended_at);
});
test('activation intervals retain old events/outcomes and limited rule batches do not retract other rules',async t=>{
  const {DB}=await setup(t),namespace='account:alice',cutoff=demoClose+1000;
  for(const r of demoRules)await registerObservedRule(DB,{namespace,rule:r,registeredAt:demoStart});
  const records=demoRows.map(row=>({payload:JSON.stringify(row),collection:'prospective',received_at:row.availableAt}));
  const windows=Object.fromEntries(demoRules.map(r=>[JSON.stringify([r.id,r.version]),[{started_at:demoStart,ended_at:null}]]));
  const input={namespace,source:'public-registry',cursor:'0',selection:{...demoSelection,cutoff},sessionClose:demoClose,observations:[],sourceRecords:records,activationWindows:windows};
  await observeBatch(DB,{...input,batchId:'first'});
  const before=await observedEventsAsOf(DB,{namespace,selection:input.selection});assert.ok(before.length>1);
  windows[JSON.stringify([demoRules[0].id,demoRules[0].version])][0].ended_at=demoClose;
  await observeBatch(DB,{...input,batchId:'second',ruleKeys:[JSON.stringify([demoRules[0].id,demoRules[0].version])]});
  const after=await observedEventsAsOf(DB,{namespace,selection:input.selection});assert.equal(after.length,before.length);
  assert.deepEqual(after.map(e=>e.id).sort(),before.map(e=>e.id).sort());
  await assert.rejects(observeBatch(DB,{...input,batchId:'second',ruleKeys:[]}),/batch identity/);
});
test('scheduler shares reads between tenants, fairly rotates failures, and never needs KV',async t=>{
  const {DB,env}=await setup(t),now=Date.now(),day=observerSessions(now)[0];
  for(const name of ['alice','other']) {
    await registerObservedRule(DB,{namespace:'account:'+name,rule,registeredAt:now-10000});
    await DB.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)').bind('account:'+name,rule.id,1,now-10000).run();
    await DB.prepare('INSERT INTO one_clock_evaluation_work(namespace,session,instrument) VALUES(?,?,?)').bind('account:'+name,day,'QQQ').run();
  }
  let discoveries=0;
  const source={id:'fixture',version:'1',instruments:['QQQ'],outputs:['price'],discover:async()=>{discoveries++;return {artifacts:[],complete:true};},read:async()=>{throw new Error('no artifacts');}};
  env.PAPER_TRADES={};
  const result=await advanceOneClockResearch(env,{now,sources:[source]});
  assert.equal(result.length,2);assert.ok(result.every(r=>r.state==='evaluated'));assert.equal(discoveries,1);
  const failed=await advanceOneClockResearch(env,{now:now+1000,sources:[{...source,discover:async()=>{throw new Error('offline');}}]});
  assert.ok(failed.every(r=>r.state==='failed'));
  assert.ok(DB.raw('SELECT updated_at FROM one_clock_evaluation_work').every(r=>r.updated_at===now+1000));
});
test('observer uses producer calendar early closes, closed days and explicit range',()=>{
  assert.equal(equitySession('2026-11-27').close,sessionInstant('2026-11-27',13));
  assert.equal(equitySession('2026-11-26'),null);
  assert.deepEqual(observerSessions(sessionInstant('2026-11-21',12)),['2026-11-20','2026-11-19']);
  assert.throws(()=>equitySession('2031-01-02'),/coverage/);
});
const emptySource={id:'fixture',version:'1',instruments:['QQQ'],outputs:['price'],discover:async()=>({artifacts:[],complete:true}),read:async()=>{throw new Error('no artifacts');}};
async function activate(DB,r,at) {
  await registerObservedRule(DB,{namespace:'account:alice',rule:r,registeredAt:at});
  await DB.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)').bind('account:alice',r.id,r.version,at).run();
}
test('active definitions recover jobs after missed sessions without any preceding-session job',async t=>{
  const {DB,env}=await setup(t),at=sessionInstant('2026-11-19',9,30);
  await activate(DB,rule,at);
  await DB.prepare('INSERT INTO one_clock_evaluation_work(namespace,session,instrument) VALUES(?,?,?)').bind('account:alice','2026-11-19','QQQ').run();
  const results=await advanceOneClockResearch({...env,PAPER_TRADES:{}},{now:sessionInstant('2026-11-23',12),sources:[emptySource]});
  assert.deepEqual(results.map(r=>r.session).sort(),['2026-11-20','2026-11-23']);
  assert.ok(results.every(r=>r.state==='evaluated'));
  assert.equal(DB.raw('SELECT COUNT(*) AS n FROM one_clock_evaluation_work')[0].n,3);
});
test('pause and replacement preserve closing scopes before outage recovery runs',async t=>{
  const realNow=Date.now;Date.now=()=>sessionInstant('2026-11-23',12);t.after(()=>{Date.now=realNow;});
  const {DB,call}=await setup(t),at=sessionInstant('2026-11-19',9,30);
  await activate(DB,rule,at);
  await DB.prepare('INSERT INTO one_clock_evaluation_work(namespace,session,instrument) VALUES(?,?,?)').bind('account:alice','2026-11-19','QQQ').run();
  assert.equal((await call('alice',{action:'deactivate',ruleId:rule.id,version:1})).status,200);
  assert.deepEqual(DB.raw("SELECT session FROM one_clock_evaluation_work WHERE session>'2026-11-19' ORDER BY session").map(r=>r.session),['2026-11-20','2026-11-23']);
  await activate(DB,{...rule,id:'replacement'},at);
  await registerObservedRule(DB,{namespace:'account:alice',rule:{...rule,id:'replacement',version:2,instruments:['SPY']},registeredAt:at});
  // Remove only disposable fixture jobs to model the same outage for replacement.
  await DB.prepare('DELETE FROM one_clock_evaluation_work').run();
  assert.equal((await call('alice',{action:'activate',ruleId:'replacement',version:2})).status,200);
  assert.deepEqual(DB.raw('SELECT session,instrument FROM one_clock_evaluation_work ORDER BY session,instrument'),[
    {session:'2026-11-20',instrument:'QQQ'},{session:'2026-11-20',instrument:'SPY'},
    {session:'2026-11-23',instrument:'QQQ'},{session:'2026-11-23',instrument:'SPY'}]);
});
test('inactive rises do not consume once allowance; activation preserves condition state and session anchors',async t=>{
  const {DB}=await setup(t),at=sessionInstant('2026-11-20',9,30),session='2026-11-20';
  const once={...rule,transition:'once-per-session'};
  const rows=[701,699,702].map((spot,i)=>({source:'price',id:String(i),revision:1,session,instrument:'QQQ',origin:'recorded',eventAt:at+i*60000,availableAt:at+i*60000,evidence:'fixture://activation/'+i,value:{spot}}));
  const selection={session,instrument:'QQQ',origin:'recorded',cutoff:at+180000},windows=[{started_at:at+90000,ended_at:null}];
  const args={rule:once,observations:rows,selection,sessionClose:equitySession(session).close,activationWindows:windows};
  assert.deepEqual(evaluateEvents(args).map(e=>e.triggeredAt),[at+120000]);
  assert.equal(evaluateEvents({...args,observations:rows.filter((_,i)=>i!==1)}).length,0,'activation does not invent a rising edge while already true');
  const anchored={...once,conditions:[{...rule.conditions[0],relativeToAnchor:true,value:1}]};
  const anchoredEvent=evaluateEvents({...args,rule:anchored})[0];
  assert.equal(anchoredEvent.inputEvidence[0].anchor,701);
  await registerObservedRule(DB,{namespace:'account:alice',rule:once,registeredAt:at});
  await observeBatch(DB,{namespace:'account:alice',batchId:'activation',source:'public-registry',cursor:'0',selection,sessionClose:args.sessionClose,observations:[],
    sourceRecords:rows.map(row=>({payload:JSON.stringify(row),received_at:row.availableAt,collection:'prospective'})),activationWindows:{[JSON.stringify([once.id,1])]:windows}});
  assert.deepEqual((await observedEventsAsOf(DB,{namespace:'account:alice',selection})).map(e=>e.triggeredAt),[at+120000]);
});
test('oversized rules retain visible as-of failures and do not starve following rules',async t=>{
  const {DB,env,call}=await setup(t),session='2026-11-20',at=sessionInstant(session,9,30),now=equitySession(session).close+1000;
  await activate(DB,{...rule,id:'a-heavy'},at);
  await activate(DB,{...rule,id:'z-small',transition:'once-per-session'},at);
  const writes=Array.from({length:900},(_,i)=>{
    const row={source:'price',id:String(i),revision:1,session,instrument:'QQQ',origin:'recorded',eventAt:at+i*1000,availableAt:at+i*1000,evidence:'fixture://bounded/'+i,value:{spot:i%2===0?701:699}};
    return DB.prepare('INSERT INTO one_clock_ingested(source,session,instrument,origin,record_id,revision,event_at,available_at,received_at,collection,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind('price',session,'QQQ','recorded',row.id,1,row.eventAt,row.availableAt,row.availableAt,'prospective',JSON.stringify(row));
  });await DB.batch(writes);
  const tick=at=>advanceOneClockResearch({...env,PAPER_TRADES:{}},{now:at,sources:[emptySource]});
  assert.equal((await tick(now))[0].state,'failed');
  assert.equal(DB.raw('SELECT rule_cursor FROM one_clock_evaluation_work')[0].rule_cursor,1);
  assert.equal((await tick(now+1000))[0].state,'evaluated');
  const result=async(user,cutoff)=>(await call(user,null,'?'+new URLSearchParams({session,instrument:'QQQ',cutoff:String(cutoff)}))).json();
  const before=await result('alice',now-1);assert.ok(before.evaluation.every(r=>r.state==='pending'));
  const after=await result('alice',now+1000);
  assert.equal(after.events.length,1);assert.equal(after.events[0].ruleId,'z-small');
  assert.deepEqual(after.evaluation.map(r=>r.state),['failed','evaluated']);
  assert.match(after.evaluation[0].reason,/capacity/);
  const other=await result('other',now+1000);assert.equal(other.events.length,0);assert.equal(other.evaluation.length,0);
});
