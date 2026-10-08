'use strict';
// SPY coverage in the read-only market MCP (#121): discovery, explicit
// underlying selection, QQQ-by-default compatibility, symbol-bound detail
// references, and explicit failures instead of QQQ substitution.
const test = require('node:test');
const assert = require('node:assert/strict');
const { Consumer, UNDERLYINGS } = require('./consumer');
const { Source, DataError, permitted } = require('./source');
const { Protocol, tools } = require('./server');
const { fixtures, NOW, CONTRACT, SPY_CONTRACT } = require('./fixtures');

const QQQ_KEY = 'intraday/latest.json';
const SPY_KEY = 'intraday/spy/latest.json';

function setup({ files = fixtures(), now = () => NOW } = {}) {
  const calls = [];
  const source = new Source({ read: async key => {
    calls.push(key);
    if (!Object.hasOwn(files, key)) throw new DataError('missing_artifact', 'Fixture is absent.', key);
    return files[key];
  } });
  return { consumer: new Consumer(source, { now }), files, calls };
}
function edit(files, key, fn) { const value = JSON.parse(files[key]); fn(value); files[key] = JSON.stringify(value); }
const locators = out => out.sources.map(s => s.locator);

test('artifact allowlist admits only the fixed SPY latest key', () => {
  assert.doesNotThrow(() => permitted(SPY_KEY));
  for (const key of ['intraday/iwm/latest.json', 'intraday/spy/20261002/snapshot_120930123456.csv', 'intraday/spy/../latest.json', 'intraday/SPY/latest.json'])
    assert.throws(() => permitted(key), e => e.code === 'invalid_reference', key);
  assert.deepEqual(Object.keys(UNDERLYINGS), ['QQQ', 'SPY']);
});

test('discovery reports SPY coverage explicitly and keeps QQQ positions stable', async () => {
  const { consumer } = setup();
  const out = await consumer.call('discover_sources');
  assert.deepEqual(out.capabilities.map(c => c.dataset), ['qqq_snapshot', 'scheduled_squeeze', 'options_returns', 'spy_snapshot']);
  const spy = out.capabilities[3];
  assert.equal(spy.underlying, 'SPY');
  assert.equal(spy.availability, 'available');
  assert.equal(spy.source_locator, SPY_KEY);
  assert.equal(spy.actual.underlying, 'SPY');
  assert.ok(spy.filters.includes('underlying'));
  assert.deepEqual(out.underlyings.map(u => [u.symbol, u.dataset, u.availability, u.default]),
    [['QQQ', 'qqq_snapshot', 'available', true], ['SPY', 'spy_snapshot', 'available', false]]);
  assert.ok(out.capabilities[0].warnings.every(w => !/SPY RVOL/.test(w)), 'QQQ warnings unchanged');
  assert.ok(spy.warnings.some(w => /SPY RVOL/.test(w)));
});

test('missing SPY publication is reported in discovery, never filled from QQQ', async () => {
  const files = fixtures(); delete files[SPY_KEY];
  const { consumer } = setup({ files });
  const out = await consumer.call('discover_sources');
  assert.equal(out.status, 'partial');
  assert.equal(out.capabilities[3].availability, 'missing_publication');
  assert.equal(out.capabilities[3].actual, null);
  assert.equal(out.underlyings[1].availability, 'missing_publication');
});

test('omitted underlying still returns QQQ exactly as before', async () => {
  const { consumer, calls } = setup();
  const out = await consumer.call('market_context', { limit: 5 });
  assert.equal(out.dataset, 'qqq_snapshot');
  assert.equal(out.requested_underlying, 'QQQ');
  assert.equal(out.actual.underlying, 'QQQ');
  assert.deepEqual(calls, [QQQ_KEY]);
  assert.ok(out.rows.every(r => r.OptionSymbol.startsWith('QQQ')));
  assert.equal(out.readings.symbol, 'QQQ');
});

test('explicit SPY query returns SPY evidence, readings and warm-up status', async () => {
  const { consumer, calls } = setup();
  const out = await consumer.call('market_context', { underlying: 'SPY', limit: 5 });
  assert.equal(out.dataset, 'spy_snapshot');
  assert.deepEqual(calls, [SPY_KEY], 'only the SPY artifact is read');
  assert.deepEqual(locators(out), [SPY_KEY]);
  assert.equal(out.actual.underlying, 'SPY');
  assert.equal(out.actual.snapshot_key, 'intraday/spy/20261002/snapshot_120930123456.csv');
  assert.equal(out.readings.symbol, 'SPY');
  assert.equal(out.readings.spot, 670);
  assert.deepEqual(out.rows.map(r => r.OptionSymbol), [SPY_CONTRACT, 'SPY261002P00670000']);
  assert.equal(out.status, 'partial', 'insufficient SPY RVOL history is partial, not available');
  assert.ok(out.warnings.some(w => /RVOL insufficient_history/.test(w)));
  assert.equal(out.freshness.status, 'fresh');
});

test('stale SPY data is reported as stale for SPY independently of QQQ', async () => {
  const files = fixtures();
  edit(files, SPY_KEY, p => { p.timestamp = '2026-10-02T15:00:00Z'; });
  const { consumer } = setup({ files });
  assert.equal((await consumer.call('market_context', { underlying: 'SPY' })).freshness.status, 'stale');
  assert.equal((await consumer.call('market_context')).freshness.status, 'fresh');
});

