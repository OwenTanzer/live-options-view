import {fanoutChanges,snapshotVersion,generationWrites,finishHistory} from './one-clock-reevaluation.js';
import { requireSession,checkOrigin,readJsonBody,jsonResponse } from './common.js';
import { cutoverFenceActive } from './fence.js';
import { eventRule } from '../site/shared/one-clock-events.js';
import { instant } from '../site/shared/one-clock.js';
import { validSession,sessionAt } from '../site/shared/one-clock-session.js';
import { registerObservedRule,observeBatch,observedEventsAsOf } from './one-clock-observer.js';
import { ingestPublicSource,ingestedAsOf } from './one-clock-ingestion.js';
import { publicIngestionSources } from './one-clock-ingestion-sources.js';
import {advanceNativePublications,nativePublicationSources,nativeScopeRecords,markPendingNativeEvidence,retainNativeCoverage,nativeHandoffExists} from './one-clock-publication-sources.js';
import {handoffSource} from './one-clock-handoff.js';
import { equitySession,observerSessions } from './one-clock-calendar.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
const list=async s=>(await s.all()).results;
export const researchFields=Object.freeze(Object.assign({},...nativePublicationSources.map(s=>Object.fromEntries(Object.entries(s.outputs).map(([source,fields])=>[source,Object.fromEntries(Object.keys(fields).map(field=>[field,s.fields?.[source]?.[field]??field]))]))),...publicIngestionSources.map(source=>source.fields)));
const instruments=[...new Set([...publicIngestionSources,...nativePublicationSources].flatMap(source=>source.instruments))];
function validatedRule(input) {
  const rule=eventRule(input);
  if(rule.id.length>100||rule.name.length>120||rule.instruments.length>3||rule.instruments.some(i=>!instruments.includes(i)))throw new Error('unsupported rule scope');
  for(const c of [...rule.conditions,...rule.outcomes]) {
    if(!Object.hasOwn(researchFields,c.source)||!Object.hasOwn(researchFields[c.source],c.field))throw new Error('This input is not yet available to the public observer.');
    if(rule.instruments.some(instrument=>![...publicIngestionSources,...nativePublicationSources].some(source=>(Array.isArray(source.outputs)?source.outputs.includes(c.source):Object.hasOwn(source.outputs,c.source))&&(source.instrumentsByOutput?.[c.source]??source.instruments).includes(instrument))))throw new Error('Input unavailable for a selected instrument');
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
          coverage:'Native revisions queue bounded reevaluation, including paused rules. Legacy sources can miss revisions overwritten between polls.'});
      }
      if([...url.searchParams.keys()].some(k=>!['session','instrument','cutoff'].includes(k)||url.searchParams.getAll(k).length!==1))throw new Error('invalid result query');
      const session=url.searchParams.get('session'),instrument=url.searchParams.get('instrument'),cutoff=instant(Number(url.searchParams.get('cutoff')));
      if(!validSession(session)||!instruments.includes(instrument)||cutoff>Date.now()+60000||sessionAt(cutoff)<session)throw new Error('invalid result scope');
      const events=await observedEventsAsOf(db,{namespace,selection:{session,instrument,cutoff,origin:'recorded'}});
      const coverage=await list(stmt(db,`SELECT h.coverage FROM one_clock_ingestion_history h WHERE session=? AND instrument=? AND recorded_at<=? AND generation=(SELECT MAX(h2.generation) FROM one_clock_ingestion_history h2 WHERE h2.source=h.source AND h2.session=h.session AND h2.instrument=h.instrument AND h2.recorded_at<=?)`,session,instrument,cutoff,cutoff));
      const attempts=await list(stmt(db,`SELECT h.rule_id,h.version,h.attempted_at,h.state,h.reason FROM one_clock_evaluation_history h
        WHERE namespace=? AND session=? AND instrument=? AND attempted_at<=?
        AND attempted_at=(SELECT MAX(h2.attempted_at) FROM one_clock_evaluation_history h2 WHERE h2.namespace=h.namespace AND h2.session=h.session AND h2.instrument=h.instrument AND h2.rule_id=h.rule_id AND h2.version=h.version AND h2.attempted_at<=?) LIMIT 101`,namespace,session,instrument,cutoff,cutoff));
      const bounds=equitySession(session);
      const applicable=bounds?await list(stmt(db,`SELECT DISTINCT r.rule_id,r.version FROM one_clock_rules r JOIN one_clock_activations a
        ON a.namespace=r.namespace AND a.rule_id=r.rule_id AND a.version=r.version
        WHERE r.namespace=? AND r.registered_at<=? AND a.started_at<=? AND (a.ended_at IS NULL OR a.ended_at>=?)
        AND EXISTS(SELECT 1 FROM json_each(r.payload,'$.instruments') WHERE value=?) LIMIT 101`,namespace,cutoff,Math.min(cutoff,bounds.close),bounds.open,instrument)):[];
      const evaluation=applicable.map(r=>attempts.find(a=>a.rule_id===r.rule_id&&a.version===r.version)??{...r,state:'pending',attempted_at:null,reason:'No evaluation receipt at this cutoff.'});
      const status=env.ONE_CLOCK_PUBLICATIONS==='enabled'?await markPendingNativeEvidence(db,evaluation.map(e=>({...e,session,instrument})),cutoff,namespace):evaluation;
      return jsonResponse({session,instrument,cutoff,events,coverage:coverage.map(r=>JSON.parse(r.coverage)),evaluation:status,origin:'recorded'});
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
    // Read the generation first: any action interleaving with the subsequent
    // interval read makes the entire mutation fail its transactional CAS.
    const activationState=await stmt(db,'SELECT generation FROM one_clock_activation_state WHERE namespace=? AND rule_id=?',namespace,body.ruleId).first();
    const active=await stmt(db,'SELECT version,started_at FROM one_clock_activations WHERE namespace=? AND rule_id=? AND ended_at IS NULL',namespace,body.ruleId).first();
    if(body.action==='activate'&&active?.version===body.version)return jsonResponse({active:true,startedAt:active.started_at});
    if(body.action==='deactivate'&&active?.version!==body.version)return jsonResponse({active:false});
    const history=await stmt(db,'SELECT COUNT(*) AS n FROM one_clock_activations WHERE namespace=?',namespace).first();
    if(history.n>=1000&&body.action==='activate')throw new Error('activation history bound');
    if(active&&now<active.started_at)throw new Error('activation generation conflict');
    const writes=[stmt(db,`INSERT INTO one_clock_activation_state(namespace,rule_id,generation) VALUES(?,?,?)
      ON CONFLICT(namespace,rule_id) DO UPDATE SET generation=excluded.generation`,namespace,body.ruleId,(activationState?.generation??0)+1)];
    if(active)writes.push(stmt(db,'UPDATE one_clock_activations SET ended_at=? WHERE namespace=? AND rule_id=? AND version=? AND started_at=? AND ended_at IS NULL',now,namespace,body.ruleId,active.version,active.started_at));
    const closing=active?await stmt(db,'SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?',namespace,body.ruleId,active.version).first():null;
    const scopes=new Set(closing?JSON.parse(closing.payload).instruments:[]);
    if(body.action==='activate') {
      writes.push(stmt(db,'INSERT INTO one_clock_activations(namespace,rule_id,version,started_at) VALUES(?,?,?,?)',namespace,body.ruleId,body.version,now));
      for(const instrument of JSON.parse(saved.payload).instruments)scopes.add(instrument);
    }
    // Pause or replacement may arrive before recovery after an outage. Preserve
    // the closing version's recent scopes atomically with its closed interval.
    for(const instrument of scopes)for(const session of observerSessions(now))writes.push(stmt(db,'INSERT INTO one_clock_evaluation_work(namespace,session,instrument) VALUES(?,?,?) ON CONFLICT DO NOTHING',namespace,session,instrument));
    await db.batch(writes);
    return jsonResponse({active:body.action==='activate',changedAt:now,pendingOutcomesContinue:true});
  } catch(error) {
    const message=String(error.message);
    if(/immutable|UNIQUE constraint|activation generation conflict/.test(message))return jsonResponse({error:'This version or activation changed concurrently. Reopen it before retrying.'},409);
    if(/bound|scope|query|rule|input|invalid|field|choose|condition|outcome|instrument|version|required/i.test(message))return jsonResponse({error:message.slice(0,200)},400);
    return jsonResponse({error:'Observer storage is unavailable; no success is claimed.'},503);
  }
}

