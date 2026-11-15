'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { Consumer } = require('./consumer');
const { Source, DataError, LIMITS, permitted } = require('./source');
const { Protocol, tools } = require('./server');
const { gzipSync, gunzipSync } = require('node:zlib');
const { windowFixtures } = require('./window_fixtures');
const { fixtures } = require('./fixtures');
const NOW = Date.parse('2026-10-10T12:00:00Z');
const OVERNIGHT = { window_id: 'overnight-2026-10-05_2026-10-06', kind: 'overnight' };
const SESSIONS3 = { window_id: 'sessions3-2026-10-05_2026-10-07', kind: 'sessions_3' };
const OVERNIGHT_PARTIAL = { window_id: 'overnight-2026-10-07_2026-10-08', kind: 'overnight' };
const WEEKLY = { window_id: 'weekly-2026-W41-cheapest', kind: 'weekly_cheapest' };
const UNSUPPORTED = { window_id: 'overnight-2026-10-06_2026-10-07', kind: 'overnight' };
test('weekly detail scans past the ordinary expansion limit and validates the gzip trailer', async () => {
  const key = 'oa203/windows/oa203-windows-v1/weekly_fixed/weekly-2026-W46-fixed/paths.csv.gz';
  const line = 'OTHER,' + 'x'.repeat(1016) + '\n';
  const text = 'symbol,value\n' + line.repeat(50000) + 'TARGET,123\n';
  assert.ok(Buffer.byteLength(text) > LIMITS.expanded_artifact_bytes);
  const data = gzipSync(text);
  const query = new Source({read: async () => data}).query();
  assert.equal(await query.get(key, 'TARGET'), 'symbol,value\nTARGET,123\n');
  const damaged = new Source({read: async () => data.subarray(0, -4)}).query();
  await assert.rejects(damaged.get(key, 'TARGET'), {code: 'excessive_or_malformed_artifact'});
});

function setup() {
  const { files, windows } = windowFixtures(), calls = [];
  // Base fixtures too, so discover_sources probes for the other datasets
  // resolve; window keys never overlap them.
  Object.assign(files, fixtures());
  const source = new Source({ read: async key => { calls.push(key); if (!Object.hasOwn(files, key)) throw new DataError('missing_artifact', 'absent', key); return files[key]; } });
  const consumer = new Consumer(source, { now: () => NOW });
  return { files, windows, calls, consumer };
}
function editJson(files, key, mutate) {
  const value = JSON.parse(files[key]); mutate(value); files[key] = JSON.stringify(value);
}
function editPathsRow(files, prefix, symbol, mutate) {
  const key = prefix + 'paths.csv.gz';
  const lines = gunzipSync(files[key]).toString().trim().split('\n');
  const header = lines[0].split(',');
  const index = lines.findIndex(l => l.startsWith(symbol + ','));
  const cells = lines[index].split(',');
  mutate(cells, header);
  lines[index] = cells.join(',');
  files[key] = gzipSync(lines.join('\n') + '\n');
}

test('window overnight final serves all three bases with different tops', async () => {
  const f = setup();
  const mid = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'mid' });
  assert.equal(mid.status, 'available');
  assert.equal(mid.rows[0].symbol, 'NBIS261009P00155000');
  assert.equal(Number(mid.rows[0].pct), 33.333333);
  assert.equal(mid.rows[0].rank, '1');
  const exec = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'exec' });
  assert.equal(exec.rows[0].symbol, 'XLU261009C00041000');
  assert.equal(Number(exec.rows[0].pct), 0.833333);
  const qualified = await f.consumer.call('window_rankings', { ...OVERNIGHT });
  assert.equal(qualified.rows[0].symbol, 'XLU261009C00040500');
  assert.equal(Number(qualified.rows[0].pct), 2.2);
  assert.equal(Number(qualified.rows[0].entry), 0.15);
  assert.equal(Number(qualified.rows[0].exit), 0.48);
  assert.equal(qualified.actual.state, 'final');
  assert.equal(qualified.actual.basis, 'qualified');
  assert.equal(qualified.summary.policy.version, 'oa203-windows-v1');
  assert.equal(qualified.summary.contracts_evaluated, 6);
  // Per-basis counts reconcile with the full universe, not just the page.
  // The envelope carries them as `coverage`; the summary repeats them.
  for (const b of ['mid', 'exec', 'qualified']) {
    const c = qualified.coverage[b];
    assert.equal(c.eligible + c.excluded, 6);
    assert.equal(Object.values(c.exclusion_reasons).reduce((a, n) => a + n, 0), c.excluded);
  }
  assert.deepEqual(qualified.coverage.qualified, { eligible: 4, negative: 1, excluded: 2,
    exclusion_reasons: { entry_spread: 1, expires_before_exit: 1 }, shortlist: 4 });
  assert.deepEqual(qualified.coverage.mid, { eligible: 5, negative: 1, excluded: 1,
    exclusion_reasons: { expires_before_exit: 1 }, shortlist: 5 });
  assert.deepEqual(qualified.summary.counts, qualified.coverage);
  assert.ok(!qualified.warnings.some(w => w.toLowerCase().includes('hindsight')));
  assert.ok(qualified.warnings.some(w => w.includes('Fixed-endpoint window.')));
});

