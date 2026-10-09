const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const KEYS = [
  'intraday/latest.json', 'intraday/spy/latest.json', 'intraday/prices.json',
  'intraday/health.json', 'derived/OIranges.csv',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
];
const ORIGIN = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const APP = 'https://live-options-view.otmooper12.workers.dev';
(async () => {
  const { default: worker } = await import('../worker.js');
  const originalFetch = global.fetch;
  const env = new Proxy({}, { get() { throw new Error('No bindings may be accessed'); } });
  let calls = [];
  const body = Uint8Array.from([0, 255, 13, 10, 0xc3, 0xa9, 123, 125]);
  let status = 200;
  let fail = false;
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    if (fail) throw new Error('secret upstream diagnostic');
    return new Response([204, 304].includes(status) ? null : body, {
      status,
      headers: { 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store',
        ETag: '"fixture"', 'Last-Modified': 'Thu, 08 Oct 2026 20:00:00 GMT',
        'Retry-After': '7', 'Set-Cookie': 'session=upstream',
        Location: 'https://untrusted.example/redirect', 'WWW-Authenticate': 'secret',
        'Access-Control-Allow-Origin': '*' },
    });
  };
  const request = (suffix, options = {}) => worker.fetch(new Request(APP + suffix, options), env);
  try {
    for (const key of KEYS) {
      for (const query of ['', '?_=1791506273571']) {
        const res = await request('/browser-data/' + key + query, { headers: {
          Cookie: 'session=private', Authorization: 'Bearer private', 'X-Live-Quote-Key': 'private',
          Range: 'bytes=1-2', 'If-None-Match': '"private"', Origin: APP,
        } });
        assert.equal(res.status, 200);
        assert.deepEqual(new Uint8Array(await res.arrayBuffer()), body);
        const call = calls.at(-1);
        assert.equal(call.url, ORIGIN + '/' + key + query);
        assert.deepEqual(call.options.headers, {});
        assert.equal(call.options.credentials, 'omit');
        assert.equal(call.options.redirect, 'manual');
        assert.equal(call.options.method, 'GET');
        assert.equal(call.options.body, undefined);
        for (const header of ['set-cookie', 'location', 'www-authenticate', 'access-control-allow-origin']) {
          assert.equal(res.headers.get(header), null, header + ' must not pass through');
        }
        assert.equal(res.headers.get('content-type'), 'application/octet-stream');
        assert.equal(res.headers.get('etag'), '"fixture"');
      }
    }
    const allowedCalls = calls.length;
    for (const host of ['https://options.moopertonic.net', 'http://localhost:8787',
      'https://preview.example', 'http://live-options-view.otmooper12.workers.dev',
      APP + ':8443', APP + '.evil.example']) {
      for (const origin of [undefined, APP]) {
        const headers = origin === undefined ? {} : { Origin: origin };
        assert.equal((await worker.fetch(new Request(host + '/browser-data/' + KEYS[0], { headers }), env)).status, 403);
      }
    }
    for (const origin of ['', 'null', 'https://other.example', APP + '/', APP + ':443',
      'http://live-options-view.otmooper12.workers.dev', APP + '.evil.example', APP + ', ' + APP]) {
      for (const key of KEYS) {
        assert.equal((await request('/browser-data/' + key, { headers: { Origin: origin } })).status, 403);
      }
    }
    assert.equal(calls.length, allowedCalls, 'wrong host or foreign/present-empty Origin never fetches');
    for (const key of [
      '', 'intraday/other.json', 'intraday/QQQ/latest.json', 'intraday/spy/latest.json/',
      'intraday//latest.json', 'intraday/%6catest.json', 'intraday%2flatest.json',
      'intraday/..%2fauth/remember_token.json', '%2e%2e%2fauth/remember_token.json',
      'https://evil.example/data', '//evil.example/data', 'auth/remember_token.json',
      'system/bot-membership-v1.json', 'paper-trades/a.json', 'macro/eia_steo.json',
      'manifest.json', 'raw/qqq_chain_2026-10-08.csv', 'derived/oiranges.csv',
    ]) {
      assert.equal((await request('/browser-data/' + key)).status, 404, key);
    }
    assert.equal((await request('/browser-data')).status, 404);
    for (const query of ['?url=https://evil.example', '?_=1&url=x', '?_=1&_=2', '?_=abc', '?_=',
      '?token=private', '?_=123456789012345678901', '?%5f=1']) {
      assert.equal((await request('/browser-data/' + KEYS[0] + query)).status, 400, query);
    }
    for (const method of ['HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
      for (const key of [...KEYS, 'auth/remember_token.json']) {
        const res = await request('/browser-data/' + key, { method });
        assert.equal(res.status, 405, method + ' ' + key);
        assert.equal(res.headers.get('allow'), 'GET');
      }
    }
    assert.equal(calls.length, allowedCalls, 'rejected requests never fetch');
    for (status of [204, 206, 301, 302, 303, 307, 308, 304, 400, 401, 403, 404, 429, 500, 503]) {
      const before = calls.length;
      const res = await request('/browser-data/' + KEYS[0]);
      assert.equal(res.status, status);
      assert.equal(calls.length, before + 1, 'no redirect or retry');
      assert.equal(res.headers.get('location'), null);
      assert.equal(res.headers.get('retry-after'), '7');
      assert.deepEqual(new Uint8Array(await res.arrayBuffer()), [204, 304].includes(status) ? new Uint8Array() : body);
    }
    fail = true;
    const failure = await request('/browser-data/' + KEYS[0]);
    assert.equal(failure.status, 502);
    assert.equal(await failure.text(), 'Public data upstream unavailable');
  } finally { global.fetch = originalFetch; }
  // Real shipped frontend source: every changed read uses only the new route.
  const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
  assert.ok(html.includes('fetch(`${BROWSER_DATA}/derived/OIranges.csv`'));
  for (const key of ['intraday/health.json', 'intraday/prices.json']) {
    assert.ok(html.includes('fetch(`${BROWSER_DATA}/' + key + '?_=${Date.now()}`)'));
  }
  assert.ok(html.includes('fetch(`${BROWSER_DATA}/${path}?_=${Date.now()}`)'));
  assert.ok(html.includes('const base = `${BROWSER_DATA}/squeeze-scanner/v1/scheduled`;'));
  assert.ok(html.includes('fetch(`${R2}/macro/eia_steo.json'));
  assert.ok(html.includes('fetch(`${R2}/raw/qqq_chain_'));
  assert.ok(!html.includes('/r2-proxy/'));
  const routing = html.slice(html.indexOf('const R2 ='), html.indexOf('const REFRESH'));
  assert.ok(routing.includes('location.origin'));
  for (const origin of [APP, 'https://options.moopertonic.net', 'http://localhost:8787',
    'https://preview.example', 'null', APP + ':8443', APP + '.evil.example',
    'http://live-options-view.otmooper12.workers.dev']) {
    const base = new Function('location', routing + '; return BROWSER_DATA;')({ origin });
    for (const key of KEYS) {
      assert.equal(base + '/' + key, (origin === APP ? '/browser-data' : ORIGIN) + '/' + key);
    }
  }
  console.log('browser data: 8 keys, strict paths/queries/methods, no bindings/credentials/redirects, byte/status preservation and frontend scope passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
