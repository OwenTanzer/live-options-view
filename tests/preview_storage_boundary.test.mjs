// G4a — the storage face of preview-production-access isolation (Steve).
//
// WHY this file exists: the preview contract promises ENVIRONMENT=preview, an
// exact preview origin, ISOLATION=disposable, a local API binding, a separate
// read-only PUBLIC_DATA and a denied /r2-proxy. Musef's preview_isolation suite
// proves the gateway refuses all of that. This suite covers the half G4a owns:
// that no mutation path reaches a capture prefix, that no caller-supplied value
// can smuggle one, that no mutation path can fall back to a remote origin, and
// that the refusal comes from product code rather than only from the local
// emulation.
//
// What it cannot prove: application and emulation isolation only. It says
// nothing about provider IAM, about an arbitrary injected binding's identity,
// or about a hosted preview's real resource IDs. Nova verifies those.
//
// Attaches to: tests/preview_storage_boundary.test.mjs on the branch carrying
// frontend/worker.mjs, frontend/preview-policy.mjs and scripts/preview-local.mjs
// (musef/225-assets-preview). One test activates only once G4a's
// backend/operational-storage.js is on that base; until then it is a todo.

import test from 'node:test';
import assert from 'node:assert/strict';
import gateway from '../frontend/worker.mjs';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { API_PATHS } from '../frontend/preview-policy.mjs';

// The prefixes the collector publishes into. Every one is capture: a preview
// write here is damage to the published record, not a preview bug.
const CAPTURE = /^(intraday|raw|prices|derived|baselines|macro|settlement|oa203|moo144|squeeze-scanner)\//;
const OPERATIONAL = /^(system|paper-trades)\//;

const boundary = await import('../backend/operational-storage.js').catch(() => null);

