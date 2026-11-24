import {origin as usdcOrigin, usdcFixture, instrument} from './helpers/usdc_fixture.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign, randomUUID } from 'node:crypto';
import worker from '../backend/index.js';
import { withD1, seedAccounts, seedSession } from './helpers/test_env.mjs';
import { setupBtcOwner } from './helpers/btc_owner_fixture.mjs';
import { createStorage } from '../backend/storage.js';
import { btcAllowed } from '../backend/btc-owner-link.js';

const origin = 'https://options.moopertonic.net';
const digest = value => createHash('sha256').update(value).digest();
async function fixture() {
  const env = withD1({ LIVE_QUOTE_ORIGIN: 'https://synthetic.invalid', LIVE_QUOTE_KEY: 'synthetic' });
  setupBtcOwner(env);
  await seedAccounts(env, ['normal_paper', 'Owen'].map(username => ({ username, trades: [], balance_cash: 10000 })));
  await seedSession(env, 'normal-session', 'normal_paper');
  await seedSession(env, 'other-session', 'Owen');
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  const jwk = publicKey.export({ format: 'jwk' });
  const cose = Buffer.concat([Buffer.from('a5010203262001215820', 'hex'), Buffer.from(jwk.x, 'base64url'),
    Buffer.from('225820', 'hex'), Buffer.from(jwk.y, 'base64url')]).toString('base64');
  const id = Buffer.from(randomUUID()).toString('base64url');
  await env.DB.prepare("INSERT INTO trade_journal_members(member,credential_id,public_key) VALUES('Owen',?,?)").bind(id, cose).run();
  const request = (path, body, token = 'normal-session', headers = {}) => new Request(origin + path, {
    method: body === undefined ? 'GET' : 'POST', headers: { Origin: origin, 'Content-Type': 'application/json',
      ...(token ? { Cookie: 'session=' + token } : {}), ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const call = (body, token, headers) => worker.fetch(request('/api/btc-options/owner-link', body, token, headers), env);
  const assertion = (challenge, patch = {}) => {
    const clientData = Buffer.from(JSON.stringify({ type: 'webauthn.get', challenge, origin, ...patch.client }));
    const authData = Buffer.concat([digest(patch.rp || 'options.moopertonic.net'), Buffer.from([patch.flags ?? 5]), Buffer.alloc(4)]);
    return { id: patch.id || id, rawId: patch.id || id, type: 'public-key', clientExtensionResults: {}, response: {
      clientDataJSON: clientData.toString('base64url'), authenticatorData: authData.toString('base64url'),
      signature: sign('sha256', Buffer.concat([authData, digest(clientData)]), patch.privateKey || privateKey).toString('base64url'),
    } };
  };
  const options = async token => { const r = await call({ action: 'options', confirm: true }, token); assert.equal(r.status, 200); return r.json(); };
  const verify = async (opts, patch, token) => call({ action: 'verify', confirm: true, response: assertion(opts.challenge, patch) }, token);
  const allowed = async (token = 'normal-session', username = 'normal_paper') => btcAllowed(env, { token, username, record: {} });
  return { env, id, call, options, verify, assertion, allowed, request, close: () => env.DB.db.close() };
}

test('explicit owner proof links only the current normal session; no text, cookie, or legacy allowlist grants', async () => {
  const h = await fixture();
  try {
    h.env.BTC_OPTIONS_USERS = 'normal_paper,Owen';
    assert.equal(await h.allowed(), false);
    assert.equal(await h.allowed('other-session', 'Owen'), false);
    assert.equal((await h.call({ action: 'options', confirm: true }, null, { Cookie: '__Host-journal=synthetic' })).status, 401);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.invalid' }, { 'Content-Type': 'text/plain' }, { 'Sec-Fetch-Site': 'cross-site' }]) {
      assert.equal((await h.call({ action: 'options', confirm: true }, undefined, headers)).status, 403);
    }
    assert.equal((await h.call({ action: 'options', username: 'Owen' })).status, 403);
    const opts = await h.options();
    assert.deepEqual(opts.allowCredentials || [], []);
    assert.equal(opts.userVerification, 'required');
    assert.equal(JSON.stringify(opts).includes(h.id), false);
    assert.equal((await h.verify(opts, {}, 'other-session')).status, 403);
    assert.equal((await h.verify(opts)).status, 200);
    assert.equal(await h.allowed(), true);
    assert.equal(await h.allowed('other-session', 'Owen'), false);
    assert.equal((await h.verify(opts)).status, 403, 'single use even with a zero-counter passkey');
    const unlink = await h.call({ action: 'unlink' }); assert.equal(unlink.status, 200);
    assert.equal(await h.allowed(), false);
    assert.equal((await h.verify(opts)).status, 403);
  } finally { h.close(); }
});

test('wrong member, signature, RP, origin, challenge and UV fail without a grant', async () => {
  const h = await fixture();
  try {
    const stranger = generateKeyPairSync('ec', { namedCurve: 'prime256v1' }).privateKey;
    for (const patch of [{ privateKey: stranger }, { rp: 'evil.invalid' }, { flags: 1 },
      { client: { origin: 'https://evil.invalid' } }, { client: { challenge: 'replay' } }, { id: 'unknown' }]) {
      const opts = await h.options(); assert.equal((await h.verify(opts, patch)).status, 403);
      assert.equal(await h.allowed(), false);
      assert.equal((await h.verify(opts)).status, 403, 'failed assertion consumes challenge');
    }
    await h.env.DB.prepare("UPDATE trade_journal_members SET member='Jayden' WHERE member='Owen'").run();
    assert.equal((await h.verify(await h.options())).status, 403, 'journal access is not subscriber ownership');
  } finally { h.close(); }
});

test('expiry, replacement challenges, concurrent replay, logout and credential revocation', async () => {
  const h = await fixture();
  try {
    const old = await h.options(); await h.options();
    assert.equal((await h.verify(old)).status, 403);
    const expired = await h.options(); await h.env.DB.prepare('UPDATE btc_owner_challenges SET expires=0').run();
    assert.equal((await h.verify(expired)).status, 403);
    const opts = await h.options();
    const pair = await Promise.all([h.verify(opts), h.verify(opts)]);
    assert.deepEqual(pair.map(r => r.status).sort(), [200, 403]);
    await h.env.DB.prepare('UPDATE btc_owner_sessions SET expires=0').run(); assert.equal(await h.allowed(), false);
    assert.equal((await h.verify(await h.options())).status, 200);
    await h.env.DB.prepare("DELETE FROM trade_journal_members WHERE member='Owen'").run();
    assert.equal(await h.allowed(), false);
    assert.equal((await h.env.DB.prepare('SELECT count(*) AS n FROM btc_owner_sessions').first()).n, 0);
    const waiting = await h.options();
    assert.equal((await worker.fetch(h.request('/api/logout', {}), h.env)).status, 204);
    assert.equal((await h.verify(waiting)).status, 401);
    assert.equal((await h.env.DB.prepare('SELECT count(*) AS n FROM btc_owner_challenges').first()).n, 0);
  } finally { h.close(); }
});

test('source rights fail closed, account deletion/recreation and server-side logout cannot carry grants', async () => {
  const h = await fixture();
  try {
    assert.equal((await h.verify(await h.options())).status, 200);
    const receipt = h.env.BTC_OWNER_RIGHTS_RECEIPT;
    h.env.BTC_OWNER_RIGHTS_RECEIPT = 'changed-source'; assert.equal(await h.allowed(), false);
    h.env.BTC_OWNER_RIGHTS_RECEIPT = ''; assert.equal(await h.allowed(), false);
    assert.equal((await h.call({ action: 'options', confirm: true })).status, 503);
    h.env.BTC_OWNER_RIGHTS_RECEIPT = receipt;
    h.env.BTC_OWNER_LINK = 'disabled'; assert.equal(await h.allowed(), false);
    h.env.BTC_OWNER_LINK = 'enabled';
    await worker.fetch(h.request('/api/logout', {}), h.env);
    assert.equal(await h.allowed(), false);
    await seedSession(h.env, 'normal-session', 'normal_paper');
    assert.equal(await h.allowed(), false);
    assert.equal((await h.verify(await h.options())).status, 200);
    await createStorage(h.env).users.delete('normal_paper');
    await seedAccounts(h.env, [{ username: 'normal_paper', trades: [], balance_cash: 10000 }]);
    assert.equal(await h.allowed(), false);
  } finally { h.close(); }
});

test('account history is private until proof; revoked source cannot be read even through /me', async () => {
  const h = await fixture();
  try {
    await createStorage(h.env).users.put('normal_paper', { username: 'normal_paper', balance_cash: 10000,
      trades: [{ instrument_type: 'futures_option', source_quote: { ask: 12345 } }, { instrument_type: 'equity', sym: 'QQQ' }] });
    const me = async () => (await worker.fetch(h.request('/api/me'), h.env)).json();
    assert.equal((await me()).trades.length, 1);
    assert.equal((await h.verify(await h.options())).status, 200);
    assert.equal((await me()).trades.length, 2);
    await h.call({ action: 'unlink' });
    assert.equal((await me()).trades.length, 1);
    h.env.BTC_OPTIONS_USERS = 'normal_paper';
    const r = await worker.fetch(h.request('/api/btc-options'), h.env);
    assert.equal(r.status, 403); assert.equal(r.headers.get('Cache-Control'), 'no-store');
  } finally { h.close(); }
});

test('in-flight source reads fail closed on revocation and missing schema cannot fall back to usernames', async () => {
  const h = await fixture(), originalFetch = global.fetch;
  try {
    assert.equal((await h.verify(await h.options())).status, 200);
    h.env.BTC_USDC_DATA_ORIGIN = usdcOrigin;
    global.fetch = async url => {
      if (new URL(url).pathname.endsWith('/get_instruments')) await h.call({ action: 'unlink' });
      return usdcFixture(url);
    };
    const response = await worker.fetch(h.request('/api/btc-options'), h.env);
    assert.equal(response.status, 403);
    assert.equal((await response.text()).includes(instrument), false);
    h.env.DB.db.exec('DROP TABLE btc_owner_sessions');
    h.env.BTC_OPTIONS_USERS = 'normal_paper';
    assert.equal(await h.allowed(), false);
    const r = await worker.fetch(h.request('/api/btc-options'), h.env);
    assert.equal(r.status, 403);
  } finally { global.fetch = originalFetch; h.close(); }
});
