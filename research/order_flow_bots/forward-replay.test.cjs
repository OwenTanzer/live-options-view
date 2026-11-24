'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {minuteFrames} = require('./frames.cjs');
const {openPaper,registerSession,applyFrame,applyQuote} = require('./forward-paper.cjs');
const replay = require('../../site/order-flow-replay.json');

test('forward ledger reproduces the audited September 25 one-share lifecycle', () => {
  const db=openPaper(':memory:');
  const bounds = {session:replay.session,...replay.coverage};
  registerSession(db,bounds);
  const slots=Array.from({length:11},(_,i)=>i+1);
  const frames=Array.from(minuteFrames(replay),frame=>({t:frame.t,frame}));
  const observations=replay.snapshots.map(quote=>({t:quote.t,quote}));
  // Fixture only: the archive lacks forward receipt times and executable quotes.
  // Replay its one-basis-point synthetic spread with a deterministic event clock.
  const events=frames.concat(observations).sort((a,b)=>a.t-b.t || Number(!!a.frame)-Number(!!b.frame));
  for (const {t,frame,quote} of events) {
    if (frame) {
      applyFrame(db,{frame,...bounds,receivedAt:t,eligibleSlots:slots},t);
    } else {
      applyQuote(db,{symbol:'QQQ',t,spot:quote.spot,bid:quote.spot*.9999,
        ask:quote.spot*1.0001,bidAt:t,askAt:t,receivedAt:t},t);
    }
  }
  assert.equal(db.prepare('SELECT count(*) AS n FROM decisions').get().n,4301);
  assert.equal(db.prepare('SELECT count(*) AS n FROM trades').get().n,49);
  const counts=db.prepare("SELECT id,count(*) AS n FROM trades WHERE status='closed' GROUP BY id ORDER BY id").all();
  assert.deepEqual(counts.map(r=>[r.id,r.n]),[[2,8],[4,1],[5,4],[6,2],[7,3],[8,15],[9,1],[10,9],[11,1]]);
  assert.equal(db.prepare("SELECT count(*) AS n FROM trades WHERE status='skipped'").get().n,5);
  db.close();
});
