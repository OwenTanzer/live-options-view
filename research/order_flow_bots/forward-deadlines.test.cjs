'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const paper = require('./forward-paper.cjs');
const {createService} = require('./forward-service.cjs');
const {FRAME_GRACE_MS} = require('./forward-deadlines.cjs');
const {mkdtempSync,rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
const session = '2026-11-27';
const open = Date.parse(session+'T14:30:00Z'), close = Date.parse(session+'T18:00:00Z');
const minute = 60000;

function make(db, state) {
  class ForwardFeed {
    observe({flow,receivedAt}) {
      if (!state.current || flow?.session?.day !== session) return {frames:[]};
      const t = Math.floor(receivedAt / minute) * minute;
      if (receivedAt > t+FRAME_GRACE_MS) return {frames:[]};
      return {frames:[{open,close,receivedAt,eligibleSlots:[],frame:{session,t,contracts:[],strikes:[],
        complete:true,minuteComplete:true,spot:650,oiVerified:false}}]};
    }
  }
  return createService({db,paper,feeds:{FLOW_URL:'flow',CHAIN_URL:'chain',ForwardFeed},clock:()=>state.now,
    createOpeningOiReader:()=>async()=>null,
    fetchQuote:async()=>({symbol:'QQQ',t:state.now,spot:650,bid:649,ask:651,
      bidAt:state.now,askAt:state.now,receivedAt:state.now}),
    fetchImpl:async()=>Response.json({session:{day:state.flowDay,last:state.watermark}})});
}

test('wrong-session 200 and a frozen current watermark remain degraded until a current frame arrives; quotes still exit',async()=>{
  const db=paper.openPaper(':memory:');
  const state={now:open,flowDay:'2026-11-25',watermark:open-minute,current:false};
  const service=make(db,state);
  try {
    await service.cycle();
    const deadline=open+FRAME_GRACE_MS;
    state.now=deadline;
    assert.equal((await service.cycle()).missedFrames,0);
    db.prepare(`INSERT INTO trades(session,id,decision_t,accepted_at,due,status,direction,entry_t,entry_price)
      VALUES (?,?,?, ?,?,'open','long',?,?)`).run(session,1,open,open,deadline+1,open,648);
    state.now=deadline+3000;
    const missing=await service.cycle();
    assert.equal(missing.status,'degraded');
    assert.equal(missing.missedFrames,1);
    assert.ok(missing.errors.some(e=>e.includes('received 2026-11-25')));
    const lost=JSON.parse(db.prepare('SELECT body FROM frames WHERE t=?').get(open).body);
    assert.deepEqual([lost.coverageReason,lost.deadlineAt,lost.detectedAt,lost.observedSourceSession],
      ['decision deadline expired without a timely flow frame',deadline,state.now,'2026-11-25']);
    const decisions=db.prepare('SELECT * FROM decisions WHERE t=?').all(open);
    assert.equal(decisions.length,11);
    assert.ok(decisions.every(d=>d.signal===null && d.episode===0 && d.evaluated_at===state.now));
    state.now+=10000;
    const repeated=await service.cycle();
    assert.equal(repeated.missedFrames,0);assert.equal(repeated.status,'degraded');
    assert.equal(db.prepare('SELECT status FROM trades').get().status,'closed');
    assert.equal(db.prepare('SELECT count(*) AS n FROM trades').get().n,1);
    state.flowDay=session;state.watermark=open;state.now=open+45000;
    assert.equal((await service.cycle()).status,'degraded');
    state.watermark=state.now+4000;state.now=state.watermark;
    assert.equal((await service.cycle()).status,'degraded');
    state.current=true;state.now=open+minute;state.watermark=state.now;
    assert.equal((await service.cycle()).status,'polling');
    assert.equal(db.prepare('SELECT count(*) AS n FROM decisions').get().n,22);
  } finally {db.close();}
});

test('late attach, crash and restart outside close record only owned minutes once with actual detection time',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'paper-watch-')), file=join(folder,'ledger.sqlite');
  let db=paper.openPaper(file);
  const state={now:close-2*minute+30000,flowDay:session,watermark:close-5*minute,current:false};
  try {
    const service=make(db,state);
    assert.equal((await service.cycle()).missedFrames,0);
    assert.equal(db.prepare('SELECT next_t FROM decision_watch').get().next_t,close-minute);
    db.close();db=paper.openPaper(file);
    state.now=close+2*minute;
    const restarted=make(db,state);
    const recovered=await restarted.cycle();
    assert.equal(recovered.status,'idle');assert.equal(recovered.missedFrames,2);
    assert.deepEqual(db.prepare('SELECT t FROM frames ORDER BY t').all().map(r=>r.t),[close-minute,close]);
    assert.ok(db.prepare('SELECT * FROM decisions').all().every(r=>r.evaluated_at===state.now));
    assert.equal((await restarted.cycle()).missedFrames,0);
    assert.equal(db.prepare('SELECT count(*) AS n FROM decisions').get().n,22);
    assert.equal(db.prepare('SELECT count(*) AS n FROM trades').get().n,0);
  } finally {db.close();rmSync(folder,{recursive:true,force:true});}
});
