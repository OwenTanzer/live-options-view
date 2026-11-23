import {levelIngestion} from './one-clock-level-ingestion.js';
import {ingestPublicSource} from './one-clock-ingestion.js';
import {collectorPublication,nativeScopeRecords} from './one-clock-publication-sources.js';
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
  const prices=await nativeScopeRecords(env.DB,collectorPublication,selection);
  if(!prices?.coverage.indexComplete)return {coverage:{...coverage,reason:'native-price-index-incomplete'},events:[],complete:false};
  const flags=await env.DB.prepare("SELECT payload FROM one_clock_ingested WHERE session=? AND instrument=? AND source='oi-levels' AND origin='recorded' AND received_at<=? LIMIT 2001")
    .bind(session,instrument,now).all();
  if(flags.results.length>2000)throw new Error('Replay level input bound');
  const observations=[...prices.records.filter(r=>r.source==='price'),...flags.results].map(r=>JSON.parse(r.payload));
  const events=evaluateEvents({rule,observations,selection,sessionClose:bounds.close});
  if(events.length>1000)throw new Error('Replay occurrence bound; narrow the rule settings');
  return {session,instrument,cutoff:now,coverage,rule,complete:true,events:events.map(e=>({...e,collection:'replay'}))};
}
