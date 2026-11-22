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

test('Kalshi and Credit Tape returns stay local and preserve failed owner/state checks', async t => {
  const origin = 'https://options.moopertonic.net';
  let status = 303, location = '/credit-tape/';
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.ok(!options.headers.get('Cookie')?.includes('btc-return'));
    return new Response(null, {status, headers: {Location: location}});
  });
  for (const query of ['', '?return=https://evil.invalid', '?return=//evil.invalid',
    '?return=/kalshi.html', '?return=kalshi&return=btc', '?return=kalshi%0d%0a']) {
    const response = await creditTape(new Request(origin + '/credit-tape/auth/login' + query));
    assert.match(response.headers.get('Set-Cookie'), /Max-Age=0/);
  }
  const login = await creditTape(new Request(origin + '/credit-tape/auth/login?return=kalshi'));
  assert.match(login.headers.get('Set-Cookie'), /__Host-btc-return=kalshi; Path=\/; Secure; HttpOnly; SameSite=Lax; Max-Age=600/);
  const callback = cookie => creditTape(new Request(origin + '/credit-tape/auth/callback?code=fixture&state=fixture',
    {headers: {Cookie: cookie}}));
  const response = await callback('__Host-btc-return=kalshi; __Host-cds-flow=fixture');
  assert.equal(response.headers.get('Location'), '/kalshi.html');
  assert.match(response.headers.get('Set-Cookie'), /Max-Age=0/);
  assert.equal((await callback('')).headers.get('Location'), '/credit-tape/');
  for (const cookie of ['__Host-btc-return=https://evil.invalid', '__Host-btc-return=kalshi; __Host-btc-return=btc']) {
    assert.equal((await callback(cookie)).headers.get('Location'), '/credit-tape/');
  }
  for (const denied of [400,401,403,500]) {
    status = denied;
    const result = await callback('__Host-btc-return=kalshi');
    assert.equal(result.status, denied);
    assert.equal(result.headers.get('Location'), '/credit-tape/');
  }
  status = 303;
  for (const target of ['https://evil.invalid/credit-tape/', '/credit-tape/?error=denied', '/credit-tape/#error']) {
    location = target;
    assert.equal((await callback('__Host-btc-return=kalshi')).headers.get('Location'), target);
  }
});

test('Kalshi login uses the same tab and an allowlisted return key', async () => {
  const {readFile} = await import('node:fs/promises');
  const html = await readFile(new URL('../site/kalshi.html', import.meta.url), 'utf8');
  const link = html.match(/<a[^>]+href="\/credit-tape\/auth\/login[^"]*"[^>]*>/)[0];
  assert.match(link, /\?return=kalshi"/);
  assert.doesNotMatch(link, /target=/);
});

test('TREU table and validated original CSV reference reach the fixed credit origin', async t => {
  let forwarded;
  t.mock.method(globalThis, 'fetch', async url => {
    forwarded = new URL(url).search;
    return new Response('fixture', {headers: {'Content-Disposition': 'attachment', 'X-Original-SHA256': 'fixture'}});
  });
  const base = 'https://options.moopertonic.net/credit-tape/';
  for (const query of ['?view=treu', '?view=treu-sample', '?view=treu&file=UK_TREU_20261120_1730.csv&sha=' + 'a'.repeat(64)]) {
    const response = await creditTape(new Request(base + query));
    assert.equal(forwarded, query);
    assert.equal(response.headers.get('Content-Disposition'), 'attachment');
  }
  await creditTape(new Request(base + '?view=treu&unexpected=1'));
  assert.equal(forwarded, '');
});

test('credit replay forwards only a frozen cutoff and bounded archive cursors', async t => {
  const path = '/credit-tape/replay', cutoff = '?one_clock_at=1795440000000';
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url);
    assert.equal(options.headers.get('Cookie'), '__Host-cds-session=fixture');
    return new Response('Sign in required', {status:401});
  });
  const request = query => creditTape(new Request('https://options.moopertonic.net' + path + query,
    {headers:{Cookie:'__Host-cds-session=fixture; other=private'}}));
  for (const query of [cutoff, cutoff+'&end=234&seq=0']) {
    const response = await request(query);
    assert.equal(response.status,401);
    assert.equal(response.headers.get('Cache-Control'),'private, no-store');
    assert.equal(calls.at(-1),'https://prolific-essence-production.up.railway.app'+path+query);
  }
  for (const query of ['', '?one_clock_at=invalid', cutoff+'&end=2', cutoff+'&from=yesterday']) {
    assert.equal((await request(query)).status,400);
  }
  assert.equal(calls.length,2);
});
