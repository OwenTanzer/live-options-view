// Read the existing producer's successful publication receipts. A run's
// execution/commit alone does not prove it ever became the public shortlist.
import { jsonResponse } from './common.js';
import { instant,observation } from '../site/shared/one-clock.js';
import { validSession,sessionAt,sessionInstant } from '../site/shared/one-clock-session.js';
const PREFIX='squeeze-scanner/v1/scheduled/';
const UUID='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const RECEIPT=new RegExp('^'+PREFIX+'publication-attempts/('+UUID+')/('+UUID+')/(started|finished|failed)\\.json$');
const stamp=value=>{try{return instant(value);}catch{return null;}};
async function hash(text) {
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));
  return [...new Uint8Array(bytes)].map(b=>b.toString(16).padStart(2,'0')).join('');
}
export async function readSqueezePublications(bucket,{session,instrument,cutoff}) {
  const start=sessionInstant(session,0),candidates=[],attempts=new Map();let cursor,complete=false,lists=0,gets=0,bytes=0,indexQualified=true;
  do {
    const page=await bucket.list({prefix:PREFIX+'publication-attempts/',limit:1000,...(cursor?{cursor}:{})});lists++;
    for(const item of page.objects) {
      const match=RECEIPT.exec(item.key),at=stamp(item.uploaded?.toISOString());
      if(match && at===null)indexQualified=false;
      if(match && at!==null && at<=cutoff) {
        const id=match[1]+'/'+match[2],entry=attempts.get(id)??{};entry[match[3]]={key:item.key,run:match[1],at};attempts.set(id,entry);
        if(match[3]==='finished' && at>=start)candidates.push({key:item.key,run:match[1],at});
      }
    }
    complete=!page.truncated;cursor=page.truncated?page.cursor:null;
  }while(cursor && lists<2);
  const coverage={indexComplete:complete,publicationComplete:false,lists,gets,bytes,
    basis:'successful-publication-receipt',origin:'recorded',reason:null};
  if(!complete || !indexQualified || candidates.length>20)return {rows:[],coverage:{...coverage,reason:!indexQualified?'publication-time-unavailable':!complete?'publication-index-bound':'publication-candidate-bound'}};
  // The pointer is advanced BEFORE finished.json is written. An interrupted
  // or failed publication may therefore have changed it. Older unresolved
  // attempts remain a conservative gap; don't guess that they were harmless.
  if([...attempts.values()].some(a=>!a.started || !a.finished || a.started.at>a.finished.at || a.failed))
    return {rows:[],coverage:{...coverage,reason:'unresolved-publication-attempt'}};
  async function read(key) {
    gets++;const object=await bucket.get(key);if(!object)return null;
    const at=stamp(object.uploaded?.toISOString());
    if(at===null || at>cutoff || !object.etag)return null;
    if(!Number.isSafeInteger(object.size) || object.size>1048576 || bytes+object.size>4194304)throw new Error('publication byte bound');
    const text=await object.text();bytes+=new TextEncoder().encode(text).byteLength;
    if(bytes>4194304 || new TextEncoder().encode(text).byteLength>1048576)throw new Error('publication byte bound');
    return {text,value:JSON.parse(text),at};
  }
  const rows=[],seen=new Set();let qualified=true;
  for(const candidate of candidates.sort((a,b)=>a.at-b.at || a.key.localeCompare(b.key))) {
    const receipt=await read(candidate.key);
    if(!receipt){qualified=false;continue;}
    const base=PREFIX+'runs/'+candidate.run,p=receipt.value;
    if(p.run_id!==candidate.run || p.archive!==base || p.archive_verified!==true){qualified=false;continue;}
    if(!['advanced','unchanged','not_eligible'].includes(p.latest)){qualified=false;continue;}
    if(p.latest!=='advanced')continue; // archive-only and superseded runs are not public appearances
    const published=stamp(p.at);
    if(published===null || published>cutoff){qualified=false;continue;}
    const marker=await read(base+'/COMMITTED.json'),manifest=await read(base+'/manifest.json'),result=await read(base+'/results.json');
    if(!marker || !manifest || !result){qualified=false;continue;}
    const m=manifest.value,meta=m.files?.['results.json'];
    if(m.schema_version!==1 || m.run_id!==candidate.run || marker.value.run_id!==candidate.run ||
       marker.value.manifest_sha256!==await hash(manifest.text) || !meta || meta.sha256!==await hash(result.text) ||
       meta.bytes!==new TextEncoder().encode(result.text).byteLength || !['complete','empty'].includes(m.status)) {qualified=false;continue;}
    const finishedAt=stamp(m.finished_at),started=stamp(m.started_at);
    if(finishedAt===null || started===null || started>finishedAt || finishedAt>published || sessionAt(finishedAt)!==session || !Array.isArray(result.value) || result.value.length>2000) {qualified=false;continue;}
    const eligible=result.value.filter(r=>r?.comparison_eligible===true);
    if(eligible.some(r=>typeof r.ticker!=='string' || !/^[A-Z][A-Z0-9.-]{0,14}$/.test(r.ticker) ||
      !Number.isFinite(r.ranks?.combined) || r.ranks.combined<1 || ['combined','factor','options'].some(k=>r.scores?.[k]!=null && (!Number.isFinite(r.scores[k]) || r.scores[k]<0 || r.scores[k]>1)))) {qualified=false;continue;}
    // Same top-ten membership and ordering as squeeze_scanner.storage.upload.
    const shortlist=eligible.sort((a,b)=>a.ranks.combined-b.ranks.combined).slice(0,10);
    const found=shortlist.find(r=>r.ticker===instrument),digest=marker.value.manifest_sha256;
    const id=candidate.run+':'+digest;if(seen.has(id))continue;seen.add(id);
    const availableAt=Math.max(published,receipt.at,marker.at,manifest.at,result.at,finishedAt);
    // This observation is a proven publication-state change, not scan finish.
    // Keep the source's exact string ordering (including microseconds) below.
    rows.push(observation({source:'shortlist',id,revision:1,session,instrument,origin:'recorded',eventAt:availableAt,availableAt,
      evidence:base+'/manifest.json',value:{listed:!!found,rank:found?.ranks.combined??null,
        combined:found?.scores.combined??null,factor:found?.scores.factor??null,options:found?.scores.options??null},
      details:{runId:candidate.run,publicationReceipt:candidate.key,availabilityBasis:'successful-publication-receipt',
        eventBasis:'proven-publication-state',startedAt:m.started_at,finishedAt,
        scoringVersion:typeof m.scoring?.version==='string'?m.scoring.version.slice(0,100):null,
        status:m.status,digestVerification:'manifest-and-results',scope:'published-top-ten'}}));
  }
  // Delayed terminal receipts cannot restore a run the producer's newer
  // (started_at, run_id) pointer has already superseded. Emit only advances.
  const order=(a,b)=>a.details.startedAt<b.details.startedAt?-1:a.details.startedAt>b.details.startedAt?1:
    a.details.runId<b.details.runId?-1:a.details.runId>b.details.runId?1:0;
  let winner=null;const states=[];
  for(const row of rows.sort((a,b)=>a.availableAt-b.availableAt || order(a,b))) {
    if(!winner || order(row,winner)>0){
      // The common selector re-sorts equal instants by identity. At one
      // millisecond there is only one observable pointer state: its winner.
      if(states.at(-1)?.availableAt===row.availableAt)states.pop();
      states.push(row);winner=row;
    }
  }
  return {rows:qualified?states:[],coverage:{...coverage,publicationComplete:qualified,gets,bytes,reason:qualified?(states.length?null:'no-published-run'):'unqualified-publication'}};
}
export async function handleOneClockSqueeze(request,env,url) {
  if(request.method!=='GET')return jsonResponse({error:'method not allowed'},405);
  const scope={session:url.searchParams.get('session'),instrument:url.searchParams.get('instrument'),cutoff:Number(url.searchParams.get('cutoff'))};
  try {
    if([...url.searchParams.keys()].some(k=>!['session','instrument','cutoff'].includes(k) || url.searchParams.getAll(k).length!==1) ||
       !validSession(scope.session) || !/^[A-Z][A-Z0-9.-]{0,14}$/.test(scope.instrument??'') ||
       sessionAt(instant(scope.cutoff))!==scope.session || scope.cutoff>Date.now()+60000)throw new Error();
  }catch{return jsonResponse({error:'invalid shortlist scope'},400);}
  try {return jsonResponse(await readSqueezePublications(env.PAPER_TRADES,scope),200);}
  catch{return jsonResponse({error:'Shortlist publication unavailable or exceeded bounded read budget'},503);}
}
