// Generic bounded metadata ingestion. Source owners still own capture and
// content verification. This catalog never reads object bodies or writes R2.
import { instant } from '../site/shared/one-clock.js';
const bind=(db,sql,...args)=>db.prepare(sql).bind(...args);
export const CATALOG_PAGE_SIZE=100;
const END=1n<<53n;
// A point has 54 ancestors; each interval decomposes into at most 106
// disjoint buckets. BigInt avoids rounding at the safe-integer boundary.
export function intervalBuckets(from,to=Number.MAX_SAFE_INTEGER) {
  let lo=BigInt(from),hi=to===Number.MAX_SAFE_INTEGER?END:BigInt(to);const buckets=[];
  if(lo<0n || hi>END || lo>hi)throw new Error('invalid catalog interval');
  while(lo<hi) {
    let level=0,width=1n;
    while(level<53 && lo%(width*2n)===0n && lo+width*2n<=hi){level++;width*=2n;}
    buckets.push(`${level}:${lo/width}`);lo+=width;
  }
  return buckets;
}
function pointBuckets(at) {
  const point=BigInt(at);if(point<0n || point>=END)throw new Error('invalid catalog cutoff');
  return Array.from({length:54},(_,level)=>`${level}:${point/(1n<<BigInt(level))}`);
}
function attemptGaps(records) {
  const kinds=Object.fromEntries(records.map(r=>[r.kind,r.uploaded]));
  const first=Math.min(...records.map(r=>r.uploaded));
  if(kinds.started===undefined || kinds.finished===undefined || kinds.started>kinds.finished)
    return intervalBuckets(first);
  if(kinds.failed!==undefined && kinds.failed<=kinds.finished)
    return intervalBuckets(Math.min(kinds.started,kinds.failed));
  return [...intervalBuckets(kinds.started,kinds.finished),
    ...(kinds.failed===undefined?[]:intervalBuckets(kinds.failed))];
}
export async function advanceSourceCatalog(db,bucket,descriptor,{now=Date.now()}={}) {
  const {id,version,prefix,classify}=descriptor;
  const state=await bind(db,'SELECT * FROM one_clock_catalog_state WHERE source=?',id).first();
  if(state && state.source_version!==version)throw new Error('catalog descriptor version mismatch');
  const at=instant(now),start=state?.cursor?state.cycle_started_at:at;
  if(state && at<state.updated_at)throw new Error('catalog clock cannot rewind');
  let page;
  try {page=await bucket.list({prefix,limit:CATALOG_PAGE_SIZE,...(state?.cursor?{cursor:state.cursor}:{})});}
  catch(error) {
    // An expired continuation must not require an operator to clear storage.
    // After three consecutive listing failures restart a cycle, retaining all
    // evidence and the last completed watermark. A conflict never overwrites
    // a concurrent successful continuation.
    if(state) {
      const failures=state.failures+1;
      await bind(db,`UPDATE one_clock_catalog_state SET generation=?,failures=?,cursor=?,updated_at=? WHERE source=?`,
        state.generation+1,failures,failures>=3?null:state.cursor,at,id).run();
    }
    throw error;
  }
  if(!Array.isArray(page.objects) || page.objects.length>CATALOG_PAGE_SIZE ||
     (page.truncated && (typeof page.cursor!=='string' || !page.cursor || page.cursor===state?.cursor)))throw new Error('invalid catalog page');
  const entries=[];
  for(const object of page.objects) {
    if(typeof object.key!=='string' || !object.key.startsWith(prefix))throw new Error('catalog prefix mismatch');
    const identity=classify(object.key);if(!identity)continue;
    if(typeof identity.group!=='string' || !identity.group || !['started','finished','failed'].includes(identity.kind))throw new Error('invalid publication identity');
    const uploaded=instant(object.uploaded?.toISOString());
    if(uploaded<0)throw new Error('invalid catalog epoch');
    if(!Number.isSafeInteger(object.size) || object.size<0 || typeof object.etag!=='string' || !object.etag)throw new Error('invalid catalog metadata');
    entries.push({key:object.key,group:identity.group,kind:identity.kind,uploaded,size:object.size,etag:object.etag});
  }
  const generation=(state?.generation??0)+1,complete=page.truncated?state?.complete_through??null:start;
  const affected=[...new Set(entries.map(row=>row.group))],byGroup=new Map(affected.map(group=>[group,new Map()]));
  if(affected.length) {
    const {results}=await bind(db,`SELECT object_key,record_group,record_kind,uploaded_at FROM one_clock_catalog_entries
      WHERE source=? AND record_group IN (SELECT value FROM json_each(?))`,id,JSON.stringify(affected)).all();
    for(const row of results)byGroup.get(row.record_group).set(row.object_key,{kind:row.record_kind,uploaded:row.uploaded_at});
    for(const row of entries)byGroup.get(row.group).set(row.key,row);
  }
  const statements=[bind(db,`INSERT INTO one_clock_catalog_state(source,source_version,generation,cursor,cycle_started_at,complete_through,updated_at)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation,cursor=excluded.cursor,
    cycle_started_at=excluded.cycle_started_at,complete_through=excluded.complete_through,updated_at=excluded.updated_at,failures=0`,
    id,version,generation,page.truncated?page.cursor:null,start,complete,at)];
  for(const row of entries)statements.push(bind(db,`INSERT INTO one_clock_catalog_entries(source,object_key,record_group,record_kind,uploaded_at,size,etag)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,object_key) DO UPDATE SET record_group=excluded.record_group,record_kind=excluded.record_kind,
    uploaded_at=excluded.uploaded_at,size=excluded.size,etag=excluded.etag
    WHERE uploaded_at<>excluded.uploaded_at OR etag<>excluded.etag OR size<>excluded.size`,
    id,row.key,row.group,row.kind,row.uploaded,row.size,row.etag));
  for(const [group,records] of byGroup) {
    if(records.size>3 || new Set([...records.values()].map(r=>r.kind)).size!==records.size)throw new Error('ambiguous publication group');
    statements.push(bind(db,'DELETE FROM one_clock_catalog_gaps WHERE source=? AND record_group=?',id,group));
    statements.push(bind(db,`INSERT INTO one_clock_catalog_gaps(source,bucket,record_group)
      SELECT ?,value,? FROM json_each(?)`,id,group,JSON.stringify(attemptGaps([...records.values()]))));
  }
  await db.batch(statements);
  return {source:id,generation,indexed:entries.length,cycleComplete:!page.truncated,completeThrough:complete};
}

