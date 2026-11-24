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
test('restart on a different strike ladder warms up a fresh segment', () => {
  const db=openPaper(':memory:');
  for(let i=0;i<11;i++) accept(db,i);
  const frame=frameAt(11);
  frame.contracts[0].symbol='shifted';
  const {decisions}=applyFrame(db,{frame,open,close,receivedAt:frame.t,eligibleSlots:[7,9]},frame.t);
  assert.equal(decisions.find(d=>d.id===7).episode,false);
  assert.equal(decisions.find(d=>d.id===9).reason,'opening put-OI reference unavailable');
  db.close();
});
test('unmarked position survives close and recovers at the next regular session quote', () => {
  const db=openPaper(':memory:');
  for(let i=0;i<=11;i++) accept(db,i);
  quote(db,open+11*minute+1,100);
  assert.equal(quote(db,close+minute,120).accepted,false);
  assert.equal(db.prepare('SELECT status FROM trades').get().status,'open');
  const day=24*60*minute, next=open+day;
  registerSession(db,{session:'2026-11-25',open:next,close:close+day});
  quote(db,next,120);
  const tr=db.prepare('SELECT * FROM trades').get();
  assert.equal(tr.exit_t,next);
  assert.equal(tr.reason,'late recovery exit at observed price');
  db.close();
});
test('regular and shortened sessions reserve a complete exit window before placing an order', () => {
  for (const [day,minutes] of [['2026-11-24',390],['2026-11-27',210]]) {
    const sessionOpen=Date.parse(day+'T14:30:00Z'),sessionClose=sessionOpen+minutes*minute;
    for (const remaining of [0,1,2]) {
      const db=openPaper(':memory:');
      const due=sessionClose-remaining*minute, decision=due-10*minute;
      for(let i=0;i<=11;i++) {
        const frame={...frameAt(i),session:day,t:decision-(11-i)*minute};
        frame.snapshotTime=frame.t;
        applyFrame(db,{frame,open:sessionOpen,close:sessionClose,receivedAt:frame.t,eligibleSlots:[7]},frame.t);
      }
      quote(db,decision+5000,101);
      if(remaining<2) {
        const tr=db.prepare('SELECT * FROM trades').get();
        assert.equal(tr.status,'skipped');
        assert.equal(tr.reason,'insufficient regular-session exit window');
        assert.equal(tr.entry_price,null);
      } else {
        assert.equal(db.prepare('SELECT status FROM trades').get().status,'open');
        quote(db,due+10000,102);
        const tr=db.prepare('SELECT * FROM trades').get();
        assert.equal(tr.status,'closed');
        assert.ok(tr.exit_t<sessionClose);
      }
      db.close();
    }
  }
});

test('side bounds cannot cross decision, receipt, expiry or closing bell; fills retain quote identity', () => {
  const db=openPaper(':memory:');
  for(let i=0;i<=11;i++) accept(db,i);
  const decision=open+11*minute, due=decision+10*minute;
  function bounded(bidAt,askAt,askThrough,receivedAt,price) {
    return applyQuote(db,{symbol:'QQQ',t:Math.min(bidAt,askAt),receivedAt,spot:price,bid:price-.01,
      ask:price+.01,bidAt,askAt,bidThrough:bidAt,askThrough},receivedAt);
  }
  assert.equal(bounded(decision,decision,decision+1,decision,100).accepted,false);
  assert.equal(bounded(decision,decision,decision+1,decision+1,100).accepted,true);
  assert.equal(db.prepare('SELECT status FROM trades').get().status,'pending');
  assert.equal(bounded(decision+1,decision+2,decision+3,decision+3,101).accepted,true);
  const entry=db.prepare('SELECT entry_t,entry_quote_t FROM trades').get();
  assert.equal(entry.entry_t,decision+3);
  assert.equal(entry.entry_quote_t,decision+1);
  bounded(due-1,due,due+1,due+1,111);
  assert.equal(db.prepare('SELECT status FROM trades').get().status,'open');
  bounded(due+1,due+2,due+3,due+3,102);
  const exit=db.prepare('SELECT exit_t,exit_quote_t FROM trades').get();
  assert.equal(exit.exit_t,due+3);
  assert.equal(exit.exit_quote_t,due+1);
  assert.equal(bounded(close-1,close,close+1,close+1,102).accepted,false);
  assert.equal(bounded(close-1,close,close+2,close+2,102).accepted,false);
  db.close();
});

test('a quote straddling the minute cannot set the frozen pre-signal breakout range', () => {
  const db=openPaper(':memory:');
  const decision=open+35*minute;
  for(let i=0;i<=35;i++) {
    const t=open+i*minute, frame=frameAt(i);
    frame.spot=100;
    frame.contracts=[99,100,101].flatMap(strike=>['C','P'].map(type=>({
      symbol:type+strike,strike,type,buy:1,sell:0,unknown:0,buyPremium:i===35?500:10,
      sellPremium:0,unknownPremium:0,printCount:1,unknownCount:0
    })));
    if (i>=30 && i<35) quote(db,t,100);
    if (i===35) applyQuote(db,{symbol:'QQQ',t:decision-1,bid:199,ask:201,spot:200,
      bidAt:decision-1,askAt:decision,bidThrough:decision-1,askThrough:decision+1,
      receivedAt:decision+2},decision+2);
    applyFrame(db,{frame,open,close,receivedAt:t+2,eligibleSlots:[6]},t+2);
  }
  const order=db.prepare('SELECT status,range_low,range_high FROM trades WHERE id=6').get();
  assert.equal(order.status,'watch');
  assert.equal(order.range_low,100);
  assert.equal(order.range_high,100);
  db.close();
});

test('a pre-precision ledger migrates repeatedly and retains old quote and trade evidence', t => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'flow-paper-upgrade-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const filename=path.join(dir,'legacy.sqlite');
  let db=openPaper(filename);
  for(let i=0;i<=11;i++) accept(db,i);
  const decision=open+11*minute;
  quote(db,decision+5,100);
  for(const [table,cols] of [['quotes',['bid_through','ask_through']],['trades',['entry_quote_t','exit_quote_t']]])
    for(const col of cols) db.exec('ALTER TABLE '+table+' DROP COLUMN '+col);
  db.close();
  for(let i=0;i<2;i++){db=openPaper(filename);db.close();}
  db=openPaper(filename);
  assert.equal(db.prepare('SELECT bid_through FROM quotes').get().bid_through,null);
  const due=decision+10*minute;
  quote(db,due,101);
  const trade=db.prepare('SELECT * FROM trades').get();
  assert.equal(trade.status,'closed');
  assert.equal(trade.entry_t,decision+5);
  assert.equal(trade.entry_quote_t,null);
  assert.equal(trade.exit_quote_t,due);
  db.close();
});
