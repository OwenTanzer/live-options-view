'use strict';
// Crypto snapshot MCP tool (crypto_snapshot) and discover_sources entry.
// Offline: the artifact is injected, never fetched from a live origin.
const test = require('node:test');
const assert = require('node:assert/strict');
const { Consumer } = require('./consumer');
const { Source, DataError } = require('./source');
const { tools } = require('./server');

const NOW = Date.parse('2026-11-23T22:00:00Z');
const KEY = 'crypto/latest.json';

function coin(symbol, source, overrides = {}) {
  return {
    rank: 1, symbol, name: symbol, source, price_basis: source,
    metadata_source: 'coingecko', currency: 'USD', price: 100,
    high_24h: 105, low_24h: 95, change_24h_pct: 1.0, change_1h_pct: 0.5,
    change_7d_pct: 2.0, volume_24h: 1000000, market_cap: 1000000000,
    provider_observed_at: '2026-11-23T21:55:00Z', as_of: '2026-11-23T21:55:00Z',
    freshness: 'fresh', status: 'ok', status_reason: null, gaps: [],
    ...overrides,
  };
}

function fixture() {
  return JSON.stringify({
    schema_version: 'crypto_snapshot.v1',
    artifact: KEY,
    constructed_at: '2026-11-23T21:00:00Z',
    generated_at: '2026-11-23T21:00:00Z',
    session_model: 'continuous_24_7',
    cadence: 'hourly',
    coins: [
      coin('BTC', 'coinbase'),
      coin('USDT', 'coingecko', { rank: 3, price: 1.0 }),
      coin('ASTER', 'coinbase', {
        rank: 59, price: null, high_24h: null, low_24h: null,
        change_24h_pct: null, change_1h_pct: null, change_7d_pct: null,
        volume_24h: null, market_cap: null, provider_observed_at: null,
        as_of: null, freshness: 'unavailable', status: 'source_unavailable',
        status_reason: 'coinbase ticker: HTTP 404',
        gaps: ['coinbase ticker: HTTP 404'],
      }),
    ],
    market: {
      total_market_cap_usd: 2130000000000, btc_dominance_pct: 58.0,
      fear_greed: { value: 11, classification: 'Extreme Fear', as_of: '2026-11-23T21:00:00Z', status: 'ok', source: 'alternative.me' },
      status: 'ok', status_reason: null,
    },
    patterns: [{
      id: 'fear_greed_extreme', dedupe_key: 'fear_greed_extreme',
      kind: 'fear_greed_extreme', severity: 'extreme',
      title: 'Fear & Greed at 11 — extreme fear',
      detail: 'The Fear & Greed index reads 11 (Extreme Fear).',
      coins: [], detected_at: '2026-11-23T21:00:00Z',
    }],
    counts: { coins: 3, available: 2, partial: 0, source_unavailable: 1 },
  });
}

function setup({ files = { [KEY]: fixture() }, now = () => NOW } = {}) {
  const source = new Source({ read: async key => {
    if (!Object.hasOwn(files, key)) throw new DataError('missing_artifact', 'Fixture is absent.', key);
    return files[key];
  } });
  return { consumer: new Consumer(source, { now }), files };
}

test('crypto_snapshot is a registered read-only tool', () => {
  const tool = tools.find(t => t.name === 'crypto_snapshot');
  assert.ok(tool);
  assert.equal(tool.annotations.readOnlyHint, true);
});

test('discover_sources lists crypto_snapshot with its fields and cadence', async () => {
  const { consumer } = setup();
  const out = await consumer.call('discover_sources');
  const cap = out.capabilities.find(c => c.dataset === 'crypto_snapshot');
  assert.ok(cap);
  assert.equal(cap.availability, 'available');
  assert.equal(cap.producer_schema, 'crypto_snapshot.v1');
  assert.equal(cap.source_locator, KEY);
  assert.ok(cap.fields.includes('price_basis'));
  assert.ok(cap.fields.includes('provider_observed_at'));
  assert.ok(cap.fields.includes('patterns'));
  assert.deepEqual(cap.filters, ['symbol', 'source', 'limit', 'offset']);
  assert.match(cap.cadence, /hourly/);
  assert.equal(cap.freshness.status, 'fresh');
  assert.equal(cap.freshness.basis,
    'collector construction time; each row carries its own provider observation time');
});

test('crypto_snapshot returns rows with the market_context field/freshness shape', async () => {
  const { consumer } = setup();
  const out = await consumer.call('crypto_snapshot');
  assert.equal(out.dataset, 'crypto_snapshot');
  assert.equal(out.status, 'partial'); // one source-unavailable row
  assert.equal(out.rows.length, 3);
  assert.equal(out.rows[0].symbol, 'BTC');
  assert.equal(out.rows[0].price_basis, 'coinbase');
  assert.equal(out.rows[0].freshness, 'fresh');
  assert.equal(out.freshness.status, 'fresh');
  assert.equal(out.market.fear_greed.value, 11);
  assert.equal(out.patterns.length, 1);
  assert.equal(out.patterns[0].severity, 'extreme');
  assert.equal(out.coverage.unavailable_rows, 1);
  assert.ok(out.warnings.some(w => w.includes('source-unavailable')));
});

test('crypto_snapshot filters by symbol and source without substitution', async () => {
  const { consumer } = setup();
  const bySymbol = await consumer.call('crypto_snapshot', { symbol: 'USDT' });
  assert.deepEqual(bySymbol.rows.map(r => r.symbol), ['USDT']);
  assert.equal(bySymbol.rows[0].source, 'coingecko');
  const bySource = await consumer.call('crypto_snapshot', { source: 'coingecko' });
  assert.deepEqual(bySource.rows.map(r => r.symbol), ['USDT']);
  // The unavailable Coinbase row keeps a null price; it is never
  // priced from the CoinGecko row for a different coin.
  const aster = await consumer.call('crypto_snapshot', { symbol: 'ASTER' });
  assert.equal(aster.rows[0].price, null);
  assert.equal(aster.rows[0].status, 'source_unavailable');
  assert.equal(aster.rows[0].price_basis, 'coinbase');
});

test('crypto_snapshot reports a missing publication as missing, not failed', async () => {
  const { consumer } = setup({ files: {} });
  const out = await consumer.call('crypto_snapshot');
  assert.equal(out.status, 'missing');
  assert.equal(out.errors[0].code, 'missing_artifact');
  const discovered = await consumer.call('discover_sources');
  const cap = discovered.capabilities.find(c => c.dataset === 'crypto_snapshot');
  assert.equal(cap.availability, 'missing_publication');
});

test('a row whose price basis disagrees with its source fails closed', async () => {
  const { consumer, files } = setup();
  const value = JSON.parse(files[KEY]);
  value.coins[0].price_basis = 'coingecko'; // BTC is coinbase-sourced
  files[KEY] = JSON.stringify(value);
  const out = await consumer.call('crypto_snapshot');
  assert.equal(out.status, 'failed');
  assert.ok(out.errors.length > 0);
});

test('market_context still rejects non-QQQ/SPY underlyings', async () => {
  const { consumer } = setup();
  const out = await consumer.call('market_context', { underlying: 'BTC' });
  assert.equal(out.status, 'failed');
  assert.ok(out.errors.some(e => /Unsupported underlying/.test(e.message)));
});
