import {ingestRegisteredPublications,publicationRowsPage} from './one-clock-publications.js';
import {publicationStream} from './one-clock-publication-contract.js';
import {instant} from '../site/shared/one-clock.js';
import {validSession} from '../site/shared/one-clock-session.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
export const collectorPublication=Object.freeze({id:'collector-chain',owner:'market_data.live_market',audience:'public',sessionBasis:'new-york',
  supersedes:{price:'archived-chain',skew:'archived-chain'},
  instruments:['QQQ','SPY','IBIT'],outputs:{price:{spot:'number'},skew:{imbalance:'number',strikes:'number',callOI:'number',putOI:'number'}}});
export const nativePublicationSources=Object.freeze([collectorPublication]);
export async function advanceNativePublications(env,{now=Date.now(),descriptors=nativePublicationSources}={}) {
  if(env.ONE_CLOCK_PUBLICATIONS!=='enabled')return [];
  return ingestRegisteredPublications(env.DB,env.PAPER_TRADES,descriptors,{now});
}

export async function nativeScopeRecords(db,descriptor,{session,instrument,cutoff,currentProgress=null}) {
  cutoff=instant(cutoff);if(!validSession(session))throw new Error('native session');
  const stream=publicationStream(descriptor,instrument);
  const first=await stmt(db,'SELECT payload FROM one_clock_publication_entries WHERE stream=? AND sequence=1 AND received_at<=?',stream,cutoff).first();
  if(!first)return null;
  const start=Math.min(...JSON.parse(first.payload).records.map(r=>instant(r.eventAt)));
  const firstSession=JSON.parse(first.payload).records.map(r=>r.session).sort()[0];
  if(session<firstSession)return null;
  const status=await stmt(db,'SELECT coverage FROM one_clock_publication_status INDEXED BY one_clock_publication_status_cutoff WHERE stream=? AND checked_at<=? ORDER BY checked_at DESC,generation DESC LIMIT 1',stream,cutoff).first();
  const retainedProgress=status?JSON.parse(status.coverage):null;
  const progress=currentProgress??retainedProgress,nextSequence=retainedProgress?.nextSequence??1,records=[];let after=null,bytes=0;
  do {
    const page=await publicationRowsPage(db,descriptor,{session,instrument,cutoff,after});after=page.next;
    for(const {row,receivedAt} of page.rows) {
      if(row.details.sequence>=nextSequence)continue;
      const payload=JSON.stringify(row);bytes+=new TextEncoder().encode(payload).byteLength;
      if(records.length>=10000||bytes>8*1024*1024)throw new Error('native evaluation scope bound');
      records.push({source:row.source,record_id:row.id,revision:row.revision,session,instrument,origin:'recorded',event_at:row.eventAt,available_at:row.availableAt,received_at:receivedAt,collection:row.details.observerCollection??'backfill',payload});
    }
  }while(after);
  const lastPublicationAt=records.length?Math.max(...records.map(r=>r.available_at)):null;
  const coverage={source:descriptor.id,adapterVersion:'1',session,instrument,updatedAt:cutoff,indexComplete:progress?.reason==='tail-at-check',pending:progress?.reason==='tail-at-check'?0:1,gaps:progress?.reason==='blocked-entry'?1:0,
    nativeStart:start,nextSequence,lastPublicationAt,publicationAgeMs:lastPublicationAt===null?null:cutoff-lastPublicationAt,
    reason:(progress?.reason??'no-receipt')+(lastPublicationAt===null?'; no native publications retained for this session':'; latest native publication '+new Date(lastPublicationAt).toISOString()),final:false,
    limitations:['Native capture begins at the first commit; a separate source handoff preserves retained legacy identities.','Tail-at-check is not producer finality. Evidence bodies are publisher-attested.']};
  return {records,coverage,start};
}

export async function nativeHandoffExists(db) {
  const table=await stmt(db,"SELECT name FROM sqlite_master WHERE type='table' AND name='one_clock_source_handoffs'").first();
  return !!table&&!!await stmt(db,'SELECT id FROM one_clock_source_handoffs LIMIT 1').first();
}

export async function retainNativeCoverage(db,coverage) {
  const args=[coverage.source,coverage.session,coverage.instrument];
  const prior=await stmt(db,'SELECT generation FROM one_clock_ingestion_work WHERE source=? AND session=? AND instrument=?',...args).first();
  const generation=(prior?.generation??0)+1,payload=JSON.stringify(coverage);
  await db.batch([
    stmt(db,`INSERT INTO one_clock_ingestion_work(source,session,instrument,generation,updated_at,coverage) VALUES(?,?,?,?,?,?)
      ON CONFLICT(source,session,instrument) DO UPDATE SET generation=excluded.generation,updated_at=excluded.updated_at,coverage=excluded.coverage`,...args,generation,coverage.updatedAt,payload),
    stmt(db,'INSERT INTO one_clock_ingestion_history(source,session,instrument,generation,recorded_at,coverage) VALUES(?,?,?,?,?,?)',...args,generation,coverage.updatedAt,payload)
  ]);
}

export async function markPendingNativeEvidence(db,evaluation,cutoff) {
  const scopes=[...new Map(evaluation.map(e=>[JSON.stringify([e.session,e.instrument]),[e.session,e.instrument]])).entries()];
  if(scopes.length>93)throw new Error('native result scope bound');
  const latest=new Map();
  for(let i=0;i<scopes.length;i+=6)await Promise.all(scopes.slice(i,i+6).map(async([key,[session,instrument]])=>{
    const row=await stmt(db,'SELECT received_at FROM one_clock_publication_records INDEXED BY one_clock_publication_record_scope WHERE session=? AND instrument=? AND received_at<=? ORDER BY received_at DESC LIMIT 1',session,instrument,cutoff).first();
    latest.set(key,row?.received_at??0);
  }));
  return evaluation.map(e=>{
    const receivedAt=latest.get(JSON.stringify([e.session,e.instrument]));
    return e.attempted_at!==null&&receivedAt>e.attempted_at?{...e,state:'pending',newEvidenceAt:receivedAt,
      reason:'Newer native evidence in this scope has not been evaluated. Retained results reflect the previous evaluation; older sessions await historical reevaluation.'}:e;
  });
}