// Both branches stop after a bounded index range. No DISTINCT/ORDER BY over
// lifetime history: de-duplication happens only after bounded reads.
export async function publicationCatalogPage(db,descriptor,{start,cutoff,maxGroups=20}) {
  const state=await bind(db,'SELECT * FROM one_clock_catalog_state WHERE source=?',descriptor.id).first();
  if(!state || state.source_version!==descriptor.version || state.complete_through===null || state.complete_through<cutoff)
    return {objects:[],truncated:true,cursor:null,reason:'catalog-catching-up',completeThrough:state?.complete_through??null};
  const buckets=pointBuckets(cutoff);
  const {results:recent}=await bind(db,`SELECT record_group FROM one_clock_catalog_entries INDEXED BY one_clock_catalog_time
    WHERE source=? AND uploaded_at>=? AND uploaded_at<=? LIMIT ?`,descriptor.id,start,cutoff,maxGroups*3+1).all();
  if(recent.length>maxGroups*3)return {objects:[],truncated:true,cursor:null,reason:'catalog-record-bound',completeThrough:state.complete_through};
  const {results:gaps}=await bind(db,`SELECT record_group FROM one_clock_catalog_gaps
    WHERE source=? AND bucket IN (${buckets.map(()=>'?').join(',')}) LIMIT ?`,descriptor.id,...buckets,maxGroups+1).all();
  const groups=[...new Set([...recent,...gaps].map(row=>row.record_group))].map(record_group=>({record_group}));
  if(groups.length>maxGroups)return {objects:[],truncated:true,cursor:null,reason:'catalog-group-bound',completeThrough:state.complete_through};
  if(!groups.length)return {objects:[],truncated:false,completeThrough:state.complete_through};
  const placeholders=groups.map(()=>'?').join(',');
  const {results}=await bind(db,`SELECT object_key,uploaded_at,size,etag FROM one_clock_catalog_entries WHERE source=? AND uploaded_at<=?
    AND record_group IN (${placeholders}) ORDER BY object_key LIMIT ?`,descriptor.id,cutoff,...groups.map(g=>g.record_group),maxGroups*3+1).all();
  if(results.length>maxGroups*3)return {objects:[],truncated:true,cursor:null,reason:'catalog-record-bound',completeThrough:state.complete_through};
  return {objects:results.map(r=>({key:r.object_key,uploaded:new Date(r.uploaded_at),size:r.size,etag:r.etag})),truncated:false,completeThrough:state.complete_through};
}
