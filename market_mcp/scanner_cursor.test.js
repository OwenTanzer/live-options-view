'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {Source,DataError} = require('./source');
const {Consumer} = require('./consumer');
const {activeFixtures,SESSION} = require('./fixtures');

const delivered = Date.parse(SESSION+'T13:57:00Z');
const finalDelivered = Date.parse(SESSION+'T20:07:00Z');
const pinned = {session:SESSION,sweep:6,limit:1,as_of_ms:delivered};
function setup() {
  const {files} = activeFixtures();
  const calls=[], receipts=new Map();
  const source = new Source({read:async key=>{
    calls.push(key);
    if(!Object.hasOwn(files,key)) throw new DataError('missing_artifact','Absent fixture',key);
    return {bytes:files[key],availableAt:receipts.has(key)?receipts.get(key):delivered};
  }});
  return {consumer:new Consumer(source,{now:()=>finalDelivered+1000}),calls,receipts};
}
test('pinned sweep becomes visible at the last required retained receipt; later pointers stay unread',async()=>{
  const {consumer,calls,receipts}=setup();
  const late=delivered+650;
  const board=Object.keys(activeFixtures().files).find(k=>k.includes('/rev_')&&k.endsWith('leaderboard.csv'));
  receipts.set(board,late);
  const early=await consumer.call('active_rankings',{...pinned,as_of_ms:late-1});
  assert.equal(early.status,'unavailable');
  assert.equal(early.availability,'unproven_at_cursor');
  assert.equal(early.rows,undefined);
  const ready=await consumer.call('active_rankings',{...pinned,as_of_ms:late});
  assert.equal(ready.rows.length,1);
  assert.ok(ready.sources.every(e=>e.available_by_ms<=late));
  assert.equal(ready.sources.at(-1).retrieved_sha256.length,64);
  assert.ok(!calls.some(key=>/latest|latest-attempt/.test(key)));
  const path='oa203/scanner/'+SESSION+'/sweeps/sweep_0001.jsonl.gz';
  receipts.set(path,late+1);
  const detail=await consumer.call('result_detail',{reference:ready.rows[0].detail_reference});
  assert.equal(detail.status,'unavailable');
  assert.equal(detail.quote_path,undefined);
});
test('missing receipts, current-session lookups and future cursors cannot produce an as-of ranking',async()=>{
  const {consumer,calls,receipts}=setup();
  for(const query of [{session:'current',sweep:6,as_of_ms:delivered},{session:SESSION,as_of_ms:delivered},
    {...pinned,as_of_ms:finalDelivered+2000}]) {
    assert.equal((await consumer.call('active_rankings',query)).errors[0].code,'invalid_filter');
  }
  assert.equal(calls.length,0);
  receipts.set('oa203/scanner/'+SESSION+'/inprogress/sweep_0006.json',null);
  const out=await consumer.call('active_rankings',pinned);
  assert.equal(out.status,'unavailable');assert.equal(out.sources.length,0);
});
test('final ranking uses finalized time and observed revision receipts, including rebuilds',async()=>{
  const {consumer,receipts}=setup();
  const query={session:SESSION,as_of_ms:finalDelivered};
  const early=await consumer.call('return_rankings',{...query,as_of_ms:delivered});
  assert.equal(early.status,'unavailable');
  const available=await consumer.call('return_rankings',query);
  assert.ok(available.rows.length>0);
  receipts.set('oa203/scanner/'+SESSION+'/leaderboard.csv',finalDelivered+1);
  const rebuilt=await consumer.call('return_rankings',query);
  assert.equal(rebuilt.availability,'unproven_at_cursor');assert.equal(rebuilt.summary,undefined);
});

test('malformed cursor arguments return the normal validation envelope',async()=>{
  const {consumer}=setup();
  for(const tool of ['return_rankings','active_rankings','result_detail'])
    assert.equal((await consumer.call(tool,null)).status,'failed');
});
