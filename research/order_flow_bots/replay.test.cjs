'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const replay = require('../../site/order-flow-replay.json');
const {minuteFrames} = require('./frames.cjs');
const {decisions} = require('./run.cjs');
const {eventStudy} = require('./event-builder.cjs');
const evaluate = require('./strategies-a.cjs');
const frames = [...minuteFrames(replay)];
const rows = decisions(frames);

test('all eleven emit one decision per minute and retain abstentions', () => {
  assert.equal(rows.length,4301);
  assert.deepEqual([...new Set(rows.map(r=>r.id))],Array.from({length:11},(_,i)=>i+1));
  assert.deepEqual(rows.filter(r=>r.id===3).map(r=>r.signal),Array(391).fill(null));
  assert.equal(frames.flatMap(f=>f.contracts).reduce((n,c)=>n+c.printCount,0),replay.prints.count);
});

test('signed and unsigned use identical anchors, past references and print-count exclusions', () => {
  let eligible=0;
  for (let i=0;i<rows.length;i+=11) {
    const a=rows[i+1], b=rows[i+5];
    assert.equal(Boolean(a.comparison),Boolean(b.comparison));
    if (a.comparison) {
      eligible++;
      assert.equal(a.comparison.references,b.comparison.references);
      assert.equal(a.comparison.prints,b.comparison.prints);
      assert.ok(a.comparison.unknownPrints<=0.3*a.comparison.prints);
    }
  }
  assert.ok(eligible>0);
  const history=frames.slice(0,100).map(f=>({...f,contracts:f.contracts.map(c=>({...c,unknownCount:c.printCount}))}));
  const signals=evaluate(history);
  assert.ok([signals[1],signals[5]].every(s=>s.signal===null && !s.comparison));
});

test('bounded gamma breakout and verified turnover paths can trigger on controlled input', () => {
  const sample=structuredClone(frames[10]);
  const history=Array.from({length:16},(_,i)=>{
    const f=structuredClone(sample);
    f.spot=i===15?743.75:743.5;
    f.t+=60000*i;
    f.oiVerified=true;
    f.strikes.forEach(k=>{k.flow[1]={low:-20,high:-1,covered:2,of:2};});
    for(const c of f.contracts) {
      c.openOi=100; c.cumSell=0; c.cumUnknown=0;
      c.cumBuy=(i===15 && c===f.contracts[0])?101:99;
    }
    return f;
  });
  const rules=evaluate(history);
  assert.equal(rules[0].signal,'long');
  assert.equal(rules[2].signal,'long');
  history.at(-1).oiVerified=false;
  assert.equal(evaluate(history)[2].signal,null);
});

test('One Clock evaluates every emitted episode through its native event engine',async()=>{
  const {evaluateEvents}=await import('../../site/shared/one-clock-events.js');
  const study=eventStudy(rows,replay.session);
  for (const [i,rule] of study.rules.entries()) {
    const events=evaluateEvents({rule,observations:study.observations,selection:{...study.selection,cutoff:replay.coverage.close},
      sessionClose:replay.coverage.close});
    assert.equal(events.length,rows.filter(r=>r.id===i+1 && r.episode).length);
  }
});

test('OI provenance must bind to the replay session and opening observation',()=>{
  assert.throws(()=>[...minuteFrames(replay,{session:'bad'})],/provenance/);
});
