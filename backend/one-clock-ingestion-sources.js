import {headlineIngestion,headlineQuoteIngestion} from './one-clock-headline-ingestion.js';
import {candleIngestion} from './one-clock-candle-ingestion.js';
import {levelIngestion} from "./one-clock-level-ingestion.js";
import { shortlistIngestion } from './one-clock-shortlist-ingestion.js';
import { readArchivedChain } from './one-clock-chain.js';
import { observation } from '../site/shared/one-clock.js';
import { snapshotInstant } from '../site/shared/one-clock-session.js';
const prefixes={QQQ:'intraday/',SPY:'intraday/spy/',IBIT:'intraday/ibit/'};
export const chainIngestion=Object.freeze({id:'archived-chain',version:'1',owner:'market_data.live_market',
  revisionPolicy:'retained-artifact',
  instruments:Object.keys(prefixes),outputs:['price','skew'],
  fields:{price:{spot:'Underlying price'},skew:{imbalance:'Published reference imbalance',strikes:'Reference band strikes',callOI:'Reference call open interest',putOI:'Reference put open interest'}},
  async discover(bucket,{session,instrument,cutoff}) {
    const prefix=prefixes[instrument]+session.replaceAll('-','')+'/snapshot_';
    const objects=new Map();let cursor,complete=false;
    for(let n=0;n<2;n++) {
      const page=await bucket.list({prefix,limit:1000,...(cursor?{cursor}:{})});
      for(const item of page.objects)if(item.key.startsWith(prefix))objects.set(item.key,item);
      complete=!page.truncated;cursor=page.cursor;if(complete)break;
    }
    const artifacts=[];
    for(const [key,meta] of objects) {
      if(!/\/snapshot_\d{12}\.provenance\.json$/.test(key))continue;
      const eventAt=snapshotInstant(key,session);if(eventAt===null||eventAt>cutoff)continue;
      const csv=objects.get(key.replace(/\.provenance\.json$/,'.csv'));
      // Both ETags matter: a correction can replace either half independently.
      const metaAt=meta.uploaded?.getTime(),csvAt=csv?.uploaded?.getTime()??null;
      if(!meta.etag||!Number.isSafeInteger(metaAt)||csv&&(!csv.etag||!Number.isSafeInteger(csvAt)))throw new Error('archive revision metadata unavailable');
      artifacts.push({key,eventAt,version:JSON.stringify([meta.etag,metaAt,csv?.etag??null,csvAt]),metaEtag:meta.etag,csvEtag:csv?.etag??null,metaAt,csvAt});
    }
    return {artifacts,complete};
  },
  async read(bucket,selection,artifact) {
    let changed=false,notYet=false,transportError=null;
    const scoped={list:async()=>({objects:[{key:artifact.key}],truncated:false}),async get(key) {
      let obj;
      try {obj=await bucket.get(key);}catch(error){transportError=error;throw error;}
      const expected=key===artifact.key?artifact.metaEtag:artifact.csvEtag;
      if((obj?.etag??null)!==expected)changed=true;
      if((obj?.uploaded?.getTime()??null)!==(key===artifact.key?artifact.metaAt:artifact.csvAt))changed=true;
      if(obj?.uploaded?.getTime()>selection.cutoff)notYet=true;
      if(obj&&key===artifact.key) {
        const meta=JSON.parse(await obj.text());
        if(Date.parse(meta.latest_published_at)>selection.cutoff||meta.market_readings?.some(r=>Date.parse(r.published_at)>selection.cutoff))notYet=true;
      }
      return obj;
    }};
    let result;
    try {result=await readArchivedChain(scoped,selection);}
    catch(error) {if(transportError)throw transportError;return {rows:[],reason:'invalid-archive-evidence'};}
    if(changed||notYet)return {rows:[],retry:true};
    const parent=result.rows[0];
    if(!parent)return {rows:[],reason:result.coverage.reason};
    const base={...parent,id:parent.value.snapshot_key,details:{...parent.details,sourceOwner:'market_data.live_market'}};
    const rows=[observation({...base,source:'price',value:{spot:parent.value.underlying_price}})];
    const reading=parent.value.market_readings.find(r=>r.reading_id==='reference_oi_skew');
    rows.push(observation({...base,source:'skew',
      availableAt:reading?Math.max(parent.availableAt,Date.parse(reading.published_at)):parent.availableAt,
      value:reading?.status?.code==='ok'?{imbalance:reading.values.imbalance_ratio,strikes:reading.values.band_strikes,
        callOI:reading.values.near_money_call_oi,putOI:reading.values.near_money_put_oi}:null}));
    return {rows};
  }
});
// A stream joins once here; all accounts share the same public ingestion pass.
export const publicIngestionSources=Object.freeze([chainIngestion,shortlistIngestion,levelIngestion,candleIngestion,headlineIngestion,headlineQuoteIngestion]);

export function levelEvaluationStatus(attempt,rule,coverage) {
  if(rule.transition==='per-candle-pair' && attempt.state==='evaluated') {
    const c=coverage.find(row=>row.source===candleIngestion.id && row.session===attempt.session && row.instrument===attempt.instrument);
    if(!c || !c.indexComplete || c.pending || c.gaps || c.processed && c.updatedAt>attempt.attempted_at)
      return {...attempt,state:'pending',reason:'QQQ minute coverage is incomplete or newly received bars await evaluation.'};
  }
  if(attempt.state!=='evaluated' || ![...rule.conditions,...rule.outcomes].some(input=>levelIngestion.outputs.includes(input.source)))return attempt;
  const receipt=coverage.find(c=>c.source===levelIngestion.id && c.session===attempt.session && c.instrument===attempt.instrument);
  if(receipt?.gaps>0)return {...attempt,state:'pending',reason:'Calibrated source evidence has recorded gaps. Some occurrences or outcomes may be unknown.'};
  if(receipt?.indexComplete && receipt.pending===0 && receipt.updatedAt<=attempt.attempted_at)return attempt;
  return {...attempt,state:'pending',reason:'Calibrated source coverage awaits complete rule evaluation.'};
}
