import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../backend/index.js';
import { ownerIdentity, CREDIT_ORIGIN, OWNER_ID, SITE } from '../backend/kalshi-access.js';
import { withD1, seedAccounts, seedSession } from './helpers/test_env.mjs';
import { setupBtcOwner } from './helpers/btc_owner_fixture.mjs';
import { btcAllowed } from '../backend/btc-owner-link.js';
import { createStorage } from '../backend/storage.js';
const cds = '__Host-cds-session=synthetic_owner_session_12345';
const req = (path, body, token = 'paper-one', cookie = cds, extra = {}) => new Request(SITE + path, {
  method: body ? 'POST' : 'GET', headers: { Cookie: `session=${token}; ${cookie}`, Origin: SITE,
    'Content-Type': 'application/json', ...extra }, ...(body ? { body: JSON.stringify(body) } : {}),
});
async function setup() {
  const env = withD1({ LIVE_QUOTE_ORIGIN: 'https://fixture.invalid' }); setupBtcOwner(env);
  await seedAccounts(env, ['normal_book', 'foreign_book'].map(username => ({ username, balance_cash: 10000, trades: [] })));
  await seedSession(env, 'paper-one', 'normal_book'); await seedSession(env, 'paper-two', 'foreign_book');
  return env;
}
test('fixed upstream identity rejects redirects, wrong status, wrong types, malformed/expired and spoofed identity', async t => {
  let result = () => Response.json({ github_id: OWNER_ID }), calls = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++; assert.equal(url, CREDIT_ORIGIN + '/api/private/identity'); assert.equal(options.redirect, 'manual');
    assert.deepEqual(options.headers, { Cookie: cds, Accept: 'application/json' });
    assert.equal(options.cache, 'no-store'); return result();
  });
  assert.equal((await ownerIdentity(req('/'))).github_id, OWNER_ID);
  for (const cookie of ['', cds + '; ' + cds, '__Host-cds-session=short']) {
    const before = calls; assert.equal(await ownerIdentity(req('/', null, 'paper-one', cookie, { 'X-GitHub-ID': String(OWNER_ID) })), null);
    assert.equal(calls, before);
  }
  for (const status of [201, 202, 204, 301, 302, 303, 307, 308, 401, 403, 500]) {
    result = () => status === 204 ? new Response(null, { status }) : Response.json({ github_id: OWNER_ID }, { status });
    assert.equal(await ownerIdentity(req('/')), null, String(status));
  }
  for (const id of [OWNER_ID+1, String(OWNER_ID), null, {}, []]) {
    result = () => Response.json({ github_id: id }); assert.equal(await ownerIdentity(req('/')), null);
  }
  for (const r of [new Response('invalid', { headers: { 'Content-Type': 'application/json' } }),
    Response.json({ github_id: OWNER_ID }, { headers: { Location: SITE } }),
    Response.json({ github_id: OWNER_ID, padding: 'x'.repeat(2000) }),
    new Response(JSON.stringify({ github_id: OWNER_ID }), { headers: { 'Content-Type': 'text/plain' } })]) {
    result = () => r; assert.equal(await ownerIdentity(req('/')), null);
  }
  result = () => { throw Error('expired/unavailable'); }; assert.equal(await ownerIdentity(req('/')), null);
});

