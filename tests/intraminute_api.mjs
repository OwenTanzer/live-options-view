import assert from 'node:assert/strict';
import {intraminute} from '../backend/one-clock-intraminute.js';
const path='https://example.test/api/one-clock/intraminute';
const call=(query='',env={},method='GET')=>{
  const url=new URL(path+query);return intraminute(new Request(url,{method}),env,url);
};
assert.equal((await call('?minute=1794580560001')).status,400);
assert.equal((await call('?cursor=-1')).status,400);
assert.equal((await call('',{},'POST')).status,405);
assert.equal((await call()).status,503);
const env={PAPER_TRADES:{get:async key=>{
  assert.equal(key,'order-flow/qqq/intraminute/v1/1794580560000.json');
  return {body:'{"version":1}'};
}}};
assert.equal((await (await call('?minute=1794580560000',env)).json()).version,1);
assert.equal((await call('?minute=1794580560000',{PAPER_TRADES:{get:()=>null}})).status,404);
globalThis.fetch=async(url,opts)=>{
  assert.equal(url.origin,'https://configured.test');assert.equal(url.pathname,'/tape');
  assert.equal(opts.headers.Authorization,'Bearer fixture');
  assert.equal(opts.redirect,'error');return Response.json({rows:[]});
};
assert.deepEqual(await (await call('?cursor=18',{QQQ_TAPE_ORIGIN:'https://configured.test',QQQ_TAPE_KEY:'fixture'})).json(),{rows:[]});
console.log('Read-only archive scope, live configuration and proxy passed');
