import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import model from '../site/shared/order-flow-model.js';
import {flowStudy} from '../site/shared/one-clock-flow-study.js';
import {evaluateEvents} from '../site/shared/one-clock-events.js';
import {projectResults,ruleKey} from '../site/shared/one-clock-results.js';

test('archived QQQ minute rows match the original GEX model at the same cursor',()=>{
  const json=JSON.parse(fs.readFileSync(new URL('../site/order-flow-replay.json',import.meta.url)));
  const replay=model.decodeReplay(json);
  const study=flowStudy(replay,model);
  const minuteRows=study.rows.filter(r=>r.source==='orderFlow');
  assert.equal(minuteRows.length,391);
  for(const i of [1,132,390]){
    const row=minuteRows[i];
    const expected=model.replayAt(replay,row.eventAt).strikes.find(s=>s.strike===744);
    assert.equal(row.value.k744.flow,expected.flow[1].covered===expected.flow[1].of?expected.flow[1].base:null);
    assert.equal(row.value.k744.oi,expected.concentration.covered===expected.concentration.of?expected.concentration.value:null);
  }
  const rule=study.rules[0];
  const evaluate=(cutoff,origin='reconstructed')=>evaluateEvents({rule,observations:study.rows,
    selection:{...study.selection,cutoff,origin},sessionClose:study.close});
  const events=evaluate(study.close);
  assert.ok(events.length);
  const view=projectResults({events,rules:[rule],origin:'reconstructed',cutoff:study.close,
    outcome:{ruleKey:ruleKey(rule),id:'spot-5m'}});
  assert.equal(view.summary.occurrences,events.length);
  assert.ok(view.summary.measured>0);
  assert.deepEqual(evaluate(events[0].triggeredAt-1),[]);
  assert.deepEqual(evaluate(study.close,'recorded'),[]);
  assert.deepEqual(evaluate(study.close,'synthetic'),[]);
});

test('an intact strike trigger still fires when the archived spot is stale or its endpoint is unusable',()=>{
  const archive=JSON.parse(fs.readFileSync(new URL('../site/order-flow-replay.json',import.meta.url)));
  const replay=model.decodeReplay(archive);
  const run=(study,rule)=>evaluateEvents({rule,observations:study.rows,
    selection:{...study.selection,cutoff:study.close},sessionClose:study.close});
  const study=flowStudy(replay,model),rule=study.rules[0];
  const first=run(study,rule)[0],outcome=rule.outcomes[0];
  const age=first.triggeredAt-first.baselines[outcome.id].evidence.eventAt;
  assert.ok(age>0);
  const staleRule={...rule,outcomes:[{...outcome,maxAgeMs:age-1}]};
  const stale=run(study,staleRule)[0];
  assert.deepEqual(stale.inputEvidence,first.inputEvidence);
  assert.equal(stale.outcomes[0].reason,'trigger-baseline-unavailable');
  const endpoint=first.outcomes[0];
  for(const snapshot of replay.snapshots) {
    if(snapshot.t>=endpoint.targetAt && snapshot.t<=endpoint.targetAt+outcome.toleranceMs) snapshot.spot=0;
  }
  const unavailable=run(flowStudy(replay,model),rule)[0];
  assert.deepEqual(unavailable.inputEvidence,first.inputEvidence);
  assert.equal(unavailable.outcomes[0].reason,'no-qualified-endpoint');
});
