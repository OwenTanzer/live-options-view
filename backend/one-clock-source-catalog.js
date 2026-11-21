// Generic bounded metadata ingestion. Source owners still own capture and
// content verification. This catalog never reads object bodies or writes R2.
import { instant } from '../site/shared/one-clock.js';
const bind=(db,sql,...args)=>db.prepare(sql).bind(...args);
export const CATALOG_PAGE_SIZE=100;
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
    const uploaded=instant(object.uploaded?.toISOString());
    if(!Number.isSafeInteger(object.size) || object.size<0 || typeof object.etag!=='string' || !object.etag)throw new Error('invalid catalog metadata');
    entries.push({key:object.key,group:identity.group,kind:identity.kind,uploaded,size:object.size,etag:object.etag});
  }
  const generation=(state?.generation??0)+1,complete=page.truncated?state?.complete_through??null:start;
  const statements=[bind(db,`INSERT INTO one_clock_catalog_state(source,source_version,generation,cursor,cycle_started_at,complete_through,updated_at)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation,cursor=excluded.cursor,
    cycle_started_at=excluded.cycle_started_at,complete_through=excluded.complete_through,updated_at=excluded.updated_at,failures=0`,
    id,version,generation,page.truncated?page.cursor:null,start,complete,at)];
  for(const row of entries)statements.push(bind(db,`INSERT INTO one_clock_catalog_entries(source,object_key,record_group,record_kind,uploaded_at,size,etag)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,object_key) DO UPDATE SET record_group=excluded.record_group,record_kind=excluded.record_kind,
    uploaded_at=excluded.uploaded_at,size=excluded.size,etag=excluded.etag
    WHERE uploaded_at<>excluded.uploaded_at OR etag<>excluded.etag OR size<>excluded.size`,
    id,row.key,row.group,row.kind,row.uploaded,row.size,row.etag));
  await db.batch(statements);
  return {source:id,generation,indexed:entries.length,cycleComplete:!page.truncated,completeThrough:complete};
}

// Select only the day's groups plus earlier unresolved attempts. A day's
// query cost does not increase with the number of completed historical days.
export async function publicationCatalogPage(db,descriptor,{start,cutoff,maxGroups=20}) {
  const state=await bind(db,'SELECT * FROM one_clock_catalog_state WHERE source=?',descriptor.id).first();
  if(!state || state.source_version!==descriptor.version || state.complete_through===null || state.complete_through<cutoff)
    return {objects:[],truncated:true,cursor:null,reason:'catalog-catching-up',completeThrough:state?.complete_through??null};
  const {results:groups}=await bind(db,`SELECT DISTINCT record_group FROM one_clock_catalog_entries e WHERE source=? AND uploaded_at<=? AND (
    uploaded_at>=? OR (record_kind='started' AND NOT EXISTS(SELECT 1 FROM one_clock_catalog_entries f
      WHERE f.source=e.source AND f.record_group=e.record_group AND f.record_kind='finished' AND f.uploaded_at<=?))
    OR record_kind='failed') ORDER BY record_group LIMIT ?`,descriptor.id,cutoff,start,cutoff,maxGroups+1).all();
  if(groups.length>maxGroups)return {objects:[],truncated:true,cursor:null,reason:'catalog-group-bound',completeThrough:state.complete_through};
  if(!groups.length)return {objects:[],truncated:false,completeThrough:state.complete_through};
  const placeholders=groups.map(()=>'?').join(',');
  const {results}=await bind(db,`SELECT object_key,uploaded_at,size,etag FROM one_clock_catalog_entries WHERE source=? AND uploaded_at<=?
    AND record_group IN (${placeholders}) ORDER BY object_key LIMIT ?`,descriptor.id,cutoff,...groups.map(g=>g.record_group),maxGroups*3+1).all();
  if(results.length>maxGroups*3)return {objects:[],truncated:true,cursor:null,reason:'catalog-record-bound',completeThrough:state.complete_through};
  return {objects:results.map(r=>({key:r.object_key,uploaded:new Date(r.uploaded_at),size:r.size,etag:r.etag})),truncated:false,completeThrough:state.complete_through};
}
