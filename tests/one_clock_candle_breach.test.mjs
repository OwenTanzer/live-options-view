import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {candleSessions,candleStudy} from '../site/shared/one-clock-candle-replay.js';
import {candleTrials} from '../site/shared/one-clock-candle-breach.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {candleIngestion,replayCandleSession,chartIndex} from '../backend/one-clock-candle-ingestion.js';
import {ingestPublicSource,ingestedAsOf} from '../backend/one-clock-ingestion.js';
import {registerObservedRule,observeBatch,observedEventsAsOf} from '../backend/one-clock-observer.js';
const read=path=>fs.readFileSync(new URL('../'+path,import.meta.url),'utf8');
const reference='research/qqq_candle_breach/';
const ledger=read(reference+'results/trials.jsonl').trim().split('\n').map(s=>JSON.parse(s));
const near=(a,b)=>b===null?assert.equal(a,null):assert.ok(Math.abs(a-Number(b))<1e-9);
for(const session of candleSessions)test('Builder, ingestion, observer and repeat replay match every frozen trial for '+session,async t=>{
  const db=makeStorage();t.after(()=>db.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])
    await db.exec(read('backend/migrations/'+file));
  const csv=read('site/candle-replay/QQQ-'+session+'.txt');
  assert.equal(csv,read(reference+'data/QQQ-'+session+'.csv'));
  const study=candleStudy(session,csv,'fixture'), [rule]=study.rules;
  const expected=ledger.filter(r=>r.decision_at.startsWith(session));
  const trials=candleTrials(study.rows,study.selection,study.close);
  assert.equal(trials.length,expected.length);
  trials.forEach((r,i)=>{
    const e=expected[i];
    assert.deepEqual([r.trialId,r.direction,r.event,r.closeClass,r.openBeyond,r.bothExtremes],
      [e.trial_id,e.direction,e.event,e.close_class,e.open_beyond,e.both_extremes]);
    near(r.level,e.level);near(r.penetration,e.penetration_points);
    if(e.event){near(r.priorAtr14,e.prior_atr14_points);near(r.penetrationAtr14,e.penetration_atr14);}
  });
  const chart={chart:{result:[{meta:{symbol:'QQQ',dataGranularity:'1m'},
    timestamp:study.rows.map(r=>r.eventAt/1000-60),
    indicators:{quote:[Object.fromEntries(['open','high','low','close'].map(k=>[k,study.rows.map(r=>r.value[k])]))]}}]}};
  const receivedAt=study.close+60000;
  const index=chartIndex(chart,session,receivedAt,receivedAt);
  assert.equal(index.artifacts.length,390);
  assert.equal(chartIndex(chart,session,study.start+90000,receivedAt).artifacts.length,1);
  const source={...candleIngestion,receiptAfterDiscovery:false,discover:async()=>index};
  const scope={session,instrument:'QQQ',now:receivedAt};
  let receipt;
  for(let i=0;i<20;i++)receipt=await ingestPublicSource(db,{},source,scope);
  assert.equal(receipt.pending,0);
  assert.equal((await ingestPublicSource(db,{},source,scope)).processed,0);
  const selection={...study.selection,origin:'recorded',cutoff:receivedAt};
  assert.equal((await ingestedAsOf(db,{...selection,cutoff:receivedAt-1})).length,0);
  const records=await ingestedAsOf(db,selection);
  assert.equal(records.length,390);
  const namespace='account:candle-test';
  await registerObservedRule(db,{namespace,rule,registeredAt:study.start});
  const batch={namespace,batchId:'fixture',source:'public-registry',cursor:'1',selection,sessionClose:study.close,
    observations:[],sourceRecords:records,activationWindows:{[JSON.stringify([rule.id,rule.version])]:[{started_at:study.start,ended_at:null}]}};
  await observeBatch(db,batch);
  assert.equal((await observeBatch(db,batch)).duplicate,true);
  const events=await observedEventsAsOf(db,{namespace,selection});
  assert.equal(events.length,expected.filter(e=>e.event).length);
  const byId=new Map(events.map(e=>[e.breach.trialId,e]));
  for(const trial of expected.filter(e=>e.event)){
    const event=byId.get(trial.trial_id);
    assert.equal(event.decisionAt,Date.parse(trial.decision_at));
    assert.equal(event.triggeredAt,receivedAt);
    near(event.outcomes[0].value,trial.signed_change_5m_points);
  }
  const replay=await replayCandleSession(db,namespace,{session,ruleId:rule.id,version:1},receivedAt);
  assert.equal(replay.trials.length,expected.length);assert.equal(replay.events.length,events.length);
  const preview=evaluateEvents({rule,observations:study.rows,selection:{...study.selection,cutoff:study.close},sessionClose:study.close});
  assert.deepEqual(preview.map(e=>e.breach.trialId),replay.events.map(e=>e.breach.trialId));
});