test('GitHub binds only the requested current normal session and revalidates rights/identity on every read', async t => {
  const env = await setup(); let id = OWNER_ID, status = 200;
  t.mock.method(globalThis, 'fetch', async url => {
    assert.equal(String(url), CREDIT_ORIGIN + '/api/private/identity'); return Response.json({ github_id: id }, { status });
  });
  const session = { token: 'paper-one', username: 'normal_book', record: {} };
  const allowed = () => btcAllowed(env, session, req('/api/btc-options'));
  const bind = (patch = {}, token) => worker.fetch(req('/api/btc-options/owner-link', {
    action: 'github', confirm: true, automatic: true, paper_account: 'normal_book', ...patch,
  }, token), env);
  try {
    assert.equal(await allowed(), false, 'verified GitHub alone is not a paper binding');
    assert.equal((await bind({ paper_account: 'foreign_book' })).status, 403);
    assert.equal((await bind({ confirm: false })).status, 403);
    assert.equal((await worker.fetch(req('/api/btc-options/owner-link', { action: 'github', confirm: true, paper_account: 'normal_book' }, 'paper-one', cds, { Origin: 'https://evil.invalid' }), env)).status, 403);
    assert.equal((await bind()).status, 200);
    assert.equal(await allowed(), true);
    assert.equal(await btcAllowed(env, { token: 'paper-two', username: 'foreign_book', record: {} }, req('/api/btc-options', null, 'paper-two')), false);
    id++; assert.equal(await allowed(), false); id = OWNER_ID;
    status = 401; assert.equal(await allowed(), false); status = 200;
    const receipt = env.BTC_OWNER_RIGHTS_RECEIPT; env.BTC_OWNER_RIGHTS_RECEIPT = 'changed';
    assert.equal(await allowed(), false); env.BTC_OWNER_RIGHTS_RECEIPT = receipt;
    await env.DB.prepare('UPDATE btc_github_sessions SET expires=0').run(); assert.equal(await allowed(), false);
    assert.equal((await bind()).status, 200);
    await worker.fetch(req('/api/btc-options/owner-link', { action: 'unlink' }), env);
    assert.equal(await allowed(), false); assert.equal((await bind()).status, 403, 'reload cannot undo unlink');
    assert.equal((await bind({ automatic: false })).status, 200);
    await worker.fetch(req('/api/logout', {}), env);
    assert.equal(await allowed(), false);
    await seedSession(env, 'paper-one', 'normal_book'); assert.equal(await allowed(), false);
    id++; assert.equal((await bind()).status, 403);
  } finally { env.DB.db.close(); }
});

test('private history never follows a stale GitHub proof; unlink wins over in-flight automatic binding', async t => {
  const env = await setup(); let authorized = true, block, arrived;
  t.mock.method(globalThis, 'fetch', async () => {
    if (arrived) { const signal = arrived; arrived = null; await new Promise(resolve => { block = resolve; signal(); }); }
    return Response.json({ github_id: authorized ? OWNER_ID : OWNER_ID+1 });
  });
  const body = { action: 'github', confirm: true, automatic: true, paper_account: 'normal_book' };
  const bind = () => worker.fetch(req('/api/btc-options/owner-link', body), env);
  try {
    assert.equal((await bind()).status, 200);
    await createStorage(env).users.put('normal_book', { username: 'normal_book', balance_cash: 10000,
      trades: [{ instrument_type: 'futures_option', source_quote: { ask: 42 } }] });
    assert.equal((await (await worker.fetch(req('/api/me'), env)).json()).trades.length, 1);
    authorized = false;
    assert.equal((await (await worker.fetch(req('/api/me'), env)).json()).trades.length, 0);
    authorized = true;
    const waiting = new Promise(resolve => { arrived = resolve; }); const pending = bind(); await waiting;
    await worker.fetch(req('/api/btc-options/owner-link', { action: 'unlink' }), env); block();
    assert.equal((await pending).status, 403);
    assert.equal((await (await worker.fetch(req('/api/me'), env)).json()).trades.length, 0);
    assert.equal((await worker.fetch(req('/api/btc-options/owner-link', { ...body, automatic: false }), env)).status, 200);
    const waitingRead = new Promise(resolve => { arrived = resolve; });
    const pendingRead = worker.fetch(req('/api/me'), env); await waitingRead;
    await worker.fetch(req('/api/btc-options/owner-link', { action: 'unlink' }), env); block();
    assert.equal((await (await pendingRead).json()).trades.length, 0, 'unlink during identity I/O wins over an earlier proof lookup');
  } finally { env.DB.db.close(); }
});
