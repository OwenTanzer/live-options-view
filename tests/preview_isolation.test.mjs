import test from 'node:test';
import assert from 'node:assert/strict';
import gateway from '../frontend/worker.mjs';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { publicReadAllowed, API_PATHS } from '../frontend/preview-policy.mjs';
import { createStorage } from '../backend/storage.js';
const request = (path, { method = 'GET', body, cookie, origin = LOCAL_ORIGIN, headers = {} } = {}) => new Request(LOCAL_ORIGIN + path, {
  method, headers: { Origin: origin, ...headers, ...(cookie ? { Cookie: cookie } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
  ...(body ? { body: JSON.stringify(body) } : {}),
});

test('preview rejects missing/mislabelled isolation, production origin and raw store/secret injection', async () => {
  const p = createPreview();
  try {
    for (const change of [{ENVIRONMENT:'production'}, {ISOLATION:undefined}, {ISOLATION:'production'}, {API_ENVIRONMENT:'production'},
      {PUBLIC_DATA_ENVIRONMENT:undefined}, {PREVIEW_ORIGIN:'https://options.moopertonic.net'},
      {DB:{}}, {PAPER_TRADES:{}}, {USERS:{}}, {SESSIONS:{}}, {LIVE_QUOTE_KEY:'forbidden'}]) {
      assert.equal((await gateway.fetch(request('/api/register', {method:'POST',body:{}}), {...p.env,...change})).status, 403);
    }
    for (const path of API_PATHS) {
      assert.equal((await p.fetch(request(path, {method:'POST', body:{},origin:'https://options.moopertonic.net'}))).status,403);
    }
    assert.equal(p.calls.api.length,0); assert.equal(p.objects.size,0);
  } finally {p.close();}
});
test('public read allowlist strips credentials, rejects bad query/date/key and cannot mutate', async () => {
  const p = createPreview();
  try {
    const r = await p.fetch(request('/browser-data/manifest.json', {cookie:'session=production-secret',headers:{Authorization:'Bearer production'}}));
    assert.equal(r.status,200); assert.equal((await r.json()).dates.length,1);
    assert.deepEqual(p.calls.reads[0].headers,{});
    for (const path of ['/browser-data/system/a.json','/browser-data/raw/qqq_chain_20260230.csv','/browser-data/manifest.json?url=https://prod.test','/browser-data/../system/a.json']) {
      assert.equal(publicReadAllowed(new URL(LOCAL_ORIGIN + path)),false);
      assert.equal((await p.fetch(request(path))).status,path.includes('?url=')?400:404);
    }
    assert.equal((await p.fetch(request('/browser-data/manifest.json',{method:'POST',body:{}}))).status,405);
    assert.equal((await p.fetch(request('/r2-proxy/intraday/latest.json'))).status,403);
    assert.equal((await p.fetch(request('/api/admin/migrate',{method:'POST',body:{}}))).status,404);
    assert.equal(p.calls.api.length,0); assert.equal(p.calls.reads.length,1);
  } finally {p.close();}
});
test('observable preview banner/identity and browser policy forbid production connections and forms', async () => {
  const p = createPreview();
  try {
    const r = await p.fetch(request('/'));
    const html = await r.text();
    assert.match(html,/LOCAL \/ DISPOSABLE PREVIEW/);
    assert.match(html,/const BROWSER_DATA = '\/browser-data';/);
    assert.doesNotMatch(html,/const BROWSER_DATA = location\.origin/);
    assert.match(r.headers.get('Content-Security-Policy'),/connect-src 'self'; form-action 'self'/);
    assert.equal(r.headers.get('X-Options-Environment'),'preview');
    const identity = await (await p.fetch(request('/__environment'))).json();
    assert.equal(identity.isolation,'disposable');
    assert.equal(identity.public_data,'read-only');
    assert.equal(p.env.DB,undefined); assert.equal(p.env.PAPER_TRADES,undefined);
  } finally {p.close();}
});
test('real D1 register/login/me/quote/execution/settle/logout stay local', async () => {
  const old = globalThis.fetch; globalThis.fetch = fixtureNetwork;
  const p = createPreview();
  try {
    const credentials = {username:'preview_reader',password:'disposable-password'};
    assert.equal((await p.fetch(request('/api/me',{cookie:'session=production-cookie'}))).status,401);
    const registered = await p.fetch(request('/api/register',{method:'POST',body:credentials}));
    assert.equal(registered.status,201);
    const cookie = registered.headers.get('Set-Cookie').split(';')[0];
    assert.match(registered.headers.get('Set-Cookie'),/HttpOnly; SameSite=Lax; Path=\//);
    assert.doesNotMatch(registered.headers.get('Set-Cookie'),/Domain=/);
    assert.equal((await p.fetch(request('/api/me',{cookie}))).status,200);
    assert.equal((await p.fetch(request('/api/login',{method:'POST',body:credentials}))).status,200);
    const quotes = await p.fetch(request('/api/live-quotes?symbols=QQQ'));
    assert.equal(quotes.status,200); assert.equal((await quotes.json()).fixture,true);
    const execution = await p.fetch(request('/api/paper-trade',{method:'POST',cookie,body:{
      execution_request_id:crypto.randomUUID(),sym:'QQQ',instrument_type:'share',side:'buy',qty:1,
    }}));
    const trade = await execution.json();
    assert.equal(execution.status,201,JSON.stringify(trade));
    assert.equal(trade.status,'filled');
    assert.ok(p.objects.size > 0);
    await createStorage(p.apiEnv).users.mutate(credentials.username, record => ({
      record: {...record, trades: [...record.trades, {sym:'.QQQ261027C500',underlying:'QQQ',instrument_type:'option',
        strike:500,type:'call',exp:'2026-10-27',side:'buy',qty:1,price:1,multiplier:100,execution_request_id:'fixture-expired'}]}, result:{}
    }));
    const settled = await p.fetch(request('/api/settle',{method:'POST',cookie,body:{as_of:'2026-10-28',spot_marks:{'QQQ:2026-10-27':501}}}));
    const settlement = await settled.json();
    assert.equal(settled.status,200,JSON.stringify(settlement));
    assert.equal(settlement.settled.length,1);
    for (const key of p.objects.keys()) assert.match(key,/^(system|paper-trades)\//);
    assert.equal(p.apiEnv.DB.raw('SELECT COUNT(*) as count FROM users')[0].count,1);
    const logout = await p.fetch(request('/api/logout',{method:'POST',cookie}));
    assert.equal(logout.status,204);
    assert.equal((await p.fetch(request('/api/me',{cookie}))).status,401);
    await assert.rejects(p.apiEnv.PAPER_TRADES.put('intraday/latest.json','bad'),/Capture write denied/);
    assert.throws(()=>fixtureNetwork('https://options.moopertonic.net/api/login'),/network denied/);
  } finally {p.close();globalThis.fetch=old;}
});
test('preview identifies fixture policy or reuses integrated G4a product validator',async()=>{
  const p=createPreview();
  try {
    if(p.policySource==='g4a-product') {
      const boundary=await import('../backend/operational-storage.js');
      for(const key of ['intraday/latest.json','manifest.json','system/../intraday/latest.json']) {
        await assert.rejects(()=>p.apiEnv.PAPER_TRADES.put(key,'bad'),boundary.CaptureWriteRefused);
      }
    } else {
      assert.equal(p.policySource,'local-fixture-pending-g4a');
      await assert.rejects(()=>p.apiEnv.PAPER_TRADES.put('intraday/latest.json','bad'),/Capture write denied/);
    }
  }finally{p.close();}
});
test('separate preview instances cannot share accounts, sessions or operational objects', async () => {
  const a=createPreview(), b=createPreview();
  try {
    const r=await a.fetch(request('/api/register',{method:'POST',body:{username:'isolated_a',password:'disposable-password'}}));
    const cookie=r.headers.get('Set-Cookie').split(';')[0];
    assert.equal((await b.fetch(request('/api/me',{cookie}))).status,401);
    assert.equal(b.apiEnv.DB.raw('SELECT COUNT(*) as count FROM users')[0].count,0);
    assert.equal(b.objects.size,0);
  } finally {a.close();b.close();}
});
