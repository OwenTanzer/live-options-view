import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {equitySession} from './one-clock-calendar.js';
import {candleSource, validBar,candleTrials} from '../site/shared/one-clock-candle-breach.js';

const endpoint='https://query1.finance.yahoo.com/v8/finance/chart/QQQ';
const minute=60000;

// A completed bar is qualified by a later provider timestamp, or by the
// regular-session close after the final minute. Preserve the first qualified
// response for each minute: later historical downloads cannot move its receipt.
export function chartIndex(payload, session, cutoff, receivedAt) {
  const bounds=equitySession(session), chart=payload?.chart?.result?.[0];
  if(chart?.meta?.symbol!=='QQQ' || chart.meta.dataGranularity!=='1m')throw new Error('Unexpected QQQ chart');
  const stamps=chart.timestamp, quote=chart.indicators?.quote?.[0];
  if(!Array.isArray(stamps) || stamps.length>410 || !quote)throw new Error('Invalid QQQ session chart');
  const artifacts=[];
  for(let i=0;i<stamps.length;i++) {
    const start=stamps[i]*1000, end=start+minute;
    if(!Number.isSafeInteger(start) || (i && stamps[i]<=stamps[i-1]))throw new Error('Unordered QQQ minutes');
    if(start%minute || start<bounds.open || end>Math.min(cutoff,bounds.close))continue;
    if(!(stamps[i+1]*1000>=end && stamps[i+1]*1000<=cutoff) &&
       !(end===bounds.close && cutoff>=bounds.close+minute))continue;
    const value=Object.fromEntries(['open','high','low','close'].map(k=>[k,quote[k]?.[i]]));
    if(validBar(value))artifacts.push({key:'QQQ/'+start,version:'first-final-v1',eventAt:end,availableAt:receivedAt,value});
  }
  // Allow one minute for the rollover witness on the latest candle.
  const expected=Math.max(0,Math.floor((Math.min(bounds.close,cutoff-minute)-bounds.open)/minute));
  const missingMinutes=expected-artifacts.filter(a=>a.eventAt<=bounds.open+expected*minute).length;
  return {artifacts,missingMinutes,complete:artifacts.length>0,reason:'awaiting-finalized-QQQ-bars'};
}

export const candleIngestion={
  id:'qqq-closed-minutes',version:'1',owner:'One Clock',demandDriven:true,receiptAfterDiscovery:true,
  instruments:['QQQ'],outputs:[candleSource],fields:{[candleSource]:{open:'Open',high:'High',low:'Low',close:'Close'}},
  async discover(bucket,{session,cutoff},db,versions=[]) {
    const bounds=equitySession(session);
    if(!bounds || cutoff<bounds.open+minute)return {artifacts:[],complete:false,reason:'before-first-minute-close'};
    const sessionMinutes=(bounds.close-bounds.open)/minute;
    if(versions.length===sessionMinutes && cutoff>=bounds.close+minute)return {artifacts:[],complete:true};
    const url=new URL(endpoint);
    url.search=new URLSearchParams({period1:String(bounds.open/1000),period2:String(bounds.close/1000),
      interval:'1m',includePrePost:'false'});
    let response;
    try {
      response=await fetch(url,{signal:AbortSignal.timeout(5000),headers:{accept:'application/json','User-Agent':'OptionsView/1.0'}});
    } catch {
      return {artifacts:[],complete:false,reason:'QQQ minute source unreachable'};
    }
    if(!response.ok)return {artifacts:[],complete:false,reason:'QQQ minute source returned '+response.status};
    const text=await response.text();
    if(text.length>150000)throw new Error('QQQ chart exceeds session bound');
    const receivedAt=Date.now();
    return chartIndex(JSON.parse(text),session,cutoff,receivedAt);
  },
  async read(bucket,{session,instrument},a) {
    return {rows:[{source:candleSource,id:a.key,revision:1,instrument,session,origin:'recorded',
      eventAt:a.eventAt,availableAt:a.availableAt,evidence:endpoint+'#'+a.key,
      value:a.value,details:{receiptBasis:'first retained finalized chart response'}}]};
  }
};

export async function replayCandleSession(db,namespace,{session,ruleId,version},now) {
  const bounds=equitySession(session);
  if(!bounds || bounds.open>now)throw new Error('Invalid candle replay session');
  const saved=await db.prepare('SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?')
    .bind(namespace,ruleId,version).first();
  const rule=saved && JSON.parse(saved.payload);
  if(rule?.transition!=='per-candle-pair')throw new Error('Choose a saved QQQ candle study');
  const selection={session,instrument:'QQQ',origin:'recorded',cutoff:now};
  const records=await db.prepare("SELECT payload FROM one_clock_ingested WHERE source=? AND session=? AND instrument='QQQ' AND origin='recorded' AND received_at<=? ORDER BY event_at LIMIT 391")
    .bind(candleSource,session,now).all();
  if(records.results.length>390)throw new Error('Candle replay exceeds session bound');
  const bars=records.results.map(r=>JSON.parse(r.payload));
  const trials=candleTrials(bars,selection,bounds.close).map(({first,second,endpoint,...trial})=>({
    ...trial,decisionAt:second.eventAt,availableAt:Math.max(first.availableAt,second.availableAt),
    signedChange5m:trial.event && endpoint?(trial.direction==='up'?1:-1)*(endpoint.value.close-second.value.close):null}));
  const expected=Math.floor((Math.min(now,bounds.close)-bounds.open)/minute);
  const gaps=Math.max(0,expected-bars.length);
  const coverage={retainedVersions:bars.length,indexComplete:!gaps,pending:0,gaps,reason:gaps?'Missing finalized minutes':'Retained finalized candles'};
  const events=evaluateEvents({rule,observations:bars,selection,sessionClose:bounds.close});
  return {...selection,rule,coverage,complete:bars.length>0,trials,events:events.map(e=>({...e,collection:'replay'}))};
}