test('SPY detail references stay bound to the originating SPY snapshot', async () => {
  const { consumer, files } = setup();
  const query = await consumer.call('market_context', { underlying: 'SPY', limit: 1 });
  const reference = query.rows[0].detail_reference;
  // Both latest pointers advance after the query.
  edit(files, SPY_KEY, p => { p.timestamp = '2026-10-02T16:10:00Z'; p.rows[0].Bid = 99; });
  edit(files, QQQ_KEY, p => { p.timestamp = '2026-10-02T16:10:00Z'; });
  const detail = await consumer.call('result_detail', { reference });
  assert.equal(detail.dataset, 'spy_snapshot');
  assert.equal(detail.requested_underlying, 'SPY');
  assert.equal(detail.actual.underlying, 'SPY');
  assert.equal(detail.row.OptionSymbol, SPY_CONTRACT);
  assert.equal(detail.row.Bid, 3, 'retained row, not the advanced pointer');
  assert.equal(detail.readings.symbol, 'SPY');
  assert.deepEqual(detail.sources.map(s => [s.locator, s.retained]), [[SPY_KEY, true]]);
});

test('unsupported or malformed underlying selectors fail without reading anything', async () => {
  for (const underlying of ['IWM', 'spy', '__proto__', 'constructor', '', null, 1, ['SPY']]) {
    const { consumer, calls } = setup();
    const out = await consumer.call('market_context', { underlying });
    assert.equal(out.status, 'failed', String(underlying));
    assert.equal(out.errors[0].code, 'invalid_filter', String(underlying));
    assert.equal(out.dataset, 'unsupported_underlying', String(underlying));
    assert.deepEqual(calls, [], `${String(underlying)} must not read QQQ or anything else`);
  }
});

test('a contract filter for the other underlying is rejected, not silently empty', async () => {
  const { consumer, calls } = setup();
  const out = await consumer.call('market_context', { underlying: 'SPY', contract: CONTRACT });
  assert.equal(out.errors[0].code, 'invalid_filter');
  assert.match(out.errors[0].message, /not a SPY option/);
  assert.deepEqual(calls, []);
});

test('missing SPY publication fails explicitly with no QQQ substitution', async () => {
  const files = fixtures(); delete files[SPY_KEY];
  const { consumer, calls } = setup({ files });
  const out = await consumer.call('market_context', { underlying: 'SPY' });
  assert.equal(out.status, 'missing');
  assert.equal(out.errors[0].code, 'missing_artifact');
  assert.equal(out.rows, undefined);
  assert.deepEqual(calls, [SPY_KEY], 'QQQ is never read as a fallback');
});

test('wrong-symbol, unlabeled and mixed payloads fail closed as symbol_mismatch', async () => {
  const cases = {
    'QQQ payload at the SPY key': files => { files[SPY_KEY] = files[QQQ_KEY].replace('{', '{"symbol":"QQQ",'); },
    'unlabeled payload at the SPY key': files => edit(files, SPY_KEY, p => { delete p.symbol; }),
    'QQQ readings in a SPY payload': files => edit(files, SPY_KEY, p => { p.underlying_market.symbol = 'QQQ'; }),
    'a QQQ contract in a SPY payload': files => edit(files, SPY_KEY, p => { p.rows.push({ ...p.rows[0], OptionSymbol: CONTRACT, Strike: 600 }); }),
  };
  for (const [label, mutate] of Object.entries(cases)) {
    const files = fixtures(); mutate(files);
    const { consumer } = setup({ files });
    const out = await consumer.call('market_context', { underlying: 'SPY' });
    assert.equal(out.status, 'failed', label);
    assert.equal(out.errors[0].code, 'symbol_mismatch', label);
    assert.equal(out.rows, undefined, label);
    const discovery = await consumer.call('discover_sources');
    assert.equal(discovery.capabilities[3].availability, 'failed', `${label} (discovery)`);
  }
  // And the reverse: a SPY payload published at QQQ's key is not QQQ.
  const files = fixtures(); files[QQQ_KEY] = files[SPY_KEY];
  const out = await setup({ files }).consumer.call('market_context');
  assert.equal(out.errors[0].code, 'symbol_mismatch');
});

test('SPY snapshot locators must point into the SPY archive', async () => {
  const files = fixtures();
  edit(files, SPY_KEY, p => { p.snapshot_key = 'intraday/20261002/snapshot_120930123456.csv'; });
  const out = await setup({ files }).consumer.call('market_context', { underlying: 'SPY' });
  assert.equal(out.status, 'failed');
  assert.match(out.errors[0].message, /locator/);
});

test('tool schema advertises the bounded underlying selector', async () => {
  const schema = tools.find(t => t.name === 'market_context').inputSchema;
  assert.deepEqual(schema.properties.underlying, { type: 'string', enum: ['QQQ', 'SPY'], default: 'QQQ' });
  assert.equal(schema.required.includes('underlying'), false, 'omission stays valid');
  const protocol = new Protocol(setup().consumer);
  protocol.initialized = true; protocol.ready = true;
  const res = await protocol.handle({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'market_context', arguments: { underlying: 'SPY', limit: 1 } } });
  assert.equal(res.result.structuredContent.dataset, 'spy_snapshot');
  assert.equal(res.result.isError, false);
});
