import assert from 'node:assert/strict';
import test from 'node:test';
import {creditTape} from '../backend/credit-tape.js';

test('private tape forwards only its own cookies to the fixed origin', async t => {
  const origin = 'https://options.moopertonic.net';
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++;
    assert.equal(url, 'https://prolific-essence-production.up.railway.app/credit-tape/');
    assert.equal(options.headers.get('Cookie'), '__Host-cds-session=fixture');
    assert.equal(options.headers.get('Authorization'), null);
    assert.equal(options.redirect, 'manual');
    return new Response('fixture');
  });
  for (const url of ['https://other.example/credit-tape/', origin + '/credit-tape/export']) {
    assert.equal((await creditTape(new Request(url))).status, 404);
  }
  assert.equal(calls, 0);
  const response = await creditTape(new Request(origin + '/credit-tape/?unused=1', {
    headers:{Cookie:'ordinary=fixture; __Host-cds-session=fixture', Authorization:'fixture'}
  }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'private, no-store');
  assert.equal(calls, 1);
});

test('BTC login return is fixed, does not forward navigation cookies, and preserves upstream callback denial', async t => {
  const origin = 'https://options.moopertonic.net'; let callbackStatus = 303, target = '/credit-tape/';
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.redirect, 'manual');
    assert.equal(options.headers.get('Cookie')?.includes('btc-return') || false, false);
    if (url.endsWith('/login')) return new Response(null, { status: 302, headers: { Location: 'https://github.com/login/oauth/authorize' } });
    assert.equal(url, 'https://prolific-essence-production.up.railway.app/credit-tape/auth/callback?code=synthetic&state=synthetic');
    return new Response(null, { status: callbackStatus, headers: { Location: target } });
  });
  const login = await creditTape(new Request(origin + '/credit-tape/auth/login?return=btc'));
  assert.match(login.headers.get('Set-Cookie'), /__Host-btc-return=btc/);
  const callback = () => creditTape(new Request(origin + '/credit-tape/auth/callback?code=synthetic&state=synthetic', {
    headers: { Cookie: '__Host-btc-return=btc; __Host-cds-flow=synthetic' },
  }));
  assert.equal((await callback()).headers.get('Location'), '/?chain=BTC');
  callbackStatus = 401; assert.equal((await callback()).status, 401);
  callbackStatus = 303; target = 'https://other.invalid/credit-tape/';
  assert.equal((await callback()).headers.get('Location'), target);
  const noReturn = await creditTape(new Request(origin + '/credit-tape/auth/login?return=https://evil.invalid'));
  assert.match(noReturn.headers.get('Set-Cookie'), /Max-Age=0/);
});
