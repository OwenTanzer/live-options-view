import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {publicationDigest,publicationKey,decodePublication,publicationStream} from '../backend/one-clock-publication-contract.js';
import {ingestPublicationStream,ingestRegisteredPublications,publicationRowsPage} from '../backend/one-clock-publications.js';
import {selectAsOf} from '../site/shared/one-clock.js';
const T=Date.parse('2026-11-20T15:00:00Z'),day='2026-11-20';
const descriptor={id:'fixture',owner:'fixture-owner',audience:'public',sessionBasis:'new-york',instruments:['QQQ'],outputs:{price:{price:'number',label:'string',qualified:'boolean'}}};
const input=(id='tick',revision=1,value=700)=>({source:'price',id,revision,session:day,eventAt:T,value:value===null?null:{price:value,label:'quote',qualified:true}});
async function setup(t){const db=makeStorage();t.after(()=>db.db.close());await db.exec(fs.readFileSync(new URL('../backend/migrations/0011_one_clock_publications.sql',import.meta.url),'utf8'));return db;}
function bucket(){
  const objects=new Map(),heads=new Map();let gets=0;
  return {objects,get gets(){return gets;},async add(records,{instrument='QQQ',uploaded=T+1000,mutate=x=>x}={}) {
    const stream='fixture/'+instrument,prior=heads.get(stream),sequence=(prior?.sequence??0)+1;
    const entry=mutate({schema:'one-clock.publication.v1',stream,sequence,publicationId:crypto.randomUUID(),owner:descriptor.owner,previousDigest:prior?.digest??null,createdAt:new Date(uploaded-1).toISOString(),records,evidence:[{key:`publications/v1/${stream}/blobs/${'a'.repeat(64)}`,sha256:'a'.repeat(64),bytes:100,contentType:'text/csv',schema:'fixture.v1'}]});
    const bytes=new TextEncoder().encode(JSON.stringify(entry)),key=publicationKey(stream,sequence);
    objects.set(key,{bytes,uploaded});heads.set(stream,{sequence,digest:await publicationDigest(bytes)});return key;
  },async get(key){gets++;const o=objects.get(key);return o?{size:o.bytes.byteLength,uploaded:new Date(o.uploaded),arrayBuffer:async()=>o.bytes.buffer.slice(o.bytes.byteOffset,o.bytes.byteOffset+o.bytes.byteLength)}:null;}};
}
const run=(db,b,options={})=>ingestPublicationStream(db,b,descriptor,{instrument:'QQQ',now:T+100000,...options});
const page=(db,options={})=>publicationRowsPage(db,descriptor,{instrument:'QQQ',session:day,cutoff:T+100000,...options});

test('bounded sequential continuation retains every intermediate commit and does not need head hints or listings',async t=>{
  const db=await setup(t),b=bucket();for(let i=0;i<135;i++)await b.add([input('tick'+i)]);
  const a=await run(db,b);assert.equal(a.processed,64);assert.equal(a.reason,'budget');assert.equal(a.nextSequence,65);
  assert.equal((await run(db,b)).processed,64);assert.equal((await run(db,b)).processed,7);
  assert.equal((await run(db,b)).reason,'tail-at-check');assert.equal(db.raw('SELECT COUNT(*) n FROM one_clock_publication_entries')[0].n,135);
  let cursor=null,rows=[];do{const p=await page(db,{after:cursor,limit:37});rows.push(...p.rows);cursor=p.next;}while(cursor);
  assert.equal(rows.length,135);assert.equal(new Set(rows.map(r=>r.row.id)).size,135);
  assert.equal((await page(db,{cutoff:T+99999})).rows.length,0); // observer receipt gate
  assert.equal(rows[0].row.availableAt,T+1000);assert.equal(rows[0].receivedAt,T+100000);
});

test('corrections to old sessions and retractions append to current log without losing rewind',async t=>{
  const db=await setup(t),b=bucket();await b.add([input()]);await run(db,b);
  await b.add([input('tick',2,701)],{uploaded:T+200000});await run(db,b,{now:T+300000});
  await b.add([input('tick',3,null)],{uploaded:T+400000});await run(db,b,{now:T+500000});
  for(const [cutoff,expected] of [[T+100000,700],[T+300000,701],[T+500000,null]]){
    const rows=(await page(db,{cutoff})).rows.map(r=>r.row),selected=selectAsOf(rows,{session:day,instrument:'QQQ',origin:'recorded',cutoff});
    assert.equal(selected.length,1);assert.equal(selected[0].value?.price??null,expected);
  }
});

