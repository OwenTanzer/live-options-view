import test from 'node:test';
import assert from 'node:assert/strict';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {calibratedLevels} from '../backend/one-clock-level-ingestion.js';

const base=Date.parse('2026-11-20T15:00:00Z');
const selection={instrument:'QQQ',session:'2026-11-20',origin:'recorded',cutoff:base+2000000};
const config={minimumBucket:5,approach:'either',contact:'exact-or-bracketed',maxGapMs:150000,groups:['put-only','call-only','both']};
const rule={id:'bright',name:'Bright strikes',version:1,instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',
  conditions:[{source:'oi-levels',field:'qualified',op:'==',value:true,maxAgeMs:150000}],levelEncounter:config,
  outcomes:[1,3,9,27].map(h=>({id:'h'+h,source:'level-spot',field:'spot',measure:'category',horizonMs:h*60000,toleranceMs:0,maxAgeMs:150000}))};
function row(source,n,value,lag=0,extra={}) {
  return {source,id:source+n,revision:1,session:selection.session,instrument:'QQQ',origin:'recorded',eventAt:base+n,availableAt:base+n+lag,evidence:'archive/'+source+n,value,...extra};
}
const price=(n,spot,lag=0,extra={})=>row('level-spot',n,spot===null?null:{spot},lag,{valueAt:base+n,...extra});
const flag=(n,call=5,put=1,lag=0)=>row('oi-levels',n,{levels:[{strike:700,call,put}],calibration:{etag:'old',tier:'0DTE_Regular'}},lag);
const run=(observations,options={})=>evaluateEvents({rule,observations,selection,sessionClose:selection.cutoff,...options});

test('known-before-hit eligibility, signed approaches, repeated touches, inclusive bounds and missing endpoints',()=>{
  const events=run([flag(0),price(1000,699),price(2000,700,20000),price(62000,701,5000),price(182000,699),price(542000,700)]);
  assert.equal(events[0].triggeredAt,base+22000);assert.equal(events[0].encounter.upperTime,base+2000);
  assert.deepEqual(events[0].outcomes.map(o=>o.value),['continuation','rejection','tie',null]);
  assert.equal(events[0].outcomes[0].targetAt,base+62000);assert.equal(events[0].outcomes[0].availableAt,base+67000);
  assert.equal(events[0].outcomes[3].reason,'no-qualified-endpoint');
  assert.equal(run([price(0,699,60000),flag(5000,5,1,45000),price(10000,701,60000)]).length,0);
  const late=run([flag(0),price(1000,699),price(2000,700,20000),price(62000,699,100)],{sessionClose:base+62000});
  assert.equal(late[0].outcomes[0].value,'rejection');assert.equal(late[0].outcomes[1].reason,'session-closed-before-horizon');
  const pending=run([flag(0),price(1000,699),price(2000,700),price(62000,699,100)],{selection:{...selection,cutoff:base+62000}});
  assert.equal(pending[0].outcomes[0].state,'pending');
  const touches=run([flag(0),price(1,699),price(2,700),price(3,700),price(4,699),price(5,700),price(6,701),price(7,699)]);
  assert.deepEqual(touches.map(e=>[e.encounter.contact,e.encounter.approach]),[['exact','up'],['exact','up'],['bracketed','down']]);
  assert.equal(run([flag(0),price(1,699),price(2,null),price(3,701)]).length,0);
  assert.equal(run([flag(0),price(1,699),price(160001,701)]).length,0);
});
test('groups use contemporaneous paired tiers and preserve qualifying evidence across intraday changes and corrections',()=>{
  const original=flag(0,5,1), newer=flag(3,1,5);
  const rows=[original,price(1,699),price(2,701),newer,price(4,699),price(5,701),
    {...newer,revision:2,availableAt:base+6,value:null},price(7,699),price(8,701),flag(9,5,5),price(10,699),price(11,701)];
  const events=run(rows);
  assert.deepEqual(events.map(e=>e.encounter.cohort),['call-only','call-only','put-only','put-only','call-only','call-only','both']);
  assert.ok(events.every(e=>e.inputEvidence.length===3));assert.equal(new Set(events.map(e=>e.id)).size,events.length);
  assert.equal(run(rows,{activationWindows:[]}).length,0);
  assert.equal(run([flag(0),price(1,699),price(2,701)],{rule:{...rule,levelEncounter:{...config,contact:'exact'}}}).length,0);
  assert.equal(run([flag(0),price(1,701),price(2,700),price(60002,699)])[0].outcomes[0].value,'continuation');
  assert.equal(run([{...original,session:'2026-11-19'},price(1,699),price(2,701)]).length,0);
});
test('calibration uses signed ladder offsets, lower ATM on a tie, tier multiplier and refuses missing thresholds',()=>{
  const header='Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj';
  const csv=[header,'0DTE_Regular,0,1,2,3,4,2,4,6,8,1,1','0DTE_Regular,1,1,2,3,10,3,6,9,12,1,1','0DTE_Weekly,0,1,2,3,4,2,4,6,8,2,3','0DTE_Weekly,1,1,2,3,10,3,6,9,12,2,3'].join('\n');
  const chain={tier:'0DTE_Weekly',underlying_price:700,rows:[698,702,710].flatMap(Strike=>['call','put'].map(Type=>({Type,Strike,OpenInterest:Type==='call'?8:24})))};
  assert.deepEqual(calibratedLevels(chain,csv),{levels:[{strike:702,call:4,put:3},{strike:698,call:5,put:5}],gaps:1});
  assert.equal(calibratedLevels(chain,csv.replace('0DTE_Regular,0,1,2,3,4','0DTE_Regular,0,1,2,3,')).gaps,2);
});

