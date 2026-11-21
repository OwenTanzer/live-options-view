import test from 'node:test';
import assert from 'node:assert/strict';
import {projectResults,compareResults,ruleKey} from '../site/shared/one-clock-results.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {replaySelection} from '../site/shared/one-clock-replay-link.js';
import {demoRules,demoRows,demoSelection,demoStart,demoClose} from '../site/shared/one-clock-demo.js';
const T=Date.parse('2026-11-20T16:00:00Z');
const spec={id:'price',source:'price',field:'spot',measure:'return',horizonMs:900000,toleranceMs:150000,maxAgeMs:150000};
const rules=[{id:'a',version:1,name:'Alpha',outcomes:[spec]},{id:'b',version:1,name:'Beta',outcomes:[{...spec,id:'different-local-id'}]},
  {id:'c',version:1,name:'Other horizon',outcomes:[{...spec,horizonMs:1800000}]}];
function event(id,value,{rule='a',session='2026-11-20',state='measured',origin='recorded',collection='prospective',...rest}={}) {
  return {id,ruleId:rule,ruleVersion:1,session,instrument:'QQQ',origin,collection,triggeredAt:T-1800000,recordedAt:T,
    outcomes:[{id:rule==='b'?'different-local-id':'price',state,value:state==='measured'?value:null,measuredAt:T-1000,availableAt:T}],...rest};
}
const project=(events,extra={})=>projectResults({events,rules,cutoff:T,origin:'recorded',outcome:{ruleKey:ruleKey(rules[0]),id:'price'},...extra});
test('one filtered ledger supplies every count, search, collection and measured denominator',()=>{
  const events=[event('one',2),event('two',4,{rule:'b',session:'2026-11-19'}),event('pending',null,{state:'pending'}),
    event('missing',null,{state:'missing'}),event('different',8,{rule:'c'}),event('replay',20,{collection:'replay'}),
    event('future',900,{recordedAt:T+1}),event('synthetic',900,{origin:'synthetic'})];
  const view=project(events,{filters:{collections:['prospective']}});
  assert.equal(view.rows.length,5);assert.equal(view.summary.occurrences,5);assert.equal(view.summary.sessions,2);
  assert.equal(view.summary.measured,2);assert.equal(view.summary.pending,1);assert.equal(view.summary.missing,1);assert.equal(view.summary.unscored,1);
  assert.equal(view.summary.mean,3);assert.equal(view.summary.median,3);
  assert.equal(view.summary.distribution.reduce((n,b)=>n+b.count,0),2);
  assert.deepEqual(project(events,{filters:{query:'beta',from:'2026-11-19',to:'2026-11-19'}}).rows.map(r=>r.event.id),['two']);
  assert.equal(project(events,{filters:{instruments:['SPY']}}).rows.length,0);
  assert.deepEqual(project(events,{filters:{states:['pending']}}).rows.map(r=>r.event.id),['pending']);
});
test('empty and all-pending scopes retain honest null statistics',()=>{
  const empty=project([]).summary;
  assert.equal(empty.occurrences,0);assert.equal(empty.mean,null);assert.equal(empty.median,null);assert.deepEqual(empty.distribution,[]);
  const pending=project([event('p',null,{state:'pending'})]).summary;
  assert.equal(pending.occurrences,1);assert.equal(pending.measured,0);assert.equal(pending.pending,1);assert.equal(pending.mean,null);
});
test('future measurements, duplicated receipts and mixed numeric types are rejected',()=>{
  const row=event('one',4);
  assert.throws(()=>project([row,row]),/Duplicate/);
  assert.throws(()=>project([{...row,outcomes:[{...row.outcomes[0],availableAt:T+1}]}]),/availability/);
  assert.throws(()=>project([event('bad','four')]),/nonnumeric/);
});
test('distribution includes its maximum once and collapses a constant sample',()=>{
  const values=[-3,-2,-1,0,1,2,3,4,5,6,7,8];
  const histogram=project(values.map((v,i)=>event(String(i),v))).summary.distribution;
  assert.equal(histogram.reduce((n,b)=>n+b.count,0),values.length);assert.equal(histogram.at(-1).high,8);
  assert.deepEqual(project([event('1',2),event('2',2)]).summary.distribution,[{low:2,high:2,count:2,includesHigh:true}]);
});
test('categorical values preserve their types and rates use only measured outcomes',()=>{
  const categorical=[{...rules[0],outcomes:[{...spec,measure:'category'}]}];
  const view=project([event('1',false),event('2','false'),event('3',false),event('4',null,{state:'pending'})],{rules:categorical});
  assert.equal(view.summary.kind,'category');assert.equal(view.summary.mean,null);
  assert.deepEqual(view.summary.categories,[{value:false,count:2,rate:2/3},{value:'false',count:1,rate:1/3}]);
});
test('shared-session comparison preserves covariance and is invariant to within-session replication',()=>{
  const events=[event('a1',1,{session:'2026-11-19'}),event('a2',3),event('b1',0,{rule:'b',session:'2026-11-19'}),event('b2',0,{rule:'b'})];
  const config={a:ruleKey(rules[0]),b:ruleKey(rules[1])};
  const result=compareResults(project(events).rows,config);
  assert.equal(result.estimate,2);assert.equal(result.standardError,1);assert.equal(result.sessions,2);
  const duplicated=events.flatMap(e=>[e,{...e,id:e.id+'copy'}]);
  assert.equal(compareResults(project(duplicated).rows,config).standardError,1);
  const one=compareResults(project(events.filter(e=>e.session==='2026-11-20')).rows,config);
  assert.equal(one.estimate,3);assert.equal(one.standardError,null);assert.match(one.reason,/not estimable/);
});
test('categorical comparison is a percentage-point difference for a chosen typed category',()=>{
  const rows=[{event:{ruleId:'a',ruleVersion:1,session:'1'},outcome:{state:'measured',value:false}},
    {event:{ruleId:'a',ruleVersion:1,session:'2'},outcome:{state:'measured',value:true}},
    {event:{ruleId:'b',ruleVersion:1,session:'1'},outcome:{state:'measured',value:true}},
    {event:{ruleId:'b',ruleVersion:1,session:'2'},outcome:{state:'measured',value:true}}];
  const result=compareResults(rows,{a:ruleKey(rules[0]),b:ruleKey(rules[1]),kind:'category',category:true});
  assert.equal(result.estimate,-50);assert.equal(result.standardError,50);assert.equal(result.unit,'percentage points');
});
test('categorical event endpoints wait for publication and never require a numeric baseline',()=>{
  const rule={...demoRules[0],outcomes:[{...spec,id:'present',source:'shortlist',field:'present',measure:'category',horizonMs:60000}]};
  const first=evaluateEvents({rule,observations:demoRows,selection:{...demoSelection,cutoff:demoStart+360000},sessionClose:demoClose})[0];
  assert.equal(first.outcomes[0].state,'pending');
  const later=evaluateEvents({rule,observations:demoRows,selection:{...demoSelection,cutoff:demoStart+421000},sessionClose:demoClose})[0];
  assert.equal(later.outcomes[0].state,'measured');assert.equal(later.outcomes[0].value,false);
});
test('replay links preserve an exact millisecond cursor and reject mixed-day or duplicate scope',()=>{
  const params=new URLSearchParams({one_clock_session:'2026-11-20',one_clock_instrument:'QQQ',one_clock_at:String(T-234)});
  assert.equal(replaySelection(params).cutoff,T-234);assert.equal(replaySelection(''),null);
  assert.throws(()=>replaySelection(params+'&one_clock_at='+T),/Invalid/);
  params.set('one_clock_session','2026-11-19');assert.throws(()=>replaySelection(params),/scope/);
  params.set('one_clock_session','2026-11-20');params.set('one_clock_instrument','private');assert.throws(()=>replaySelection(params),/scope/);
});