test('corrupt chain stops at entry and commits only validated prefix; repaired read resumes',async t=>{
  const db=await setup(t),b=bucket();await b.add([input('one')]);const key=await b.add([input('two')]);
  const original=b.objects.get(key).bytes;b.objects.get(key).bytes=new TextEncoder().encode('{}');
  const first=await run(db,b);assert.equal(first.processed,1);assert.equal(first.reason,'blocked-entry');assert.equal(first.nextSequence,2);
  b.objects.get(key).bytes=original;assert.equal((await run(db,b)).processed,1);assert.equal((await page(db)).rows.length,2);
});

test('future upload, size mismatch and transport errors never advance past unread evidence',async t=>{
  const db=await setup(t),b=bucket();const key=await b.add([input()],{uploaded:T+200000});
  assert.equal((await run(db,b)).reason,'future-publication');assert.equal((await page(db)).rows.length,0);
  b.objects.get(key).uploaded=T+1000;const original=b.get;b.get=async key=>({...await original(key),size:999});
  assert.equal((await run(db,b)).reason,'blocked-entry');
  b.get=async()=>{throw new Error('private transport detail');};const result=await run(db,b);assert.equal(result.reason,'blocked-entry');assert.ok(!JSON.stringify(result).includes('private transport'));
  b.get=original;b.objects.get(key).uploaded=T+200000;assert.equal((await run(db,b,{now:T+300000})).processed,1);
});

test('D1 rollback and stale concurrent consumer leave exactly one checkpoint and delivery',async t=>{
  const db=await setup(t),b=bucket();await b.add([input()]);db.flags.failAfterRuns=3;
  await assert.rejects(run(db,b),/simulated/);assert.equal(db.raw('SELECT COUNT(*) n FROM one_clock_publication_work')[0].n,0);assert.equal(db.raw('SELECT COUNT(*) n FROM one_clock_publication_entries')[0].n,0);
  db.flags.failAfterRuns=null;const outcomes=await Promise.allSettled([run(db,b),run(db,b)]);
  assert.equal(outcomes.filter(r=>r.status==='fulfilled').length,1);assert.equal((await page(db)).rows.length,1);
  await assert.rejects(run(db,b,{now:T+99999}),/regression/);
});

test('duplicate publication ID, nonmonotonic revisions and moved identity fail atomic commit',async t=>{
  const db=await setup(t),b=bucket();const key=await b.add([input()]);await run(db,b);
  const id=JSON.parse(new TextDecoder().decode(b.objects.get(key).bytes)).publicationId;
  const second=await b.add([input('tick',2)],{mutate:entry=>({...entry,publicationId:id})});
  await assert.rejects(run(db,b),/UNIQUE/);assert.equal(db.raw('SELECT next_sequence n FROM one_clock_publication_work')[0].n,2);
  const entry=JSON.parse(new TextDecoder().decode(b.objects.get(second).bytes));entry.publicationId=crypto.randomUUID();entry.records[0].revision=1;b.objects.get(second).bytes=new TextEncoder().encode(JSON.stringify(entry));
  await assert.rejects(run(db,b),/revision conflict/);
  entry.records[0].revision=2;entry.records[0].session='2026-11-19';entry.records[0].eventAt=T-86400000;b.objects.get(second).bytes=new TextEncoder().encode(JSON.stringify(entry));
  await assert.rejects(run(db,b),/revision conflict/);assert.equal((await page(db)).rows.length,1);
});

test('shared budget gives all instruments service and isolates a failed transaction',async t=>{
  const db=await setup(t),b=bucket(),d={...descriptor,instruments:['QQQ','SPY','IBIT']};
  for(const instrument of d.instruments)for(let i=0;i<70;i++)await b.add([input('tick'+i)],{instrument});
  const results=await ingestRegisteredPublications(db,b,[d],{now:T+100000});assert.deepEqual(results.map(r=>r.processed),[64,64,64]);assert.ok(b.gets<=192);assert.ok(results.reduce((sum,r)=>sum+r.bytes,0)<=2*1024*1024);
  const next=await ingestRegisteredPublications(db,b,[d],{now:T+100001});assert.deepEqual(next.map(r=>r.processed),[6,6,6]);
  await assert.rejects(ingestRegisteredPublications(db,b,[d,d]),/duplicate/);
  await assert.rejects(run(db,b,{maxGets:193}),/budget/);
});

