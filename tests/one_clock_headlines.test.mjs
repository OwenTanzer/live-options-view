import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {makeStorage} from './helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResearch,advanceOneClockResearch} from '../backend/one-clock-research.js';
import {handleOneClockResults} from '../backend/one-clock-results.js';
import {headlineIngestion,headlineQuoteIngestion,projectHeadlines} from '../backend/one-clock-headline-ingestion.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {observedEventsAsOf} from '../backend/one-clock-observer.js';
import {headlineDefinition} from '../site/shared/one-clock-headlines.js';
import {projectResults,ruleKey} from '../site/shared/one-clock-results.js';

const published=JSON.parse(execFileSync('python3',['tests/helpers/headline_publication_fixture.py'],{encoding:'utf8'}));
const storyAt=Date.parse('2026-11-09T10:05:00Z'), minute=60000, session='2026-11-09';
const iso=t=>new Date(t).toISOString();
const scope={session,instrument:'NVDA',origin:'recorded',cutoff:storyAt};
test('qualified publisher pages and legacy headlines share revision identity',()=>{
  const page=Object.values(published).find(p=>p.rows?.some(r=>r.title==='NVDA guides higher'));
  const actual=projectHeadlines([page,page],scope);
  assert.equal(actual.length,1);assert.equal(actual[0].value.instrumentId,'eq.nvda');
  assert.equal(actual[0].availableAt,storyAt);
  assert.equal(projectHeadlines([page],{...scope,instrument:'BP'}).length,0);
  assert.equal(projectHeadlines([{...page,mode:'publication'}],scope).length,0);
  assert.equal(projectHeadlines([{...page,data_label:'SYNTHETIC fixture data (not real news or market data)'}],scope).length,0);
  const legacy={...page,rows:page.rows.map(({company_mapping,...r})=>r)};
  assert.equal(projectHeadlines([legacy],scope)[0].id,actual[0].id);
});

