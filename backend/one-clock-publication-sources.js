import {ingestRegisteredPublications,publicationRowsPage} from './one-clock-publications.js';
import {publicationStream} from './one-clock-publication-contract.js';
import {instant} from '../site/shared/one-clock.js';
import {validSession} from '../site/shared/one-clock-session.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
export const collectorPublication=Object.freeze({id:'collector-chain',owner:'market_data.live_market',audience:'public',sessionBasis:'new-york',
  supersedes:{price:'archived-chain',skew:'archived-chain'},
  instruments:['QQQ','SPY','IBIT'],outputs:{price:{spot:'number'},skew:{imbalance:'number',strikes:'number',callOI:'number',putOI:'number'}}});
export const botAuditPublication = Object.freeze({
  id: 'bot-audit', owner: 'crassus', audience: 'public', sessionBasis: 'new-york',
  instrumentsByOutput: {'bot-anchored-skew':['QQQ']},
  instruments: ['QQQ', 'SPY', 'IBIT'],
  outputs: {
    'bot-decision': {bot:'string', outcome:'string', action:'string', inputAgeMs:'number'},
    'bot-anchored-skew': {anchor:'number', imbalance:'number', delta:'number', strikes:'number',
      anchorAt:'string', run:'string', threshold:'number', minChange:'number'},
  },
});
export const nativePublicationSources=Object.freeze([collectorPublication,botAuditPublication]);
export async function advanceNativePublications(env,{now=Date.now(),descriptors=nativePublicationSources}={}) {
  if(env.ONE_CLOCK_PUBLICATIONS!=='enabled')return [];
  return ingestRegisteredPublications(env.DB,env.PAPER_TRADES,descriptors,{now});
}

export async function nativeScopeRecords(db,descriptor,{session,instrument,cutoff,currentProgress=null,maxSequence=null}) {
  cutoff=instant(cutoff);if(!validSession(session))throw new Error('native session');
  const stream=publicationStream(descriptor,instrument);
  const first=await stmt(db,'SELECT payload FROM one_clock_publication_entries WHERE stream=? AND sequence=1 AND received_at<=?',stream,cutoff).first();
  if(!first)return null;
  const start=Math.min(...JSON.parse(first.payload).records.map(r=>instant(r.eventAt)));
  const firstSession=JSON.parse(first.payload).records.map(r=>r.session).sort()[0];
  const status=await stmt(db,'SELECT coverage FROM one_clock_publication_status INDEXED BY one_clock_publication_status_cutoff WHERE stream=? AND checked_at<=? ORDER BY checked_at DESC,generation DESC LIMIT 1',stream,cutoff).first();
  const retainedProgress=status?JSON.parse(status.coverage):null;
  const progress=currentProgress??retainedProgress,nextSequence=Math.min(retainedProgress?.nextSequence??1,maxSequence===null?Infinity:maxSequence+1),records=[];let after=null,bytes=0;
  do {
    const page=await publicationRowsPage(db,descriptor,{session,instrument,cutoff,after});after=page.next;
    for(const {row,receivedAt} of page.rows) {
      if(row.details.sequence>=nextSequence)continue;
      const payload=JSON.stringify(row);bytes+=new TextEncoder().encode(payload).byteLength;
      if(records.length>=10000||bytes>8*1024*1024)throw new Error('native evaluation scope bound');
      records.push({source:row.source,record_id:row.id,revision:row.revision,session,instrument,origin:'recorded',event_at:row.eventAt,available_at:row.availableAt,received_at:receivedAt,collection:row.details.observerCollection??'backfill',payload});
    }
  }while(after);
  // A later commit may correct an earlier session than the stream's first
  // publication. Decide ownership from this scope's retained evidence.
  if(session<firstSession&&!records.length)return null;
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

export async function markPendingNativeEvidence(db,evaluation,cutoff,namespace) {
  if(!evaluation.length)return [];
  const indexing=await stmt(db,'SELECT 1 AS pending FROM one_clock_change_bootstrap WHERE last_sequence<end_sequence LIMIT 1').first();
  const out=[];
  // Results may cover many rules and sessions. Bind each page once and seek
  // the scope/receipt indexes inside SQLite instead of making an RPC per rule.
  for(let start=0;start<evaluation.length;start+=100) {
    const page=evaluation.slice(start,start+100);
    const checks=(await stmt(db,`SELECT e.key AS idx,
      (SELECT MAX((SELECT c.generation FROM one_clock_native_changes c WHERE
        (c.session,c.instrument,c.stream,c.bootstrap)=(l.session,l.instrument,l.stream,l.bootstrap)
        AND c.received_at<=? ORDER BY c.received_at DESC,c.generation DESC LIMIT 1))
        FROM one_clock_change_lanes l WHERE l.session=e.value->>'$.session' AND l.instrument=e.value->>'$.instrument') desired,
      (SELECT r.generation FROM one_clock_generation_receipts r WHERE r.namespace=?
        AND r.session=e.value->>'$.session' AND r.instrument=e.value->>'$.instrument'
        AND r.rule_id=e.value->>'$.rule_id' AND r.version=e.value->>'$.version' AND r.committed_at<=?
        ORDER BY r.committed_at DESC,r.generation DESC LIMIT 1) completed
      FROM json_each(?) e`,cutoff,namespace,cutoff,JSON.stringify(page)).all()).results;
    for(const c of checks) {
      const e=page[c.idx],desired=c.desired??0,completed=c.completed??0;
      out.push(indexing?{...e,state:'pending',reason:'Existing native publications are being indexed for historical evaluation.'}:desired>completed?{...e,state:e.state==='failed'?'failed':'pending',desiredGeneration:desired,completedGeneration:completed,
        reason:e.state==='failed'?e.reason:'New native evidence awaits evaluation. Retained results reflect the previous evaluation.'}:
        {...e,desiredGeneration:desired,completedGeneration:completed});
    }
  }
  return out;
}
