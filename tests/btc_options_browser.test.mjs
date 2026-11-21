// Local disposable full-page mobile flow. No provider network or credentials.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const preview = createPreview();
preview.apiEnv.BTC_OPTIONS_USERS = 'btc_reader';
preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
const sym = 'BTCOPT:' + 'a'.repeat(40);
const c = { contract_key: sym, instrument_type: 'futures_option', product: '/BTC', series: 'monthly',
  exchange: 'CME', multiplier: 5, type: 'call', strike: 90000,
  provider_symbol: './BTCZ6 BTCZ6 261127C90000', streamer_symbol: './BTCZ26C90000:XCME',
  future_symbol: '/BTCZ6', future_month: '2026-11', future_expiry: '2026-11-27', exp: '2026-11-27',
  expires_at: '2026-11-27T16:00:00Z', stops_trading_at: '2026-11-27T16:00:00Z' };
const clock = Date.parse('2026-11-21T18:00:00Z'), OldDate = Date, oldFetch = global.fetch;
global.Date = class extends OldDate { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } };
let bid = 99, ask = 100, missing = false;
global.fetch = async (url, options) => {
  if (new URL(url).pathname !== '/btc-options') return fixtureNetwork(url, options);
  const ts = new Date(clock).toISOString();
  return Response.json({ contracts: [c], entry_keys: [sym], session: 'open', source_status: 'fixture',
    quotes: [{ symbol: sym, contract: c, available: !missing, bid, ask, bid_size: 2, ask_size: 1,
      bid_ts: ts, ask_ts: ts, bid_received_ts: ts, ask_received_ts: ts }] });
};
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(clock => {
    const D = Date;
    window.Date = class extends D { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } };
  }, clock);
  await page.route('**/*', async route => {
    const r = route.request(); assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
    const response = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(),
      ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
    await route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
  });
  await page.goto(LOCAL_ORIGIN);
  await page.locator('#auth-toggle').click();
  await page.locator('#auth-username').fill('btc_reader');
  await page.locator('#auth-password').fill('fixture-password');
  await page.locator('#auth-submit').click();
  await page.waitForFunction(() => document.querySelector('#user-name').textContent === 'btc_reader');
  await page.locator('#btc-select').click();
  await page.locator('.btc-contract').click();
  assert.match(await page.locator('#trade-ticket').textContent(), /500.00/);
  assert.equal(await page.locator('#t-qty').isDisabled(), true);
  await page.locator('#trade-ticket .btn-buy').click();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('BUY 1'));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('BUY 1'));
  await page.locator('#btc-select').click();
  await page.locator('.btc-contract').waitFor();
  missing = true; await page.evaluate(() => btcOptions.refresh());
  assert.match(await page.locator('#paper-positions').textContent(), /Exit unavailable/);
  bid = 120; ask = 121; missing = false; await page.evaluate(() => btcOptions.refresh());
  assert.match(await page.locator('#paper-positions').textContent(), /600.00 liquidation/);
  await page.locator('.btc-contract').click();
  const box = await page.locator('#trade-ticket').boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
  await page.locator('#trade-ticket .btn-sell').click();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('SELL 1'));
  assert.match(await page.locator('#paper-summary').textContent(), /95.00/);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('SELL 1'));
  assert.match(await page.locator('#paper-log-body').textContent(), /fee \$2.50/);
  await page.locator('#btc-select').click();
  await page.locator('.btc-contract').click();
  await page.locator('#underlying-select').selectOption('SPY');
  assert.equal(await page.locator('#trade-ticket').count(), 0);
  assert.equal(await page.locator('#btc-panel').isVisible(), false);
  assert.deepEqual(errors, []);
  console.log('PASS mobile BTC fixture buy/reload/missing bid/close/reload/fees/selection isolation');
} finally { await browser?.close(); preview.close(); global.fetch = oldFetch; global.Date = OldDate; }
