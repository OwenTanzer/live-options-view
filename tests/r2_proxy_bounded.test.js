// tests/r2_proxy_bounded.test.js — G4b: the legacy /r2-proxy/ route enforces
// the same public-key/read policy as /browser-data/. Nothing wider.
// Mirrors tests/browser_data.test.js case-for-case for the new route, plus
// consumer-compatibility for diag.html's exact URLs and a drift guard
// pinning the backend allowlist to the gateway preview-policy.
const test = require('node:test');
const assert = require('node:assert/strict');

const APP = 'https://live-options-view.otmooper12.workers.dev';
const ORIGIN = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const POSITIVE_KEYS = [
  'intraday/latest.json', 'intraday/spy/latest.json', 'intraday/ibit/latest.json',
  'intraday/prices.json', 'intraday/health.json', 'derived/OIranges.csv',
  'macro/eia_steo.json', 'manifest.json',
  'raw/qqq_chain_20261027.csv', 'raw/opex/qqq_chain_20240229.csv',
  'prices/qqq_intraday_20261027.csv',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
  'news/v1/views/today.json', 'news/v1/views/2026-11-11.json',
];
const NEGATIVE_KEYS = [
  '', 'system/bot-membership-v1.json', 'paper-trades/requests/x.json',
  'intraday/other.json', 'raw/qqq_chain_20260229.csv', 'raw/qqq_chain_20261301.csv',
  'raw/qqq_chain_20260431.csv', 'manifest.json.png', 'manifest.json/',
  'news/v1/views/', 'news/v1/views/../manifest.json', 'news/v1/views/%2e%2e/manifest.json',
];
const BAD_QUERIES = [
  '?url=x', '?_=abc', '?_=', '?_=1&_=2', '?_=' + '1'.repeat(21),
  '?imgtest=1791506273571', '?_=1791506273571_0b',
];

let worker;
let calls;
let status;
let fail;
const body = Uint8Array.from([0, 255, 13, 10, 0xc3, 0xa9, 123, 125]);
const env = new Proxy({}, { get() { throw new Error('No bindings may be accessed'); } });
const request = (suffix, options = {}) => worker.fetch(new Request(APP + suffix, options), env);

test.before(async () => {
  worker = (await import('../backend/index.js')).default;
  calls = [];
  status = 200;
  fail = false;
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    if (fail) throw new Error('secret upstream diagnostic');
    return new Response([204, 304].includes(status) ? null : body, {
      status,
      headers: {
        'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store',
        ETag: '"fixture"', 'Last-Modified': 'Thu, 08 Oct 2026 20:00:00 GMT',
        'Retry-After': '7', 'Set-Cookie': 'session=upstream',
        Location: 'https://untrusted.example/redirect', 'WWW-Authenticate': 'secret',
        'Access-Control-Allow-Origin': '*',
      },
    });
  };
});