test('late-fill opening revisions are visible at actual publication only; restarts and rewinds keep opening calibration',async()=>{
  const {readOpeningOi}=await import('../backend/one-clock-opening-oi.js');
  const open=Date.parse('2026-11-20T14:30:00Z');
  const csv='Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj\n'+
    [-1,0,1].map(n=>'0DTE_Regular,'+n+',1,2,3,4,1,2,3,4,1,1').join('\n');
  const packets=[{version:1,at:open+30000,put:null},{version:2,at:base+15*60000,put:0}].map(({version,at,put})=>{
    const data={schema:'opening-oi.v1',instrument:'QQQ',session:selection.session,settledSession:'2026-11-19',version,openAt:new Date(open).toISOString(),
      capturedAt:new Date(at-100).toISOString(),openingSpot:700,tier:'0DTE_Regular',calibration:{etag:'june',csv,uploaded:new Date(open-86400000).toISOString()},
      legs:[{strike:699,call:0,put:0},{strike:700,call:15,put},{strike:701,call:0,put:0}]};
    return {key:'intraday/20261120/opening-oi/v1/'+String(version).padStart(4,'0')+'.json',
      etag:'opening'+version,uploaded:new Date(at),size:JSON.stringify(data).length,text:async()=>JSON.stringify(data)};
  });
  const bucket={async list(){return {objects:packets,truncated:false};},async get(k){return packets.find(p=>p.key===k);}};
  const lookup=cutoff=>readOpeningOi(bucket,selection.session,cutoff);
  assert.equal((await lookup(open)).reason,'opening-evidence-unavailable');
  const early=await lookup(base),late=await lookup(base+16*60000);
  assert.equal(early.levels.find(s=>s.strike===700).put,undefined);assert.equal(late.levels.find(s=>s.strike===700).put,0);
  assert.deepEqual(await lookup(base),early);assert.equal(late.calibration.openingAtm,700);
  const maps=[early,late].map((map,i)=>row('oi-levels',map.capturedAt-base,{levels:map.levels,calibration:map.calibration},map.availableAt-map.capturedAt,{id:packets[i].key}));
  const draws=run([...maps,price(1,699),price(2,701),price(16*60000,699),price(16*60000+1,701)]);
  assert.equal(draws.length,1);assert.equal(draws[0].encounter.cohort,'call-only');assert.equal(draws[0].triggeredAt,base+16*60000+1);
});

test('level adapter budgets apply only to prices inside the calendar session and retain opening revisions',async t=>{
  const {levelIngestion}=await import('../backend/one-clock-level-ingestion.js');
  const {makeStorage}=await import('./helpers/d1_shim.mjs');
  const {equitySession}=await import('../backend/one-clock-calendar.js');
  const db=makeStorage();t.after(()=>db.db.close());
  await db.exec(`CREATE TABLE one_clock_publication_status(stream,checked_at,generation,coverage);
    CREATE INDEX one_clock_publication_status_cutoff ON one_clock_publication_status(stream,checked_at,generation);
    CREATE TABLE one_clock_publication_records(stream,session,source,event_at,available_at,received_at,sequence,record_id,revision,payload);
    CREATE INDEX one_clock_native_chain_replay ON one_clock_publication_records(stream,session,source,event_at,available_at,record_id,revision);`);
  const day='2026-11-27',bounds=equitySession(day),cutoff=bounds.close+300000,stream='collector-chain/QQQ';
  await db.prepare('INSERT INTO one_clock_publication_status VALUES(?,?,?,?)').bind(stream,cutoff,1,JSON.stringify({reason:'tail-at-check',nextSequence:200})).run();
  const times=[...Array.from({length:100},(_,i)=>bounds.open-(100-i)*60000),bounds.open,bounds.open+60000,bounds.close,bounds.close+120000];
  await db.batch(times.map((eventAt,i)=>db.prepare('INSERT INTO one_clock_publication_records VALUES(?,?,?,?,?,?,?,?,?,?)')
    .bind(stream,day,'price',eventAt,eventAt+200,eventAt+1000,i+1,'price'+i,1,
      JSON.stringify({id:'price'+i,eventAt,availableAt:eventAt+200,revision:1,details:{sequence:i+1}}))));
  const key='intraday/20261127/opening-oi/v1/0001.json',bucket={list:async()=>({objects:[{key,etag:'frozen',uploaded:new Date(bounds.open+500)}]})};
  const discovered=await levelIngestion.discover(bucket,{instrument:'QQQ',session:day,cutoff},db);
  assert.equal(discovered.complete,true);
  assert.deepEqual(discovered.artifacts.filter(a=>a.price).map(a=>a.price.id),['price100','price101','price102']);
  assert.deepEqual(discovered.artifacts.filter(a=>a.opening).map(a=>a.key),[key]);
});
