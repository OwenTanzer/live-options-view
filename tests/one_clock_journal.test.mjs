import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { makeStorage } from './helpers/d1_shim.mjs';
import { seedJournalSession } from './helpers/journal-session.mjs';
import { journalSnapshots,projectJournalPosition,journalCutoff } from '../backend/one-clock-journal.js';
import { journalDay,handleTradeJournalDay,handleTradeJournalRow } from '../backend/trade-journal.js';
import { handleTradeJournalContext } from '../backend/trade-journal-context.js';
import { sessionInstant } from '../site/shared/one-clock-session.js';
import { createClock,createSourceRegistry } from '../site/shared/one-clock.js';
import { journalSource } from '../site/shared/one-clock-journal-source.js';
const day='2026-11-09',T=sessionInstant(day,10),id='pos:0123456789abcdef',page='a0000000-0000-4000-8000-000000000001';
const instrument={underlying:'QQQ',instrument_type:'option',multiplier:100,occ_symbol:'QQQ261109C00600000',strike:600,option_type:'call'};
const fill=(side,qty,price,at,fee=.5)=>({execution_id:`${side}:${at}`,instrument,side,qty,price,fee,fee_basis:'estimated',ts:new Date(at).toISOString(),ts_kind:'execution_alert',ts_note:'later exit and future fee review'});
const position=(executions)=>({position_id:id,journal_page_id:page,supported:true,contract:instrument,executions,report:'future retrospective',gaps:['future close'],row_checks:{flat:true},closed:true,net_pnl:999});
async function setup(t){const db=makeStorage();t.after(()=>db.db.close());for(const migration of ['0003_trade_journal.sql','0007_one_clock_journal.sql'])await db.exec(fs.readFileSync(new URL('../backend/migrations/'+migration,import.meta.url),'utf8'));return db;}
async function record(db,p,at,revision=1,status='active') {
  await db.prepare('INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at) VALUES(?,?,?,?,?,?,?)')
    .bind(page,id,status,revision,'synthetic:'+revision,JSON.stringify(p),at).run();
}
test('revision payload selection precedes fill filtering, totals and metadata responses',async t=>{
  const db=await setup(t),first=position([fill('buy',10,2,T-1000),fill('sell',4,3,T+1000)]);
  await record(db,first,T);
  const correction=structuredClone(first);correction.executions[0].fee=9;
  correction.executions.push(fill('sell',6,4,T+2000));await record(db,correction,T+3000,2);
  assert.deepEqual(await journalSnapshots(db,{cutoff:T-1}),[]);
  let result=await journalDay(db,day,T);
  assert.equal(result.positions[0].open_qty,10);assert.equal(result.totals.closed_positions,0);assert.equal(result.totals.open_fees_paid,.5);
  assert.equal(result.sync,null);assert.equal(result.positions[0].report,undefined);assert.equal(result.positions[0].executions[0].ts_note,undefined);
  result=await journalDay(db,day,T+1500);assert.equal(result.positions[0].open_qty,6);assert.equal(result.positions[0].realized_gross_pnl,400);
  result=await journalDay(db,day,T+3000);assert.equal(result.positions[0].closed,true);assert.equal(result.positions[0].fees,10);
  assert.equal(result.totals.net,1590);
  await record(db,correction,T+4000,2,'removed');
  assert.equal((await journalDay(db,day,T+4000)).totals.excluded_not_active,1);
  assert.equal((await journalDay(db,day,T)).positions[0].revision,1);
});
test('retained history is automatic and unchanged refreshes do not invent revisions',async t=>{
  const db=await setup(t),p=position([fill('buy',1,2,T)]);
  const before=Date.now();
  await db.prepare(`INSERT INTO trade_journal_rows VALUES(?,?,'active',1,?,?,?,1,?,'[]',?,NULL)`)
    .bind(page,id,new Date(T).toISOString(),new Date(T).toISOString(),'one',JSON.stringify(p),new Date(T).toISOString()).run();
  let rows=db.raw('SELECT * FROM trade_journal_history');assert.equal(rows.length,1);assert.ok(rows[0].recorded_at>=before-1);
  assert.deepEqual(await journalSnapshots(db,{cutoff:T+5000}),[],'backdated fills do not backdate availability');
  await db.prepare('UPDATE trade_journal_rows SET body_fetched_at=? WHERE page_id=?').bind(new Date().toISOString(),page).run();
  assert.equal(db.raw('SELECT count(*) n FROM trade_journal_history')[0].n,1);
  await db.prepare("UPDATE trade_journal_rows SET source_status='archived' WHERE page_id=?").bind(page).run();
  await db.prepare('DELETE FROM trade_journal_rows WHERE page_id=?').bind(page).run();
  assert.deepEqual(db.raw('SELECT source_status FROM trade_journal_history ORDER BY snapshot_id').map(r=>r.source_status),['active','archived','removed']);
});
test('combinations filter each leg, and expiration has no inferred intraday timestamp',()=>{
  const a=position([fill('buy',2,3,T),fill('sell',2,4,T+1000)]),b=position([fill('buy',2,1,T+2000)]);
  const projected=projectJournalPosition({...a,legs:[a,b]},T);
  assert.equal(projected.legs.length,1);assert.equal(projected.executions.length,1);assert.equal(projected.closed,false);
  const expiry={...a,executions:a.executions.slice(0,1),expired_on:day};
  assert.equal(projectJournalPosition(expiry,T).open_qty,2);
  assert.equal(projectJournalPosition(expiry,sessionInstant('2026-11-10',0)).gross_pnl,-600);
});
test('private routes authorize before any history, cache or archive read',async()=>{
  const env={DB:{prepare(){throw new Error('unauthenticated storage read');}}};
  for(const [route,path] of [[handleTradeJournalDay,`?date=${day}`],[handleTradeJournalRow,`?page_id=${page}`],[handleTradeJournalContext,`?position_id=${id}&date=${day}`]]) {
    const url=new URL('https://example.test/api/trade-journal'+path+'&cutoff='+T);
    const response=await route(new Request(url),env,url);assert.equal(response.status,401);
  }
});
test('private context uses eligible revision before planning and never consults retrospective cache',async t=>{
  const db=await setup(t);await seedJournalSession(db,'test');await record(db,position([fill('buy',1,2,T)]),T);
  const env={DB:db,PAPER_TRADES:{list:async()=>({objects:[],truncated:false}),get(){throw new Error('no candidate body');}}};
  const url=new URL(`https://example.test/api/trade-journal/context?position_id=${id}&date=${day}&cutoff=${T}`);
  const response=await handleTradeJournalContext(new Request(url,{headers:{Cookie:'__Host-journal=test'}}),env,url);
  assert.equal(response.status,200);const body=await response.json();assert.equal(body.view,'recorded-as-of');assert.equal(body.option,null);
  assert.ok(!db.queries.some(q=>/FROM trade_journal_context|INTO trade_journal_context/.test(q)));
});
test('cutoff validation rejects duplicates, seconds, unknown dates and future values',()=>{
  for(const query of ['cutoff=1','cutoff=123&cutoff=456',`cutoff=${T+60001}`])assert.throws(()=>journalCutoff(new URL('https://x/?'+query),T));
});
test('private adapter uses the shared clock cancellation and scope checks',async()=>{
  const registry=createSourceRegistry();let resolve;
  registry.register(journalSource({fetchImpl:()=>new Promise(r=>resolve=r)}));
  const initial={mode:'replay',origin:'recorded',instrument:'JOURNAL',session:day,cutoff:T};
  const clock=createClock({registry,initial}),pending=clock.read('private-journal');await Promise.resolve();
  clock.select({...initial,cutoff:T+1});
  resolve({ok:true,json:async()=>({view:'recorded-as-of',date:day,cutoff:T,positions:[]})});
  assert.equal((await pending).discarded,true);
});
