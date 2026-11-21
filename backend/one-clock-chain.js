// Bounded market-only projection. No client-supplied object key, URL, account,
// capture, storage write, or fallback to a current snapshot during replay.
import { jsonResponse } from './common.js';
import { instant, observation } from '../site/shared/one-clock.js';
import { validSession, sessionAt, snapshotInstant } from '../site/shared/one-clock-session.js';

const PREFIX = { QQQ:'intraday/', SPY:'intraday/spy/', IBIT:'intraday/ibit/' };
const MAX_LISTS=2, MAX_CANDIDATES=12, MAX_BYTES=4*1024*1024, MAX_OBJECT=1024*1024;
const finite=v=>v!=='' && v!=null && Number.isFinite(Number(v)) ? Number(v) : null;
const stamp=v=>{ try { return instant(v); } catch { return null; } };
function csvLine(line) {
  const cells=[]; let value='',quoted=false;
  for(let i=0;i<line.length;i++) {
    const c=line[i];
    if(c==='"') { if(quoted && line[i+1]==='"') { value+='"'; i++; } else quoted=!quoted; }
    else if(c===',' && !quoted) { cells.push(value); value=''; } else value+=c;
  }
  if(quoted) throw new Error('invalid CSV');
  cells.push(value); return cells;
}
export function projectChainCsv(text,instrument,session) {
  const lines=text.split(/\r?\n/).filter(Boolean);
  if(lines.length<2 || lines.length>2001) throw new Error('chain row bound');
  const header=csvLine(lines[0]);
  const required=['Strike','Type','OpenInterest','Expiration','UnderlyingPrice'];
  if(required.some(name=>!header.includes(name))) throw new Error('missing chain fields');
  const numbers=['Strike','OpenInterest','Volume','Bid','Ask','UnderlyingPrice','DTE','Delta','Gamma','Theta','Vega','IV'];
  return lines.slice(1).map(line=>{
    const values=csvLine(line), raw=Object.fromEntries(header.map((name,i)=>[name,values[i]]));
    if(!['call','put'].includes(raw.Type) || !validSession(raw.Expiration)) throw new Error('invalid contract');
    if(raw.Symbol && raw.Symbol!==instrument) throw new Error('wrong underlying');
    const row=Object.fromEntries(numbers.map(name=>[name,finite(raw[name])]));
    if(!(row.Strike>0) || row.OpenInterest===null || row.OpenInterest<0) throw new Error('invalid strike or interest');
    return {...row,Type:raw.Type,Expiration:raw.Expiration,Symbol:instrument,
      OptionSymbol: typeof raw.OptionSymbol==='string' && new RegExp(`^${instrument}\\s*\\d{6}[CP]\\d{8}$`).test(raw.OptionSymbol) ? raw.OptionSymbol : null};
  });
}
function readingProjection(reading,cutoff) {
  if(reading?.schema_version!=='market_reading.v1' || reading.reading_id!=='reference_oi_skew' || reading.interpretation?.kind!=='reference' || reading.publication?.status!=='published') return null;
  const published=stamp(reading.published_at);
  if(published===null || published>cutoff) return null;
  if([reading.observed_at,reading.calculated_at].some(value=>value!=null && (stamp(value)===null || stamp(value)>cutoff))) return null;
  const short=value=>typeof value==='string'?value.slice(0,200):null;
  return {schema_version:reading.schema_version,reading_id:reading.reading_id,symbol:short(reading.symbol),
    interpretation:{kind:'reference'},published_at:published,observed_at:stamp(reading.observed_at),
    calculated_at:stamp(reading.calculated_at),publication:{status:'published'},
    calculation:{version:short(reading.calculation?.version),parameters:{near_money_pct:finite(reading.calculation?.parameters?.near_money_pct)}},
    status:{code:short(reading.status?.code),reason:short(reading.status?.reason)},
    freshness:{status:short(reading.freshness?.status)},
    values:Object.fromEntries(['imbalance_ratio','near_money_call_oi','near_money_put_oi','band_strikes'].map(k=>[k,finite(reading.values?.[k])]))};
}
export async function readArchivedChain(bucket,{session,instrument,cutoff}) {
  if(!validSession(session) || !Object.hasOwn(PREFIX,instrument)) throw new Error('invalid archive scope');
  cutoff=instant(cutoff);
  if(sessionAt(cutoff)!==session) throw new Error('cutoff outside session date');
  const prefix=PREFIX[instrument]+session.replaceAll('-','')+'/snapshot_';
  const candidates=[]; let cursor,lists=0,bytes=0,gets=0,complete=false;
  do {
    const page=await bucket.list({prefix,limit:1000,...(cursor?{cursor}:{})}); lists++;
    for(const object of page.objects) {
      if(!object.key.startsWith(prefix) || !/^snapshot_\d{12}\.provenance\.json$/.test(object.key.slice(prefix.lastIndexOf('/')+1))) continue;
      const eventAt=snapshotInstant(object.key,session);
      if(eventAt!==null && eventAt<=cutoff) candidates.push({key:object.key,eventAt});
    }
    cursor=page.truncated?page.cursor:null; complete=!page.truncated;
  } while(cursor && lists<MAX_LISTS);
  const coverage={lists,gets:0,bytes:0,indexComplete:complete,candidates:candidates.length,examined:0,
    availabilityBasis:'archived-object-revision',digestVerification:'not-verified',missingSidecars:'not-inferred'};
  if(!complete) return {rows:[],coverage:{...coverage,reason:'archive-index-bound'}};
  candidates.sort((a,b)=>b.eventAt-a.eventAt || b.key.localeCompare(a.key));
  async function read(key) {
    const object=await bucket.get(key); gets++;
    if(!object) return null;
    if(!Number.isSafeInteger(object.size) || object.size>MAX_OBJECT || bytes+object.size>MAX_BYTES) throw new Error('archive byte bound');
    const text=await object.text(); bytes+=new TextEncoder().encode(text).byteLength;
    if(bytes>MAX_BYTES || new TextEncoder().encode(text).byteLength>MAX_OBJECT) throw new Error('archive byte bound');
    return {object,text};
  }
  for(const candidate of candidates.slice(0,MAX_CANDIDATES)) {
    coverage.examined++;
    const meta=await read(candidate.key); if(!meta) continue;
    const p=JSON.parse(meta.text), csvKey=candidate.key.replace(/\.provenance\.json$/,'.csv');
    if(p.schema_version!=='snapshot_provenance.v1' || p.symbol!==instrument || p.snapshot_key!==csvKey || p.publication?.status!=='published') continue;
    const eventAt=stamp(p.constructed_at), published=stamp(p.latest_published_at), metaAt=stamp(meta.object.uploaded?.toISOString());
    if(eventAt===null || published===null || metaAt===null || sessionAt(eventAt)!==session || published<eventAt || eventAt>cutoff || Math.max(published,metaAt)>cutoff) continue;
    const csv=await read(csvKey); if(!csv) continue;
    const csvAt=stamp(csv.object.uploaded?.toISOString());
    if(csvAt===null || csvAt>cutoff || !csv.object.etag || !meta.object.etag) continue;
    const availableAt=Math.max(eventAt,published,metaAt,csvAt);
    const chain=projectChainCsv(csv.text,instrument,session);
    const rows=chain.filter(row=>row.Expiration===session);
    if(!rows.length) continue;
    const payload={symbol:instrument,date:session,expiration:session,timestamp:new Date(eventAt).toISOString(),
      snapshot_time:new Date(eventAt).toISOString(),snapshot_key:csvKey,
      underlying_price:rows.find(r=>r.UnderlyingPrice>0)?.UnderlyingPrice??null,rows,
      market_readings:(Array.isArray(p.market_readings)?p.market_readings:[]).map(r=>readingProjection(r,cutoff)).filter(Boolean)};
    const row=observation({source:'chain',id:`${csvKey}@${csv.object.etag}:${meta.object.etag}`,revision:1,
      session,instrument,origin:'recorded',eventAt,availableAt,evidence:csvKey,value:payload,
      details:{sidecar:candidate.key,publicationAt:published,archiveRevisionAt:Math.max(metaAt,csvAt),
        observationBasis:'snapshot-construction',availabilityBasis:'archived-object-revision',
        digestVerification:'not-verified',sourceDigest:p.market_readings_digest??null}});
    return {rows:[row],coverage:{...coverage,gets,bytes,reason:null}};
  }
  return {rows:[],coverage:{...coverage,gets,bytes,reason:candidates.length>MAX_CANDIDATES?'candidate-bound':'no-qualified-snapshot'}};
}
export async function handleOneClockChain(request,env,url) {
  if(request.method!=='GET') return jsonResponse({error:'method not allowed'},405);
  if([...url.searchParams.keys()].some(k=>!['session','instrument','cutoff'].includes(k)) || [...url.searchParams.keys()].some(k=>url.searchParams.getAll(k).length!==1)) return jsonResponse({error:'invalid query'},400);
  const selection={session:url.searchParams.get('session'),instrument:url.searchParams.get('instrument'),cutoff:Number(url.searchParams.get('cutoff'))};
  try {
    if(selection.cutoff>Date.now()+60000) throw new Error('future cutoff');
    // Validate before accessing any storage. Only fixed market prefixes allowed.
    if(!validSession(selection.session) || !Object.hasOwn(PREFIX,selection.instrument) || sessionAt(instant(selection.cutoff))!==selection.session) throw new Error('invalid scope');
  } catch { return jsonResponse({error:'invalid archive scope'},400); }
  try { return jsonResponse(await readArchivedChain(env.PAPER_TRADES,selection),200); }
  catch { return jsonResponse({error:'Archive unavailable or exceeded bounded read budget'},503); }
}
