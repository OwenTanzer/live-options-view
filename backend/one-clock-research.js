import { requireSession,checkOrigin,readJsonBody,jsonResponse } from './common.js';
import { cutoverFenceActive } from './fence.js';
import { eventRule } from '../site/shared/one-clock-events.js';
import { instant } from '../site/shared/one-clock.js';
import { validSession,sessionAt } from '../site/shared/one-clock-session.js';
import { registerObservedRule,observeBatch,observedEventsAsOf } from './one-clock-observer.js';
import { ingestPublicSource,ingestedAsOf } from './one-clock-ingestion.js';
import { publicIngestionSources } from './one-clock-ingestion-sources.js';
import { equitySession,observerSessions } from './one-clock-calendar.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
const list=async s=>(await s.all()).results;
export const researchFields=Object.freeze(Object.assign({},...publicIngestionSources.map(source=>source.fields)));
const instruments=[...new Set(publicIngestionSources.flatMap(source=>source.instruments))];
function validatedRule(input) {
  const rule=eventRule(input);
  if(rule.id.length>100||rule.name.length>120||rule.instruments.length>3||rule.instruments.some(i=>!instruments.includes(i)))throw new Error('unsupported rule scope');
  for(const c of [...rule.conditions,...rule.outcomes]) {
    if(!Object.hasOwn(researchFields,c.source)||!Object.hasOwn(researchFields[c.source],c.field))throw new Error('This input is not yet available to the public observer.');
    if(rule.instruments.some(instrument=>!publicIngestionSources.some(source=>source.outputs.includes(c.source)&&source.instruments.includes(instrument))))throw new Error('Input unavailable for a selected instrument');
    if(c.maxAgeMs>86400000||(c.horizonMs??0)>86400000||(c.toleranceMs??0)>86400000)throw new Error('rule time bound');
  }
  return rule;
}
async function ruleList(db,namespace) {return list(stmt(db,'SELECT rule_id,version,registered_at,payload FROM one_clock_rules WHERE namespace=? ORDER BY rule_id,version LIMIT 101',namespace));}
export async function handleOneClockResearch(request,env,url=new URL(request.url)) {
  if(!['GET','POST'].includes(request.method))return jsonResponse({error:'method not allowed'},405);
  const auth=await requireSession(request,env);
  if(!auth||auth.record.is_bot)return jsonResponse({error:'Sign in to Options View to save and observe your event definitions.'},401);
  const namespace='account:'+auth.username,db=env.DB;
  if(env.ONE_CLOCK_OBSERVER!=='enabled')return jsonResponse({error:'The observer is not activated on this service.'},503);
  try {
    if(request.method==='GET') {
      if(url.searchParams.size===0) {
        const rules=await ruleList(db,namespace),activations=await list(stmt(db,'SELECT rule_id,version,started_at,ended_at FROM one_clock_activations WHERE namespace=? ORDER BY started_at LIMIT 1001',namespace));
        return jsonResponse({rules:rules.map(r=>({rule:JSON.parse(r.payload),registeredAt:r.registered_at})),activations,sources:researchFields,instruments,defaultSession:observerSessions(Date.now())[0],
          cadence:'Periodic archive observation on the existing 15-minute service invocation; catch-up and tenant rotation may add delay.',
          coverage:'Provisional: publications overwritten between polls and arbitrary late corrections to old sessions are not recoverable.'});
      }
      if([...url.searchParams.keys()].some(k=>!['session','instrument','cutoff'].includes(k)||url.searchParams.getAll(k).length!==1))throw new Error('invalid result query');
      const session=url.searchParams.get('session'),instrument=url.searchParams.get('instrument'),cutoff=instant(Number(url.searchParams.get('cutoff')));
      if(!validSession(session)||!instruments.includes(instrument)||cutoff>Date.now()+60000||sessionAt(cutoff)<session)throw new Error('invalid result scope');
      const events=await observedEventsAsOf(db,{namespace,selection:{session,instrument,cutoff,origin:'recorded'}});
      const coverage=await list(stmt(db,`SELECT h.coverage FROM one_clock_ingestion_history h WHERE session=? AND instrument=? AND recorded_at<=? AND generation=(SELECT MAX(h2.generation) FROM one_clock_ingestion_history h2 WHERE h2.source=h.source AND h2.session=h.session AND h2.instrument=h.instrument AND h2.recorded_at<=?)`,session,instrument,cutoff,cutoff));
      return jsonResponse({session,instrument,cutoff,events,coverage:coverage.map(r=>JSON.parse(r.coverage)),origin:'recorded'});
    }
    const origin=checkOrigin(request);if(origin)return origin;
    if(await cutoverFenceActive(env))return jsonResponse({error:'Writes are paused; try again shortly.'},503);
    const parsed=await readJsonBody(request);if(parsed.error)return parsed.error;
    const body=parsed.body,now=Date.now();
    if(body?.action==='save') {
      const rule=validatedRule(body.rule);
      await registerObservedRule(db,{namespace,rule,registeredAt:now});
      return jsonResponse({rule,registeredAt:now},201);
    }
    if(!['activate','deactivate'].includes(body?.action)||typeof body.ruleId!=='string'||body.ruleId.length>100||!Number.isSafeInteger(body.version))throw new Error('invalid rule action');
    const saved=await stmt(db,'SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?',namespace,body.ruleId,body.version).first();
    if(!saved)return jsonResponse({error:'Saved definition not found.'},404);
    validatedRule(JSON.parse(saved.payload));
    const active=await stmt(db,'SELECT version,started_at FROM one_clock_activations WHERE namespace=? AND rule_id=? AND ended_at IS NULL',namespace,body.ruleId).first();
    if(body.action==='activate'&&active?.version===body.version)return jsonResponse({active:true,startedAt:active.started_at});
    if(body.action==='deactivate'&&active?.version!==body.version)return jsonResponse({active:false});
    const history=await stmt(db,'SELECT COUNT(*) AS n FROM one_clock_activations WHERE namespace=?',namespace).first();
    if(history.n>=1000&&body.action==='activate')throw new Error('activation history bound');
    const writes=[stmt(db,'UPDATE one_clock_activations SET ended_at=? WHERE namespace=? AND rule_id=? AND ended_at IS NULL',now,namespace,body.ruleId)];
    if(body.action==='activate') {
      writes.push(stmt(db,'INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)',namespace,body.ruleId,body.version,now));
      const [day,previous]=observerSessions(now);
      for(const instrument of JSON.parse(saved.payload).instruments)for(const session of [day,previous])writes.push(stmt(db,'INSERT INTO one_clock_evaluation_work(namespace,session,instrument) VALUES(?,?,?) ON CONFLICT DO NOTHING',namespace,session,instrument));
    }
    await db.batch(writes);
    return jsonResponse({active:body.action==='activate',changedAt:now,pendingOutcomesContinue:true});
  } catch(error) {
    const message=String(error.message);
    if(/immutable|UNIQUE constraint/.test(message))return jsonResponse({error:'This version or activation changed concurrently. Reopen it and save a new version.'},409);
    if(/bound|scope|query|rule|input|invalid|field|choose|condition|outcome|instrument|version|required/i.test(message))return jsonResponse({error:message.slice(0,200)},400);
    return jsonResponse({error:'Observer storage is unavailable; no success is claimed.'},503);
  }
}

