// Render the actual authenticated handlers over disposable SQLite and fixtures.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const { chromium } = createRequire(import.meta.url)('playwright');
const out = process.env.PREDICTION_EVIDENCE_DIR || resolve(tmpdir(), 'prediction-browser-evidence');
mkdirSync(out, { recursive: true });
const preview = createPreview();
const originalFetch = globalThis.fetch;
globalThis.fetch = fixtureNetwork;
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: 'chromium' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const r = route.request(); requests.push(r.url());
    assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
    const response = await preview.fetch(new Request(r.url(), {
      method: r.method(), headers: await r.allHeaders(),
      ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}),
    }));
    await route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
  });
  await page.goto(`${LOCAL_ORIGIN}/prediction.html`);
  await page.waitForFunction(() => document.querySelector('#status').textContent.includes('Sign in'));
  await page.goto(LOCAL_ORIGIN);
  await page.locator('#auth-toggle').click();
  await page.locator('#auth-username').fill('prediction_reader');
  await page.locator('#auth-password').fill('disposable-fixture-password');
  await page.locator('#auth-submit').click();
  await page.waitForFunction(() => document.querySelector('#user-name').textContent === 'prediction_reader');
  await page.goto(`${LOCAL_ORIGIN}/prediction.html`);
  await page.locator('#markets button').waitFor();
  assert.match(await page.locator('#environment').textContent(), /Synthetic practice/);
  const before = await page.evaluate(async () => (await fetch('/api/me')).json());
  await page.screenshot({ path: resolve(out, 'prediction-desktop.png'), fullPage: true });
  await page.locator('#markets button').click();
  await page.locator('#qty').fill('3');
  await page.locator('#submit').click();
  await page.waitForFunction(() => document.querySelector('#receipt').textContent.includes('Filled 3 at $0.4500'));
  await page.screenshot({ path: resolve(out, 'prediction-desktop-ticket.png'), fullPage: true });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#portfolio').textContent.includes('3 held'));
  const bought = await page.evaluate(async () => (await fetch('/api/me')).json());
  assert.equal(Math.round((before.balance_cash - bought.balance_cash) * 100), 135);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(out, 'prediction-mobile.png'), fullPage: true });
  await page.locator('#markets button').click();
  await page.locator('#side').selectOption('sell');
  await page.locator('#qty').fill('3');
  await page.locator('#submit').click();
  await page.waitForFunction(() => document.querySelector('#receipt').textContent.includes('Filled 3 at $0.4000'));
  await page.screenshot({ path: resolve(out, 'prediction-mobile-ticket.png'), fullPage: true });
  await page.locator('#dismiss').click();
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#portfolio').textContent.includes('0 held'));
  const sold = await page.evaluate(async () => (await fetch('/api/me')).json());
  assert.equal(Math.round((before.balance_cash - sold.balance_cash) * 100), 15);
  assert.equal(sold.trades.filter(t => t.instrument_type === 'prediction').length, 2);
  assert.deepEqual(errors, []);
  const receipt = { tested_at: new Date().toISOString(), environment: 'disposable synthetic',
    viewports: ['1440x1000', '390x844'], authenticated_buy_exit_reload: true,
    all_requests_local: requests.every(url => new URL(url).origin === LOCAL_ORIGIN), page_errors: errors };
  writeFileSync(resolve(out, 'receipt.json'), JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify(receipt));
} finally { await browser?.close(); preview.close(); globalThis.fetch = originalFetch; }
