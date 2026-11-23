import rangesLib from '../site/shared/csv-ranges.js';
import heatmap from '../site/shared/heatmap.js';
import {equitySession,observerSessions} from './one-clock-calendar.js';
import {validSession} from '../site/shared/one-clock-session.js';
import {jsonResponse} from './common.js';
const calibrationKey='derived/OIranges.csv';
const quantiles=['p25','p50','p75','p90'];
// Fail closed on absent cells. The display has fallback colors for an
// uncalibrated chart, which cannot establish a percentile for research.
export function calibratedLevels(chain, csv, fullLadder=false) {
  const {rows,tier,underlying_price:spot}=chain;
  if(!Number.isFinite(spot) || !tier) return null;
  const ranges=rangesLib.parseRangesCsv(csv);
  const raw=new Map(rangesLib.parseCsv(csv).map(row=>[row.Tier+'/'+row.StrikeOffset,row]));
  const byOption=new Map(rows.map(row=>[row.Strike+'/'+row.Type,row]));
  const levels=[];
  let gaps=0;
  for(const {strike,offset} of heatmap.computeAtmWindow(rows,spot,fullLadder?rows.length:heatmap.DISPLAY).window) {
    const result={strike};
    for(const [side,prefix] of [['call','Call'],['put','Put']]) {
      const contract=byOption.get(strike+'/'+side), regular=raw.get('0DTE_Regular/'+offset);
      const adjustment=raw.get(tier+'/'+offset)?.[prefix+'_adj'];
      if(!Number.isSafeInteger(contract?.OpenInterest) || contract.OpenInterest<0 || !regular || quantiles.some(q=>!regular[prefix+'_'+q]?.trim()) ||
          (tier!=='0DTE_Regular' && !(Number(adjustment)>0))) continue;
      const thresholds=rangesLib.getThresh(ranges,tier,offset,side);
      if(thresholds.some((t,i)=>!Number.isFinite(t)||t<0||(i>0 && t<thresholds[i-1])) || thresholds[3]<=0) continue;
      result[side]=rangesLib.oiBucket(contract.OpenInterest,thresholds);
      if(fullLadder)result[side+"OI"]=contract.OpenInterest;
    }
    if(result.call!==undefined && result.put!==undefined) levels.push(result);
    else {gaps++;if(fullLadder && (result.call!==undefined||result.put!==undefined))levels.push(result);}
  }
  return {levels,gaps};
}
export async function openingIndex(bucket,session,cutoff) {
  const prefix='intraday/'+session.replaceAll('-','')+'/opening-oi/v1/';
  const page=await bucket.list({prefix,limit:404});
  if(page.truncated || page.objects.length>402)return {reason:'opening-index-bound',items:[]};
  const items=[];
  for(const [i,obj] of page.objects.entries()) {
    if(obj.key!==prefix+String(i+1).padStart(4,'0')+'.json' || !obj.etag || !Number.isSafeInteger(obj.uploaded?.getTime()))
      return {reason:'invalid-opening-index',items:[]};
    const at=obj.uploaded.getTime();
    if(at>cutoff)break;
    items.push({key:obj.key,version:obj.etag,availableAt:at,openingVersion:i+1});
  }
  return {items,reason:items.length?null:'opening-evidence-unavailable'};
}

export async function readOpeningOi(bucket, session, cutoff, item=null) {
  const bounds=equitySession(session);
  if(!bounds || cutoff<bounds.open)return {reason:'before-regular-session-open'};
  if(!item) {
    const index=await openingIndex(bucket,session,cutoff);
    if(!index.items.length)return {reason:index.reason};
    item=index.items.at(-1);
  }
  const key=item.key,obj=await bucket.get(key);
  if(!obj)return {reason:'opening-evidence-unavailable'};
  const availableAt=obj.uploaded?.getTime();
  if(!Number.isSafeInteger(availableAt) || availableAt>cutoff)return {reason:'opening-evidence-not-yet-available'};
  const invalid={reason:'invalid-opening-evidence'};
  if(obj.etag!==item.version || availableAt!==item.availableAt || !(obj.size>0 && obj.size<=512*1024))return invalid;
  const p=JSON.parse(await obj.text()), capturedAt=Date.parse(p.capturedAt),cal=p.calibration;
  if(p.schema!=='opening-oi.v1' || p.version!==item.openingVersion || p.session!==session || p.instrument!=='QQQ' ||
    p.settledSession!==observerSessions(bounds.open)[1] || Date.parse(p.openAt)!==bounds.open ||
    !Number.isSafeInteger(capturedAt)||capturedAt<bounds.open || capturedAt>=bounds.close || capturedAt>availableAt ||
    !['0DTE_Regular','0DTE_Weekly','0DTE_Monthly'].includes(p.tier) || !(Number.isFinite(p.openingSpot)&&p.openingSpot>0) ||
    !Array.isArray(p.legs) || !p.legs.length || p.legs.length>201 || !cal || typeof cal.csv!=='string' || cal.csv.length>131072 ||
    typeof cal.etag!=='string' || !cal.etag || !(Date.parse(cal.uploaded)<=bounds.open))return invalid;
  const strikes=new Set(),rows=[];
  for(const leg of p.legs) {
    if(!(Number.isFinite(leg?.strike)&&leg.strike>0) || strikes.has(leg.strike))return invalid;
    strikes.add(leg.strike);
    for(const side of ['call','put']){
      const oi=leg[side];
      if(oi!==null && !(Number.isSafeInteger(oi)&&oi>=0))return invalid;
      rows.push({Strike:leg.strike,Type:side,OpenInterest:oi});
    }
  }
  const map=calibratedLevels({rows,underlying_price:p.openingSpot,tier:p.tier},cal.csv,true);
  if(!map?.levels.length)return {reason:'no-qualified-opening-calibration'};
  return {...map,session,availableAt,capturedAt,complete:p.legs.every(l=>l.call!==null && l.put!==null),etag:obj.etag,calibration:{key:calibrationKey,etag:cal.etag,
    uploaded:Date.parse(cal.uploaded),tier:p.tier,openingSpot:p.openingSpot,openingAt:bounds.open,
    openingAtm:heatmap.computeAtmWindow(rows,p.openingSpot).atm,settledSession:p.settledSession,openingEvidence:key}};
}
export async function handleOpeningOi(request,env,url) {
  if(request.method!=='GET')return jsonResponse({error:'method not allowed'},405);
  const s=url.searchParams,session=s.get('session'),cutoff=Number(s.get('cutoff'));
  if(s.size!==2 || !validSession(session) || !Number.isSafeInteger(cutoff) || cutoff<1e12 || cutoff>Date.now()+60000)
    return jsonResponse({error:'Invalid opening reference scope'},400);
  try{return jsonResponse(await readOpeningOi(env.PAPER_TRADES,session,cutoff));}
  catch{return jsonResponse({reason:'opening-evidence-unavailable'},503);}
}