export async function advanceOneClockResearch(env,{now=Date.now(),sources=publicIngestionSources}={}) {
  if(env.ONE_CLOCK_OBSERVER!=='enabled')return [];
  const db=env.DB,[day,previous]=observerSessions(now);
  // Keep pending measurements after deactivation. Existing intervals retain
  // history; they never cause the evaluator to retract an old accepted event.
  // One bounded SQL rollover, not a statement loop for every user or rule.
  // Activation creates the initial scope jobs; old scopes retain evidence.
  await stmt(db,`INSERT INTO one_clock_evaluation_work(namespace,session,instrument)
    SELECT DISTINCT w.namespace,?,w.instrument FROM one_clock_evaluation_work w WHERE w.session=?
    AND EXISTS(SELECT 1 FROM one_clock_activations a WHERE a.namespace=w.namespace AND (a.ended_at IS NULL OR a.ended_at>=?))
    LIMIT 300 ON CONFLICT DO NOTHING`,day,previous,now-2*86400000).run();
  // Two accounts/four rules each per invocation; shared source reads are memoized.
  const jobs=await list(stmt(db,'SELECT * FROM one_clock_evaluation_work WHERE session IN (?,?) ORDER BY updated_at,namespace,session,instrument LIMIT 2',day,previous));
  const reads=new Map(),results=[];let statementBudget=800;
  for(const job of jobs) {
    try {
      const key=JSON.stringify([job.session,job.instrument]);
      if(!reads.has(key))reads.set(key,(async()=>{
        const coverage=[];
        for(const adapter of sources.filter(a=>a.instruments.includes(job.instrument)))coverage.push(await ingestPublicSource(db,env.PAPER_TRADES,adapter,{session:job.session,instrument:job.instrument,now,
          collection:job.session===day?'prospective':'backfill'}));
        return {coverage,records:await ingestedAsOf(db,{session:job.session,instrument:job.instrument,cutoff:now})};
      })());
      const data=await reads.get(key);
      if(data.coverage.some(c=>!c.indexComplete||c.pending>0)) {
        await stmt(db,'UPDATE one_clock_evaluation_work SET updated_at=? WHERE namespace=? AND session=? AND instrument=?',now,job.namespace,job.session,job.instrument).run();
        results.push({state:'catching-up',session:job.session,instrument:job.instrument});continue;
      }
      const windows={};
      for(const a of await list(stmt(db,'SELECT rule_id,version,started_at,ended_at FROM one_clock_activations WHERE namespace=? LIMIT 1001',job.namespace))) {
        const ruleKey=JSON.stringify([a.rule_id,a.version]);(windows[ruleKey]??=[]).push(a);
      }
      const bounds=equitySession(job.session);
      const rules=(await ruleList(db,job.namespace)).filter(r=>JSON.parse(r.payload).instruments.includes(job.instrument)&&
        (windows[JSON.stringify([r.rule_id,r.version])]??[]).some(w=>w.started_at<=bounds.close&&(w.ended_at===null||w.ended_at>=bounds.open)));
      const start=job.rule_cursor%Math.max(1,rules.length);let chosen=rules.slice(start,start+4);
      const selection={session:job.session,instrument:job.instrument,cutoff:now,origin:'recorded'};
      // Regular-session observations share the producer's calendar; premarket
      // payloads do not silently become an anchor or trigger for this policy.
      const sourceRecords=data.records.filter(r=>r.event_at>=bounds.open&&r.event_at<=bounds.close);
      const evaluate=()=>observeBatch(db,{namespace:job.namespace,batchId:`tick:${job.session}:${job.instrument}:${now}:${start}`,source:'public-registry',cursor:String(start),
        selection,sessionClose:bounds.close,collection:'prospective',observations:[],sourceRecords,activationWindows:windows,maxStatements:statementBudget,
        ruleKeys:chosen.map(r=>JSON.stringify([r.rule_id,r.version]))});
      let result;
      try{result=await evaluate();}catch(error){
        if(error.message!=='evaluation statement budget'||chosen.length<=1)throw error;
        chosen=chosen.slice(0,1);result=await evaluate();
      }
      statementBudget-=result.statements;
      await stmt(db,'UPDATE one_clock_evaluation_work SET updated_at=?,rule_cursor=? WHERE namespace=? AND session=? AND instrument=?',now,(start+chosen.length)%Math.max(1,rules.length),job.namespace,job.session,job.instrument).run();
      results.push({state:'evaluated',session:job.session,instrument:job.instrument,...result});
    } catch {
      // A broken source/account cannot monopolize the fair rotation. This is
      // scheduling progress only; no evidence or evaluation checkpoint moves.
      await stmt(db,'UPDATE one_clock_evaluation_work SET updated_at=? WHERE namespace=? AND session=? AND instrument=?',now,job.namespace,job.session,job.instrument).run();
      console.error('one_clock_observer_scope_failed');results.push({state:'failed',session:job.session,instrument:job.instrument});
    }
  }
  return results;
}
