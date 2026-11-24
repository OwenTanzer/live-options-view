'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { schedule, artifact, createService } = require('./forward-service.cjs');
const open = Date.parse('2026-11-27T14:30:00Z');
const close = Date.parse('2026-11-27T18:00:00Z');

test('NYSE holiday, half-day, pre-open and closing receipt window', () => {
  assert.equal(schedule(Date.parse('2026-11-26T15:00:00Z')), null);
  assert.equal(schedule(open - 120001), null);
  assert.deepEqual(schedule(open - 120000), { session: '2026-11-27', open, close });
  assert.equal(schedule(close + 60000).close, close);
  assert.equal(schedule(close + 60001), null);
});

test('conditional reads preserve receipt and space requests, including a 304', async () => {
  let time = open, hits = 0;
  const read = artifact('flow', 3000, async (url, options) => {
    if (++hits === 2) {
      assert.equal(options.headers['If-None-Match'], '"a1"');
      return new Response(null, { status: 304 });
    }
    return Response.json({ prints: 6 }, { headers: { ETag: '"a1"' } });
  }, () => time);
  const initial = await read();
  time += 2999; assert.equal(await read(), initial);
  time++; const validated = await read();
  assert.equal(validated.body,initial.body);
  assert.equal(initial.sampledAt,open); assert.equal(validated.receivedAt,open);
  assert.equal(validated.requestedAt,open+3000); assert.equal(validated.sampledAt,open+3000);
  time++; assert.equal(await read(),validated); assert.equal(hits,2);
});

test('polls are shared, executable quotes precede new orders and survive a flow outage', async () => {
  const calls = [], urls = [];
  let time = open, outage = false, quoteHits = 0;
  const paper = {
    registerSession(db, session) { assert.equal(session.close, close); calls.push('session'); },
    restoreFeedState() { return { frozenLadderSpot: 400 }; },
    applyQuote() { calls.push('quote'); return { accepted: true }; },
    hasQuoteWork() { return true; },
    applyFrame() { calls.push('frame'); return { accepted: true, decisions: [1,2] }; }
  };
  class ForwardFeed {
    observe({ flow, chainReceivedAt, frozenLadderSpot }) {
      assert.equal(frozenLadderSpot, 400);
      assert.equal(chainReceivedAt, open);
      return { frames: flow ? [{receivedAt:time}] : [] };
    }
  }
  const db = require('./forward-paper.cjs').openPaper(':memory:');
  const service = createService({
    db, paper, feeds: { FLOW_URL: 'flow', CHAIN_URL: 'chain', ForwardFeed },
    clock: () => time, fetchQuote: async () => { quoteHits++; return { bid: 1, ask: 2 }; },
    createOpeningOiReader: () => () => new Promise(() => {}),
    fetchImpl: async url => {
      urls.push(url);
      if (outage && url === 'flow') throw Error('offline');
      return Response.json({});
    }
  });
  assert.equal((await service.cycle()).decisions, 2);
  assert.deepEqual(calls, ['session', 'quote', 'frame']);
  time += 3000; await service.cycle();
  assert.equal(quoteHits, 1); assert.deepEqual(urls, ['flow','chain','flow']);
  time += 9000; outage = true;
  const result = await service.cycle();
  assert.equal(result.status, 'degraded'); assert.equal(result.quoteAccepted, true);
  assert.deepEqual(result.errors, ['flow: offline']); assert.equal(quoteHits, 2);
  time = close + 60001;
  assert.equal((await service.cycle()).status, 'idle'); assert.equal(quoteHits, 2);
  db.close();
});

