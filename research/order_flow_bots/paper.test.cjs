'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { paper } = require('./paper.cjs');
const M = 60000;
function replay(prices, close=12, gaps=[]) {
  return {session:'sample',coverage:{open:0,close:close*M,gaps},
    snapshots:prices.map(([minute,spot]) => ({t:minute*M,spot}))};
}
function episode(id,t,signal,horizon=3) {
  return {id,t:t*M,signal,horizon,episode:true,complete:true,session:'sample'};
}
test('eleven independent accounts use future entry, due exit and adverse costs', () => {
  const r=replay([[0,99],[1,100],[3,110],[4,999]]);
  const rows=[episode(1,0,'long'),episode(2,0,'short')];
  const {accounts,trades}=paper(rows,r,{costBpsPerSide:10});
  assert.equal(accounts.length,11); assert.equal(trades[0].entry.t,M);
  assert.equal(trades[0].exit.t,3*M);
  assert.ok(Math.abs(trades[0].pnl-9.79)<1e-10); assert.ok(Math.abs(trades[1].pnl+10.21)<1e-10);
  assert.ok(Math.abs(accounts[0].endingCash-10009.79)<1e-8);
  assert.ok(Math.abs(accounts[1].endingCash-9989.79)<1e-8); assert.equal(accounts[10].endingCash,10000);
});
test('duplicate, overlapping and late episodes cannot open a second position', () => {
  const rows=[episode(1,0,'long'),episode(1,0,'long'),episode(1,1,'short'),episode(1,10,'long')];
  const {accounts,trades}=paper(rows,replay([[1,100],[3,101]]));
  assert.deepEqual(trades.map(x=>x.status),['closed','skipped','skipped','skipped']);
  assert.equal(accounts[0].closed,1); assert.equal(accounts[0].skipped,3);
});
test('missing exit retains an open position and freezes subsequent slot orders', () => {
  const {accounts,trades}=paper([episode(1,0,'short'),episode(1,6,'short')],replay([[1,100],[7,101]]));
  assert.deepEqual(trades.map(x=>x.status),['unresolved','skipped']);
  assert.equal(accounts[0].openPosition.direction,'short'); assert.equal(accounts[0].realizedPnl,0);
});
test('range breakout uses a frozen observed range and a distinct subsequent fill', () => {
  const r=replay([[0,98],[1,99],[2,100],[3,99],[4,100],[5,99],[6,102],[7,101],[9,105]]);
  const trade=paper([episode(6,5,'range',4)],r).trades[0];
  assert.equal(trade.status,'closed'); assert.equal(trade.range.high,100); assert.equal(trade.breakoutAt,6*M);
  assert.equal(trade.entry.t,7*M); assert.equal(trade.direction,'long'); assert.equal(trade.exit.t,9*M);
  r.snapshots[6].spot=97; assert.equal(paper([episode(6,5,'range',4)],r).trades[0].direction,'short');
  r.snapshots.splice(7,1); assert.equal(paper([episode(6,5,'range',4)],r).trades[0].status,'skipped');
});
test('no breakout, incomplete range, expired price and missing entry cannot generate fills', () => {
  const flat=replay(Array.from({length:13},(_,i)=>[i,100]));
  const rows=[episode(6,5,'range'),episode(6,6,'range')];
  const a=paper(rows,flat).trades; assert.equal(a[0].reason,'no sampled breakout before expiry');
  assert.equal(a[1].reason,'previous order or position outstanding');
  assert.equal(paper([episode(6,1,'range')],flat).trades[0].reason,'incomplete pre-signal range');
  assert.equal(paper([episode(1,0,'long')],replay([[3,101]])).trades[0].status,'skipped');
});
test('invalid or duplicate timestamps yield only a later unambiguous sampled fill', () => {
  const r=replay([[0,99],[0.5,100],[0.5,101],[1,null],[1.5,101],[3,102]]);
  const {trades}=paper([episode(2,0,'long')],r);assert.equal(trades[0].entry.t,1.5*M);
});
test('outages inside entry or exit windows prohibit fills', () => {
  let r=replay([[1,100],[3,101]],12,[{receipt:M,outage_seconds:20}]);
  assert.equal(paper([episode(1,0,'long')],r).trades[0].status,'skipped');
  r=replay([[1,100],[3.5,101]],12,[{receipt:3.4*M,outage_seconds:5}]);
  assert.equal(paper([episode(1,0,'long')],r).trades[0].status,'unresolved');
});
