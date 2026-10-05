#!/usr/bin/env node
'use strict';
const fs = require('node:fs/promises'); const assert = require('node:assert/strict');
const { createHttpServer } = require('./http'); const { Consumer } = require('./consumer'); const { Source } = require('./source');
const { fixtures, SESSION } = require('./fixtures'); const { testAuth, request } = require('./test-auth');
async function run() {
  const files = fixtures(), { settings, auth, keys } = testAuth(); await auth.refresh();
  const server = createHttpServer(new Consumer(new Source({ read: async key => files[key] })), { policy: settings, auth });
  await new Promise(r => server.listen(0, '127.0.0.1', r)); const token = keys.token(), transcript = []; let id = 0;
  const rpc = async message => { const r = await request(server, message, { token }); assert.equal(r.status, message.id ? 200 : 202); return r.body; };
  const tool = async (name, args) => { const r = await rpc({ jsonrpc: '2.0', id: ++id, method: 'tools/call', params: { name, arguments: args } }); const value = r.result.structuredContent; assert.deepEqual(JSON.parse(r.result.content[0].text), value); assert.equal(r.result.isError, false); transcript.push({ tool: name, arguments: args, response: value }); return value; };
  try {
    const challenge = await request(server, { jsonrpc: '2.0', id: 1, method: 'ping' }); assert.equal(challenge.status, 401);
    const metadata = await request(server, null, { path: '/.well-known/oauth-protected-resource/mcp', method: 'GET' }); assert.equal(metadata.status, 200);
    const init = await rpc({ jsonrpc: '2.0', id: ++id, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'production-fixture-smoke', version: '1' } } });
    await rpc({ jsonrpc: '2.0', method: 'notifications/initialized' }); const listed = await rpc({ jsonrpc: '2.0', id: ++id, method: 'tools/list' }); assert.equal(listed.result.tools.length, 5);
    await tool('discover_sources', { session: SESSION });
    for (const [name, args] of [['market_context', { limit: 1 }], ['squeeze_results', { limit: 1 }], ['return_rankings', { session: SESSION, view: 'clean', limit: 1 }]]) { const r = await tool(name, args); await tool('result_detail', { reference: r.rows[0].detail_reference, limit: 1 }); }
    return { kind: 'synthetic_local_authenticated_http', transport: 'loopback TCP with explicit production Host/auth policy and local test keys', node: process.version,
      unauthorized_status: challenge.status, resource_metadata: metadata.body, protocol_version: init.result.protocolVersion,
      tools: listed.result.tools.map(t => t.name), transcript, limitations: ['No live OAuth registration/grant, deployed endpoint or actual Chej connection is asserted.'] };
  } finally { await server.shutdown(); }
}
if (require.main === module) {
  const a = process.argv.slice(2); if (a.length && !(a.length === 2 && a[0] === '--out')) { process.stderr.write('Usage: node market_mcp/production-smoke.js [--out evidence.json]\n'); process.exit(2); }
  run().then(async value => { if (a.length) await fs.writeFile(a[1], JSON.stringify(value, null, 2) + '\n'); process.stdout.write(JSON.stringify({ kind: value.kind, protocol_version: value.protocol_version, tools: value.tools, queries: value.transcript.map(t => ({ tool: t.tool, status: t.response.status, freshness: t.response.freshness.status })) }, null, 2) + '\n'); }).catch(() => { process.stderr.write('Synthetic authenticated HTTP smoke failed.\n'); process.exitCode = 1; });
}
module.exports = { run };
