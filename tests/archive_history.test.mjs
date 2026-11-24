import test from 'node:test';
import assert from 'node:assert/strict';
import {archiveHistory} from '../backend/archive-history.js';
import {OWNER_ID, SITE} from '../backend/kalshi-access.js';

test('history requires the owner and streams the immutable compressed series', async t => {
  const digest='a'.repeat(64);
  const url=SITE+'/api/archive-history?'+new URLSearchParams({snapshot:digest,symbol:'BTC',interval:'1h',provider:'coinbase'});
  const cookie={Cookie:'__Host-cds-session='+'x'.repeat(24)};
  const env={ARCHIVE_HISTORY:{get:async key=>{
    assert.equal(key,'crypto/'+digest+'/BTC/1h/coinbase.json.gz');
    return {body:new Uint8Array([1,2,3])};
  }}};
  assert.equal((await archiveHistory(new Request(url),env)).status,401);
  assert.equal((await archiveHistory(new Request(url.replace('BTC','invalid')),env)).status,400);
  assert.equal((await archiveHistory(new Request(url),{...env,ENVIRONMENT:'preview'})).status,404);
  let github_id=1;
  t.mock.method(globalThis,'fetch',async()=>Response.json({github_id}));
  assert.equal((await archiveHistory(new Request(url,{headers:cookie}),env)).status,401);
  let member='Another member';
  const journalEnv={...env,DB:{prepare:()=>({bind:()=>({first:async()=>({member})})})}};
  const journalRequest=new Request(url,{headers:{Cookie:'__Host-journal=test-session'}});
  assert.equal((await archiveHistory(journalRequest,journalEnv)).status,401);
  member='Owen';
  assert.equal((await archiveHistory(journalRequest,journalEnv)).status,200);
  github_id=OWNER_ID;
  const result=await archiveHistory(new Request(url,{headers:cookie}),env);
  assert.equal(result.status,200);
  assert.equal(result.headers.get('Content-Encoding'),'gzip');
  assert.equal(result.headers.get('Cache-Control'),'private, no-store');
  assert.deepEqual([...new Uint8Array(await result.arrayBuffer())],[1,2,3]);
});
