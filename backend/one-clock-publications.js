// Dormant library: the owning integration supplies registered public streams.
// No timers, active registry, scheduler hook, credentials or KV operations.
import {instant} from '../site/shared/one-clock.js';
import {validSession} from '../site/shared/one-clock-session.js';
import {PUBLICATION_LIMITS,publicationStream,publicationKey,decodePublication} from './one-clock-publication-contract.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
const encoder=new TextEncoder();
const byteLength=value=>encoder.encode(value).byteLength;
function bounded(value,max){if(!Number.isSafeInteger(value)||value<1||value>max)throw new Error('publication budget');return value;}

export async function ingestPublicationStream(db,bucket,descriptor,{instrument,now=Date.now(),maxGets=64,maxBytes=512*1024}={}) {
  now=instant(now);bounded(maxGets,PUBLICATION_LIMITS.gets);bounded(maxBytes,PUBLICATION_LIMITS.bytes);
  const stream=publicationStream(descriptor,instrument);
  const prior=await stmt(db,'SELECT * FROM one_clock_publication_work WHERE stream=?',stream).first();
  if(prior&&now<prior.updated_at)throw new Error('publication clock regression');
  let next=prior?.next_sequence??1,digest=prior?.last_digest??null,lastAvailableAt=prior?.last_available_at??null,gets=0,bytes=0,reason='budget';
  const completed=[];
  while(gets<maxGets) {
    // Reserve a full maximum entry before issuing GET. A small remaining byte
    // allowance must not cause an unaccounted download on every continuation.
    if(bytes+PUBLICATION_LIMITS.entryBytes>maxBytes)break;
    gets++;
    let object,wire,decoded;
    try {
      object=await bucket.get(publicationKey(stream,next));
      if(!object){reason='tail-at-check';break;}
      if(!Number.isSafeInteger(object.size)||object.size<1||object.size>PUBLICATION_LIMITS.entryBytes)throw new Error('entry size');
      wire=new Uint8Array(await object.arrayBuffer());bytes+=wire.byteLength;
      if(wire.byteLength!==object.size||wire.byteLength>PUBLICATION_LIMITS.entryBytes)throw new Error('entry size');
      decoded=await decodePublication(wire,{descriptor,instrument,sequence:next,previousDigest:digest,uploaded:object.uploaded instanceof Date?object.uploaded.toISOString():object.uploaded,cutoff:now});
      if(decoded.deferred){reason='future-publication';break;}
      if(lastAvailableAt!==null&&decoded.availableAt<lastAvailableAt)throw new Error('publication upload order');
    } catch {reason='blocked-entry';break;}
    completed.push({...decoded,payload:new TextDecoder().decode(wire)});
    digest=decoded.digest;lastAvailableAt=decoded.availableAt;next++;
  }
  const generation=(prior?.generation??0)+1;
  const coverage={stream,generation,checkedAt:now,nextSequence:next,processed:completed.length,gets,bytes,reason,final:false,
    limitations:['Tail means only that the next sequential commit was absent at this read; it is not session finality.',
      'Availability is immutable commit upload time. Evidence bodies are publisher-attested, not verified by this projection reader.']};
  const statements=[stmt(db,`INSERT INTO one_clock_publication_work(stream,generation,next_sequence,last_digest,last_available_at,updated_at,coverage) VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(stream) DO UPDATE SET generation=excluded.generation,next_sequence=excluded.next_sequence,last_digest=excluded.last_digest,last_available_at=excluded.last_available_at,updated_at=excluded.updated_at,coverage=excluded.coverage`,stream,generation,next,digest,lastAvailableAt,now,JSON.stringify(coverage)),
    stmt(db,'INSERT INTO one_clock_publication_status(stream,generation,checked_at,coverage) VALUES(?,?,?,?)',stream,generation,now,JSON.stringify(coverage))];
  // Each bounded commit takes two statements regardless of record count. All
  // entries, corrections and checkpoint changes commit atomically with CAS.
  for(const entry of completed) {
    statements.push(stmt(db,'INSERT INTO one_clock_publication_entries(stream,sequence,publication_id,digest,available_at,received_at,payload) VALUES(?,?,?,?,?,?,?)',stream,entry.sequence,entry.publicationId,entry.digest,entry.availableAt,now,entry.payload));
    statements.push(stmt(db,`INSERT INTO one_clock_publication_records(stream,source,record_id,revision,session,instrument,event_at,available_at,received_at,sequence,payload)
      SELECT ?,json_extract(value,'$.source'),json_extract(value,'$.id'),json_extract(value,'$.revision'),json_extract(value,'$.session'),json_extract(value,'$.instrument'),json_extract(value,'$.eventAt'),json_extract(value,'$.availableAt'),?,?,value FROM json_each(?)`,stream,now,entry.sequence,JSON.stringify(entry.rows)));
  }
  await db.batch(statements);
  return coverage;
}

