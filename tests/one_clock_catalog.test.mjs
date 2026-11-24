import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { makeStorage } from './helpers/d1_shim.mjs';
import { advanceSourceCatalog,publicationCatalogPage,CATALOG_PAGE_SIZE } from '../backend/one-clock-source-catalog.js';
import { squeezeCatalog,advanceOneClockCatalogs } from '../backend/one-clock-catalog-sources.js';
import { sessionInstant } from '../site/shared/one-clock-session.js';
import { readSqueezePublications } from '../backend/one-clock-squeeze.js';
const T=sessionInstant('2026-11-09',12),old=T-86400000,now=T+86400000;
async function setup(t) {const db=makeStorage();t.after(()=>db.db.close());await db.exec(fs.readFileSync(new URL('../backend/migrations/0005_one_clock_catalog.sql',import.meta.url),'utf8'));return db;}
function object(i,kind='started',at=T) {
  const run=`00000000-0000-4000-8000-${String(i).padStart(12,'0')}`;
  return {key:squeezeCatalog.prefix+run+'/11111111-1111-4111-8111-111111111111/'+kind+'.json',uploaded:new Date(at),size:100,etag:`${i}:${kind}`};
}
function bucket(objects) {
  const calls=[];return {calls,async list({prefix,limit,cursor}) {
    calls.push({prefix,limit,cursor});assert.equal(limit,CATALOG_PAGE_SIZE);
    const from=Number(cursor??0),next=from+limit;
    return {objects:objects.slice(from,next),truncated:next<objects.length,cursor:next<objects.length?String(next):undefined};
  },get(){throw new Error('catalog must not read bodies');},put(){throw new Error('catalog must not write R2');}};
}
test('resumable catalog exceeds lifetime 2000 keys while per-day query stays bounded',async t=>{
  const db=await setup(t),objects=[];
  for(let i=0;i<1001;i++)objects.push(object(i,'started',old),object(i,'finished',old+1000));
  objects.push(object(1001,'started'),object(1001,'finished',T+1000));
  const r2=bucket(objects);
  let result;
  do{result=await advanceSourceCatalog(db,r2,squeezeCatalog,{now});}while(!result.cycleComplete);
  assert.equal(r2.calls.length,21);assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,2004);
  const page=await publicationCatalogPage(db,squeezeCatalog,{start:sessionInstant('2026-11-09',0),cutoff:T+5000});
  assert.equal(page.truncated,false);assert.equal(page.objects.length,2);assert.equal(page.completeThrough,now);
  const dayPlan=db.raw('EXPLAIN QUERY PLAN SELECT record_group FROM one_clock_catalog_entries INDEXED BY one_clock_catalog_time WHERE source=? AND uploaded_at>=? AND uploaded_at<=? LIMIT ?',squeezeCatalog.id,T,T+5000,61);
  assert.match(dayPlan.map(r=>r.detail).join(' '),/SEARCH.*one_clock_catalog_time.*uploaded_at>.*uploaded_at</);
  const gapPlan=db.raw('EXPLAIN QUERY PLAN SELECT record_group FROM one_clock_catalog_gaps WHERE source=? AND bucket IN (?,?) LIMIT ?',squeezeCatalog.id,'0:0','1:0',21);
  assert.match(gapPlan.map(r=>r.detail).join(' '),/SEARCH.*source=\? AND bucket=\?/);
  assert.ok([...dayPlan,...gapPlan].every(r=>!r.detail.includes('SCAN')));
  // A restart uses the persisted state; completed cycles safely rescan for
  // new UUIDs that sort before the previous cursor, without duplicate entries.
  const next=await advanceSourceCatalog(db,r2,squeezeCatalog,{now:now+1000});assert.equal(next.cycleComplete,false);
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,2004);
});
test('catalog rescans write only added and removed gap buckets',async t=>{
  const db=await setup(t),objects=[object(1)],r2=bucket(objects);
  await db.exec(`CREATE TABLE gap_changes(op TEXT, bucket TEXT);
    CREATE TRIGGER gap_added AFTER INSERT ON one_clock_catalog_gaps BEGIN
      INSERT INTO gap_changes VALUES ('add',NEW.bucket); END;
    CREATE TRIGGER gap_removed AFTER DELETE ON one_clock_catalog_gaps BEGIN
      INSERT INTO gap_changes VALUES ('remove',OLD.bucket); END;`);
  const members=()=>db.raw('SELECT bucket FROM one_clock_catalog_gaps').map(row=>row.bucket);
  async function checkChanges(at) {
    const before=new Set(members());
    await db.exec('DELETE FROM gap_changes');
    await advanceSourceCatalog(db,r2,squeezeCatalog,{now:at});
    const after=new Set(members());
    const expected=[...[...before].filter(b=>!after.has(b)).map(b=>'remove '+b),
      ...[...after].filter(b=>!before.has(b)).map(b=>'add '+b)];
    const actual=db.raw('SELECT op,bucket FROM gap_changes').map(row=>row.op+' '+row.bucket);
    assert.deepEqual(actual.sort(),expected.sort());
    return actual.length;
  }
  assert.ok(await checkChanges(now)>0);
  const cleanup = db.queries.find(sql => sql.startsWith('DELETE FROM one_clock_catalog_gaps'));
  assert.ok(cleanup);
  const plan = db.raw('EXPLAIN QUERY PLAN ' + cleanup, squeezeCatalog.id, 'sample', '[]');
  assert.ok(plan.some(row => row.detail.includes('one_clock_catalog_gap_group (source=? AND record_group=?)')));
  assert.equal(await checkChanges(now+1),0);
  objects.push(object(1,'finished',T+1000));
  assert.ok(await checkChanges(now+2)>0);
  assert.equal(await checkChanges(now+3),0);
  objects[1]=object(1,'finished',T);
  assert.ok(await checkChanges(now+4)>0);
  assert.deepEqual(members(),[]);
});
test('old missing-start and reverse-order attempts retain direct-reader gaps',async t=>{
  for(const objects of [[object(1,'finished',old)], [object(1,'finished',old),object(1,'started',old+1)]]) {
    const db=await setup(t),r2=bucket(objects);await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
    const scope={session:'2026-11-09',instrument:'ABC',cutoff:T};
    const catalog=await publicationCatalogPage(db,squeezeCatalog,{start:sessionInstant(scope.session,0),cutoff:T});
    const direct=await readSqueezePublications({list:async()=>({objects,truncated:false})},scope);
    const indexed=await readSqueezePublications(r2,scope,{catalog});
    assert.equal(direct.coverage.reason,'unresolved-publication-attempt');
    assert.equal(indexed.coverage.reason,direct.coverage.reason);
  }
});
test('interval lookup agrees with direct qualification across randomized boundaries and late receipts',async t=>{
  const db=await setup(t);let seed=42;
  const random=()=>{seed=(seed*1664525+1013904223)>>>0;return seed;};
  const all=[];
  for(let i=0;i<16;i++)for(const kind of ['started','finished','failed'])if(random()%3)all.push(object(i,kind,old+random()%100));
  const selected=all.filter(o=>!o.key.endsWith('/finished.json')),r2=bucket(selected);
  await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
  const unresolved=objects=>{
    const groups=new Map();for(const o of objects){const identity=squeezeCatalog.classify(o.key),group=groups.get(identity.group)??{};group[identity.kind]=o.uploaded.getTime();groups.set(identity.group,group);}
    return [...groups].filter(([,a])=>a.started===undefined || a.finished===undefined || a.started>a.finished || a.failed!==undefined).map(([id])=>id).sort();
  };
  for(const late of [false,true]) {
    if(late){selected.push(...all.filter(o=>o.key.endsWith('/finished.json')));await advanceSourceCatalog(db,r2,squeezeCatalog,{now:now+1});}
    for(let offset=-1;offset<=101;offset++) {
      const cutoff=old+offset,catalog=await publicationCatalogPage(db,squeezeCatalog,{start:old+1000,cutoff});
      assert.equal(catalog.truncated,false);
      assert.deepEqual(unresolved(catalog.objects),unresolved(selected.filter(o=>o.uploaded.getTime()<=cutoff)),`offset ${offset}, late ${late}`);
    }
  }
});
test('catalog checkpoint and entries roll back atomically; retry does not skip a page',async t=>{
  const db=await setup(t),r2=bucket([object(1),object(1,'finished')]);db.flags.failAfterRuns=2;
  await assert.rejects(advanceSourceCatalog(db,r2,squeezeCatalog,{now}),/simulated D1/);
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_state')[0].n,0);
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,0);
  db.flags.failAfterRuns=null;await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,2);
});
test('unfinished indexing and a cursor beyond the completed watermark stay unavailable',async t=>{
  const db=await setup(t),r2=bucket(Array.from({length:101},(_,i)=>object(i)));
  await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
  let page=await publicationCatalogPage(db,squeezeCatalog,{start:old,cutoff:T});assert.equal(page.reason,'catalog-catching-up');
  await advanceSourceCatalog(db,r2,squeezeCatalog,{now:now+1000});
  page=await publicationCatalogPage(db,squeezeCatalog,{start:old,cutoff:now+1});assert.equal(page.reason,'catalog-catching-up');
});
test('earlier unresolved and failed attempts remain gaps in indexed queries',async t=>{
  const db=await setup(t),r2=bucket([object(1,'started',old),object(2,'started',old),object(2,'failed',old+1000),object(3),object(3,'finished')]);
  await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
  const page=await publicationCatalogPage(db,squeezeCatalog,{start:sessionInstant('2026-11-09',0),cutoff:T+5000});
  assert.equal(page.truncated,false);assert.equal(page.objects.length,5);
});
test('three failed continuation attempts automatically restart the scan without deleting evidence',async t=>{
  const db=await setup(t),objects=Array.from({length:101},(_,i)=>object(i)),r2=bucket(objects);
  await advanceSourceCatalog(db,r2,squeezeCatalog,{now});const list=r2.list;
  r2.list=async()=>{throw new Error('expired cursor');};
  for(let i=1;i<=3;i++)await assert.rejects(advanceSourceCatalog(db,r2,squeezeCatalog,{now:now+i}),/expired cursor/);
  assert.equal(db.raw('SELECT cursor FROM one_clock_catalog_state')[0].cursor,null);
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,100);
  r2.list=list;await advanceSourceCatalog(db,r2,squeezeCatalog,{now:now+4});
  assert.equal(r2.calls.at(-1).cursor,undefined);assert.equal(db.raw('SELECT failures FROM one_clock_catalog_state')[0].failures,0);
});
test('concurrent catalog advances reject the stale generation without losing entries',async t=>{
  const db=await setup(t),r2=bucket([object(1)]);let release;const gate=new Promise(resolve=>release=resolve),list=r2.list;
  r2.list=async args=>{await gate;return list(args);};
  const a=advanceSourceCatalog(db,r2,squeezeCatalog,{now}),b=advanceSourceCatalog(db,r2,squeezeCatalog,{now});await Promise.resolve();release();
  const results=await Promise.allSettled([a,b]);assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal(db.raw('SELECT generation FROM one_clock_catalog_state')[0].generation,1);
  assert.equal(db.raw('SELECT count(*) n FROM one_clock_catalog_entries')[0].n,1);
});
test('descriptor mismatch, bad metadata and rewind cannot silently alter catalog state',async t=>{
  const db=await setup(t),r2=bucket([object(1)]);await advanceSourceCatalog(db,r2,squeezeCatalog,{now});
  await assert.rejects(advanceSourceCatalog(db,r2,{...squeezeCatalog,version:'2'},{now}),/version mismatch/);
  await assert.rejects(advanceSourceCatalog(db,r2,squeezeCatalog,{now:now-1}),/rewind/);
  const bad=bucket([{...object(2),uploaded:null}]);await assert.rejects(advanceSourceCatalog(db,bad,squeezeCatalog,{now:now+1}));
  assert.equal(db.raw('SELECT generation FROM one_clock_catalog_state')[0].generation,1);
});
test('disabled catalog does not touch storage, and source failures do not escape the scheduled task',async t=>{
  const db=await setup(t);assert.deepEqual(await advanceOneClockCatalogs({}),[]);
  const results=await advanceOneClockCatalogs({ONE_CLOCK_CATALOG:'enabled',DB:db,PAPER_TRADES:{list:async()=>{throw new Error('outage');}}},{now});
  assert.deepEqual(results,[{source:squeezeCatalog.id,error:true}]);
});
