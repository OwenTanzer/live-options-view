// Private recorded-as-of projection. Authentication remains in every caller.
// Payload revision selection precedes projection, response and context sampling.
import { instant } from '../site/shared/one-clock.js';
import { sessionAt,sessionInstant } from '../site/shared/one-clock-session.js';
import { journalBookState } from './journal-book.js';
const round=(n,d)=>Math.round(n*10**d)/10**d;
export function journalCutoff(url,now=Date.now()) {
  if(!url.searchParams.has('cutoff'))return null;
  const values=url.searchParams.getAll('cutoff');
  if(values.length!==1 || !/^\d+$/.test(values[0]))throw new Error('cutoff must be epoch milliseconds');
  const cutoff=instant(Number(values[0]));
  if(cutoff>now+60000)throw new Error('cutoff is in the future');
  return cutoff;
}
const keep=(value,fields)=>Object.fromEntries(fields.filter(k=>value[k]!==undefined).map(k=>[k,value[k]]));
export function projectJournalPosition(position,cutoff) {
  const base=keep(position,['position_id','journal_page_id','journal_url','actor_kind','owner','account','mode','strategy_id']);
  if(!position.supported)return {...base,supported:false,executions:[],gaps:[],
    ...keep(position,['trade_date','contract_text','unsupported_reason'])};
  if(position.legs) {
    const legs=position.legs.map(p=>projectJournalPosition(p,cutoff)).filter(Boolean);
    if(!legs.length)return null;
    const executions=legs.flatMap(p=>p.executions.map(e=>({...e,execution_id:e.execution_id+':'+p.contract.occ_symbol}))).sort((a,b)=>a.ts.localeCompare(b.ts)||a.execution_id.localeCompare(b.execution_id));
    const closed=legs.every(p=>p.closed),sum=field=>round(legs.reduce((n,p)=>n+(p[field]??0),0),6);
    const closeDay=closed?legs.map(p=>p.expired_on??sessionAt(Date.parse(p.closed_ts))).sort().at(-1):null;
    const expired=closed&&legs.some(p=>p.expired_on===closeDay);
    return {...base,supported:true,legs,contract:legs[0].contract,executions,n_fills:executions.length,
      opened_ts:executions[0].ts,closed_ts:closed&&!expired?executions.at(-1).ts:null,closed,
      ...(expired?{expired_on:closeDay}:{}),
      gross_pnl:closed?sum('gross_pnl'):null,net_pnl:closed?sum('net_pnl'):null,
      fees:sum('fees'),realized_gross_pnl:sum('realized_gross_pnl'),open_cost_basis:sum('open_cost_basis'),
      open_qty:legs.reduce((n,p)=>n+Math.abs(p.open_qty),0),open_side:closed?null:'across legs',
      fee_basis:[...new Set(legs.flatMap(p=>p.fee_basis))],gaps:[],row_checks:{flat:closed}};
  }
  const executions=position.executions.filter(e=>Number.isFinite(Date.parse(e.ts)) && Date.parse(e.ts)<=cutoff)
    .map(e=>keep(e,['execution_id','instrument','side','qty','price','fee','fee_basis','account','owner','ts','ts_kind','order_ref']))
    .sort((a,b)=>a.ts.localeCompare(b.ts)||a.execution_id.localeCompare(b.execution_id));
  if(!executions.length)return null;
  let {pos,avg,realized,maxQty,m}=journalBookState(executions);
  // No timestamp was retained for a worthless-expiration annotation. Only
  // apply it after that whole NY date has elapsed and the payload is known.
  const expired=position.expired_on && position.expired_on<sessionAt(cutoff);
  if(expired){realized-=pos*avg*m;pos=0;}
  const closed=pos===0,fees=round(executions.reduce((n,e)=>n+e.fee,0),6);
  return {...base,supported:true,contract:executions[0].instrument,executions,
    opened_ts:executions[0].ts,closed_ts:closed&&!expired?executions.at(-1).ts:null,
    ...(expired?{expired_on:position.expired_on}:{}),closed,max_qty:maxQty,n_fills:executions.length,
    gross_pnl:closed?round(realized,2):null,net_pnl:closed?round(realized-fees,6):null,
    realized_gross_pnl:round(realized,2),fees,fee_basis:[...new Set(executions.map(e=>e.fee_basis))].sort(),
    open_qty:pos,open_side:pos>0?'long':pos<0?'short':null,
    open_cost_basis:closed?0:round(avg*Math.abs(pos)*m,2),row_checks:{flat:closed},gaps:[]};
}
export async function journalSnapshots(db,{cutoff,positionId=null,pageId=null}) {
  const filter=positionId?' AND position_id=?':pageId?' AND page_id=?':'';
  const args=[cutoff,...(positionId?[positionId]:pageId?[pageId]:[])];
  const {results}=await db.prepare(`SELECT * FROM (
    SELECT *,ROW_NUMBER() OVER(PARTITION BY page_id ORDER BY recorded_at DESC,snapshot_id DESC) rank
    FROM trade_journal_history WHERE recorded_at<=?${filter}) WHERE rank=1 LIMIT 1001`).bind(...args).all();
  if(results.length>1000)throw new Error('journal snapshot bound exceeded');
  return results.flatMap(row=>{
    const position=projectJournalPosition(JSON.parse(row.position_json),cutoff);
    if(!position)return [];
    return [{...position,source_status:row.source_status,revision:row.revision,recording_revision:row.snapshot_id,revisions:[],
      app_read_at:new Date(row.recorded_at).toISOString(),available_at:row.recorded_at,
      evidence:'private',origin:'recorded',status_changed_at:null}];
  });
}
export function journalScope(day,cutoff) {
  if(cutoff!==null && sessionAt(cutoff)<day)throw new Error('cutoff cannot precede the selected journal date');
  // Also validates the calendar date, including in retrospective mode.
  sessionInstant(day,0);
}
