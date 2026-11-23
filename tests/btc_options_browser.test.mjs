import { setupBtcOwner, seedBtcOwnerSession } from './helpers/btc_owner_fixture.mjs';
// Local disposable full-page mobile flow. No provider network or credentials.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const preview = createPreview();
setupBtcOwner(preview.apiEnv);
preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
const cases = Object.fromEntries(btcClockScenarios().map(s => [s.name, s]));
let market = cases.snapshot.payload, clock = cases.snapshot.now;
const OldDate = Date, oldFetch = global.fetch;
global.Date = class extends OldDate { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } };
global.fetch = async (url, options) => {
  if (new URL(url).pathname !== '/btc-options') return fixtureNetwork(url, options);
  return Response.json(market);
};
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(clock => {
    const D = Date;
    window.fixtureClock = clock;
    window.Date = class extends D { constructor(...args) { super(...(args.length ? args : [window.fixtureClock])); } static now() { return window.fixtureClock; } };
  }, clock);
  const phase = async name => {
    market = cases[name].payload; clock = cases[name].now;
    await page.evaluate(clock => { window.fixtureClock = clock; }, clock);
    await page.evaluate(() => btcOptions.refresh());
  };
  await page.route('**/*', async route => {
    const r = route.request(); assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
    const response = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(),
      ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
    // Synthetic fixture proof; the separate owner-link suite exercises real WebAuthn.
    if (['/api/login','/api/register'].includes(new URL(r.url()).pathname) && response.ok && r.postDataJSON()?.username === 'btc_reader') {
      const token = response.headers.get('Set-Cookie').split(';')[0].slice('session='.length);
      await seedBtcOwnerSession(preview.apiEnv, token, 'btc_reader');
    }
    await route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
  });
  await page.goto(LOCAL_ORIGIN);
  await page.locator('#auth-toggle').click();
  await page.locator('#auth-username').fill('btc_reader');
  await page.locator('#auth-password').fill('fixture-password');
  await page.locator('#auth-submit').click();
  await page.waitForFunction(() => document.querySelector('#user-name').textContent === 'btc_reader');
  await page.locator('#underlying-select').selectOption('BTC');
  await page.locator('.btc-ticket').click();
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true, 'initial snapshot cannot fill');
  await phase('replay');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true, 'unchanged replay cannot fill');
  await phase('bid_only');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true, 'bid change cannot freshen ask');
  await phase('ask_price');
  assert.match(await page.locator('.btc-contract').textContent(), /observed side change/);
  assert.match(await page.locator('#trade-ticket .btn-buy').getAttribute('title'), /observed side change/);
  assert.match(await page.locator('#trade-ticket').textContent(), /500.00/);
  assert.equal(await page.locator('#t-qty').isDisabled(), true);
  await page.locator('#trade-ticket .btn-buy').click();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('BUY 1'));
  await page.reload();
  await page.evaluate(clock => { window.fixtureClock = clock; }, clock);
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('BUY 1'));
  await page.locator('#underlying-select').selectOption('BTC');
  await page.locator('.btc-contract').waitFor();
  await phase('aged_replay');
  assert.match(await page.locator('#paper-positions').textContent(), /Exit unavailable/);
  await phase('ask_size');
  assert.match(await page.locator('#paper-positions').textContent(), /Exit unavailable/, 'ask size cannot freshen held bid');
  await phase('missing_bid');
  assert.match(await page.locator('#paper-positions').textContent(), /Exit unavailable/);
  await phase('bid_baseline');
  assert.match(await page.locator('#paper-positions').textContent(), /Exit unavailable/, 'returning side starts a new baseline');
  await phase('close_bid');
  assert.match(await page.locator('#paper-positions').textContent(), /600.00 liquidation/);
  await page.locator('.btc-ticket').click();
  const box = await page.locator('#trade-ticket').boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
  await page.locator('#trade-ticket .btn-sell').click();
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('SELL 1'));
  assert.match(await page.locator('#paper-summary').textContent(), /95.00/);
  await page.reload();
  await page.evaluate(clock => { window.fixtureClock = clock; }, clock);
  await page.waitForFunction(() => document.querySelector('#paper-log-body').textContent.includes('SELL 1'));
  assert.match(await page.locator('#paper-log-body').textContent(), /fee \$2.50/);
  assert.match(await page.locator('#paper-log-body').textContent(), /observed side change \(not exchange time\)/);
  await page.locator('#underlying-select').selectOption('BTC');
  await page.locator('.btc-ticket').click();
  await phase('disconnected');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true);
  await phase('reconnect_snapshot');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true);
  await phase('reconnect_replay');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), true);
  await phase('reconnect_ask_size');
  assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), false);
  await page.locator('#underlying-select').selectOption('SPY');
  assert.equal(await page.locator('#trade-ticket').count(), 0);
  assert.equal(await page.locator('#btc-panel').isVisible(), false);
  assert.deepEqual(errors, []);
  console.log('PASS mobile real-collector fixture: snapshot/replay/opposite side blocked; changed-side buy/reload/stale/withdrawal/close/fees/provenance/reconnect/selection');
} finally { await browser?.close(); preview.close(); global.fetch = oldFetch; global.Date = OldDate; }