test('window partial states stay partial with reasons and served rows', async () => {
  const f = setup();
  const bounded = await f.consumer.call('window_rankings', { ...SESSIONS3, basis: 'qualified' });
  assert.equal(bounded.status, 'partial');
  assert.equal(bounded.rows[0].symbol, 'VST261009C00160000');
  assert.equal(Number(bounded.rows[0].pct), 13.38);
  assert.ok(bounded.warnings.some(w => w.includes('bounded_universe_replay')));
  const partial = await f.consumer.call('window_rankings', { ...OVERNIGHT_PARTIAL, basis: 'qualified' });
  assert.equal(partial.status, 'partial');
  assert.equal(partial.rows.length, 2);
  assert.ok(partial.warnings.some(w => w.includes('session_partial:2026-10-08')));
});

test('window in_progress returns the summary only; no ranking is served', async () => {
  const f = setup();
  const out = await f.consumer.call('window_rankings', { ...WEEKLY, basis: 'qualified' });
  assert.equal(out.status, 'in_progress');
  assert.deepEqual(out.rows, []);
  assert.equal(out.pagination.total, 0);
  assert.equal(out.summary.state, 'in_progress');
  assert.ok(out.summary.state_reasons.includes('session_not_final:2026-10-08'));
  assert.ok(out.warnings.some(w => w.includes('no final exit')));
  assert.ok(out.warnings.some(w => w.toLowerCase().includes('hindsight')));
  assert.ok(!f.calls.some(k => k.endsWith('rankings.csv')));
});

test('window unsupported policy and missing window stay explicit', async () => {
  const f = setup();
  const unsupported = await f.consumer.call('window_rankings', { ...UNSUPPORTED, basis: 'qualified' });
  assert.equal(unsupported.status, 'unsupported');
  assert.equal(unsupported.summary.policy.version, 'oa203-windows-v2');
  assert.deepEqual(f.calls, ['oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-06_2026-10-07/summary.json']);
  const g = setup();
  const missing = await g.consumer.call('window_rankings', { window_id: 'overnight-2026-10-08_2026-10-09', kind: 'overnight' });
  assert.equal(missing.status, 'missing');
  assert.equal(missing.availability, 'missing_publication');
});

test('window invalid ids, kinds, policy and bases fail closed without reads', async () => {
  for (const a of [{ window_id: '../secret', kind: 'overnight' }, { ...OVERNIGHT, kind: 'monthly' },
    { window_id: 'weekly-2026-W41-cheapest', kind: 'overnight' }, { ...OVERNIGHT, basis: 'vwap' },
    { window_id: 'overnight-2026-10-06_2026-10-05', kind: 'overnight' },
    { ...OVERNIGHT, limit: 51 }, { ...OVERNIGHT, url: 'https://example.com' }]) {
    const f = setup();
    const out = await f.consumer.call('window_rankings', a);
    assert.equal(out.status, 'failed', JSON.stringify(a));
    assert.equal(out.errors.at(-1).code, 'invalid_filter', JSON.stringify(a));
    assert.equal(f.calls.length, 0, JSON.stringify(a));
  }
  // Producer-shaped but wrong: close tolerance is part of the stable contract.
  const f = setup();
  editJson(f.files, f.windows.overnightFinal.prefix + 'summary.json', p => { p.policy.close_tolerance_s = 899; });
  const badPolicy = await f.consumer.call('window_rankings', { ...OVERNIGHT });
  assert.equal(badPolicy.status, 'failed');
  assert.equal(badPolicy.errors.at(-1).code, 'incompatible_schema');
  // Ranks are cut only after a full ranking: a gap in 1..shortlist fails.
  const g = setup();
  const key = g.windows.overnightFinal.prefix + 'rankings.csv';
  g.files[key] = g.files[key].split('\n').filter(l => !l.startsWith('qualified,2,')).join('\n');
  const gap = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified' });
  assert.equal(gap.status, 'failed');
  assert.equal(gap.errors.at(-1).code, 'incompatible_schema');
});