test('positive keys return 200 with upstream discipline', async () => {
  for (const key of POSITIVE_KEYS) {
    for (const query of ['', '?_=1791506273571']) {
      calls.length = 0;
      const res = await request('/r2-proxy/' + key + query, { headers: { Origin: APP } });
      assert.equal(res.status, 200, key + query);
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
  // No Origin header is also allowed (same rule as /browser-data/).
  const res = await request('/r2-proxy/manifest.json');
  assert.equal(res.status, 200);
});

test('negative keys return 404 without upstream fetch', async () => {
  for (const key of NEGATIVE_KEYS) {
    const before = calls.length;
    const res = await request('/r2-proxy/' + key, { headers: { Origin: APP } });
    assert.equal(res.status, 404, JSON.stringify(key));
    assert.equal(calls.length, before, key + ' must not reach upstream');
  }
});

test('bad queries return 400 without upstream fetch', async () => {
  for (const query of BAD_QUERIES) {
    const before = calls.length;
    const res = await request('/r2-proxy/manifest.json' + query, { headers: { Origin: APP } });
    assert.equal(res.status, 400, query);
    assert.equal(calls.length, before, query + ' must not reach upstream');
  }
});

test('non-GET methods return 405 with Allow: GET', async () => {
  for (const method of ['POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']) {
    const before = calls.length;
    const res = await request('/r2-proxy/manifest.json', { method, headers: { Origin: APP } });
    assert.equal(res.status, 405, method);
    assert.equal(res.headers.get('Allow'), 'GET');
    assert.equal(calls.length, before, method + ' must not reach upstream');
  }
});

test('wrong origin returns 403 without upstream fetch', async () => {
  const other = 'https://options.mooptonic.net';
  const before = calls.length;
  const res1 = await worker.fetch(new Request(other + '/r2-proxy/manifest.json', { headers: { Origin: APP } }), env);
  assert.equal(res1.status, 403, 'wrong url.origin');
  const res2 = await request('/r2-proxy/manifest.json', { headers: { Origin: other } });
  assert.equal(res2.status, 403, 'foreign Origin header');
  const res3 = await request('/r2-proxy/manifest.json', { headers: { Origin: '' } });
  assert.equal(res3.status, 403, 'empty Origin header');
  assert.equal(calls.length, before);
});

test('upstream status preserved and throw becomes 502', async () => {
  for (const s of [204, 304, 404, 503]) {
    status = s;
    const res = await request('/r2-proxy/manifest.json', { headers: { Origin: APP } });
    assert.equal(res.status, s);
  }
  status = 200;
  fail = true;
  const res = await request('/r2-proxy/manifest.json', { headers: { Origin: APP } });
  assert.equal(res.status, 502);
  fail = false;
});

test('bare /r2-proxy returns 404', async () => {
  const before = calls.length;
  const res = await request('/r2-proxy', { headers: { Origin: APP } });
  assert.equal(res.status, 404);
  assert.equal(calls.length, before);
});

test("diag consumer URLs keep working through the bounded route", async () => {
  const urls = [
    '/r2-proxy/manifest.json',
    '/r2-proxy/derived/OIranges.csv',
    '/r2-proxy/intraday/latest.json',
    '/r2-proxy/intraday/latest.json?_=' + Date.now(),
    '/r2-proxy/intraday/latest.json?_=' + Date.now() + '0',
  ];
  for (const suffix of urls) {
    const res = await request(suffix, { headers: { Origin: APP } });
    assert.equal(res.status, 200, suffix);
  }
  // The old burst form (digits + _ + index + b) is now refused.
  const res = await request('/r2-proxy/intraday/latest.json?_=' + Date.now() + '_0b', { headers: { Origin: APP } });
  assert.equal(res.status, 400);
});

test('bounded proxy matches /browser-data/ status for the same keys', async () => {
  const cases = [
    ...POSITIVE_KEYS.map(k => ['/r2-proxy/' + k, '/browser-data/' + k, 200]),
    ...NEGATIVE_KEYS.map(k => ['/r2-proxy/' + k, '/browser-data/' + k, 404]),
  ];
  for (const [proxy, browser, expected] of cases) {
    const a = await request(proxy, { headers: { Origin: APP } });
    const b = await request(browser, { headers: { Origin: APP } });
    assert.equal(a.status, expected, proxy);
    assert.equal(b.status, expected, browser);
    assert.equal(a.status, b.status, proxy + ' vs ' + browser);
  }
});

test('backend allowlist agrees with gateway preview-policy', async () => {
  const { isPublicBrowserDataKey } = await import('../backend/browser-data.js');
  const { publicReadAllowed } = await import('../frontend/preview-policy.mjs');
  const keys = [...POSITIVE_KEYS, ...NEGATIVE_KEYS, 'raw/qqq_chain_20240229.csv', 'news/v1/views/a/b/c.json'];
  for (const key of keys) {
    const backend = isPublicBrowserDataKey(key);
    const gateway = publicReadAllowed(new URL(APP + '/browser-data/' + key));
    assert.equal(gateway, backend, key);
  }
});