test('byte budget is reserved before download, and scoped cursor cannot cross cutoff or instrument',async t=>{
  const db=await setup(t),b=bucket();for(let i=0;i<3;i++)await b.add([input('tick'+i)]);
  const first=await run(db,b,{maxBytes:16384});assert.equal(first.processed,1);assert.equal(first.gets,1);
  await run(db,b);const p=await page(db,{limit:1});assert.ok(p.next);
  await assert.rejects(page(db,{after:p.next,cutoff:T+100001}),/cursor/);
  await assert.rejects(publicationRowsPage(db,descriptor,{instrument:'SPY',session:day,cutoff:T+100000}),/unregistered/);
});

test('strict publication contract rejects private/unregistered outputs, wrong evidence scopes and future event time',async()=>{
  const b=bucket(),key=await b.add([input()]);const base=JSON.parse(new TextDecoder().decode(b.objects.get(key).bytes));
  const decode=entry=>decodePublication(new TextEncoder().encode(JSON.stringify(entry)),{descriptor,instrument:'QQQ',sequence:1,previousDigest:null,uploaded:T+1000,cutoff:T+2000});
  for(const mutate of [e=>{e.records[0].source='constructor';},e=>{e.records[0].value.secret='x';},e=>{e.records[0].eventAt=T+1001;},e=>{e.evidence[0].key='private/secret';},e=>{e.owner='other';},e=>{e.previousDigest='a'.repeat(64);},e=>{e.records.push(e.records[0]);},e=>{e.records[0].value.price='700';},e=>{e.records[0].revision=0;}]){
    const e=structuredClone(base);mutate(e);await assert.rejects(decode(e));
  }
  assert.throws(()=>publicationStream({...descriptor,audience:'private'},'QQQ'),/unregistered/);
  assert.throws(()=>publicationStream({...descriptor,outputs:{price:{secret:'object'}}},'QQQ'),/field type/);
  assert.throws(()=>publicationKey('../private',1),/sequence/);
  assert.match((await decode(base)).rows[0].details.evidenceVerification,/not read/);
});

test('rewritten commit upload order is rejected, and missing next sequence is never a finality receipt',async t=>{
  const db=await setup(t),b=bucket();await b.add([input('one')],{uploaded:T+2000});const second=await b.add([input('two')],{uploaded:T+1000});
  const first=await run(db,b);assert.equal(first.processed,1);assert.equal(first.reason,'blocked-entry');
  b.objects.delete(second);const tail=await run(db,b);assert.equal(tail.reason,'tail-at-check');assert.equal(tail.nextSequence,2);assert.equal(tail.final,false);
});

test('large session pages use an index tuple seek without sorting; identity check reads only latest revision',async t=>{
  const db=await setup(t),b=bucket();
  for(let i=0;i<2050;i++)await b.add([input('tick'+i)]);
  while((await run(db,b,{now:T+100000})).reason==='budget'){}
  let after=null,total=0;do{const p=await page(db,{after});total+=p.rows.length;after=p.next;}while(after);
  assert.equal(total,2050);
  const query=db.queries.find(q=>q.startsWith('SELECT sequence,source,record_id,payload,received_at '));
  const plan=db.raw('EXPLAIN QUERY PLAN '+query,'fixture/QQQ',day,'QQQ',T+100000,T+100000,1000,'price','native:fixture/QQQ:tick999',101).map(r=>r.detail).join('\n');
  assert.match(plan,/SEARCH.*one_clock_publication_record_page/);assert.match(plan,/received_at/);assert.doesNotMatch(plan,/TEMP B-TREE|SCAN one_clock_publication_records/);
  const latest=`SELECT session,instrument,revision FROM one_clock_publication_records WHERE stream=? AND source=? AND record_id=? ORDER BY revision DESC LIMIT 1`;
  const identityPlan=db.raw('EXPLAIN QUERY PLAN '+latest,'fixture/QQQ','price','native:fixture/QQQ:tick0').map(r=>r.detail).join('\n');
  assert.match(identityPlan,/SEARCH.*stream=\? AND source=\? AND record_id=\?/);assert.doesNotMatch(identityPlan,/TEMP B-TREE|SCAN/);
  const trigger=db.raw("SELECT sql FROM sqlite_master WHERE name='one_clock_publication_record_identity'")[0].sql;
  assert.match(trigger,/ORDER BY revision DESC LIMIT 1/);
  assert.throws(()=>db.raw('UPDATE one_clock_publication_records SET received_at=available_at-1'),/immutable/);
  assert.throws(()=>db.raw('UPDATE one_clock_publication_entries SET payload=?','{}'),/immutable/);
});