test('window pinned detail survives newer publication and fails closed on change', async () => {
  const f = setup();
  const first = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  assert.equal(first.rows[0].symbol, 'XLU261009C00040500');
  // Newer publication elsewhere (sessions_3) does not move the pinned row.
  editJson(f.files, f.windows.sessions3Partial.prefix + 'summary.json', p => { p.generated_at = '2026-10-07T21:00:00Z'; });
  const detail = await f.consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(detail.status, 'available');
  assert.equal(detail.row.symbol, 'XLU261009C00040500');
  assert.equal(detail.path.symbol, 'XLU261009C00040500');
  assert.equal(Number(detail.path.qualified_pct), 2.2);
  assert.equal(Number(detail.path.qualified_entry_ms), 1791229787876);
  assert.equal(Number(detail.path.qualified_exit_ms), 1791293734798);
  assert.equal(Number(detail.path.mid_pct), 1.5);
  assert.equal(Number(detail.path.exec_pct), 0.5);
  assert.equal(detail.path.qualified_entry_session, '2026-10-05');
  assert.equal(detail.path.qualified_exit_session, '2026-10-06');
  // Changed summary fails closed and returns no path.
  editJson(f.files, f.windows.overnightFinal.prefix + 'summary.json', p => { p.generated_at = '2026-10-06T21:00:00Z'; });
  const changed = await f.consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(changed.status, 'failed');
  assert.equal(changed.errors[0].code, 'source_changed');
  assert.equal(changed.path, undefined);
  // Changed rankings fail closed the same way.
  const g = setup();
  const gFirst = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  g.files[g.windows.overnightFinal.prefix + 'rankings.csv'] += '\n';
  const changedBoard = await g.consumer.call('result_detail', { reference: gFirst.rows[0].detail_reference });
  assert.equal(changedBoard.status, 'failed');
  assert.equal(changedBoard.errors[0].code, 'source_changed');
  assert.equal(changedBoard.path, undefined);
});

test('window detail is consistency-verified against the pinned ranking row', async () => {
  const f = setup();
  const first = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  // Paths row disagrees with the pinned ranking row: fail closed.
  editPathsRow(f.files, f.windows.overnightFinal.prefix, 'XLU261009C00040500', (cells, header) => {
    cells[header.indexOf('qualified_pct')] = '2.3';
  });
  const mismatch = await f.consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(mismatch.status, 'failed');
  assert.equal(mismatch.errors[0].code, 'source_changed');
  assert.equal(mismatch.path, undefined);
  // Paths file absent: the pinned ranking stands, explicitly rankings-only.
  const g = setup();
  const gFirst = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  delete g.files[g.windows.overnightFinal.prefix + 'paths.csv.gz'];
  const noPaths = await g.consumer.call('result_detail', { reference: gFirst.rows[0].detail_reference });
  assert.equal(noPaths.status, 'partial');
  assert.equal(noPaths.path, null);
  assert.equal(noPaths.row.symbol, 'XLU261009C00040500');
  assert.ok(noPaths.warnings.some(w => w.includes('rankings-only')));
});