test('real service distinguishes flow body and revalidation clocks from delayed sibling completion and hard deadline', async () => {
  const paper = require('./forward-paper.cjs');
  const feeds = require('./forward-feed.cjs');
  const { fetchQqqQuote } = require('./forward-quote.cjs');
  const { createOpeningOiReader } = require('./forward-opening-oi.cjs');
  const db = paper.openPaper(':memory:');
  const symbols = [];
  for (let strike = 647; strike <= 654; strike++) {
    for (const type of ['C','P']) symbols.push('QQQ261127' + type + String(strike * 1000).padStart(8, '0'));
  }
  const counts = Object.fromEntries(symbols.map(symbol => [symbol, { B: 0, S: 0, U: 0, prints: 0 }]));
  counts[symbols[0]] = { B: 1, S: 0, U: 0, prints: 1 };
  const timestamp = new Date(open).toISOString();
  const payloads = {
    [feeds.FLOW_URL]: { cap: 2048, rows: [{ symbol:symbols[0], seq:1, t:open, size:1, side:'buy', price:1 }],
      session:{ day:'2026-11-27', prints:1, first:open, last:open, contracts:counts, opening:counts, gaps:[] } },
    [feeds.CHAIN_URL]: { symbol:'QQQ', date:'2026-11-27', expiration:'2026-11-27', timestamp,
      underlying_price:650.5, rows:symbols.map(OptionSymbol => ({ OptionSymbol, Gamma:.01, OpenInterest:1000 })) }
  };
  const quote = { health:{ state:'live' }, quotes:[{ symbol:'QQQ', instrument_class:'equity', kind:'ticker',
    source:'dxlink', bid:650.4, ask:650.6, bid_ts:timestamp, ask_ts:timestamp }] };
  try {
    let time=open,releaseChain=null,slowChain=false,revalidate=false;
    const service = createService({ db, paper, feeds, fetchQuote:fetchQqqQuote, createOpeningOiReader, clock:() => time,
      fetchImpl:async (url,options) => {
        if(url===feeds.CHAIN_URL && slowChain) await new Promise(resolve=>{releaseChain=resolve;});
        if(url===feeds.FLOW_URL && revalidate) {
          assert.equal(options.headers['If-None-Match'],'"q"');
          return new Response(null,{status:304});
        }
        return Response.json(payloads[url] || quote,{headers:{ETag:'"q"'}});
      } });
    const cycle = await service.cycle();
    assert.deepEqual([cycle.status, cycle.frames, cycle.decisions, cycle.quoteAccepted], ['polling', 1, 11, true]);
    assert.equal(db.prepare('SELECT count(*) AS n FROM decisions WHERE session=?').get('2026-11-27').n, 11);
    assert.equal(paper.restoreFeedState(db, '2026-11-27').frozenLadderSpot, 650);
    const t=open+60000,flow=payloads[feeds.FLOW_URL],row=flow.rows[0];
    function event(at,seq) {
      flow.rows.push({...row,seq,t:at});flow.session.prints++;flow.session.last=at;
      counts[symbols[0]].B++;counts[symbols[0]].prints++;
    }
    time=t+1000;event(t+100,2);slowChain=true;
    const slow=service.cycle();
    // The flow body is fully read at +1s while the chain remains blocked.
    await new Promise(resolve=>setImmediate(resolve));assert.ok(releaseChain);
    time=t+9000;slowChain=false;releaseChain();
    assert.equal((await slow).frames,0);
    const bodyAt=db.prepare('SELECT body FROM frames WHERE t=?');
    assert.equal(bodyAt.get(t),undefined);
    // A fresh conditional HTTP request validates the same bytes at +10s.
    time=t+10000;revalidate=true;
    assert.equal((await service.cycle()).frames,1);
    const f=JSON.parse(bodyAt.get(t).body),audit=f.coverageDiagnostics;
    assert.equal(audit.publicationSettlement.mode,'bounded fallback');
    assert.equal(audit.publicationSettlement.firstReceipt,t+1000);
    assert.equal(audit.flowReceivedAt,t+1000);assert.equal(audit.flowRequestedAt,t+10000);
    assert.equal(audit.flowSampledAt,t+10000);assert.equal(audit.receivedAt,t+10000);
    assert.equal(f.contracts[0].printCount,0);
    // A subsequent slow sibling that crosses the hard deadline records a
    // missed frame even though the flow HTTP response was timely.
    const next=t+60000;
    time=next+1000;event(next+100,3);slowChain=true;revalidate=false;
    const expired=service.cycle();
    await new Promise(resolve=>setImmediate(resolve));assert.ok(releaseChain);
    time=next+15001;slowChain=false;releaseChain();
    const missed=await expired;
    assert.equal(missed.frames,0);assert.equal(missed.missedFrames,1);
    assert.match(JSON.parse(bodyAt.get(next).body).coverageReason,/deadline expired/);
  } finally { db.close(); }
});
