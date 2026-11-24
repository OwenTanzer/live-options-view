'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const feeds = require('./forward-feed.cjs');
const {createService} = require('./forward-service.cjs');
const {fetchQqqQuote} = require('./forward-quote.cjs');
const {mkdtempSync,rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
const paper = require('./forward-paper.cjs');
const {createOpeningOiReader} = require('./forward-opening-oi.cjs');

const minute = 60000;
const open = Date.parse('2026-11-24T14:30:00Z');
const close = open + 390 * minute;
const day = '2026-11-24';

function makeFixture() {
  const contracts = [];
  for (let strike = 647; strike <= 654; strike++) {
    for (const type of ['C','P']) {
      const symbol = 'QQQ261124'+type+String(strike*1000).padStart(8,'0');
      contracts.push({symbol,strike,type});
    }
  }
  const opening = Object.fromEntries(contracts.map(c=>[c.symbol,open]));
  const totals = {};
  const rows = [];
  let prints = 0;
  return function snapshot(t) {
    const elapsed = Math.floor((t-open)/minute);
    const phase = Math.ceil((t-open)/minute);
    const spot = ({16:651.05,17:650.8,22:650.95,23:650.5,40:651.2,41:650.7,48:651.7,49:651.45})[elapsed] ?? 650.9;
    for (const c of contracts) {
      const bullish = phase>=29 && phase<=34 || phase>=44 && phase<=46 || phase===16;
      let side = 'sell';
      if (phase>4) side = (c.type==='C') === bullish ? 'buy':'sell';
      const size = phase<=4 ? (c.strike===647?35:12) : phase>=44 && phase<=46 ? 20 :
        c.strike===(phase%20>=10?653:648) ? 7 : 1;
      const price = c.type==='P' ? 1.5 : 1.5 + 0.1*(elapsed%3);
      if (t===open && c!==contracts[0]) continue;
      const counts = totals[c.symbol] ??= {B:0,S:0,U:0,prints:0};
      counts[side==='buy'?'B':'S'] += size;
      const seq = ++counts.prints;
      // Two strikes print near the underlying tick. A more distant contract
      // advances the session watermark independently of those parity pairs.
      const printTime = t===open || c===contracts.at(-1) ? t : t-1400;
      rows.push({...c,t:printTime,side,size,price,seq});
      prints++;
    }
    if (rows.length>2048) rows.splice(0,rows.length-2048);
    const session = {day,opening,first:open,last:t,prints,contracts:totals,gaps:[],
      rebuilt:{status:'rebuilt',segments:0,through:null,at:open-45000,session_open:open}};
    const flow = {cap:2048,lastReceived:t,rows:rows.slice(),session:{...session,
      contracts:Object.fromEntries(Object.entries(totals).map(([symbol,x])=>[symbol,{...x}]))}};
    const chain = {symbol:'QQQ',date:day,expiration:day,timestamp:new Date(t).toISOString(),
      underlying_price:spot,underlying_market:{symbol:'QQQ',spot,spot_ts:new Date(t).toISOString(),source:'dxlink',freshness:'live',provenance:{collector_received_at:new Date(t).toISOString(),price:spot,price_basis:'bid_ask_midpoint',freshness:'live'}},rows:contracts.map(c=>({OptionSymbol:c.symbol,UnderlyingPrice:spot,
        OpenInterest:c.strike===650?1000:5,Gamma:0.12}))};
    return {flow,chain,receivedAt:t,chainReceivedAt:t,open,close};
  };
}

test('ten strategies can enter and exit from forward observations through the paper service', async () => {
  const dirname = mkdtempSync(join(tmpdir(),'forward-paper-integration-'));
  const filename = join(dirname,'paper.sqlite');
  const db = paper.openPaper(filename);
  const snapshot = makeFixture();
  let t = open;
  let flow, chain, spot;
  const clock = () => t;
  const response = body => ({ok:true,status:200,json:async()=>body,headers:{get:()=>null}});
  async function fetchImpl(url) {
    if (url.includes('/opening-oi/v1/')) {
      if(t<open+30000) return {ok:false,status:404};
      const packet = {schema:'opening-oi.v1',version:1,instrument:'QQQ',session:day,settledSession:'2026-11-23',openingSpot:650.9,
        openAt:new Date(open).toISOString(),capturedAt:new Date(open+20000).toISOString(),snapshotKey:'intraday/20261124/snapshot_093020000000.csv',
        legs:Array.from({length:8},(_,i)=>({strike:647+i,call:2,put:647+i===650?1000:5}))};
      return {ok:true,status:200,text:async()=>JSON.stringify(packet),headers:{get:k=>k==='last-modified'?new Date(open+21000).toUTCString():null}};
    }
    if (url===feeds.FLOW_URL) return response(flow);
    if (url===feeds.CHAIN_URL) return response(chain);
    assert.equal(url,'https://options.moopertonic.net/api/live-quotes?symbols=QQQ');
    const sourceTs = new Date(t-100).toISOString();
    return response({health:{state:'live'},quotes:[{symbol:'QQQ',instrument_class:'equity',kind:'ticker',
      source:'dxlink',bid:spot-0.01,ask:spot+0.01,bid_ts:sourceTs,ask_ts:sourceTs}]});
  }
  const service = createService({db,paper,feeds,fetchQuote:fetchQqqQuote,createOpeningOiReader,fetchImpl,clock});
  try {
    for (; t<=open+70*minute; t+=3000) {
      const data = snapshot(t);
      flow = data.flow;
      spot = data.chain.underlying_price;
      if (t===open || (t-open)%minute===3000) {
        chain = data.chain;
        // The chain is captured just off the minute. The underlying price
        // has an independent observed time, as on the real QQQ publication.
        const captured = t===open ? t-1000 : t-2999;
        chain.timestamp = new Date(captured).toISOString();
        const market = chain.underlying_market;
        market.spot_ts = new Date(captured-500).toISOString();
        market.provenance.collector_received_at = market.spot_ts;
      }
      const cycle = await service.cycle();
      assert.equal(cycle.status,'polling',JSON.stringify(cycle.errors));
    }
    const observed = db.prepare('SELECT id,COUNT(*) AS n FROM trades WHERE status=? GROUP BY id ORDER BY id').all('closed');
    const entered = observed.map(row=>row.id);
    const audit = db.prepare('SELECT id,reason,signal,count(*) AS n FROM decisions GROUP BY id,reason,signal ORDER BY id,n DESC').all();
    console.log('closed trades per slot',JSON.stringify(observed));
    for (let i=1;i<=11;i++) if (!entered.includes(i)) console.log('remaining',i,JSON.stringify(audit.filter(r=>r.id===i)));
    const opening = JSON.parse(db.prepare('SELECT body FROM frames ORDER BY t LIMIT 1').get().body);
    assert.equal(opening.openingReferenceComplete,true);
    assert.deepEqual(entered,[1,2,4,5,6,7,8,9,10,11]);
    assert.equal(audit.filter(r=>r.id===3 && r.signal!==null).length,0);
    const invalid = db.prepare(`SELECT COUNT(*) AS n FROM trades t
      LEFT JOIN quotes entry ON entry.t=t.entry_t LEFT JOIN quotes exit ON exit.t=t.exit_t
      WHERE status='closed' AND (entry.t IS NULL OR exit.t IS NULL OR
      entry_t<=accepted_at OR exit_t<=entry_t OR exit_t<due OR
      entry_price != CASE WHEN direction='long' THEN entry.ask ELSE entry.bid END OR
      exit_price != CASE WHEN direction='long' THEN exit.bid ELSE exit.ask END)`).get();
    assert.equal(invalid.n,0);
  } finally {
    db.close();
    rmSync(dirname,{recursive:true,force:true});
  }
});