test('window paging, bounds and filters follow producer rank', async () => {
  const f = setup();
  const page1 = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 2, offset: 0 });
  assert.equal(page1.rows.length, 2);
  assert.deepEqual(page1.pagination, { offset: 0, limit: 2, total: 4, truncated: true, next_offset: 2 });
  assert.equal(page1.rows[0].rank, '1');
  assert.equal(page1.rows[1].rank, '2');
  const page2 = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 2, offset: 2 });
  assert.equal(page2.rows.length, 2);
  assert.equal(page2.pagination.next_offset, null);
  assert.equal(page2.rows[1].rank, '4');
  const beyond = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 2, offset: 4 });
  assert.deepEqual(beyond.rows, []);
  assert.equal(beyond.pagination.total, 4);
  const xlu = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', underlying: 'XLU' });
  assert.equal(xlu.rows.length, 2);
  const puts = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', type: 'put' });
  assert.equal(puts.rows.length, 1);
  assert.equal(puts.rows[0].symbol, 'NBIS261009P00155000');
  // Five paged calls; each reads summary, rankings, both re-reads and the
  // pointer: 5 x 5 artifact reads, each call inside the per-query limit.
  assert.ok(f.calls.length <= 25);
});

test('window detail rejects future entry evidence; later windows do not change it', async () => {
  // Entry qualification uses entry-time evidence only: a quote stamp after
  // the entry observation is future evidence, never an entry.
  const f = setup();
  const first = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  editPathsRow(f.files, f.windows.overnightFinal.prefix, 'XLU261009C00040500', (cells, header) => {
    const entryMs = Number(cells[header.indexOf('qualified_entry_ms')]);
    cells[header.indexOf('qualified_entry_bid_ms')] = String(entryMs + 1);
  });
  const future = await f.consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(future.status, 'failed');
  assert.equal(future.errors[0].code, 'incompatible_schema');
  assert.equal(future.path, undefined);
  // Later windows' publications do not change an earlier window's ranking.
  const g = setup();
  const before = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified' });
  // detail_reference UUIDs are fresh per call; compare rows without them.
  const stripRefs = rows => JSON.stringify(rows.map(r => ({ ...r, detail_reference: undefined })));
  const beforeRows = stripRefs(before.rows);
  editJson(g.files, g.windows.sessions3Partial.prefix + 'summary.json', p => { p.generated_at = '2026-10-07T21:00:00Z'; });
  editJson(g.files, g.windows.weeklyInProgress.prefix + 'summary.json', p => { p.generated_at = '2026-10-09T21:00:00Z'; });
  const after = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified' });
  assert.equal(stripRefs(after.rows), beforeRows);
  assert.equal(after.actual.summary_sha256, before.actual.summary_sha256);
});

test('window mixed-revision flat members are refused against the pointer', async () => {
  const f = setup();
  const prefix = f.windows.overnightFinal.prefix;
  // A second revision with the same shape (same counts/shortlist) but
  // different values: flat summary rev1 + flat rankings rev2. Without the
  // pointer check the consumer serves the rev2 rows under the rev1 summary.
  const key = prefix + 'rankings.csv';
  f.files[key] = f.files[key].split('\n').map(l =>
    l.startsWith('qualified,1,') ? l.replace(',2.2,0.15,', ',2,0.16,') : l).join('\n');
  const out = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified' });
  assert.equal(out.status, 'failed');
  assert.equal(out.errors[0].code, 'source_changed');
  assert.equal(out.rows, undefined);
});

test('window without a pointer is served unverified with a warning', async () => {
  const f = setup();
  delete f.files[f.windows.overnightFinal.prefix + 'latest.json'];
  const out = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified' });
  assert.equal(out.status, 'available');
  assert.equal(out.rows[0].symbol, 'XLU261009C00040500');
  assert.ok(out.warnings.some(w => w.includes('not revision-pinned')));
});

test('window detail pins paths to the published pointer', async () => {
  const f = setup();
  const first = await f.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  const detail = await f.consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(detail.status, 'available');
  assert.equal(detail.paths.verified, 'matches_published_pointer');
  assert.ok(!first.warnings.some(w => w.includes('no live window artifacts') || w.includes('unapproved')));
  const rankingsSource = first.sources.find(s => s.locator.endsWith('/rankings.csv'));
  const pointerSource = first.sources.find(s => s.locator.endsWith('/latest.json'));
  assert.equal(first.actual.rankings_sha256, rankingsSource.retrieved_sha256);
  assert.notEqual(first.actual.rankings_sha256, pointerSource.retrieved_sha256);
  assert.equal(detail.actual.rankings_sha256, rankingsSource.retrieved_sha256);
  // Same paths text plus a trailing blank line, re-gzipped: the pinned row
  // still matches, but the bytes are no longer the published revision's.
  const g = setup();
  const gFirst = await g.consumer.call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  const gKey = g.windows.overnightFinal.prefix + 'paths.csv.gz';
  g.files[gKey] = gzipSync(gunzipSync(g.files[gKey]).toString() + '\n');
  const changed = await g.consumer.call('result_detail', { reference: gFirst.rows[0].detail_reference });
  assert.equal(changed.status, 'failed');
  assert.equal(changed.errors[0].code, 'source_changed');
  assert.equal(changed.path, undefined);
});

