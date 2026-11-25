import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {makeStorage} from './helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResearch} from '../backend/one-clock-research.js';
import {journalDefinition} from '../site/shared/one-clock-owen-trades.js';

test('owner saves a historical fill rule and measures only the visible journal revision and captured endpoints',async t=>{
  const DB=makeStorage();t.after(()=>DB.db.close());
  for(const file of ['0003_trade_journal','0004_one_clock','0009_one_clock_journal','0010_one_clock_ingestion']){
    await DB.exec(await readFile(new URL('../backend/migrations/'+file+'.sql',import.meta.url),'utf8'));
  }
  const env={DB,ONE_CLOCK_OBSERVER:'enabled'};
  const store=createStorage(env);
  await store.users.insert('tester',{username:'tester',version:0});
  await store.sessions.create('login','tester',3600);
  for(const member of ['Owen','Jayden']) {
    const hash=createHash('sha256').update(member).digest('hex');
    await DB.prepare('INSERT INTO trade_journal_members(member,credential_id) VALUES(?,?)').bind(member,member).run();
    await DB.prepare("INSERT INTO trade_journal_sessions VALUES(?,?,?,unixepoch()+3600)").bind(hash,member,member).run();
  }
  const origin='https://options.moopertonic.net';
  const call=(member,body)=>handleOneClockResearch(new Request(origin+'/api/one-clock/research',{method:'POST',
    headers:{Origin:origin,Cookie:'session=login; __Host-journal='+member,'Content-Type':'application/json'},body:JSON.stringify(body)}),env);
  const rule={id:'private-study',name:'Owen Trades',version:1,...journalDefinition('QQQ',[{
    id:'5m',source:'price',field:'spot',horizonMs:300000,toleranceMs:90000,maxAgeMs:90000,measure:'return'}])};
  for(const member of ['','Jayden'])assert.equal((await call(member,{action:'save',rule})).status,403);
  assert.equal((await call('Owen',{action:'save',rule})).status,201);
  const session='2026-11-13',start=Date.parse(session+'T15:12:05Z'),recorded=start+86400000;
  const body={action:'replay-journal',session,ruleId:rule.id,version:1,cutoff:recorded};
  const contract={instrument_type:'option',underlying:'QQQ',occ_symbol:'FAKE',multiplier:100};
  const position={supported:true,actor_kind:'human',owner:'Owen',mode:'live',position_id:'pos:0123456789abcdef',
    executions:[{execution_id:'sensitive-id',ts:new Date(start).toISOString(),ts_kind:'execution-alert',side:'buy',qty:1,price:1,fee:0,instrument:contract}]};
  async function insert(p,when,status='active'){
    await DB.prepare('INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at) VALUES(?,?,?,1,?,?,?)')
      .bind(p.position_id,p.position_id,status,'h',JSON.stringify(p),when).run();
  }
  await insert(position,recorded);
  await insert({...position,position_id:'bot',actor_kind:'bot',mode:'paper'},recorded);
  const objects=new Map(['101150','101200','101750','101800'].map((h,i)=>{
    const key='intraday/20261113/snapshot_'+h+'000000.csv',value=i===0?100:i===3?101:0,
      csv='Symbol,OptionSymbol,Bid,Ask,UnderlyingPrice\nSPY,SPY261113P00600000,1,2,999\nQQQ,QQQ261113P00600000,1,2,'+value+'\n';
    return [key,{key,size:csv.length,body:true,uploaded:new Date(start+(i?360000:0)),etag:'v',text:async()=>csv}];
  }));
  env.PAPER_TRADES={list:async()=>({objects:[...objects.values()],truncated:false}),get:async k=>objects.get(k)};
  assert.equal((await call('Owen',{...body,action:'activate'})).status,400);
  assert.equal((await call('Jayden',body)).status,403);
  assert.equal((await (await call('Owen',{...body,cutoff:recorded-1})).json()).events.length,0);
  const result=await (await call('Owen',body)).json();
  assert.equal(result.events.length,1,JSON.stringify(result));
  const [event]=result.events;
  assert.equal(event.triggeredAt,start);assert.equal(event.recordedAt,recorded);
  assert.equal(event.journalFill.executionId,'sensitive-id');assert.equal(event.journalFill.timestampKind,'execution-alert');
  assert.equal(event.outcomes[0].state,'measured');assert.ok(Math.abs(event.outcomes[0].value-1)<1e-8);
  assert.equal(DB.raw('SELECT COUNT(*) n FROM one_clock_activations')[0].n,0);
  objects.values().next().value.uploaded=new Date(recorded+1000);
  const gap=await (await call('Owen',body)).json();assert.equal(gap.events[0].outcomes[0].reason,'trigger-baseline-unavailable');
  await insert(position,recorded+1000,'removed');
  const removed=await (await call('Owen',{...body,cutoff:recorded+1000})).json();
  assert.equal(removed.events.length,0);
});
