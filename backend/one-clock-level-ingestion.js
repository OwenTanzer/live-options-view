import rangesLib from '../site/shared/csv-ranges.js';
import heatmap from '../site/shared/heatmap.js';
import {verifiedNativeChain} from './one-clock-chain.js';
import {observation} from '../site/shared/one-clock.js';

const calibrationKey='derived/OIranges.csv';
const quantiles=['p25','p50','p75','p90'];
// Fail closed on absent cells. The display has fallback colors for an
// uncalibrated chart, which cannot establish a percentile for research.
export function calibratedLevels(chain, csv) {
  const {rows,tier,underlying_price:spot}=chain;
  if(!Number.isFinite(spot) || !tier) return null;
  const ranges=rangesLib.parseRangesCsv(csv);
  const raw=new Map(rangesLib.parseCsv(csv).map(row=>[row.Tier+'/'+row.StrikeOffset,row]));
  const byOption=new Map(rows.map(row=>[row.Strike+'/'+row.Type,row]));
  const levels=[];
  let gaps=0;
  for(const {strike,offset} of heatmap.computeAtmWindow(rows,spot).window) {
    const result={strike};
    for(const [side,prefix] of [['call','Call'],['put','Put']]) {
      const contract=byOption.get(strike+'/'+side), regular=raw.get('0DTE_Regular/'+offset);
      const adjustment=raw.get(tier+'/'+offset)?.[prefix+'_adj'];
      if(!contract || !regular || quantiles.some(q=>!regular[prefix+'_'+q]?.trim()) ||
          (tier!=='0DTE_Regular' && !(Number(adjustment)>0))) break;
      const thresholds=rangesLib.getThresh(ranges,tier,offset,side);
      if(thresholds.some((t,i)=>!Number.isFinite(t)||t<0||(i>0 && t<thresholds[i-1])) || thresholds[3]<=0) break;
      result[side]=rangesLib.oiBucket(contract.OpenInterest,thresholds);
    }
    if(result.call!==undefined && result.put!==undefined) levels.push(result);
    else gaps++;
  }
  return {levels,gaps};
}
export const levelIngestion={
  id:'calibrated-oi-levels',version:'1',owner:'market_data.live_market',demandDriven:true,reuseObjects:true,reportGapReasons:true,
  instruments:['QQQ'],outputs:['oi-levels','level-spot'],fields:{'level-spot':{spot:'Qualified underlying observation'},'oi-levels':{qualified:'Calibrated QQQ strike levels'}},
  async discover(bucket,{instrument,session,cutoff},db) {
    const stream='collector-chain/'+instrument;
    const status=await db.prepare('SELECT coverage FROM one_clock_publication_status INDEXED BY one_clock_publication_status_cutoff WHERE stream=? AND checked_at<=? ORDER BY checked_at DESC,generation DESC LIMIT 1').bind(stream,cutoff).first();
    const progress=status && JSON.parse(status.coverage);
    if(progress?.reason!=='tail-at-check')return {artifacts:[],complete:false,reason:'native-price-index-incomplete'};
    const indexed=(await db.prepare(`SELECT payload FROM one_clock_publication_records INDEXED BY one_clock_native_chain_replay
      WHERE stream=? AND session=? AND source='price' AND event_at<=? AND available_at<=? AND received_at<=? AND sequence<?
      ORDER BY event_at,available_at,record_id,revision LIMIT 1001`).bind(stream,session,cutoff,cutoff,cutoff,progress.nextSequence).all()).results;
    if(indexed.length>1000)return {artifacts:[],complete:false,reason:'native-session-index-bound'};
    if(!indexed.length)return {artifacts:[],complete:false,reason:'no-native-chain-publications'};
    const artifacts=indexed.map(item=>{
      const price=JSON.parse(item.payload);
      return {key:JSON.stringify([price.id,price.revision]),version:String(price.details.sequence),eventAt:price.eventAt,price};
    });
    return {artifacts,complete:true};
  },
  async read(bucket,selection,{price}) {
    const base={...price,source:'oi-levels',value:null};
    let spotRow=observation({...price,source:'level-spot',value:null});
    const gap=reason=>({rows:[spotRow,observation({...base,details:{...price.details,coverageReason:reason}})],gap:true,reason});
    if(!Number.isFinite(price.value?.spot))return gap('spot-unavailable');
    const result=await verifiedNativeChain(bucket,selection,price);
    const chain=result.rows[0]?.value;
    if(chain?.spotFreshness!=='live' || !Number.isSafeInteger(chain.spotAt) || chain.spotAt>price.eventAt ||
      price.eventAt-chain.spotAt>150000)return gap('spot-observation-time-unqualified');
    spotRow=observation({...price,source:'level-spot',valueAt:chain.spotAt});
    const calibration=await bucket.get(calibrationKey);
    const uploaded=calibration?.uploaded?.getTime();
    if(!calibration?.etag || !Number.isSafeInteger(uploaded) || uploaded>price.availableAt)return gap('as-of-calibration-unavailable');
    const projection=result.rows[0] && calibratedLevels(result.rows[0].value,await calibration.text());
    if(!projection?.levels.length)return gap('no-qualified-side-calibration');
    return {rows:[spotRow,observation({...base,value:{qualified:true,levels:projection.levels,
      calibration:{key:calibrationKey,etag:calibration.etag,uploaded,tier:result.rows[0].value.tier}}})],
      gap:projection.gaps>0,reason:projection.gaps?'incomplete-strike-calibration':null};
  }
};
