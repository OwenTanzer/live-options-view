'use strict';
// Test-only entrypoint verification. Mounted into, never baked into, the image.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { env, testKeys, request } = require('./test-auth');
const keys = testKeys();
const preload = '/tmp/market-mcp-test-preload.cjs';
fs.writeFileSync(preload, `
const auth = require('/app/market_mcp/auth');
const Original = auth.Authenticator;
auth.Authenticator = class extends Original {
 constructor(options) { super({ ...options, readJwks: async () => JSON.parse(process.env.FIXTURE_PUBLIC_JWKS) }); }
};
const source = require('/app/market_mcp/source');
const Source = source.Source;
const files = require('/app/market_mcp/fixtures').fixtures();
source.Source = class extends Source { constructor() { super({ read: async key => files[key] }); } };
`);
async function run() {
  assert.notEqual(process.getuid(), 0);
  const child = spawn(process.execPath, ['--max-old-space-size=256', '--require', preload, '/app/market_mcp/production.js'], {
    env: { ...process.env, ...env(), PORT: '18765', FIXTURE_PUBLIC_JWKS: JSON.stringify({ keys: [keys.jwk] }) }, stdio: ['ignore', 'pipe', 'pipe']
  });
  const exited = once(child, 'exit'); let logs = '';
  child.stderr.on('data', c => logs += c); child.stdout.resume();
  const endpoint = { address: () => ({ port: 18765 }) };
  const token = keys.token();
  try {
    for (let i = 0; i < 100; i++) {
      if (logs.includes('"listening"')) break;
      if (child.exitCode !== null) throw new Error('Entrypoint exited before readiness');
      await new Promise(r => setTimeout(r, 50));
    }
    assert.match(logs, /"listening"/);
    assert.equal((await request(endpoint, null, { method: 'GET', path: '/healthz' })).status, 200);
    const ping = { jsonrpc: '2.0', id: 1, method: 'ping' };
    assert.equal((await request(endpoint, ping)).status, 401);
    assert.equal((await request(endpoint, ping, { token: keys.token({ sub: 'auth0|another-owner' }) })).status, 403);
    const call = async (method, params) => {
      const r = await request(endpoint, { jsonrpc: '2.0', id: 1, method, params }, { token });
      assert.equal(r.status, 200); assert.ok(!r.body.error); return r.body.result;
    };
    await call('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'container-fixture', version: '1' } });
    assert.equal((await call('tools/list')).tools.length, 5);
    const query = await call('tools/call', { name: 'market_context', arguments: { limit: 1 } });
    assert.equal(query.structuredContent.status, 'available');
    const detail = await call('tools/call', { name: 'result_detail', arguments: { reference: query.structuredContent.rows[0].detail_reference, limit: 1 } });
    assert.equal(detail.structuredContent.status, 'available');
    child.kill('SIGTERM');
    const deadline = setTimeout(() => child.kill('SIGKILL'), 30000);
    let result; try { result = await exited; } finally { clearTimeout(deadline); }
    assert.deepEqual(result, [0, null]); assert.match(logs, /"shutdown"/);
    assert.ok(!logs.includes(token));
    console.log(JSON.stringify({ kind: 'container_entrypoint_with_injected_test_artifacts_and_public_test_key', uid: process.getuid(), node: process.version, health: 200, missing_token: 401, wrong_owner: 403, initialize_list_query_detail: 'passed', sigterm_exit: 0, live_oauth_or_chej: false }));
  } finally { if (child.exitCode === null) { child.kill('SIGKILL'); await exited; } fs.unlinkSync(preload); }
}
run().catch(() => { console.error('Container entrypoint fixture failed'); process.exitCode = 1; });
