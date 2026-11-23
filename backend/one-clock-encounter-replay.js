import {levelIngestion} from './one-clock-level-ingestion.js';
import {ingestPublicSource} from './one-clock-ingestion.js';
import {equitySession} from './one-clock-calendar.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';

export async function replayEncounter(env,namespace,{session,ruleId,version},now) {
  const bounds=equitySession(session);
  if(!bounds || bounds.open>now)throw new Error('Invalid replay session');
  const saved=await env.DB.prepare('SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?')
    .bind(namespace,ruleId,version).first();
  if(!saved)throw new Error('Saved rule version required');
  const rule=JSON.parse(saved.payload);
  if(!rule.levelEncounter)throw new Error('Choose a saved level encounter rule');
  const [instrument]=rule.instruments, selection={session,instrument,cutoff:now,origin:'recorded'};
  const coverage=await ingestPublicSource(env.DB,env.PAPER_TRADES,levelIngestion,{session,instrument,now,collection:'replay'});
  if(!coverage.indexComplete || coverage.pending)return {coverage,events:[],complete:false};
  const flags=await env.DB.prepare("SELECT payload FROM one_clock_ingested WHERE session=? AND instrument=? AND source IN ('oi-levels','level-spot') AND origin='recorded' AND received_at<=? LIMIT 4001")
    .bind(session,instrument,now).all();
  if(flags.results.length>4000)throw new Error('Replay level input bound');
  const observations=flags.results.map(r=>JSON.parse(r.payload)).filter(r=>r.eventAt>=bounds.open && r.eventAt<=bounds.close);
  const events=evaluateEvents({rule,observations,selection,sessionClose:bounds.close});
  if(events.length>1000)throw new Error('Replay occurrence bound; narrow the rule settings');
  return {session,instrument,cutoff:now,coverage,rule,complete:true,events:events.map(e=>({...e,collection:'replay'}))};
}
