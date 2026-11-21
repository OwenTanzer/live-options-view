import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { selectAsOf } from '../site/shared/one-clock.js';
const fixture=JSON.parse(fs.readFileSync(new URL('./fixtures/one_clock_contract.json',import.meta.url),'utf8'));
test('JS and Python bindings share cutoff, correction, cohort and deterministic ordering fixtures',()=>{
  for(const example of fixture.cases) {
    const rows=fixture.rows.map(row=>({...fixture.base,...row}));
    const scope={session:fixture.base.session,instrument:fixture.base.instrument,origin:example.origin,cutoff:fixture.base.eventAt+example.offset};
    assert.deepEqual(selectAsOf(rows,scope).map(r=>[r.id,r.revision,r.value]),example.expected);
    assert.deepEqual(selectAsOf(rows.reverse(),scope).map(r=>[r.id,r.revision,r.value]),example.expected);
  }
});
