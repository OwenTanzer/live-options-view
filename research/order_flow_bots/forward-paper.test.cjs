'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {openPaper,registerSession,applyFrame,applyQuote} = require('./forward-paper.cjs');

const session = '2026-11-24';
const open = Date.parse(session+'T14:30:00Z'), minute=60000, close=open+390*minute;
function frameAt(i) {
  const t = open+i*minute;
  return {session,t,spot:i===10?99:i===11?101:100,snapshotTime:t,strikes:[],complete:true,minuteComplete:true,
    contracts:[{type:'C',symbol:'test',strike:100,buy:0,sell:1,unknown:0,buyPremium:0,sellPremium:10,unknownPremium:0}]};
}
function accept(db, i, eligibleSlots=[7], delay=0) {
  const frame=frameAt(i);
  return applyFrame(db,{frame,open,close,receivedAt:frame.t+delay,eligibleSlots},frame.t+delay);
}
function quote(db,t,spot,extra={}) {
  return applyQuote(db,{symbol:'QQQ',t,spot,receivedAt:t,bid:spot-.01,ask:spot+.01,bidAt:t,askAt:t,...extra},t);
}
test('forward rule, later quote, restart, another quote, durable unique closed trade', t => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'flow-paper-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const filename=path.join(dir,'paper.sqlite');
  let db=openPaper(filename);
  const decision=open+11*minute;
  for (let i=0;i<=11;i++) accept(db,i);
  assert.equal(db.prepare("SELECT status FROM trades WHERE id=7").get().status,'pending');
  quote(db,decision,100);
  assert.equal(db.prepare("SELECT status FROM trades").get().status,'pending');
  quote(db,decision+1000,101,{bid:100.99,ask:101.01});
  db.close();
  db=openPaper(filename);
  assert.equal(accept(db,11).accepted,false);
  assert.equal(quote(db,decision+1000,101).accepted,false);
  quote(db,decision+10*minute,102,{bid:101.99,ask:102.01});
  const trade=db.prepare('SELECT * FROM trades').get();
  assert.equal(trade.status,'closed');
  assert.equal(trade.entry_price,101.01);
  assert.equal(trade.exit_price,101.99);
  assert.ok(Math.abs(trade.pnl-0.98)<1e-9);
  assert.equal(db.prepare('SELECT count(*) AS n FROM decisions').get().n,12*11);
  db.close();
});
test('missing prerequisites, late minutes and isolated duplicate sessions cannot issue orders', () => {
  for (const [eligibleSlots,delay] of [[[],0],[[7],minute]]) {
    const db=openPaper(':memory:');
    for(let i=0;i<11;i++) accept(db,i);
    accept(db,11,eligibleSlots,delay);
    assert.equal(db.prepare('SELECT count(*) AS n FROM trades').get().n,0);
    assert.throws(()=>registerSession(db,{session,open,close:close+minute}));
    db.close();
  }
});
test('missed horizon recovers on an observed quote and stale ticks cannot fill', () => {
  const db=openPaper(':memory:');
  const decision=open+11*minute;
  for(let i=0;i<=11;i++) accept(db,i);
  assert.equal(applyQuote(db,{symbol:'QQQ',t:decision+1,receivedAt:decision+1,spot:99},decision+3*minute).accepted,false);
  quote(db,decision+1,100);
  quote(db,decision+23*minute,80);
  const tr=db.prepare('SELECT * FROM trades').get();
  assert.equal(tr.reason,'late recovery exit at observed price');
  assert.ok(tr.pnl < -20);
  db.close();
});
test('two-sided quotes cannot straddle market close or carry a stale side', () => {
  const db=openPaper(':memory:');
  registerSession(db,{session,open,close});
  const envelope={symbol:'QQQ',spot:100,bid:99.99,ask:100.01,t:close-1,bidAt:close-1,
    askAt:close+1,receivedAt:close+1};
  assert.equal(applyQuote(db,envelope,close+1).accepted,false);
  assert.equal(applyQuote(db,{...envelope,bidAt:close-20000,t:close-20000,askAt:close},close+1).accepted,false);
  assert.equal(db.prepare('SELECT count(*) AS n FROM quotes').get().n,0);
  db.close();
});