export async function advanceOneClockResearch(env,{now=Date.now(),sources=publicIngestionSources,publicationSources=nativePublicationSources}={}) {
  if(env.ONE_CLOCK_OBSERVER!=='enabled')return [];
  const db=env.DB,[day,previous]=observerSessions(now);
  if(env.ONE_CLOCK_PUBLICATIONS!=='enabled'&&await nativeHandoffExists(db))return [{state:'paused',reason:'Native handoff history exists. Re-enable its consumer or perform an explicit reverse handoff before legacy evaluation.'}];
  // Capture progress is independent of how many accounts have active rules.
  // Existing invocation only; no additional feed, schedule or tenant ingestion.
  const publicationProgress=await advanceNativePublications(env,{now,descriptors:publicationSources});
  // Keep pending measurements after deactivation. Existing intervals retain
  // history; they never cause the evaluator to retract an old accepted event.
  // Recover from the capped active set, not lifetime definition history.
  // CROSS JOIN preserves the indexed active-first plan. Paused definitions
  // already retain their activation-created jobs for maturing same-day outcomes.
  // Existing jobs are excluded before LIMIT so recovery progresses fairly.
  await stmt(db,`INSERT INTO one_clock_evaluation_work(namespace,session,instrument)
    SELECT DISTINCT r.namespace,d.value,i.value FROM one_clock_activations a INDEXED BY one_clock_one_active_version
    CROSS JOIN one_clock_rules r ON a.namespace=r.namespace AND a.rule_id=r.rule_id AND a.version=r.version
    JOIN json_each(r.payload,'$.instruments') i JOIN json_each(json_array(?,?)) d
    WHERE a.ended_at IS NULL AND a.started_at<=MIN(?,CASE WHEN d.value=? THEN ? ELSE ? END)
    AND NOT EXISTS(SELECT 1 FROM one_clock_evaluation_work w WHERE w.namespace=r.namespace AND w.session=d.value AND w.instrument=i.value)
    ORDER BY r.namespace,d.value,i.value LIMIT 300 ON CONFLICT DO NOTHING`,day,previous,now,day,equitySession(day).close,equitySession(previous).close).run();
  // Two accounts/four rules each per invocation; shared source reads are memoized.
  const nativeEnabled=env.ONE_CLOCK_PUBLICATIONS==='enabled';
  let discovery=null;
  if(nativeEnabled) discovery=await fanoutChanges(db,now);
  const jobs=await list(stmt(db,'SELECT * FROM one_clock_evaluation_work WHERE session IN (?,?) ORDER BY updated_at,namespace,session,instrument LIMIT 2',day,previous));
  if(nativeEnabled && discovery.state!=='indexing') {
    const backlog=await list(stmt(db,'SELECT * FROM one_clock_historical_rules WHERE retry_at<=? ORDER BY retry_at,enqueued_at LIMIT 2',now));
    const fresh=backlog.filter(h=>!jobs.some(j=>j.namespace===h.namespace && j.session===h.session && j.instrument===h.instrument));
    if(fresh.length) {
      if(jobs.length) {
        jobs.splice(1,1);
        if(Math.floor(now/900000)%2===0) jobs.unshift(fresh[0]); else jobs.push(fresh[0]);
      } else jobs.push(...fresh);
    }
  }
  const reads=new Map(),results=[];let statementBudget=800;
  for(const job of jobs) {
    try {
      const historic=job.target!==undefined;
      const version=nativeEnabled && discovery.state!=='indexing' ? await snapshotVersion(db,job.session,job.instrument,now,job.target) : null;
      if(historic) {
        const done=await stmt(db,'SELECT MAX(generation) generation FROM one_clock_generation_receipts WHERE namespace=? AND session=? AND instrument=? AND rule_id=? AND version=?',job.namespace,job.session,job.instrument,job.rule_id,job.version).first();
        if(done.generation>=job.target) {await finishHistory(db,job,now);results.push({state:'already-evaluated',session:job.session,instrument:job.instrument});continue;}
      }
      const snapshotAt=historic?version.receivedAt:now;
      const key=JSON.stringify([job.session,job.instrument,snapshotAt,version?.generation,historic]);
      if(!reads.has(key))reads.set(key,(async()=>{
        const coverage=[],owners=new Map();
        if(env.ONE_CLOCK_PUBLICATIONS==='enabled')for(const descriptor of publicationSources.filter(d=>d.instruments.includes(job.instrument))) {
          const native=await nativeScopeRecords(db,descriptor,{session:job.session,instrument:job.instrument,cutoff:snapshotAt,
            maxSequence:version ? (version.sequences[descriptor.id+'/'+job.instrument]??0) : null,
            currentProgress:publicationProgress.find(p=>p.stream===descriptor.id+'/'+job.instrument)});
          if(!native)continue;
          coverage.push(native.coverage);if(!historic)await retainNativeCoverage(db,native.coverage);
          if(!native.coverage.indexComplete)continue;
          for(const source of Object.keys(descriptor.outputs)) {
            if(owners.has(source))throw new Error('multiple native source owners');
            owners.set(source,{descriptor,native});
          }
        }
        for(const adapter of sources.filter(a=>a.instruments.includes(job.instrument))) {
          if(adapter.outputs.every(source=>owners.get(source)?.descriptor.supersedes?.[source]===adapter.id))continue;
          if(!historic)coverage.push(await ingestPublicSource(db,env.PAPER_TRADES,adapter,{session:job.session,instrument:job.instrument,now,collection:job.session===day?'prospective':'backfill'}));
        }
        const legacy=await ingestedAsOf(db,{session:job.session,instrument:job.instrument,cutoff:snapshotAt});
        const records=legacy.filter(r=>!owners.has(r.source));
        for(const [source,{descriptor,native}] of owners) {
          if(!descriptor.supersedes?.[source]&&legacy.some(r=>r.source===source))throw new Error('native source needs explicit supersession');
          records.push(...await handoffSource(db,descriptor,native,legacy,{source,session:job.session,instrument:job.instrument,cutoff:now,snapshotCutoff:snapshotAt}));
        }
        return {coverage,records};
      })());
      const data=await reads.get(key);
      if(data.coverage.some(c=>!c.indexComplete||c.pending>0)) {
        if(historic) await finishHistory(db,job,now,'Historical source coverage pending.');
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
      const start=(job.rule_cursor??0)%Math.max(1,rules.length);let chosen=historic?rules.filter(r=>r.rule_id===job.rule_id&&r.version===job.version):rules.slice(start,start+4);
      // Mapping a legacy identity can itself become known after the source
      // snapshot. Keep that actual handoff time and the transaction time distinct.
      const evidenceAt=historic?Math.max(snapshotAt,...data.records.map(r=>r.received_at)):now;
      const selection={session:job.session,instrument:job.instrument,cutoff:evidenceAt,origin:'recorded'};
      // Regular-session observations share the producer's calendar; premarket
      // payloads do not silently become an anchor or trigger for this policy.
      const sourceRecords=data.records.filter(r=>r.event_at>=bounds.open&&r.event_at<=bounds.close);
      if(statementBudget<4)break; // Defer untouched rules, rather than call a depleted budget a failure.
      const attempts=(selected,state,reason)=>selected.map(r=>stmt(db,`INSERT INTO one_clock_evaluation_history
        (namespace,session,instrument,rule_id,version,attempted_at,state,reason) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(namespace,session,instrument,rule_id,version,attempted_at) DO UPDATE SET state=excluded.state,reason=excluded.reason
        WHERE excluded.state='evaluated'`,job.namespace,job.session,job.instrument,r.rule_id,r.version,now,state,reason));
      const evaluate=()=>observeBatch(db,{namespace:job.namespace,batchId:historic?`history:${job.target}:${job.rule_id}:${job.version}`:`tick:${job.session}:${job.instrument}:${now}:${start}`,source:'public-registry',cursor:String(start),
        selection,committedAt:now,targetGeneration:version?.generation,completionStatements:[...attempts(chosen,'evaluated',null),...(version?generationWrites(db,job,chosen,version.generation,now):[])],sessionClose:bounds.close,collection:'prospective',observations:[],sourceRecords,activationWindows:windows,maxStatements:statementBudget,
        ruleKeys:chosen.map(r=>JSON.stringify([r.rule_id,r.version]))});
      let result,failure=null;
      try{result=await evaluate();}catch(error){
        chosen=chosen.slice(0,1);
        try{result=await evaluate();}catch(singleError){
          if(singleError.message==='evaluation statement budget'&&statementBudget<800){results.push({state:'deferred',session:job.session,instrument:job.instrument});continue;}
          failure=singleError.message==='evaluation statement budget'?'Rule exceeds the bounded evaluation capacity. Narrow its scope or save a less frequent trigger.':'Rule evaluation failed; retained evidence is unchanged.';
        }
      }
      statementBudget-=result?.statements??0;
      // Evidence, successful attempt and generation acknowledgment commit together.
      // Advancing a queue cursor afterward is safe to retry after a crash.
      const schedule=failure?attempts(chosen,'failed',failure):[];
      if(!historic) schedule.push(stmt(db,'UPDATE one_clock_evaluation_work SET updated_at=?,rule_cursor=? WHERE namespace=? AND session=? AND instrument=?',now,(start+chosen.length)%Math.max(1,rules.length),job.namespace,job.session,job.instrument));
      if(schedule.length)await db.batch(schedule);
      if(historic) await finishHistory(db,job,now,failure);
      else if(version && !failure) {
        for(const rule of chosen) {
          const queued=await stmt(db,'SELECT * FROM one_clock_historical_rules WHERE namespace=? AND session=? AND instrument=? AND rule_id=? AND version=?',job.namespace,job.session,job.instrument,rule.rule_id,rule.version).first();
          if(queued && queued.target<=version.generation) await finishHistory(db,queued,now);
        }
      }
      results.push({state:failure?'failed':'evaluated',session:job.session,instrument:job.instrument,...result});
    } catch {
      // A broken source/account cannot monopolize the fair rotation. This is
      // scheduling progress only; no evidence or evaluation checkpoint moves.
      await stmt(db,'UPDATE one_clock_evaluation_work SET updated_at=? WHERE namespace=? AND session=? AND instrument=?',now,job.namespace,job.session,job.instrument).run();
      if(job.target!==undefined) await finishHistory(db,job,now,'Historical source evaluation failed.');
      console.error('one_clock_observer_scope_failed');results.push({state:'failed',session:job.session,instrument:job.instrument});
    }
  }
  if(discovery?.state==='indexing') results.push({state:'indexing-history'});
  if(discovery?.state==='discovered' && results.length) results[0].historicalDiscovery=discovery;
  return results;
}
