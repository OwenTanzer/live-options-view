'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { gaps, anchors, labels } = require('./outcomes.cjs');
const m = 60_000, session = '2026-09-25', t = Date.parse(session + 'T13:30:00Z');
const makeReplay = () => ({ session, coverage: { open:t, close:t+10*m, gaps:[] },
  snapshots: [[4,110],[0,50],[2,90],[1,100],[3,100],[5,90],[6,95],[7,97]].map(([n,spot]) => ({t:t+n*m,spot})) });
const blank = (offset, horizon) => ({ id:2, t:t+offset*m, horizon, episode:false, signal:null });

test('all decision anchors include quiet times and deduplicate shared horizons', () => {
  const rs = [blank(0,4), {...blank(0,4), id:8}, blank(0,5), blank(7,5)];
  const out = anchors(rs,makeReplay());
  assert.equal(out.length,3);
  assert.deepEqual(out[0], {session,t,horizon:4,due:t+4*m,start:t+m,end:t+4*m,
    spotReturnBps:(1.1-1)*1e4,sampledRangeBps:2000,downsideExcursionBps:1000,missingReason:null});
  assert.equal(out[2].missingReason,'outside regular session');
  assert.deepEqual(labels(rs,makeReplay(),out),[]);
});

test('direction and range labels share frozen post-decision anchors', () => {
  const rs = ['long','short','range'].map((signal,id) => ({...blank(0,4),signal,id,episode:true}));
  const out = labels(rs,makeReplay());
  assert.equal(out[0].valueBps,-out[1].valueBps);
  assert.equal(out[2].valueBps,2000);
  assert.equal(out[0].start,t+m);
});

test('archive gaps use end receipt minus duration; later gaps preserve earlier outcomes', () => {
  const r = makeReplay(), end = t+6*m;
  r.coverage.gaps = [{receipt:new Date(end).toISOString(),outage_seconds:60}];
  assert.deepEqual(gaps(r),[{start:t+5*m,end}]);
  const out = anchors([blank(0,4),blank(2,4)],r);
  assert.equal(out[0].missingReason,null);
  assert.equal(out[1].missingReason,'outage intersects outcome window');
  r.coverage.gaps = [{ start: null, end: null }];
  assert.equal(anchors([blank(0,4)],r)[0].missingReason,'outage intersects outcome window');
});

test('horizon endpoint, interior coverage and valid spot are required', () => {
  const r = makeReplay();
  assert.equal(anchors([blank(0,9)],r)[0].missingReason,'missing forward endpoint');
  r.snapshots = r.snapshots.filter(s=>s.t!==t+2*m && s.t!==t+3*m);
  assert.equal(anchors([blank(0,4)],r)[0].missingReason,'missing interior spot interval');
  r.snapshots = makeReplay().snapshots;
  r.snapshots.find(s=>s.t===t+2*m).spot=null;
  assert.equal(anchors([blank(0,4)],r)[0].missingReason,'invalid forward spot');
});