const request = (path, { method = 'GET', body, cookie, origin = LOCAL_ORIGIN, headers = {} } = {}) =>
  new Request(LOCAL_ORIGIN + path, {
    method,
    headers: {
      Origin: origin,
      ...headers,
      ...(cookie ? { Cookie: cookie } : {}),
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });

const captureKeys = objects => [...objects.keys()].filter(key => CAPTURE.test(key));

test('every mutation route is delegated locally and leaves no capture object', async () => {
  const p = createPreview();
  try {
    const bodies = {
      '/api/register': { username: 'storage_probe', password: 'disposable-password' },
      '/api/login': { username: 'storage_probe', password: 'disposable-password' },
      '/api/logout': {},
      '/api/bot-metadata': { username: 'storage_probe' },
      '/api/bot-index/reconcile': {},
      '/api/paper-trade': {
        execution_request_id: crypto.randomUUID(), sym: 'QQQ', instrument_type: 'share', side: 'buy', qty: 1,
      },
      '/api/settle': { as_of: '2026-10-28', spot_marks: {} },
      '/api/live-quotes': {},
      '/api/me': {},
    };
    for (const [path, body] of Object.entries(bodies)) {
      assert.ok(API_PATHS.has(path), `${path} must be a reserved API path`);
      const response = await p.fetch(request(path, { method: 'POST', body }));
      assert.ok(response.status < 500, `${path} must not fail server-side in preview (${response.status})`);
      assert.ok(p.calls.api.includes(path), `${path} must reach the local API binding`);
    }
    assert.deepEqual(captureKeys(p.objects), [], 'no mutation path may write a capture prefix');
    for (const key of p.objects.keys()) {
      assert.match(key, OPERATIONAL, `unexpected preview object outside operational space: ${key}`);
    }
  } finally {
    p.close();
  }
});

test('caller-supplied key material cannot smuggle a capture write', async () => {
  const p = createPreview();
  try {
    const hostile = [
      { path: '/api/paper-trade', body: {
        execution_request_id: '../../../intraday/latest.json', sym: 'QQQ',
        instrument_type: 'share', side: 'buy', qty: 1 } },
      { path: '/api/paper-trade', body: {
        execution_request_id: crypto.randomUUID(), sym: 'raw/qqq_chain_20261111.csv',
        instrument_type: 'share', side: 'buy', qty: 1 } },
      { path: '/api/register', body: { username: '../system/x', password: 'disposable-password' } },
      { path: '/api/register', body: { username: 'a/../../intraday/latest', password: 'disposable-password' } },
      { path: '/api/bot-metadata', body: { username: 'system/../intraday', alias: 'x' } },
    ];
    for (const { path, body } of hostile) {
      const response = await p.fetch(request(path, { method: 'POST', body }));
      assert.ok([400, 401, 403, 404, 409, 503].includes(response.status),
        `${path} accepted hostile material: ${response.status}`);
    }
    assert.deepEqual(captureKeys(p.objects), [], 'hostile input must not create a capture object');

    // The store itself is the last line: it must refuse a capture key outright.
    await assert.rejects(() => p.apiEnv.PAPER_TRADES.put('intraday/latest.json', 'bad'), /Capture write denied/);
    await assert.rejects(() => p.apiEnv.PAPER_TRADES.put('manifest.json', 'bad'), /Capture write denied/);
    await assert.rejects(() => p.apiEnv.PAPER_TRADES.put('paper-trades/../../intraday/x', 'bad'), /Capture write denied/);
  } finally {
    p.close();
  }
});

// A todo rather than a skip: when G4a's module lands on this base the test
// starts running by itself, so preview cannot silently keep its own copy of the
// production rule.
test('the write refusal comes from the production operational boundary',
  { todo: boundary ? false : 'G4a backend/operational-storage.js is not on this base; the preview store still re-implements the prefix rule' },
  () => {
    if (!boundary) return;    // the todo above carries the reason until it lands
    for (const key of ['intraday/latest.json', 'raw/qqq_chain_20261111.csv', 'manifest.json',
      'system/../intraday/latest.json']) {
      assert.throws(() => boundary.assertOperationalKey(key), boundary.CaptureWriteRefused, key);
    }
    for (const key of ['system/bot-membership-v1.json',
      'paper-trades/requests/12345678-1234-4234-8234-123456789abc.json']) {
      assert.equal(boundary.assertOperationalKey(key), key);
    }
  });

test('no mutation path can reach a remote origin', async () => {
  const p = createPreview();
  const original = globalThis.fetch;
  const seen = [];
  globalThis.fetch = input => {
    seen.push(String(input?.url ?? input));
    throw new Error('network denied in preview');
  };
  try {
    const registered = await p.fetch(request('/api/register', {
      method: 'POST', body: { username: 'no_network', password: 'disposable-password' } }));
    assert.equal(registered.status, 201);
    const cookie = registered.headers.get('Set-Cookie').split(';')[0];
    // With every outbound call denied, an execution must fail rather than find
    // another route to a quote or an account store.
    const execution = await p.fetch(request('/api/paper-trade', {
      method: 'POST', cookie,
      body: { execution_request_id: crypto.randomUUID(), sym: 'QQQ',
        instrument_type: 'share', side: 'buy', qty: 1 },
    }));
    assert.ok(execution.status >= 500, `a denied network must fail the execution, got ${execution.status}`);
    assert.deepEqual(captureKeys(p.objects), []);
    for (const url of seen) {
      assert.match(url, /^https:\/\/preview-quotes\.invalid\/live-quotes/,
        `unexpected outbound origin: ${url}`);
    }
  } finally {
    globalThis.fetch = original;
    p.close();
  }
});

test('the fixture network is the only remote any mutation path may use', async () => {
  const p = createPreview();
  const original = globalThis.fetch;
  const seen = [];
  globalThis.fetch = input => {
    seen.push(String(input?.url ?? input));
    return fixtureNetwork(input);
  };
  try {
    const registered = await p.fetch(request('/api/register', {
      method: 'POST', body: { username: 'fixture_only', password: 'disposable-password' } }));
    assert.equal(registered.status, 201);
    const cookie = registered.headers.get('Set-Cookie').split(';')[0];
    const execution = await p.fetch(request('/api/paper-trade', {
      method: 'POST', cookie,
      body: { execution_request_id: crypto.randomUUID(), sym: 'QQQ',
        instrument_type: 'share', side: 'buy', qty: 1 },
    }));
    assert.ok(execution.status === 201 || execution.status === 409, `execution: ${execution.status}`);
    assert.ok(seen.length > 0, 'the execution should have consulted the fixture quote network');
    for (const url of seen) {
      assert.match(url, /^https:\/\/preview-quotes\.invalid\/live-quotes/, `escaped fixture network: ${url}`);
    }
    assert.deepEqual(captureKeys(p.objects), []);
  } finally {
    globalThis.fetch = original;
    p.close();
  }
});

test('PUBLIC_DATA is read-only and the generic R2 proxy is denied', async () => {
  const p = createPreview();
  try {
    assert.equal(typeof p.env.PUBLIC_DATA.put, 'undefined', 'PUBLIC_DATA must expose no write method');
    assert.equal(typeof p.env.PUBLIC_DATA.delete, 'undefined');

    for (const key of Object.keys(p.docs)) {
      const read = await p.fetch(request('/browser-data/' + key));
      assert.equal(read.status, 200, `${key} should be an approved read`);
      const write = await p.fetch(request('/browser-data/' + key, { method: 'POST', body: {} }));
      assert.equal(write.status, 405, `${key} must reject a write`);
    }

    for (const path of ['/browser-data/system/bot-membership-v1.json',
      '/browser-data/paper-trades/requests/x.json',
      '/browser-data/intraday/20261111/snapshot_123535793801.csv',
      '/browser-data/raw/weekly/20261113/qqq_chain_20261111_093000000000.csv']) {
      assert.equal((await p.fetch(request(path))).status, 404, `${path} must not be preview-readable`);
    }

    for (const path of ['/r2-proxy/intraday/latest.json', '/r2-proxy/manifest.json', '/r2-proxy/']) {
      assert.equal((await p.fetch(request(path))).status, 403, `${path} proxy must be denied`);
    }
    assert.deepEqual(captureKeys(p.objects), []);
  } finally {
    p.close();
  }
});

test('binding mismatches and a production identity are refused before any route runs', async () => {
  const p = createPreview();
  try {
    const changes = [
      { ISOLATION: 'production' },
      { ISOLATION: undefined },
      { API_ENVIRONMENT: 'production' },
      { PUBLIC_DATA_ENVIRONMENT: 'production' },
      { PUBLIC_DATA_ENVIRONMENT: undefined },
      { PREVIEW_ORIGIN: 'https://options.moopertonic.net' },
      { PREVIEW_ORIGIN: 'http://127.0.0.1:8787' },
      { PREVIEW_ORIGIN: 'http://localhost:8788' },
      { DB: {} }, { PAPER_TRADES: {} }, { USERS: {} }, { SESSIONS: {} },
      { LIVE_QUOTE_KEY: 'forbidden' }, { BOT_REGISTRATION_KEY: 'forbidden' },
    ];
    for (const change of changes) {
      const env = { ...p.env, ...change };
      const response = await gateway.fetch(request('/api/me'), env);
      assert.equal(response.status, 403,
        `${JSON.stringify(Object.keys(change))} must be refused, got ${response.status}`);
    }
    // A production identity must not survive the environment object.
    const serialized = JSON.stringify(Object.keys(p.env)) + String(p.env.PREVIEW_ORIGIN);
    assert.doesNotMatch(serialized, /qqq-options-chain-data|otmooper12\.workers\.dev|moopertonic\.net/);
  } finally {
    p.close();
  }
});
