import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {ingestPublicSource,ingestedAsOf} from '../backend/one-clock-ingestion.js';
import {chainIngestion} from '../backend/one-clock-ingestion-sources.js';
import {sessionInstant} from '../site/shared/one-clock-session.js';
import {selectAsOf} from '../site/shared/one-clock.js';
const session='2026-11-09',T=sessionInstant(session,9,30);
async function setup(t){const db=makeStorage();t.after(()=>db.db.close());for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])await db.exec(fs.readFileSync(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));return db;}
function bucket(){
  const records=new Map();let gets=0;
  return {records,get gets(){return gets;},
    add(minute,spot=700,etag='1',published=T+minute*60000+1000) {
      const at=T+minute*60000,d=new Date(at-5*3600000),key='intraday/20261109/snapshot_'+d.toISOString().slice(11,19).replaceAll(':','')+'000000.csv';
      const csv=`Strike,Type,OpenInterest,Expiration,UnderlyingPrice\n700,call,100,${session},${spot}\n700,put,50,${session},${spot}`;
      const meta={schema_version:'snapshot_provenance.v1',snapshot_key:key,symbol:'QQQ',constructed_at:new Date(at).toISOString(),latest_published_at:new Date(published).toISOString(),publication:{status:'published'}};
      records.set(key,{text:csv,etag:'csv'+etag,uploaded:published});records.set(key.replace('.csv','.provenance.json'),{text:JSON.stringify(meta),etag:'meta'+etag,uploaded:published});return key;
    },
    async list({prefix}){return {objects:[...records].filter(([key])=>key.startsWith(prefix)).map(([key,r])=>({key,etag:r.etag,uploaded:new Date(r.uploaded)})),truncated:false};},
    async get(key){gets++;const r=records.get(key);return r?{size:Buffer.byteLength(r.text),etag:r.etag,uploaded:new Date(r.uploaded),text:async()=>r.text}:null;}
  };
}
const run=(db,b,now=T+3600000)=>ingestPublicSource(db,b,chainIngestion,{session,instrument:'QQQ',now});

test('ingests intermediate publications, bounds bodies, resumes and deduplicates',async t=>{
  const db=await setup(t),b=bucket();for(let i=0;i<35;i++)b.add(i,700+i%2);
  const first=await run(db,b);assert.equal(first.processed,20);assert.equal(first.pending,15);assert.equal(first.gets,40);
  const second=await run(db,b,T+3600001);assert.equal(second.processed,15);assert.equal(second.pending,0);
  assert.equal((await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:T+3600001})).length,70);
  assert.equal((await run(db,b,T+3600002)).gets,0);
  assert.equal(first.final,false);assert.equal(first.reason,'catching-up');
});

test('late earlier keys and independently overwritten CSV revisions are retained without backdating receipt',async t=>{
  const db=await setup(t),b=bucket();const key=b.add(5);await run(db,b);
  b.add(1,701);b.records.get(key).etag='csv2';b.records.get(key).text=b.records.get(key).text.replaceAll(',700\n',',702\n');b.records.get(key).uploaded=T+3600500;
  const next=await run(db,b,T+3601000);assert.equal(next.processed,2);
  const before=await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:T+3600000});assert.equal(before.length,2);
  const after=await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:T+3601000});assert.equal(after.length,6);
  assert.ok(after.filter(r=>r.received_at>T+3600000).every(r=>r.received_at===T+3601000));
  const corrected=selectAsOf(after.map(r=>JSON.parse(r.payload)),{session,instrument:'QQQ',origin:'recorded',cutoff:T+3601000});
  assert.equal(corrected.filter(r=>r.source==='price'&&r.id===key).length,1);
  assert.equal(corrected.find(r=>r.source==='price'&&r.id===key).revision,2);
  // Restoring old content with its old ETag is still a later retained revision.
  b.records.get(key).etag='csv1';b.records.get(key).uploaded=T+3601500;
  assert.equal((await run(db,b,T+3602000)).processed,1);
  assert.equal((await db.prepare('SELECT MAX(revision) AS n FROM one_clock_ingested WHERE record_id=?').bind(key).first()).n,3);
});

test('atomic failure and concurrent workers cannot advance partial ingestion',async t=>{
  const db=await setup(t),b=bucket();b.add(0);db.flags.failAfterRuns=2;
  await assert.rejects(run(db,b),/simulated/);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_ingestion_work')[0].n,0);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_ingested')[0].n,0);
  db.flags.failAfterRuns=null;
  const results=await Promise.allSettled([run(db,b),run(db,b)]);
  assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_ingested')[0].n,2);
});

