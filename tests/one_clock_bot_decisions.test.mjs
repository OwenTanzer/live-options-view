import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createStorage} from '../backend/storage.js';
import {makeStorage} from './helpers/d1_shim.mjs';
import {ingestPublicationStream} from '../backend/one-clock-publications.js';
import {botAuditPublication} from '../backend/one-clock-publication-sources.js';
import {handleBotDecisions,readBotDecisions} from '../backend/one-clock-bot-decisions.js';
import {botDecisionSource} from '../site/shared/one-clock-bot-decisions.js';

// Friday decisions reach the consumer on Saturday.
const T=Date.parse('2026-11-21T15:02:00Z');
const scope={bot:'crassus_oiskew',instrument:'QQQ',session:'2026-11-20',cutoff:T,after:null};
test('private fixture goes through unchanged producer and real consumer, before/at receipt and on rewind',async t=>{
  const cmd=`import sys, json, hashlib
sys.path.insert(0,'tests')
from test_one_clock_bot_decisions import Store, decision, publish_chunk
public,private=Store(),Store()
rows=[decision(),decision('crassus_ankit','smoke_atm_roundtrip'),decision()]
rows[-1]['outcome_class']='runner_error'
rows[-1]['decision']=None
data=b''.join(json.dumps(r).encode()+b'\\n' for r in rows)
key='crassus-ledger/v1/'+('a'*32)+'/decisions-test.jsonl/'+f'{0:020d}-{len(data):020d}-{hashlib.sha256(data).hexdigest()}.jsonl'
private.objects[key]=data
publish_chunk(private,'private',public,'public',key)
print(json.dumps({k:v.decode() for k,v in public.objects.items() if '/entries/' in k}))`;
  const built=spawnSync('python3',['-c',cmd],{encoding:'utf8'});
  assert.equal(built.status,0,built.stderr);const entries=JSON.parse(built.stdout);
  const db=makeStorage();t.after(()=>db.db.close());
  await db.exec(fs.readFileSync(new URL('../backend/migrations/0011_one_clock_publications.sql',import.meta.url),'utf8'));
  const bucket={async get(key){if(!entries[key])return null;const wire=new TextEncoder().encode(entries[key]);return {size:wire.length,uploaded:new Date(T-2000),arrayBuffer:async()=>wire.buffer};}};
  await ingestPublicationStream(db,bucket,botAuditPublication,{instrument:'QQQ',now:T});
  assert.deepEqual((await readBotDecisions(db,{...scope,cutoff:T-1})).rows,[]);
  const env={DB:db,BOT_INDEX_READS:'legacy'},store=createStorage(env);
  await store.users.insert(scope.bot,{username:scope.bot,is_bot:true,strategy_id:'oi_skew_qqq',version:0});
  await store.botMarkers.put(scope.bot,{username:scope.bot});
  const route=async cutoff=>{
    const query=new URLSearchParams({bot:scope.bot,instrument:scope.instrument,session:scope.session,cutoff});
    const url=new URL('https://test/api/one-clock/bot-decisions?'+query);
    return handleBotDecisions(new Request(url),env,url);
  };
  t.mock.method(Date,'now',()=>T+86400000);
  assert.equal((await route(Date.parse('2026-11-19T15:00:00Z'))).status,400);
  for(const [cutoff,expected] of [[T-1,0],[T,2],[T+86400000,2],[T-1,0]]) {
    const response=await route(cutoff);
    assert.equal(response.status,200);
    assert.equal((await response.json()).rows.length,expected);
  }
  const data=await readBotDecisions(db,scope);
  assert.equal(data.rows.length,2);assert.equal(data.next,null);
  assert.equal(data.rows.find(r=>r.value.outcome==='no_trade').details.anchor.anchor,0.1);
  assert.equal(data.rows.find(r=>r.value.outcome==='runner_error').details.anchor,null);
  assert.doesNotMatch(JSON.stringify(data),/PRIVATE|account_state_before|execution_request_id/);
  const adapter=botDecisionSource({getBot:()=>scope.bot,fetchImpl:async()=>Response.json(data)});
  const rows=await adapter.read({...scope,signal:new AbortController().signal});
  assert.deepEqual(rows,data.rows);
  await ingestPublicationStream(db,bucket,botAuditPublication,{instrument:'QQQ',now:T+100});
  assert.deepEqual((await readBotDecisions(db,scope)).rows,data.rows);
  const statement=db.queries.find(s=>s.includes('SELECT d.payload'));
  const details=db.raw('EXPLAIN QUERY PLAN '+statement,T,'bot-audit/QQQ','a','b',T,scope.session,'QQQ',41).map(x=>x.detail).join('\n');
  assert.match(details,/SEARCH d USING INDEX.*record_id>/);assert.doesNotMatch(details,/SCAN|TEMP B-TREE/);
});
test('invalid decision query is rejected before storage',async()=>{
  const req=new Request('https://test/api/one-clock/bot-decisions?bot=raw&scope=private');
  assert.equal((await handleBotDecisions(req,{},new URL(req.url))).status,400);
});
test('publisher and observer keep the original action clock across recovery and ignore legacy audit-only rows',async t=>{
  const cmd=`import sys,json,hashlib
sys.path.insert(0,'tests')
from test_one_clock_bot_decisions import Store,decision,publish_chunk
a=decision()
a.update(strategy_decided_at='2026-11-20T14:31:00Z',timestamp_utc='2026-11-20T14:31:15Z',outcome_class='ambiguous',decision={'action':'buy'})
retry={**a,'timestamp_utc':'2026-11-20T14:50:00Z','outcome_class':'reconciled_after_ambiguity'}
b=decision()
b.update(strategy_decided_at='2026-11-20T14:32:00Z',timestamp_utc='2026-11-20T14:33:00Z')
error=decision()
error.update(decision=None,outcome_class='runner_error')
data=b''.join(json.dumps(r).encode()+b'\\n' for r in (a,retry,b,error,decision()))
public,private=Store(),Store()
key='crassus-ledger/v1/'+('a'*32)+'/decisions-test.jsonl/'+f'{0:020d}-{len(data):020d}-{hashlib.sha256(data).hexdigest()}.jsonl'
private.objects[key]=data
publish_chunk(private,'private',public,'public',key)
print(json.dumps({k:v.decode() for k,v in public.objects.items() if '/entries/' in k}))`;
  const built=spawnSync('python3',['-c',cmd],{encoding:'utf8'});
  assert.equal(built.status,0,built.stderr);
  const entries=JSON.parse(built.stdout);
  const db=makeStorage();t.after(()=>db.db.close());
  await db.exec(fs.readFileSync(new URL('../backend/migrations/0011_one_clock_publications.sql',import.meta.url),'utf8'));
  const bucket={async get(key){if(!entries[key])return null;const wire=new TextEncoder().encode(entries[key]);
    return {size:wire.length,uploaded:new Date(T-2000),arrayBuffer:async()=>wire.buffer};}};
  await ingestPublicationStream(db,bucket,botAuditPublication,{instrument:'QQQ',now:T});
  const rows=db.raw('SELECT payload FROM one_clock_publication_records').map(r=>JSON.parse(r.payload));
  assert.equal(rows.filter(r=>r.source==='bot-decision').length,5);
  const evaluations=rows.filter(r=>r.source==='bot-evaluation');
  assert.equal(evaluations.length,3);
  const {oiSkewAuditRule}=await import('../site/shared/one-clock-decision-events.js');
  const {evaluateEvents}=await import('../site/shared/one-clock-events.js');
  const open=Date.parse('2026-11-20T14:30:00Z'),minute=60000;
  const selection={...scope,origin:'recorded'};
  for(const [m,spot] of [[1,100],[2,101],[11,103],[12,104]])rows.push({
    source:'price',id:String(m),revision:1,session:scope.session,instrument:'QQQ',origin:'recorded',
    eventAt:open+m*minute,availableAt:open+m*minute,value:{spot},evidence:'fixture://quote/'+m
  });
  const events=evaluateEvents({rule:oiSkewAuditRule('audit'),observations:rows,selection,sessionClose:open+390*minute});
  assert.deepEqual(events.map(e=>[e.inputEvidence[0].value.action,e.decisionAt,e.outcomes[0].value]),
    [['buy',open+minute,3],['no_trade',open+2*minute,3]]);
});
