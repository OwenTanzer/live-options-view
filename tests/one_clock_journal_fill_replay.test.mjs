import test from 'node:test';
import assert from 'node:assert/strict';
import {journalFillReplayHref,journalFillReplayTarget,journalFillMarketContext} from '../site/shared/one-clock-journal-replay.js';

const session='2026-11-13', fillAt=Date.parse(session+'T15:12:05Z'), T=60_000;
const journalFill={positionId:'pos:0123456789abcdef',executionId:'notion:abc:order:7203',fillAt};
const fill={instrument:{underlying:'QQQ',occ_symbol:'QQQ261113P00780000'}};

test('fill identity travels in the fragment and date of later import remains distinct from the event day',()=>{
  const cutoff=fillAt+3*86400000, href=journalFillReplayHref({journalFill},cutoff);
  const url=new URL(href,'https://example.org');
  assert.equal(url.search,'');
  assert.deepEqual(journalFillReplayTarget(url.hash),{...journalFill,cutoff,session});
  assert.equal(journalFillReplayHref({},cutoff),null);
  assert.equal(journalFillReplayHref({journalFill},fillAt-1),null);
  assert.throws(()=>journalFillReplayTarget(url.hash+'&position=extra'));
});

test('market window clips to the results cutoff, drops late and stale samples, preserves exact-contract gaps',async()=>{
  const cutoff=fillAt+T,times=[];
  const source={maxAgeMs:120000,read:async selection=>{
    const t=selection.cutoff; times.push(t);
    const eventAt=t===fillAt-2*T?t-3*T:t;
    return [{eventAt,availableAt:t===fillAt-4*T?t+1:t,value:{underlying_price:100,
      rows:[{OptionSymbol:t===fillAt?'QQQ261113P00779000':fill.instrument.occ_symbol,Bid:2,Ask:2.2}]}}];
  }};
  const result=await journalFillMarketContext(fill,{...journalFill,cutoff,session},new AbortController().signal,source);
  assert.deepEqual(times,[fillAt-4*T,fillAt-2*T,fillAt,cutoff]);
  assert.deepEqual(result.underlying.segments[0].points.map(r=>Date.parse(r[0])),[fillAt,cutoff]);
  assert.deepEqual(result.option.segments[0].points.map(r=>Date.parse(r[0])),[cutoff]);
  assert.equal(result.option.segments[0].kind,'points');
});

