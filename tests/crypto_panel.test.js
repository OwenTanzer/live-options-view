const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
// The panel code lives in site/js/crypto-panel.js, loaded in the same
// position the inline section occupied; the test loads the shipped
// module and pins the ?v= hash in the page to the module's content,
// like tests/squeeze_panel.test.js does for the squeeze panel.
const panelSource = fs.readFileSync(path.join(__dirname, '../site/js/crypto-panel.js'), 'utf8');
const panelHash = require('node:crypto').createHash('sha256').update(panelSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/crypto-panel.js?v=' + panelHash), 'crypto-panel script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function fetchCryptoLatest'), 'fetchCryptoLatest must live in js/crypto-panel.js, not inline in index.html');
assert.ok(html.includes('id="tab-crypto"'), 'Crypto tab button must exist');
assert.ok(html.includes('id="crypto-panel"'), 'Crypto panel section must exist');
assert.ok(html.includes('id="crypto-tbody"'), 'Crypto table body must exist');
assert.ok(html.includes('id="crypto-patterns"'), 'Crypto patterns list must exist');
// The Live panel and the private #btc-panel are untouched by this change.
assert.ok(html.includes('id="btc-panel"'), 'private #btc-panel must remain');

const elements = {};
const el = id => elements[id] ||= { textContent: '', innerHTML: '', dataset: {}, classList: { toggle() {} } };
const NOW = Date.parse('2026-11-23T22:00:00Z');
function snapshot() {
  return {
    schema_version: 'crypto_snapshot.v1',
    constructed_at: '2026-11-23T21:00:00Z',
    session_model: 'continuous_24_7',
    coins: [
      { rank: 1, symbol: 'BTC', name: 'Bitcoin', source: 'coinbase', price: 61232.71, high_24h: 64533.96, low_24h: 60286.92, change_24h_pct: 0.35, market_cap: 1230730252727, as_of: '2026-11-23T21:55:00Z', status: 'ok', volume_24h: 1041320023 },
      { rank: 3, symbol: 'USDT', name: 'Tether', source: 'coingecko', price: 1.0, high_24h: 1.001, low_24h: 0.999, change_24h_pct: 0.01, market_cap: 174692289241, as_of: '2026-11-23T21:55:00Z', status: 'ok', volume_24h: 38049718230 },
      { rank: 59, symbol: 'ASTER', name: 'Aster', source: 'coinbase', price: null, high_24h: null, low_24h: null, change_24h_pct: null, market_cap: null, as_of: null, status: 'source_unavailable', volume_24h: null },
    ],
    market: {
      total_market_cap_usd: 2122420129367, btc_dominance_pct: 58.0,
      fear_greed: { value: 11, classification: 'Extreme Fear', as_of: '2026-11-23T21:00:00Z', status: 'ok' },
    },
    patterns: [{ id: 'fear_greed_extreme', dedupe_key: 'fear_greed_extreme', kind: 'fear_greed_extreme', severity: 'extreme', title: 'Fear & Greed at 11 — extreme fear', detail: 'The Fear & Greed index reads 11 (Extreme Fear).', coins: [], detected_at: '2026-11-23T21:00:00Z' }],
  };
}
let responses = {};
const response = value => ({ ok: true, status: 200, json: async () => value });
function loadPanel() {
  for (const key of Object.keys(elements)) delete elements[key];
  const scope = { document: { getElementById: el }, BROWSER_DATA: '/browser-data',
    escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    fetch: async url => {
      const key = url.split('?')[0].split('/').at(-1);
      const value = responses[key];
      if (value instanceof Error) throw value;
      if (value && value.status === 404) return { ok: false, status: 404, json: async () => ({}) };
      return typeof value === 'function' ? value() : value;
    },
  };
  return new Function(...Object.keys(scope), panelSource + `
  let clock = Date.parse('2026-11-23T22:00:00Z');
  function cryptoNow() { return clock; }
  return { fetchCryptoLatest, renderCryptoPanel,
    formatUsd, formatPct, formatMarketCap, formatAsOf, cryptoAgeState,
    set now(v) { clock = Date.parse(v); },
    get snapshot() { return cryptoSnapshot; },
    get error() { return cryptoError; } };
`)(...Object.values(scope));
}

(async () => {
  // Pure helpers.
  const panel = loadPanel();
  assert.equal(panel.formatUsd(61232.71), '$61,232.71');
  assert.equal(panel.formatUsd(0.99876), '$0.9988');
  assert.equal(panel.formatUsd(null), '—');
  assert.equal(panel.formatPct(-5.7824), '-5.78%');
  assert.equal(panel.formatPct(0.35), '+0.35%');
  assert.equal(panel.formatMarketCap(2122420129367), '$2.12T');
  assert.equal(panel.formatAsOf('2026-11-23T21:55:00Z'), '21:55 UTC');
  assert.equal(panel.formatAsOf(null), '—');
  assert.equal(panel.cryptoAgeState('2026-11-23T21:00:00Z', NOW), 'fresh');
  assert.equal(panel.cryptoAgeState('2026-11-23T19:00:00Z', NOW), 'stale');
  assert.equal(panel.cryptoAgeState(null, NOW), 'unavailable');

  // Fresh snapshot: hero, table, patterns, source labels, as-of times.
  responses = { 'latest.json': response(snapshot()) };
  await panel.fetchCryptoLatest();
  assert.match(el('crypto-status').textContent, /fresh/);
  assert.match(el('crypto-status').textContent, /1 source unavailable/);
  assert.match(el('crypto-warning').textContent, /ASTER/);
  assert.match(el('crypto-hero').innerHTML, /\$61,232\.71/);
  assert.match(el('crypto-hero').innerHTML, /24h high \$64,533\.96/);
  assert.match(el('crypto-market').innerHTML, /Extreme Fear/);
  assert.match(el('crypto-market').innerHTML, /58\.0%/);
  assert.match(el('crypto-tbody').innerHTML, /BTC/);
  assert.match(el('crypto-tbody').innerHTML, /Coinbase/);
  assert.match(el('crypto-tbody').innerHTML, /CoinGecko/);
  assert.match(el('crypto-tbody').innerHTML, /source unavailable/);
  assert.match(el('crypto-tbody').innerHTML, /21:55 UTC/);
  assert.match(el('crypto-patterns').innerHTML, /Extreme/);
  assert.match(el('crypto-patterns').innerHTML, /Fear &amp; Greed at 11/);

  // Stale snapshot: the status says so instead of looking current.
  panel.now = '2026-11-24T01:00:00Z';
  panel.renderCryptoPanel();
  assert.match(el('crypto-status').textContent, /stale/);

  // No snapshot published yet: explicit state, no invented numbers.
  responses = { 'latest.json': { status: 404 } };
  const empty = loadPanel();
  await empty.fetchCryptoLatest();
  assert.match(el('crypto-status').textContent, /no snapshot published yet/);
  assert.match(el('crypto-tbody').innerHTML, /No crypto snapshot published yet/);

  // A transient fetch failure keeps the last-rendered table.
  responses = { 'latest.json': response(snapshot()) };
  const retry = loadPanel();
  await retry.fetchCryptoLatest();
  const goodTable = el('crypto-tbody').innerHTML;
  responses = { 'latest.json': new Error('network down') };
  await retry.fetchCryptoLatest();
  assert.match(el('crypto-status').textContent, /showing last known/);
  assert.equal(el('crypto-tbody').innerHTML, goodTable);

  console.log('crypto panel tests passed');
})().catch(e => { console.error(e); process.exit(1); });
