import {observation} from '../site/shared/one-clock.js';
import {publicationStream} from './one-clock-publication-contract.js';
const stmt=(db,sql,...args)=>db.prepare(sql).bind(...args);
const list=async s=>(await s.all()).results;
const canonical=v=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);
const bytes=v=>new TextEncoder().encode(v).byteLength;
function chunks(rows) {
  const output=[];let chunk=[],size=2;
  for(const row of rows){const n=bytes(JSON.stringify(row))+1;if(size+n>128*1024){output.push(JSON.stringify(chunk));chunk=[];size=2;}chunk.push(row);size+=n;}
  if(chunk.length)output.push(JSON.stringify(chunk));return output;
}

export async function handoffSource(db,descriptor,native,legacy,{source,session,instrument,cutoff,snapshotCutoff=cutoff}) {
  const stream=publicationStream(descriptor,instrument),adapter=descriptor.supersedes?.[source];
  if(!adapter)return native.records.filter(r=>r.source===source);
  const scope=[stream,session,instrument,source];
  let handoff=await stmt(db,'SELECT * FROM one_clock_source_handoffs WHERE stream=? AND session=? AND instrument=? AND source=?',...scope).first();
  const nativeRows=native.records.filter(r=>r.source===source);
  if(!handoff) {
    if(!native.coverage.indexComplete)throw new Error('native handoff awaits tail');
    // Read generation before the snapshot. The INSERT trigger checks it inside
    // the freezing transaction, rejecting a concurrent legacy ingestion commit.
    const work=await stmt(db,'SELECT generation FROM one_clock_ingestion_work WHERE source=? AND session=? AND instrument=?',adapter,session,instrument).first();
    // Freeze membership through the real cutover, including legacy arrivals
    // since the historical target. The projection below applies snapshotCutoff.
    const legacyQuery="FROM one_clock_ingested WHERE source=? AND session=? AND instrument=? AND origin='recorded' AND received_at<=? LIMIT 10001",legacyArgs=[source,session,instrument,cutoff];
    const sizes=await list(stmt(db,'SELECT length(CAST(payload AS BLOB)) n '+legacyQuery,...legacyArgs));
    if(sizes.length>10000||sizes.reduce((n,r)=>n+r.n,0)>8*1024*1024)throw new Error('legacy handoff snapshot bound');
    const retained=await list(stmt(db,'SELECT * '+legacyQuery,...legacyArgs)),heads=new Map(),baselines=new Map();
    for(const r of retained)if(r.revision>(heads.get(r.record_id)?.revision??0))heads.set(r.record_id,r);
    for(const r of nativeRows){const row=JSON.parse(r.payload),id=row.details.originalRecordId;if(r.revision>(baselines.get(id)?.revision??0))baselines.set(id,r);}
    const identities=[];
    for(const [id,old] of heads) {
      const next=baselines.get(id),oldRow=JSON.parse(old.payload),newRow=next?JSON.parse(next.payload):null;
      const changed=!!next&&canonical([oldRow.eventAt,oldRow.value])!==canonical([newRow.eventAt,newRow.value]);
      identities.push({id,legacy:old.revision,native:next?.revision??0,sequence:newRow?.details.sequence??0,changed:changed?1:0});
    }
    const id=crypto.randomUUID();
    const statements=[stmt(db,'INSERT INTO one_clock_source_handoffs(stream,session,instrument,source,id,cutover_at,native_sequence,legacy_adapter,legacy_generation) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',...scope,id,cutoff,native.coverage.nextSequence-1,adapter,work?.generation??0)];
    for(const chunk of chunks(retained.map(r=>({id:r.record_id,revision:r.revision}))))statements.push(stmt(db,`INSERT INTO one_clock_handoff_records(handoff_id,record_id,revision)
      SELECT ?,json_extract(value,'$.id'),json_extract(value,'$.revision') FROM json_each(?) WHERE EXISTS(SELECT 1 FROM one_clock_source_handoffs WHERE id=?)`,id,chunk,id));
    for(const chunk of chunks(identities))statements.push(stmt(db,`INSERT INTO one_clock_handoff_identities(handoff_id,original_id,legacy_revision,native_revision,native_sequence,baseline_changed)
      SELECT ?,json_extract(value,'$.id'),json_extract(value,'$.legacy'),json_extract(value,'$.native'),json_extract(value,'$.sequence'),json_extract(value,'$.changed') FROM json_each(?) WHERE EXISTS(SELECT 1 FROM one_clock_source_handoffs WHERE id=?)`,id,chunk,id));
    await db.batch(statements);
    handoff=await stmt(db,'SELECT * FROM one_clock_source_handoffs WHERE stream=? AND session=? AND instrument=? AND source=?',...scope).first();
  }
  if(handoff.legacy_adapter!==adapter||handoff.cutover_at>cutoff)throw new Error('source handoff scope');
  const identities=new Map((await list(stmt(db,'SELECT * FROM one_clock_handoff_identities WHERE handoff_id=? LIMIT 10001',handoff.id))).map(r=>[r.original_id,r]));
  const from=`FROM one_clock_handoff_records h CROSS JOIN one_clock_ingested r ON r.source=? AND r.session=? AND r.instrument=? AND r.origin='recorded' AND r.record_id=h.record_id AND r.revision=h.revision WHERE h.handoff_id=? AND r.received_at<=? LIMIT 10001`;
  const args=[source,session,instrument,handoff.id,snapshotCutoff];
  const sizes=await list(stmt(db,'SELECT length(CAST(r.payload AS BLOB)) n '+from,...args));
  if(sizes.length>10000||sizes.reduce((n,r)=>n+r.n,0)>8*1024*1024||identities.size>10000)throw new Error('handoff scope bound');
  const result=await list(stmt(db,'SELECT r.* '+from,...args));
  for(const record of nativeRows) {
    const row=JSON.parse(record.payload),mapping=identities.get(row.details.originalRecordId);
    if(!mapping){result.push(record);continue;}
    const baseline=row.details.sequence<=handoff.native_sequence;
    if(baseline&&(!mapping.baseline_changed||row.details.sequence!==mapping.native_sequence))continue;
    const revision=mapping.legacy_revision+mapping.baseline_changed+row.revision-mapping.native_revision;
    if(!Number.isSafeInteger(revision)||revision<=mapping.legacy_revision)throw new Error('handoff revision overflow');
    const mapped=observation({...row,id:row.details.originalRecordId,revision,availableAt:baseline?Math.max(row.availableAt,handoff.cutover_at):row.availableAt,
      details:{...row.details,nativeIdentity:row.id,nativeRevision:row.revision,handoffId:handoff.id,handoffAt:handoff.cutover_at}});
    result.push({...record,record_id:mapped.id,revision,available_at:mapped.availableAt,received_at:baseline?handoff.cutover_at:record.received_at,collection:baseline?'backfill':record.collection,payload:JSON.stringify(mapped)});
  }
  if(result.length>10000||result.reduce((n,r)=>n+bytes(r.payload),0)>8*1024*1024)throw new Error('composed source bound');
  return result;
}
