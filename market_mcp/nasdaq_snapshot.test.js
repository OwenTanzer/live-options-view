'use strict';
// Read-only NASDAQ-100 snapshot tool: discover, query, filters, freshness,
// detail retention and failure modes. Fixture snapshot is synthetic; never
// represented as acquired market data.
const test = require('node:test');
const assert = require('node:assert/strict');
const { Consumer } = require('./consumer');
const { Source, DataError } = require('./source');
const { Protocol, tools } = require('./server');

const NOW = Date.parse('2026-11-23T15:00:00Z'); // Monday, 10:00 ET
const AS_OF = '2026-11-23T14:59:00Z';
const STOCK_BASIS = "Nasdaq latest reported move (after-hours move after the close)";
const INDEX_BASIS = 'regular-session move vs previous close';

function stock(symbol, price, pct, cap, extra = {}) {
  return { symbol, name: `${symbol} Inc.`, price, change: 1, percent_change: pct,
    percent_change_basis: STOCK_BASIS, market_cap: cap, rank: null,
    day_high: price + 1, day_low: price - 1, volume: 1000,
    change_1h_pct: 0.1, change_7d_pct: 2.0, news: [], status: 'available',
    price_basis: 'nasdaq_last_sale', provider_observed_at: null,
    freshness: { status: 'fresh', observed_at: AS_OF, age_seconds: 60,
      stale_after_seconds: 3600, basis: 'collector receipt' }, ...extra };
}

function snapshot(overrides = {}) {
  const stocks = [
    stock('AAPL', 350, 2.0, 5e12),
    stock('MSFT', 490, -0.5, 3.6e12),
    { ...stock('GOOG', null, null, null), price: null, change: null, percent_change: null,
      market_cap: null, day_high: null, day_low: null, volume: null,
      change_1h_pct: null, change_7d_pct: null, status: 'source_unavailable',
      freshness: { status: 'unknown', observed_at: null, age_seconds: null,
        stale_after_seconds: 3600, basis: 'collector receipt' } },
  ];
  stocks[0].rank = 1; stocks[1].rank = 2;
  return { schema_version: 1, dataset: 'nasdaq_snapshot', as_of: AS_OF, generated_at: AS_OF,
    universe: { source: 'Nasdaq official list-type/nasdaq100 API', as_of: '2026-11-23', listings: 101, companies: 100 },
    index: { symbol: 'NDX', status: 'available', level: 27000, change: 200, percent_change: 0.8,
      percent_change_basis: INDEX_BASIS, previous_close: 26800, day_high: 27100, day_low: 26900,
      market_status: { status: 'open', reason: 'in_session' }, price_basis: 'nasdaq_last_sale',
      provider_observed_at: null,
      freshness: { status: 'fresh', observed_at: AS_OF, age_seconds: 60, stale_after_seconds: 3600, basis: 'collector receipt' },
      breadth: { advancers: 2, decliners: 1, unchanged: 0 },
      top_gainers: [{ symbol: 'AAPL', percent_change: 2.0 }],
      top_losers: [{ symbol: 'MSFT', percent_change: -0.5 }] },
    stocks,
    coverage: { listings: 101, available: 2, source_unavailable: 1, with_news: 0, with_candles: 0 },
    sources: { prices: 'Nasdaq official quote API (api.nasdaq.com)' },
    session: { model: 'US equity session, weekdays 09:30-16:00 America/New_York', status: { status: 'open', reason: 'in_session' } },
    ...overrides };
}

function setup({ files = null, now = () => NOW } = {}) {
  const store = files ?? { 'nasdaq/latest.json': JSON.stringify(snapshot()) };
  const source = new Source({ read: async key => {
    if (!Object.hasOwn(store, key)) throw new DataError('missing_artifact', 'Fixture is absent.', key);
    return store[key];
  } });
  return { consumer: new Consumer(source, { now }), store };
}

test('discovery lists nasdaq_snapshot with its contract and freshness', async () => {
  const { consumer } = setup();
  const out = await consumer.call('discover_sources');
  const cap = out.capabilities.find(c => c.dataset === 'nasdaq_snapshot');
  assert.ok(cap);
  assert.equal(cap.availability, 'available');
  assert.equal(cap.source_locator, 'nasdaq/latest.json');
  assert.ok(cap.fields.includes('percent_change_basis'));
  assert.ok(cap.filters.includes('symbols'));
  assert.equal(cap.freshness.status, 'fresh');
  assert.equal(cap.actual.listings, 101);
  assert.equal(cap.actual.index_level, 27000);
});

test('nasdaq_snapshot returns rows, index block and distinct percent bases', async () => {
  const { consumer } = setup();
  const out = await consumer.call('nasdaq_snapshot');
  assert.equal(out.dataset, 'nasdaq_snapshot');
  assert.equal(out.status, 'partial'); // one source-unavailable row in the fixture
  assert.equal(out.rows.length, 3);
  assert.equal(out.index.symbol, 'NDX');
  assert.equal(out.index.percent_change_basis, INDEX_BASIS);
  assert.equal(out.rows[0].percent_change_basis, STOCK_BASIS);
  assert.notEqual(out.rows[0].percent_change_basis, out.index.percent_change_basis);
  assert.equal(out.coverage.available, 2);
  assert.equal(out.coverage.source_unavailable, 1);
  assert.equal(out.coverage.advancers, 2);
  assert.equal(out.freshness.status, 'fresh');
  assert.match(out.freshness.basis, /collector receipt/);
  assert.equal(out.actual.payload_sha256.length, 64);
  assert.ok(out.rows[0].detail_reference);
});