test('authenticated save, activate, twice-delivered revision, Results and original publication-time Replay',async t=>{
  const db=makeStorage();t.after(()=>db.db.close());
  for(const name of ['0004_one_clock.sql','0010_one_clock_ingestion.sql'])
    await db.exec(readFileSync(new URL('../backend/migrations/'+name,import.meta.url),'utf8'));
  let tick=storyAt-2*minute, quoteAt=tick-1000,price=100;
  t.mock.method(Date,'now',()=>tick);
  t.mock.method(globalThis,'fetch',async url=>{
    const u=new URL(url);assert.equal(u.origin,'https://pub-102bfd9119094f0092ad5c4c0a204200.r2.dev');
    return published[u.pathname.slice(1)]?Response.json(published[u.pathname.slice(1)]):new Response('',{status:404});
  });
  const env={DB:db,ONE_CLOCK_OBSERVER:'enabled',PAPER_TRADES:{async get(key){
    assert.equal(key,'intraday/prices.json');
    const timestamp=iso(quoteAt+500),base={price,source_symbol:'NVDA',source:'dxlink',price_basis:'last_trade',
      instrument_id:'NVDA',observed_at:iso(quoteAt),collector_received_at:iso(quoteAt+200)};
    const text=JSON.stringify({schema_version:2,timestamp,prices:{NVDA:base,QQQ:{...base,instrument_id:'etf.qqq',source_symbol:'QQQ',price:600}}});
    return {size:Buffer.byteLength(text),uploaded:new Date(quoteAt+1000),text:async()=>text};
  }}};
  const user='headline-test',storage=createStorage(env);
  await storage.users.insert(user,{username:user,version:0});await storage.sessions.create('headline-token',user,86400*4);
  const origin='https://options.moopertonic.net';
  const headers={Cookie:'session=headline-token',Origin:origin,'Content-Type':'application/json'};
  async function research(body){const response=await handleOneClockResearch(new Request(origin+'/api/one-clock/research',
    {method:'POST',headers,body:JSON.stringify(body)}),env);const result=await response.json();assert.ok(response.ok,JSON.stringify(result));return result;}
  const rule={id:'headline',version:1,name:'NVDA headline',...headlineDefinition('NVDA')};
  assert.deepEqual((await research({action:'save',rule})).rule,rule);
  assert.equal((await research({action:'activate',ruleId:rule.id,version:1})).active,true);
  const observe=async()=>{const progress=await advanceOneClockResearch(env,{now:tick,sources:[headlineIngestion,headlineQuoteIngestion],publicationSources:[]});
    assert.ok(progress.some(r=>['evaluated','catching-up'].includes(r.state) && r.generation),JSON.stringify(progress));};
  const replay=async cutoff=>(await research({action:'replay-headlines',session,ruleId:rule.id,version:1,cutoff})).events;
  async function results(cutoff,instrument='NVDA'){
    const response=await handleOneClockResults(new Request(origin+'/api/one-clock/results?'+new URLSearchParams({from:session,to:session,instrument,cutoff}),{headers}),env);
    const body=await response.json();assert.ok(response.ok,JSON.stringify(body));return body;
  }
  await observe();
  tick=storyAt+1;await observe();
  tick++;await observe();
  assert.equal((await results(tick,'ALL')).events.length,1);
  assert.equal((await replay(storyAt-1)).length,0);
  assert.equal((await replay(storyAt))[0].outcomes[0].state,'pending');
  quoteAt=storyAt+15*minute;price=104;const receipt=quoteAt+1000;
  tick=receipt+50;await observe();
  const [event]=(await results(tick)).events;
  assert.equal(event.baselines['return-5'].evidence.value.price,100);
  const endpoint=event.outcomes[0];
  assert.equal(endpoint.evidence.instrumentId,'eq.nvda');assert.equal(endpoint.evidence.value.collectorReceivedAt,quoteAt+200);
  assert.equal(endpoint.elapsedMs,15*minute);assert.equal(endpoint.lagMs,10*minute);assert.ok(Math.abs(endpoint.value-4)<1e-8);
  assert.equal((await replay(receipt-1))[0].outcomes[0].state,'pending');
  assert.deepEqual((await replay(receipt))[0].outcomes[0],endpoint);
  assert.equal((await results(receipt)).events[0].outcomes[0].state,'pending');
  const projection={events:[event],rules:[rule],origin:'recorded',cutoff:tick,outcome:{ruleKey:ruleKey(rule),id:'return-5'}};
  assert.equal(projectResults({...projection,filters:{endpointTiming:'exact'}}).summary.measured,0);
  assert.equal(projectResults({...projection,filters:{endpointTiming:'delayed'}}).summary.measured,1);
  await research({action:'deactivate',ruleId:rule.id,version:1});
  const dayDeadline=storyAt+2*86400000;
  tick=dayDeadline;
  const [stillPending]=(await results(dayDeadline)).events;
  // Move the service clock without invoking the observer. Both Results routes
  // must project expiry from the saved window for a rolled-off, paused rule.
  tick=dayDeadline+1;
  const [expired]=(await results(tick)).events;
  assert.equal(stillPending.outcomes[3].state,'pending');
  assert.equal(expired.outcomes[3].state,'missing');
  assert.equal((await observedEventsAsOf(db,{namespace:'account:'+user,selection:{...scope,cutoff:tick}}))[0].outcomes[3].state,'missing');
  // On Wednesday the normal jobs are Wednesday and Tuesday. A Monday price
  // measured at its last eligible millisecond arrives one second later.
  quoteAt=dayDeadline;price=110;tick=dayDeadline+1000;await observe();
  const [closed]=(await results(tick)).events;
  assert.equal(closed.outcomes[3].evidence.value.price,110);
  assert.equal(closed.outcomes[3].elapsedMs,2*86400000);
  assert.equal((await replay(tick-1))[0].outcomes[3].state,'missing');
  assert.equal((await replay(tick))[0].outcomes[3].state,'measured');
});

test('headline endpoint window is inclusive and rejects weekend spillover',()=>{
  const session='2026-11-06',start=Date.parse(session+'T20:55:00Z');
  const rule={id:'weekend',name:'Friday NVDA',version:1,...headlineDefinition('NVDA')};
  function run(sample,receipt,cutoff){
    const observations=[
      {source:'company-quote',id:'base',eventAt:start-2,availableAt:start-1,value:{price:100}},
      {source:'company-headline',id:'story',eventAt:start,availableAt:start,value:{title:'NVDA'}},
      {source:'company-quote',id:'forward',eventAt:sample,availableAt:receipt,value:{price:105}},
    ].map(r=>({...r,revision:1,session,origin:'recorded',instrument:'NVDA',evidence:r.id,
      value:{...r.value,ticker:'NVDA',instrumentId:'eq.nvda'}}));
    return evaluateEvents({rule,observations,selection:{session,instrument:'NVDA',origin:'recorded',cutoff},
      sessionClose:start+5*minute})[0].outcomes[0];
  }
  const deadline=start+5*minute+86400000,monday=Date.parse('2026-11-09T14:30:00Z');
  assert.equal(run(deadline,deadline+2,deadline).state,'pending');
  assert.equal(run(deadline,deadline+2,deadline+1).state,'missing');
  assert.equal(run(deadline,deadline+2,deadline+2).state,'measured');
  assert.equal(run(deadline+1,deadline+2,deadline+2).state,'missing');
  assert.equal(run(monday,monday+1,monday+1).state,'missing');
});
