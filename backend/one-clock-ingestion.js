// Shared public ingestion, independent of tenant/rule counts. Adapters own
// evidence qualification; the runner owns retry, atomic progress and budgets.
import { instant,observation } from '../site/shared/one-clock.js';
import { validSession } from '../site/shared/one-clock-session.js';
export const INGESTION_LIMITS=Object.freeze({artifacts:20,bytes:4*1024*1024,objectBytes:1024*1024,versions:2000,observations:10000});
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
const scopeArgs=s=>[s.source,s.session,s.instrument];
const canonical=value=>Array.isArray(value)?'['+value.map(canonical).join(',')+']':value&&typeof value==='object'?'{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}':JSON.stringify(value);
export class IngestionBudget extends Error {}

export async function ingestPublicSource(db,bucket,adapter,{session,instrument,now=Date.now(),collection='prospective'}={}) {
  now=instant(now);
  if(!validSession(session)||!adapter.instruments.includes(instrument)||!['prospective','backfill','replay'].includes(collection))throw new Error('invalid public ingestion scope');
  const scope={source:adapter.id,session,instrument},args=scopeArgs(scope);
  const prior=await stmt(db,'SELECT * FROM one_clock_ingestion_work WHERE source=? AND session=? AND instrument=?',...args).first();
  const versions=(await stmt(db,'SELECT artifact,version,status FROM one_clock_ingestion_versions WHERE source=? AND session=? AND instrument=? LIMIT 2001',...args).all()).results;
  if(versions.length>INGESTION_LIMITS.versions)throw new Error('source revision bound');
  const index=await adapter.discover(bucket,{session,instrument,cutoff:now});
  if(!Array.isArray(index.artifacts)||index.artifacts.length>1000)throw new Error('source index bound');
  const known=new Set(versions.map(v=>JSON.stringify([v.artifact,v.version])));
  const candidates=index.artifacts.filter(v=>!known.has(JSON.stringify([v.key,v.version])));
  // Half oldest and half newest pending work: catch-up cannot starve live
  // records, and a busy live tail cannot starve retained older evidence.
  candidates.sort((a,b)=>a.eventAt-b.eventAt||a.key.localeCompare(b.key));
  const half=INGESTION_LIMITS.artifacts/2;
  const work=[...candidates.slice(0,half),...candidates.slice(Math.max(half,candidates.length-half))];
  let bytes=0,gets=0;
  const readBucket={async get(key) {
    const object=await bucket.get(key);gets++;
    if(!object)return null;
    if(!Number.isSafeInteger(object.size)||object.size>INGESTION_LIMITS.objectBytes)throw new Error('source object bound');
    if(bytes+object.size>INGESTION_LIMITS.bytes)throw new IngestionBudget('source byte budget');
    const text=await object.text(),size=new TextEncoder().encode(text).byteLength;
    if(size>INGESTION_LIMITS.objectBytes)throw new Error('source object bound');
    if(bytes+size>INGESTION_LIMITS.bytes)throw new IngestionBudget('source byte budget');
    bytes+=size;
    return {size,etag:object.etag,uploaded:object.uploaded,text:async()=>text};
  }};
  const completed=[],rows=[];
  if(index.complete)for(const artifact of work) {
    let result;
    try { result=await adapter.read(readBucket,{session,instrument,cutoff:now},artifact); }
    catch(error) { if(error instanceof IngestionBudget)break;throw error; }
    if(result.retry)continue; // object changed after listing; rediscover its revision
    if(!Array.isArray(result.rows)||result.rows.length>10)throw new Error('invalid source projection');
    for(const input of result.rows) {
      const row=observation(adapter.revisionPolicy==='retained-artifact'?{...input,
        revision:versions.filter(v=>v.artifact===artifact.key).length+1,
        details:{...input.details,revisionBasis:'observer-retained-artifact-order'}}:input);
      if(!adapter.outputs.includes(row.source)||row.session!==session||row.instrument!==instrument||row.origin!=='recorded'||row.availableAt===null||row.availableAt>now)throw new Error('source projection outside scope');
      rows.push(row);
    }
    completed.push({artifact,result});
  }
  if(versions.length+completed.length>INGESTION_LIMITS.versions)throw new Error('source revision bound');
  const count=await stmt(db,'SELECT COUNT(*) AS n FROM one_clock_ingested WHERE session=? AND instrument=? AND origin=?',session,instrument,'recorded').first();
  if(count.n+rows.length>INGESTION_LIMITS.observations)throw new Error('ingested session bound');
  const coverage={source:adapter.id,adapterVersion:adapter.version,session,instrument,updatedAt:now,
    indexComplete:index.complete,pending:candidates.length-completed.length,processed:completed.length,
    gets,bytes,retainedVersions:versions.length+completed.length,
    gaps:versions.filter(v=>v.status==='gap').length+completed.filter(v=>!v.result.rows.length).length,
    final:false,reason:!index.complete?'metadata-index-bound':candidates.length>completed.length?'catching-up':'periodic-archive-observation',
    limitations:['No producer finality guarantee. Older sessions may receive late evidence.',
      'Versions overwritten between polls cannot be reconstructed. Retained versions remain available.']};
  const generation=(prior?.generation??0)+1;
  const statements=[stmt(db,`INSERT INTO one_clock_ingestion_work(source,session,instrument,generation,updated_at,coverage) VALUES(?,?,?,?,?,?)
    ON CONFLICT(source,session,instrument) DO UPDATE SET generation=excluded.generation,updated_at=excluded.updated_at,coverage=excluded.coverage`,...args,generation,now,JSON.stringify(coverage))];
  statements.push(stmt(db,'INSERT INTO one_clock_ingestion_history(source,session,instrument,generation,recorded_at,coverage) VALUES(?,?,?,?,?,?)',...args,generation,now,JSON.stringify(coverage)));
  for(const {artifact,result} of completed)statements.push(stmt(db,`INSERT INTO one_clock_ingestion_versions(source,session,instrument,artifact,version,received_at,status,detail) VALUES(?,?,?,?,?,?,?,?)`,
    ...args,artifact.key,artifact.version,now,result.rows.length?'retained':'gap',result.reason??''));
  for(const row of rows)statements.push(stmt(db,`INSERT INTO one_clock_ingested(source,session,instrument,origin,record_id,revision,event_at,available_at,received_at,collection,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING`,
    row.source,row.session,row.instrument,row.origin,row.id,row.revision,row.eventAt,row.availableAt,now,
    collection==='prospective'&&(!prior||row.availableAt<=prior.updated_at)?'backfill':collection,canonical(row)));
  await db.batch(statements);
  return coverage;
}

export async function ingestedAsOf(db,{session,instrument,cutoff}) {
  cutoff=instant(cutoff);
  const rows=(await stmt(db,`SELECT * FROM one_clock_ingested WHERE session=? AND instrument=? AND origin='recorded' AND received_at<=? ORDER BY received_at,source,record_id LIMIT 10001`,session,instrument,cutoff).all()).results;
  if(rows.length>INGESTION_LIMITS.observations)throw new Error('ingested session bound');
  return rows;
}