export async function ingestRegisteredPublications(db,bucket,descriptors,{now=Date.now()}={}) {
  if(!Array.isArray(descriptors))throw new Error('publication descriptors');
  const streams=descriptors.flatMap(descriptor=>descriptor.instruments.map(instrument=>({descriptor,instrument,stream:publicationStream(descriptor,instrument)})));
  if(streams.length>PUBLICATION_LIMITS.streams||new Set(streams.map(s=>s.stream)).size!==streams.length)throw new Error('publication registry bound or duplicate');
  if(!streams.length)return [];
  // Every registered stream receives a fair share on each invocation. Neither
  // tenant count nor an indefinitely busy first stream can starve another.
  const maxGets=Math.floor(PUBLICATION_LIMITS.gets/streams.length),maxBytes=Math.floor(PUBLICATION_LIMITS.bytes/streams.length),results=[];
  for(const {descriptor,instrument,stream} of streams) {
    try {results.push(await ingestPublicationStream(db,bucket,descriptor,{instrument,now,maxGets,maxBytes}));}
    catch {results.push({stream,reason:'transaction-failed',final:false});}
  }
  return results;
}

export async function publicationRowsPage(db,descriptor,{instrument,session,cutoff,after=null,limit=100}={}) {
  const stream=publicationStream(descriptor,instrument);cutoff=instant(cutoff);bounded(limit,100);
  if(!validSession(session))throw new Error('publication session');
  const scope=JSON.stringify([stream,session,cutoff]);
  if(after&&(after.scope!==scope||!Number.isSafeInteger(after.sequence)||after.sequence<1||typeof after.source!=='string'||typeof after.id!=='string'))throw new Error('publication page cursor');
  const args=[stream,session,instrument,cutoff,cutoff,cutoff,after?.sequence??0,after?.sequence??0,after?.source??'',after?.source??'',after?.id??'',limit+1];
  const query=`FROM one_clock_publication_records WHERE stream=? AND session=? AND instrument=? AND received_at<=? AND available_at<=? AND event_at<=?
    AND (sequence>? OR (sequence=? AND (source>? OR (source=? AND record_id>?)))) ORDER BY sequence,source,record_id LIMIT ?`;
  const sizes=(await stmt(db,'SELECT length(CAST(payload AS BLOB)) AS n '+query,...args).all()).results;
  if(sizes.reduce((n,r)=>n+r.n,0)>2*1024*1024)throw new Error('publication page byte bound');
  const records=(await stmt(db,'SELECT sequence,source,record_id,payload,received_at '+query,...args).all()).results;
  if(records.reduce((n,r)=>n+byteLength(r.payload),0)>2*1024*1024)throw new Error('publication page byte bound');
  const rows=records.slice(0,limit),last=rows.at(-1);
  return {rows:rows.map(r=>({row:JSON.parse(r.payload),receivedAt:r.received_at})),next:records.length>limit?{scope,sequence:last.sequence,source:last.source,id:last.record_id}:null};
}
