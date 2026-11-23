// Bounded market-only projection. No client-supplied object key, URL, account,
// capture, storage write, or fallback to a current snapshot during replay.
import { publicationDigest } from './one-clock-publication-contract.js';
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
    let optionSymbol=null;
    if(raw.OptionSymbol) {
      const compact=raw.OptionSymbol.replaceAll(' ','');
      const occ=/^([A-Z]+)(\d{6})([CP])(\d{8})$/.exec(compact);
      const expiry=raw.Expiration.slice(2).replaceAll('-','');
      if(!occ || occ[1]!==instrument || occ[2]!==expiry || occ[3]!==({call:'C',put:'P'})[raw.Type] || Math.abs(Number(occ[4])/1000-row.Strike)>1e-9) throw new Error('contract identity mismatch');
      optionSymbol=raw.OptionSymbol;
    }
    return {...row,Type:raw.Type,Expiration:raw.Expiration,Symbol:instrument,
      OptionSymbol:optionSymbol};
  });
}
function readingProjection(reading,{cutoff,instrument,csvKey,eventAt}) {
  if(reading?.schema_version!=='market_reading.v1' || reading.reading_id!=='reference_oi_skew' || reading.interpretation?.kind!=='reference' || reading.publication?.status!=='published') return null;
  const published=stamp(reading.published_at);
  if(published===null || published>cutoff) return null;
  if(reading.symbol!==instrument || reading.input?.snapshot_key!==csvKey || reading.input?.snapshot_key_status!=='valid' || stamp(reading.input?.snapshot_timestamp)!==eventAt) return null;
  if([reading.observed_at,reading.calculated_at].some(value=>value!=null && (stamp(value)===null || stamp(value)>cutoff))) return null;
  const short=value=>typeof value==='string'?value.slice(0,200):null;
  const iso=value=>stamp(value)===null?null:new Date(stamp(value)).toISOString();
  return {schema_version:reading.schema_version,reading_id:reading.reading_id,symbol:short(reading.symbol),
    interpretation:{kind:'reference'},published_at:iso(published),observed_at:iso(reading.observed_at),
    calculated_at:iso(reading.calculated_at),publication:{status:'published'},
    input:{snapshot_key:csvKey,snapshot_key_status:'valid',snapshot_timestamp:iso(eventAt)},
    calculation:{version:short(reading.calculation?.version),parameters:{near_money_pct:finite(reading.calculation?.parameters?.near_money_pct)}},
    status:{code:short(reading.status?.code),reason:short(reading.status?.reason)},
    freshness:{status:short(reading.freshness?.status)},
    values:Object.fromEntries(['imbalance_ratio','near_money_call_oi','near_money_put_oi','band_strikes'].map(k=>[k,finite(reading.values?.[k])]))};
}
export async function readArchivedChain(bucket,{session,instrument,cutoff}) {
  if(!validSession(session) || !Object.hasOwn(PREFIX,instrument)) throw new Error('invalid archive scope');
  cutoff=instant(cutoff);
  // The archive partition remains the event day. A private journal can ask
  // what was known about that day at a later cutoff (including late receipts).
  if(sessionAt(cutoff)<session) throw new Error('cutoff precedes session date');
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
    const rows=projectChainCsv(csv.text,instrument,session);
    const expirations=[...new Set(rows.map(row=>row.Expiration))];
    if(expirations.length!==1 || expirations[0]<session || (instrument!=='IBIT' && expirations[0]!==session)) continue;
    const payload={symbol:instrument,date:session,expiration:expirations[0],timestamp:new Date(eventAt).toISOString(),
      snapshot_time:new Date(eventAt).toISOString(),snapshot_key:csvKey,
      underlying_price:rows.find(r=>r.UnderlyingPrice>0)?.UnderlyingPrice??null,rows,
      market_readings:(Array.isArray(p.market_readings)?p.market_readings:[]).map(r=>readingProjection(r,{cutoff,instrument,csvKey,eventAt})).filter(Boolean)};
    const row=observation({source:'chain',id:`${csvKey}@${csv.object.etag}:${meta.object.etag}`,revision:1,
      session,instrument,origin:'recorded',eventAt,availableAt,evidence:csvKey,value:payload,
      details:{sidecar:candidate.key,publicationAt:published,archiveRevisionAt:Math.max(metaAt,csvAt),
        observationBasis:'snapshot-construction',availabilityBasis:'archived-object-revision',
        digestVerification:'not-verified',sourceDigest:typeof p.market_readings_digest==='string' && /^[0-9a-f]{64}$/.test(p.market_readings_digest)?p.market_readings_digest:null}});
    return {rows:[row],coverage:{...coverage,gets,bytes,reason:null}};
  }
  return {rows:[],coverage:{...coverage,gets,bytes,reason:candidates.length>MAX_CANDIDATES?'candidate-bound':'no-qualified-snapshot'}};
}
// The observer retains the verified commit after publication. Replaying what
// the market source exposed uses the commit's availability, even if the
// observer recorded it later. Results itself keeps enforcing its receipt time.
export async function publicChainAt(env,selection) {
  if(env.ONE_CLOCK_PUBLICATIONS!=='enabled') return readArchivedChain(env.PAPER_TRADES,selection);
  const {session,instrument,cutoff}=selection,stream='collector-chain/'+instrument;
  const candidates=(await env.DB.prepare(`SELECT payload,available_at,record_id,revision
    FROM one_clock_publication_records INDEXED BY one_clock_native_chain_replay
    WHERE stream=? AND session=? AND source='price' AND event_at<=?
    ORDER BY event_at DESC,available_at DESC,record_id DESC,revision DESC LIMIT ?`)
    .bind(stream,session,cutoff,MAX_CANDIDATES+1).all()).results;
  let selected;
  for(const candidate of candidates.slice(0,MAX_CANDIDATES)) {
    if(candidate.available_at>cutoff) continue;
    const newer=(await env.DB.prepare(`SELECT available_at FROM one_clock_publication_records
      WHERE stream=? AND source='price' AND record_id=? AND revision>?
      ORDER BY revision LIMIT ?`).bind(stream,candidate.record_id,candidate.revision,MAX_CANDIDATES+1).all()).results;
    if(newer.some(r=>r.available_at<=cutoff)) continue;
    if(newer.length>MAX_CANDIDATES) return {rows:[],coverage:{reason:'native-chain-revision-bound'}};
    selected=JSON.parse(candidate.payload);
    break;
  }
  if(!selected) {
    if(candidates.length>MAX_CANDIDATES) return {rows:[],coverage:{reason:'native-chain-candidate-bound'}};
    return readArchivedChain(env.PAPER_TRADES,selection);
  }
  // A qualified legacy snapshot can precede the next observer sweep. Allow
  // that newer market evidence without letting a damaged sidecar mask an
  // already retained, independently verifiable native publication.
  let legacy;
  try { legacy=await readArchivedChain(env.PAPER_TRADES,selection); }
  catch { legacy={rows:[],coverage:{reason:'legacy-chain-unavailable'}}; }
  if(legacy.rows[0]?.eventAt>selected.eventAt) return legacy;
  const coverage={reason:null,availabilityBasis:'immutable-publication',digestVerification:'verified',legacyCoverage:legacy.coverage};
  return verifiedNativeChain(env.PAPER_TRADES, selection, selected, coverage);
}
export async function verifiedNativeChain(bucket,{session,instrument,cutoff},selected,coverage={reason:null,availabilityBasis:'immutable-publication',digestVerification:'verified'}) {
  const stream='collector-chain/'+instrument;
  const refs=selected.details.evidence;
  const spot=selected.value?.spot??null;
  if(selected.value===null && !refs.some(ref=>['collector.snapshot.csv.v1','collector.snapshot.payload.v1'].includes(ref.schema)))
    return {rows:[],coverage:{...coverage,reason:'native-snapshot-withdrawn',digestVerification:'not-verified'}};

  async function body(schema,contentType) {
    const matches=refs.filter(ref=>ref.schema===schema && ref.contentType===contentType);
    if(matches.length!==1) throw new Error('missing native chain evidence');
    const [ref]=matches;
    if(!/^[a-f0-9]{64}$/.test(ref.sha256) || ref.key!=='publications/v1/'+stream+'/blobs/'+ref.sha256 ||
       ref.bytes<1 || ref.bytes>MAX_OBJECT) throw new Error('invalid native chain evidence');
    const object=await bucket.get(ref.key);
    if(!object || object.size!==ref.bytes) throw new Error('native chain evidence size');
    const bytes=typeof object.arrayBuffer==='function' ? new Uint8Array(await object.arrayBuffer()) : new TextEncoder().encode(await object.text());
    if(bytes.length!==ref.bytes || await publicationDigest(bytes)!==ref.sha256) throw new Error('native chain evidence digest');
    return new TextDecoder('utf-8',{fatal:true}).decode(bytes);
  }
  const [csv,json]=await Promise.all([body('collector.snapshot.csv.v1','text/csv'),body('collector.snapshot.payload.v1','application/json')]);
  const payload=JSON.parse(json),csvKey=selected.details.originalRecordId,eventAt=selected.eventAt;
  if(payload.symbol!==instrument || payload.date!==session || payload.snapshot_key!==csvKey ||
     !csvKey.startsWith(PREFIX[instrument]+session.replaceAll('-','')+'/snapshot_') ||
     !/^snapshot_\d{12}\.csv$/.test(csvKey.split('/').at(-1)) || stamp(payload.timestamp)!==eventAt ||
     (spot!==null && payload.underlying_price!==spot)) throw new Error('native chain evidence identity');
  const rows=projectChainCsv(csv,instrument,session), expirations=[...new Set(rows.map(row=>row.Expiration))];
  if(expirations.length!==1 || expirations[0]!==payload.expiration || expirations[0]<session ||
     (instrument!=='IBIT' && expirations[0]!==session) || (spot!==null && rows.find(r=>r.UnderlyingPrice>0)?.UnderlyingPrice!==spot))
    throw new Error('native chain evidence contract');
  const timestamp=new Date(eventAt).toISOString();
  return {coverage,rows:[observation({source:'chain',id:selected.id,revision:selected.revision,session,instrument,origin:'recorded',
    eventAt,availableAt:selected.availableAt,evidence:selected.evidence,
    details:{availabilityBasis:coverage.availabilityBasis,digestVerification:coverage.digestVerification,sequence:selected.details.sequence},
    value:{symbol:instrument,date:session,tier:['0DTE_Regular','0DTE_Weekly','0DTE_Monthly'].includes(payload.tier)?payload.tier:null,spotAt:stamp(payload.underlying_market?.spot_ts),spotFreshness:payload.underlying_market?.freshness==='live'?'live':null,expiration:expirations[0],timestamp,snapshot_time:timestamp,snapshot_key:csvKey,
      underlying_price:spot,rows:spot===null?rows.map(row=>({...row,UnderlyingPrice:null})):rows,market_readings:(payload.market_readings??[]).map(r=>readingProjection(r,{cutoff,instrument,csvKey,eventAt})).filter(Boolean)}})]};
}
export async function handleOneClockChain(request,env,url) {
  if(request.method!=='GET') return jsonResponse({error:'method not allowed'},405);
  if([...url.searchParams.keys()].some(k=>!['session','instrument','cutoff'].includes(k)) || [...url.searchParams.keys()].some(k=>url.searchParams.getAll(k).length!==1)) return jsonResponse({error:'invalid query'},400);
  const selection={session:url.searchParams.get('session'),instrument:url.searchParams.get('instrument'),cutoff:Number(url.searchParams.get('cutoff'))};
  try {
    if(selection.cutoff>Date.now()+60000) throw new Error('future cutoff');
    // Validate before accessing any storage. Only fixed market prefixes allowed.
    if(!validSession(selection.session) || !Object.hasOwn(PREFIX,selection.instrument) || sessionAt(instant(selection.cutoff))<selection.session) throw new Error('invalid scope');
  } catch { return jsonResponse({error:'invalid archive scope'},400); }
  try { return jsonResponse(await publicChainAt(env,selection),200); }
  catch { return jsonResponse({error:'Archive unavailable or exceeded bounded read budget'},503); }
}
