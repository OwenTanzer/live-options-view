import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import gateway from '../frontend/worker.mjs';
import { buildAssets, verifyArtifact, ROOT, FLOOR } from '../scripts/build-assets.mjs';

const req = (path, init) => new Request('https://options.moopertonic.net' + path, init);
test('production delegation preserves URL, Origin, Cookie, body, status and Set-Cookie', async () => {
  let received;
  const response = await gateway.fetch(req('/api/login?test=1', { method: 'POST',
    headers: { Origin: 'https://options.moopertonic.net', Cookie: 'session=opaque' }, body: 'original' }), {
    ENVIRONMENT: 'production', API: { async fetch(r) { received = r; assert.equal(await r.text(), 'original');
      return new Response('ok', { status: 201, headers: { 'Set-Cookie': 'session=new; HttpOnly; Secure; SameSite=Lax; Path=/' } }); } },
    ASSETS: { fetch() { throw new Error('API reached static fallback'); } },
  });
  assert.equal(received.url, 'https://options.moopertonic.net/api/login?test=1');
  assert.equal(received.headers.get('Origin'), 'https://options.moopertonic.net');
  assert.equal(received.headers.get('Cookie'), 'session=opaque');
  assert.equal(response.status, 201); assert.match(response.headers.get('Set-Cookie'), /Secure/);
});
test('every reserved namespace remains on API including unknown future routes', async () => {
  for (const path of ['/api', '/api/new-route', '/browser-data', '/browser-data/manifest.json', '/r2-proxy/a']) {
    let called = false;
    const r = await gateway.fetch(req(path), { ENVIRONMENT: 'production', API: { fetch: async () => { called = true; return new Response('api'); } } });
    assert.equal(await r.text(), 'api'); assert.ok(called);
  }
});
test('missing API never falls back to network or static', async () => {
  const old = globalThis.fetch; globalThis.fetch = () => { throw Error('Network'); };
  try { assert.equal((await gateway.fetch(req('/api/me'), { ENVIRONMENT: 'production' })).status, 503); }
  finally { globalThis.fetch = old; }
});
test('static GET/HEAD only, HTML no-store, no scheduled export', async () => {
  let calls = 0;
  const env = { ENVIRONMENT: 'production', ASSETS: { fetch: async () => { calls++; return new Response('<html>ok</html>', {headers: {'Content-Type':'text/html'}}); } } };
  assert.equal((await gateway.fetch(req('/'), env)).headers.get('Cache-Control'), 'no-store');
  assert.equal((await gateway.fetch(req('/index.html'), env)).status, 200);
  assert.equal((await gateway.fetch(req('/x.js', { method: 'POST' }), env)).status, 405);
  assert.equal(calls, 2); assert.equal(gateway.scheduled, undefined);
});
test('unknown environment fails closed', async () => {
  assert.equal((await gateway.fetch(req('/'), {})).status, 503);
});
test('asset artifact contains no backend, producers, config, secrets or documentation', () => {
  const parent = mkdtempSync(resolve(tmpdir(), 'musef-assets-'));
  const out = resolve(parent, 'release');
  const manifest = buildAssets({ out, revision: FLOOR, now: new Date('2026-11-11T22:00:00Z') });
  assert.deepEqual(verifyArtifact(out), manifest);
  assert.ok(Object.keys(manifest.files).length > 25);
  for (const name of Object.keys(manifest.files)) assert.ok(name.startsWith('site/') || name.startsWith('frontend/'));
  assert.ok(!existsSync(resolve(out, 'backend'))); assert.ok(!existsSync(resolve(out, 'site/DEPLOYMENTS.md')));
  assert.ok(!existsSync(resolve(out, 'wrangler.toml')));
  assert.deepEqual(manifest.targets.producers, []);
  assert.match(readFileSync(resolve(out, 'site/index.html'), 'utf8'), /90cdc56-20261111220000/);
  assert.throws(() => buildAssets({ out, revision: FLOOR }), /never overwrite/);
});
test('gateway source graph is closed to operational imports and network fetch', () => {
  for (const name of ['frontend/worker.mjs', 'frontend/preview-policy.mjs']) {
    const source = readFileSync(resolve(ROOT, name), 'utf8');
    assert.doesNotMatch(source, /from\s+['"][^'"]*backend/);
    const code = source.replace(/\/\/[^\n]*/g, '');
    assert.doesNotMatch(code, /(?:await|return)\s+fetch\(|globalThis\.fetch/); // binding.fetch only
    assert.doesNotMatch(source, /scheduled\s*\(/);
  }
});