test('incomplete metadata index never samples itself as complete; unknown sources rejected',async t=>{
  const db=await setup(t),b=bucket();b.add(0);const list=b.list;b.list=async args=>({...await list(args),truncated:true,cursor:'next'});
  const result=await run(db,b);assert.equal(result.reason,'metadata-index-bound');assert.equal(result.gets,0);
  await assert.rejects(ingestPublicSource(db,b,chainIngestion,{session,instrument:'PRIVATE',now:T}),/scope/);
});

test('future publication retries and permanent malformed evidence remains an explicit gap',async t=>{
  const db=await setup(t),b=bucket();b.add(0,700,'1',T+3601000);
  assert.equal((await run(db,b)).pending,1);
  assert.equal((await run(db,b,T+3601000)).processed,1);
  const key=b.add(1);b.records.get(key).text='invalid csv';
  const result=await run(db,b,T+3601001);assert.equal(result.gaps,1);
  assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_ingested')[0].n,2);
});

test('changed object after metadata listing retries; transient transport failure never commits',async t=>{
  const db=await setup(t),b=bucket();const key=b.add(0),get=b.get;
  b.get=async name=>{const obj=await get(name);if(name===key)obj.etag='changed';return obj;};
  assert.equal((await run(db,b)).pending,1);assert.equal(db.raw('SELECT COUNT(*) AS n FROM one_clock_ingested')[0].n,0);
  b.get=async()=>{throw new Error('transport failed');};
  await assert.rejects(run(db,b,T+3600001),/transport failed/);
  assert.equal(db.raw('SELECT generation FROM one_clock_ingestion_work')[0].generation,1);
});

test('body budget commits complete artifacts only and resumes remaining work',async t=>{
  const db=await setup(t),b=bucket();
  for(let i=0;i<10;i++) {
    const key=b.add(i),r=b.records.get(key),lines=r.text.split('\n');
    lines[0]+=',PRIVATE';lines[1]+=','+'x'.repeat(900000);lines[2]+=',';r.text=lines.join('\n');
  }
  const first=await run(db,b);assert.equal(first.processed,4);assert.equal(first.pending,6);assert.ok(first.bytes<=4*1024*1024);
  const second=await run(db,b,T+3600001);assert.equal(second.processed,4);assert.equal(second.pending,2);
  const third=await run(db,b,T+3600002);assert.equal(third.pending,0);
  const records=await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:T+3600002});
  assert.equal(records.length,20);assert.ok(records.every(r=>!r.payload.includes('xxxx')));
  assert.ok(records.every(r=>r.collection==='backfill'));
});

test('calibrated catch-up drains overdue minutes at production-sized body weights and resumes at its bound',async t=>{
  const {levelIngestion}=await import('../backend/one-clock-level-ingestion.js');
  const db=await setup(t);
  const artifacts=Array.from({length:110},(_,i)=>({key:String(i),version:'1',eventAt:T+i*60000,availableAt:T+i*60000+1000}));
  const adapter={...levelIngestion,discover:async()=>({complete:true,artifacts:[...artifacts].reverse()}),
    async read(bucket,scope,a){
      await Promise.all(['csv','json'].map(ext=>bucket.get(a.key+'.'+ext)));
      return {rows:[{source:'level-spot',id:a.key,revision:1,session,instrument:'QQQ',origin:'recorded',
        eventAt:a.eventAt,availableAt:a.availableAt,evidence:a.key,value:{spot:650}}]};
    }};
  const body='x'.repeat(220*1024),bucket={get:async()=>({size:body.length,text:async()=>body})};
  const go=now=>ingestPublicSource(db,bucket,adapter,{session,instrument:'QQQ',now});
  const now=T+120*60000;
  const first=await go(now);
  assert.equal(first.processed,Math.floor(adapter.orderedBudget.bytes/(2*body.length)));
  assert.equal(first.through,artifacts[first.processed].availableAt-1);
  assert.ok(first.bytes<=adapter.orderedBudget.bytes);
  assert.equal((await go(now+1)).pending,0);
  assert.equal((await go(now+2)).gets,0);
  assert.equal((await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:now-1})).length,0);
  assert.equal((await ingestedAsOf(db,{session,instrument:'QQQ',cutoff:now+1})).length,artifacts.length);
});