test('symbols filter, percent sort and unmatched symbols stay explicit', async () => {
  const { consumer } = setup();
  const one = await consumer.call('nasdaq_snapshot', { symbols: ['MSFT'] });
  assert.equal(one.rows.length, 1);
  assert.equal(one.rows[0].symbol, 'MSFT');
  assert.deepEqual(one.unmatched_symbols, []);
  const sorted = await consumer.call('nasdaq_snapshot', { sort: 'percent_desc' });
  assert.deepEqual(sorted.rows.map(r => r.symbol), ['AAPL', 'MSFT', 'GOOG']);
  const missing = await consumer.call('nasdaq_snapshot', { symbols: ['AAPL', 'ZZZZ'] });
  assert.equal(missing.rows.length, 1);
  assert.deepEqual(missing.unmatched_symbols, [{ symbol: 'ZZZZ', status: 'not_in_nasdaq_100' }]);
  assert.ok(missing.warnings.some(w => w.includes('ZZZZ')));
});

test('detail follows a retained nasdaq row without refetching latest', async () => {
  const { consumer, store } = setup();
  const first = await consumer.call('nasdaq_snapshot', { symbols: ['AAPL'] });
  store['nasdaq/latest.json'] = JSON.stringify(snapshot({ as_of: '2026-11-23T15:30:00Z' }));
  const out = await consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(out.status, 'available');
  assert.equal(out.row.symbol, 'AAPL');
  assert.equal(out.row.price, 350);
  assert.equal(out.actual.as_of, AS_OF);
});

test('missing publication is missing, not failed; bad schema fails closed', async () => {
  const { consumer } = setup({ files: {} });
  const missing = await consumer.call('nasdaq_snapshot');
  assert.equal(missing.status, 'missing');
  assert.equal(missing.errors[0].code, 'missing_artifact');
  const bad = setup({ files: { 'nasdaq/latest.json': JSON.stringify(snapshot({
    index: { ...snapshot().index, percent_change_basis: STOCK_BASIS } })) } });
  const out = await bad.consumer.call('nasdaq_snapshot');
  assert.equal(out.status, 'failed');
  assert.equal(out.errors[0].code, 'incompatible_schema');
});

test('snapshot older than the hourly cadence is stale, not fresh', async () => {
  const { consumer } = setup({ now: () => NOW + 2 * 3600 * 1000 });
  const out = await consumer.call('nasdaq_snapshot');
  assert.equal(out.freshness.status, 'stale');
  assert.equal(out.freshness.stale_after_seconds, 3600);
});

test('dynamic membership: a snapshot with a changed listing count still validates', async () => {
  // The collector re-fetches the live constituent list every run, so the
  // listing count is whatever the index holds now — not hard-coded 101.
  // A snapshot with 100 listings (and the same 3 fixture rows) must read
  // fine; a snapshot whose rows exceed its declared listings must not.
  const { consumer } = setup({ files: { 'nasdaq/latest.json': JSON.stringify(snapshot({
    universe: { source: 'Nasdaq official list-type/nasdaq100 API', as_of: '2026-11-23',
      listings: 100, companies: 99 },
    coverage: { listings: 100, available: 2, source_unavailable: 1, with_news: 0, with_candles: 0 },
  })) } });
  const out = await consumer.call('nasdaq_snapshot');
  assert.equal(out.status, 'partial');
  assert.equal(out.rows.length, 3);
  assert.equal(out.coverage.listings, 100);
  const bad = setup({ files: { 'nasdaq/latest.json': JSON.stringify(snapshot({
    universe: { source: 'Nasdaq official list-type/nasdaq100 API', as_of: '2026-11-23',
      listings: 2, companies: 2 },
  })) } });
  const failed = await bad.consumer.call('nasdaq_snapshot');
  assert.equal(failed.status, 'failed');
  assert.equal(failed.errors[0].code, 'incompatible_schema');
});

test('tools/list exposes nine read-only tools including nasdaq_snapshot', async () => {
  assert.equal(tools.length, 9);
  const entry = tools.find(t => t.name === 'nasdaq_snapshot');
  assert.ok(entry);
  assert.equal(entry.annotations.readOnlyHint, true);
  assert.equal(entry.inputSchema.additionalProperties, false);
  const { consumer } = setup();
  const p = new Protocol(consumer);
  await p.handle({ jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'test', version: '1' } } });
  await p.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const out = await p.handle({ jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'nasdaq_snapshot', arguments: { symbols: ['AAPL'], limit: 1 } } });
  assert.equal(out.result.isError, false);
  assert.equal(JSON.parse(out.result.content[0].text).rows[0].symbol, 'AAPL');
});