test('noon activation publishes visible pairs while historical minutes are still pending',async t=>{
  const {advanceOneClockResearch}=await import('../backend/one-clock-research.js');
  const db=makeStorage();t.after(()=>db.db.close());
  for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])
    await db.exec(read('backend/migrations/'+file));
  const session=candleSessions[0],study=candleStudy(session,read(reference+'data/QQQ-'+session+'.csv'),'fixture');
  const namespace='account:noon', [rule]=study.rules, noon=study.start+150*60000;
  await registerObservedRule(db,{namespace,rule,registeredAt:noon});
  await db.prepare('INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)')
    .bind(namespace,rule.id,1,noon).run();
  let tick=noon;
  t.mock.method(Date,'now',()=>tick+250);
  const rows=study.rows;
  t.mock.method(globalThis,'fetch',async()=>Response.json({chart:{result:[{
    meta:{symbol:'QQQ',dataGranularity:'1m'},timestamp:rows.map(r=>r.eventAt/1000-60),
    indicators:{quote:[Object.fromEntries(['open','high','low','close'].map(k=>[k,rows.map(r=>r.value[k])]))]}}]}}));
  const selection={...study.selection,origin:'recorded'};
  const env={DB:db,ONE_CLOCK_OBSERVER:'enabled',PAPER_TRADES:{}};
  const eventsAt=cutoff=>observedEventsAsOf(db,{namespace,selection:{...selection,cutoff}});
  for(;tick<=study.close+30*60000;tick+=15*60000){
    await advanceOneClockResearch(env,{now:tick,sources:[candleIngestion]});
    const events=await eventsAt(tick);
    assert.ok(events.every(e=>e.decisionAt>=noon && e.triggeredAt<=tick));
    if(tick===noon)assert.equal(events.length,0);
    if(tick===noon+30*60000)assert.ok(events.length>0);
    if(tick===study.close){
      const replay=await replayCandleSession(db,namespace,{session,ruleId:rule.id,version:1},tick+250);
      assert.equal(replay.complete,false);assert.equal(replay.coverage.indexComplete,false);
      assert.ok(replay.coverage.gaps>0 && replay.trials.length>0);
    }
  }
  const resolved=await eventsAt(tick);
  const expected=ledger.filter(r=>r.event && r.decision_at.startsWith(session) && Date.parse(r.decision_at)>=noon);
  assert.deepEqual(resolved.map(e=>e.breach.trialId).sort(),expected.map(e=>e.trial_id).sort());
});

test('Results keeps signed candle outcomes apart from raw change for current and prior saved rules',async()=>{
  const {projectResults,ruleKey}=await import('../site/shared/one-clock-results.js');
  const session=candleSessions[0],study=candleStudy(session,read(reference+'data/QQQ-'+session+'.csv'),'fixture');
  const [rule]=study.rules;
  const event=evaluateEvents({rule,observations:study.rows,selection:{...study.selection,cutoff:study.close},sessionClose:study.close}).find(e=>e.breach.direction==='down');
  for(const measure of ['directional-change','change']){
    const candle={...rule,outcomes:[{...rule.outcomes[0],measure}]};
    const raw={...rule,id:'raw',transition:'rising',outcomes:[{...rule.outcomes[0],measure:'change'}]};
    const value=event.outcomes[0].value;
    const rawEvent={...event,id:'raw-event',ruleId:'raw',outcomes:[{...event.outcomes[0],value:-value}]};
    for(const selected of [raw,candle]){
      const result=projectResults({events:[event,rawEvent],rules:[raw,candle],cutoff:study.close,origin:'reconstructed',
        outcome:{ruleKey:ruleKey(selected),id:selected.outcomes[0].id}});
      assert.equal(result.summary.measured,1);assert.equal(result.summary.unscored,1);
      assert.equal(result.summary.mean,selected===raw?-value:value);
    }
  }
});