test('window discovery advertises the fixed contract without an index', async () => {
  const f = setup();
  const out = await f.consumer.call('discover_sources', { session: '2026-10-02' });
  assert.deepEqual(out.capabilities.map(c => c.dataset),
    ['qqq_snapshot', 'scheduled_squeeze', 'options_returns', 'spy_snapshot', 'cross_market_overview', 'options_windows', 'options_active']);
  const win = out.capabilities.find(c => c.dataset === 'options_windows');
  assert.equal(win.availability, 'explicit_window_required');
  assert.equal(win.producer_schema, 'oa203-windows-v1');
  assert.deepEqual(win.kinds, ['weekly_cheapest', 'weekly_fixed', 'weekly_cheapest_best_exit', 'overnight', 'sessions_2', 'sessions_3']);
  assert.deepEqual(win.bases, ['mid', 'exec', 'qualified']);
  assert.ok(win.filters.includes('window_id'));
  assert.equal(win.publication, 'query_explicit_window');
  assert.equal(out.capabilities.at(-1).dataset, 'options_active');
  assert.equal(out.capabilities[2].availability, 'available');
});

test('MCP window_rankings and pinned detail use read-only bounded schemas', async () => {
  const f = setup(), p = new Protocol(f.consumer);
  assert.equal(tools.length, 8);
  const tool = tools.find(t => t.name === 'window_rankings');
  assert.ok(tool.annotations.readOnlyHint);
  assert.equal(tool.inputSchema.additionalProperties, false);
  await p.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25',
    capabilities: {}, clientInfo: { name: 'window-fixture', version: '1' } } });
  await p.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const call = async (name, args) => (await p.handle({ jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name, arguments: args } })).result.structuredContent;
  const ranking = await call('window_rankings', { ...OVERNIGHT, basis: 'qualified', limit: 1 });
  assert.equal(ranking.status, 'available');
  const detail = await call('result_detail', { reference: ranking.rows[0].detail_reference });
  assert.equal(detail.status, 'available');
  assert.equal(detail.path.symbol, 'XLU261009C00040500');
  assert.equal(detail.actual.window_id, OVERNIGHT.window_id);
});

test('window allowlist admits only strict window keys', async () => {
  const base = 'oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06/';
  assert.doesNotThrow(() => permitted(base + 'summary.json'));
  assert.doesNotThrow(() => permitted(base + 'rankings.csv'));
  assert.doesNotThrow(() => permitted(base + 'paths.csv.gz'));
  assert.doesNotThrow(() => permitted(base + 'latest.json'));
  assert.doesNotThrow(() => permitted('oa203/windows/oa203-windows-v1/weekly_cheapest/weekly-2026-W41-cheapest/summary.json'));
  assert.doesNotThrow(() => permitted('oa203/windows/oa203-windows-v1/sessions_3/sessions3-2026-10-05_2026-10-07/paths.csv.gz'));
  for (const key of [
    'oa203/windows/oa203-windows-v1/overnight/weekly-2026-W41-cheapest/summary.json',
    'oa203/windows/oa203-windows-v2/overnight/overnight-2026-10-05_2026-10-06/summary.json',
    'oa203/windows/oa203-windows-v1/monthly/monthly-2026-10/summary.json',
    'oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-05_2026-10-06/leaderboard.csv',
    'oa203/windows/oa203-windows-v1/overnight/../../secret.json',
    'oa203/windows/oa203-windows-v1/sessions_2/sessions3-2026-10-05_2026-10-07/summary.json',
  ]) assert.throws(() => permitted(key), /allowlist/);
});
