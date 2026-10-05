#!/usr/bin/env node
'use strict';
// Synthetic authenticated local load, never live tokens or market data.
const fs = require('node:fs/promises'); const assert = require('node:assert/strict');
const { performance } = require('node:perf_hooks');
const { createHttpServer } = require('./http'); const { Consumer } = require('./consumer');
const { Source, LIMITS } = require('./source'); const { fixtures } = require('./fixtures');
const { testAuth, request } = require('./test-auth');
async function run() {
  const files = fixtures(), p = JSON.parse(files['intraday/latest.json']);
  p.underlying_market.fixture_padding = 'x'.repeat(160 * 1024); files['intraday/latest.json'] = JSON.stringify(p);
  const { auth, settings, keys } = testAuth(); await auth.refresh();
  const consumer = new Consumer(new Source({ read: async key => { await new Promise(r => setTimeout(r, 30)); return files[key]; } }));
  const server = createHttpServer(consumer, { policy: settings, auth });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const token = keys.token(), times = [], baseline = process.memoryUsage(), usage = process.resourceUsage(), began = performance.now();
  let peakActive = 0, peakRss = baseline.rss, peakHeap = baseline.heapUsed;
  const sample = setInterval(() => { const m = process.memoryUsage(); peakRss = Math.max(peakRss, m.rss); peakHeap = Math.max(peakHeap, m.heapUsed); peakActive = Math.max(peakActive, server.requestCounts().active); }, 2);
  try {
    for (let batch = 0; batch < 16; batch++) await Promise.all(Array.from({ length: 4 }, async (_, i) => {
      const start = performance.now(); const result = await request(server, { jsonrpc: '2.0', id: batch * 4 + i,
        method: 'tools/call', params: { name: 'market_context', arguments: { limit: 1 } } }, { token });
      assert.equal(result.status, 200); assert.equal(result.body.result.structuredContent.status, 'available'); times.push(performance.now() - start);
    }));
    assert.equal(peakActive, 4); assert.equal(consumer.references.size, 64); assert.ok(consumer.retainedBytes <= LIMITS.reference_bytes);
    const old = consumer.references.keys().next().value;
    const detail = await request(server, { jsonrpc: '2.0', id: 65, method: 'tools/call', params: { name: 'result_detail', arguments: { reference: old, limit: 1 } } }, { token }); assert.equal(detail.body.result.structuredContent.status, 'available');
    times.sort((a, b) => a - b); const final = process.memoryUsage(), cpu = process.resourceUsage();
    return { kind: 'synthetic_local_authenticated_http_load', node: process.version, platform: process.platform, arch: process.arch,
      fixture: { artifact_bytes: Buffer.byteLength(files['intraday/latest.json']), retained_readings_padding_bytes: 160 * 1024, note: 'Synthetic fixture with large retained readings; not worst-case upstream artifacts or measured Railway usage.' },
      completed_queries: times.length, verified_detail_queries: 1, peak_active_requests: peakActive,
      retained_references: consumer.references.size, retained_serialized_bytes: consumer.retainedBytes, retained_budget_bytes: LIMITS.reference_bytes,
      elapsed_ms: Math.round(performance.now() - began), latency_ms: { p50: Math.round(times[31]), p95: Math.round(times[60]), max: Math.round(times[63]) },
      memory_bytes: { baseline_rss: baseline.rss, peak_sampled_rss: Math.max(peakRss, final.rss), peak_process_rss: cpu.maxRSS * 1024, peak_sampled_heap: Math.max(peakHeap, final.heapUsed), final_heap: final.heapUsed },
      cpu_ms: Math.round((cpu.userCPUTime + cpu.systemCPUTime - usage.userCPUTime - usage.systemCPUTime) / 1000),
      limitations: ['Synthetic local process/container measurement; production Railway usage may differ.', 'Four simultaneous fixture reads are held 30 ms; upstream latency/expanded gzip shapes are not modeled.', 'CPU/egress/monthly Railway charges must be measured in the isolated pilot.'] };
  } finally { clearInterval(sample); await server.shutdown(); }
}
if (require.main === module) {
  const a = process.argv.slice(2); if (a.length && !(a.length === 2 && a[0] === '--out')) { process.stderr.write('Usage: node market_mcp/load.js [--out evidence.json]\n'); process.exit(2); }
  run().then(async result => { const body = JSON.stringify(result, null, 2) + '\n'; if (a.length) await fs.writeFile(a[1], body); process.stdout.write(body); }).catch(() => { process.stderr.write('Synthetic local load failed; inspect the test suite.\n'); process.exitCode = 1; });
}
module.exports = { run };
