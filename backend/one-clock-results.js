import {headlineDay} from '../site/shared/one-clock-headlines.js';
import {requireSession,jsonResponse} from './common.js';
import {instant} from '../site/shared/one-clock.js';
import {validSession,sessionAt,sessionInstant} from '../site/shared/one-clock-session.js';
import {equitySession} from './one-clock-calendar.js';
import {publicIngestionSources,levelEvaluationStatus} from './one-clock-ingestion-sources.js';
import {nativePublicationSources,markPendingNativeEvidence,nativeHandoffExists} from './one-clock-publication-sources.js';
import {observedEventsRangeAsOf} from './one-clock-observer.js';
const instruments=[...new Set([...publicIngestionSources,...nativePublicationSources].flatMap(s=>s.instruments))];
const query=(db,sql,...args)=>db.prepare(sql).bind(...args).all().then(r=>r.results);

export async function handleOneClockResults(request,env,url=new URL(request.url)) {
  if(request.method!=='GET')return jsonResponse({error:'method not allowed'},405);
  const auth=await requireSession(request,env);
  if(!auth||auth.record.is_bot)return jsonResponse({error:'Sign in to view your retained observation results.'},401);
  if(env.ONE_CLOCK_OBSERVER!=='enabled')return jsonResponse({error:'The observer is not activated on this service.'},503);
  try {
    if([...url.searchParams.keys()].some(k=>!['from','to','instrument','cutoff'].includes(k)||url.searchParams.getAll(k).length!==1))throw new Error('invalid result query');
    const from=url.searchParams.get('from'),to=url.searchParams.get('to'),instrument=url.searchParams.get('instrument'),cutoff=instant(Number(url.searchParams.get('cutoff')));
    if(!validSession(from)||!validSession(to)||from>to||!['ALL',...instruments].includes(instrument)||cutoff>Date.now()+60000||to>sessionAt(cutoff))throw new Error('invalid result scope');
    if(Date.parse(to)-Date.parse(from)>30*86400000)throw new Error('Choose at most 31 calendar days per result scope.');
    equitySession(from);equitySession(to);
    const selected=instrument==='ALL'?instruments:[instrument],namespace='account:'+auth.username,db=env.DB;
    const events=await observedEventsRangeAsOf(db,{namespace,from,to,instruments:selected,cutoff});
    const rules=await query(db,'SELECT payload FROM one_clock_rules WHERE namespace=? AND registered_at<=? ORDER BY rule_id,version LIMIT 101',namespace,cutoff);
    const marks=selected.map(()=>'?').join(',');
    const coverage=await query(db,`SELECT h.session,h.instrument,h.recorded_at,h.coverage FROM one_clock_ingestion_history h
      WHERE h.session>=? AND h.session<=? AND h.instrument IN (${marks}) AND h.recorded_at<=?
      AND h.generation=(SELECT MAX(h2.generation) FROM one_clock_ingestion_history h2 WHERE h2.source=h.source AND h2.session=h.session AND h2.instrument=h.instrument AND h2.recorded_at<=?) LIMIT 1001`,from,to,...selected,cutoff,cutoff);
    const attempts=await query(db,`SELECT h.session,h.instrument,h.rule_id,h.version,h.attempted_at,h.state,h.reason FROM one_clock_evaluation_history h
      WHERE h.namespace=? AND h.session>=? AND h.session<=? AND h.instrument IN (${marks}) AND h.attempted_at<=?
      AND h.attempted_at=(SELECT MAX(h2.attempted_at) FROM one_clock_evaluation_history h2 WHERE h2.namespace=h.namespace AND h2.session=h.session AND h2.instrument=h.instrument AND h2.rule_id=h.rule_id AND h2.version=h.version AND h2.attempted_at<=?) LIMIT 10001`,namespace,from,to,...selected,cutoff,cutoff);
    const activations=await query(db,'SELECT rule_id,version,started_at,ended_at FROM one_clock_activations WHERE namespace=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?) LIMIT 1001',namespace,Math.min(cutoff,sessionInstant(to,23,59,59,999)),sessionInstant(from,0));
    if(rules.length>100||coverage.length>1000||attempts.length>10000||activations.length>1000)throw new Error('Result scope exceeds response bound; choose fewer sessions.');
    const sourceCoverage=coverage.map(r=>JSON.parse(r.coverage));
    const definitions=rules.map(r=>JSON.parse(r.payload)),byAttempt=new Map(attempts.map(a=>[JSON.stringify([a.session,a.instrument,a.rule_id,a.version]),a])),evaluation=[];
    for(let day=Date.parse(from);day<=Date.parse(to);day+=86400000){
      const session=new Date(day).toISOString().slice(0,10),bounds=equitySession(session);if(!bounds)continue;
      for(const rule of definitions)for(const instrument of selected.filter(i=>rule.instruments.includes(i))) {
        const interval=rule.transition==='per-headline'?headlineDay(session):bounds;
        if(!activations.some(a=>a.rule_id===rule.id&&a.version===rule.version&&a.started_at<=Math.min(cutoff,interval.close)&&(a.ended_at===null||a.ended_at>=interval.open)))continue;
        evaluation.push(levelEvaluationStatus(byAttempt.get(JSON.stringify([session,instrument,rule.id,rule.version]))??{session,instrument,rule_id:rule.id,version:rule.version,state:'pending',attempted_at:null,reason:'No evaluation receipt at this cutoff.'},rule,sourceCoverage));
      }
    }
    const paused=env.ONE_CLOCK_PUBLICATIONS!=='enabled'&&await nativeHandoffExists(db);
    const currentEvaluation=env.ONE_CLOCK_PUBLICATIONS==='enabled'?await markPendingNativeEvidence(db,evaluation,cutoff,namespace):evaluation;
    const observerNotice=paused?'Current service state: native observation is paused. Historical receipts and retained results are unchanged; an explicit reverse handoff is required before legacy evaluation.':null;
    const body=JSON.stringify({owner:auth.username,session:from+'/'+to,instrument,cutoff,events,rules:definitions,coverage:coverage.map(r=>({...r,coverage:JSON.parse(r.coverage)})),evaluation:currentEvaluation,observerNotice,origin:'recorded'});
    if(new TextEncoder().encode(body).byteLength>8*1024*1024)throw new Error('Result scope exceeds response byte bound; choose fewer sessions.');
    return new Response(body,{headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
  }catch(error){
    const message=String(error.message);
    if(/invalid|scope|bound|31 calendar|milliseconds|calendar coverage/.test(message))return jsonResponse({error:message.slice(0,200)},400);
    return jsonResponse({error:'Retained results are unavailable; no complete result is claimed.'},503);
  }
}
