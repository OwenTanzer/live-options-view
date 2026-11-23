import { setupBtcOwner, seedBtcOwnerSession } from './helpers/btc_owner_fixture.mjs';
// Production entry HTML/scripts through the disposable gateway and real account API.
// All accounts, credentials, quotes and fills are local synthetic fixtures.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';
import { seedAccounts } from './helpers/test_env.mjs';
import { derivePasswordHash } from '../backend/auth.js';
const { chromium } = createRequire(import.meta.url)('playwright');
const cases = Object.fromEntries(btcClockScenarios().map(s => [s.name, s]));
const OriginalDate = Date, originalFetch = global.fetch;
let clock = cases.ask_price.now, market = cases.ask_price.payload;
global.Date = class extends OriginalDate { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } };
global.fetch = async (url, options) => new URL(url).pathname === '/btc-options' ? Response.json(market) : fixtureNetwork(url, options);
const browser = await chromium.launch({ headless: true });
try {
  for (const width of [390, 320]) {
    const preview = createPreview();
    setupBtcOwner(preview.apiEnv); preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
    const password = 'local-fixture-password', salt = btoa('local-test-salt');
    const hash = await derivePasswordHash(password, salt);
    await seedAccounts(preview.apiEnv, ['entitled_fixture', 'wrong_fixture'].map(username => ({
      username, hash, salt, iterations: 100000, balance_cash: 10000, trades: [], createdAt: new Date(clock).toISOString(),
    })));
    const context = await browser.newContext({ viewport: { width, height: 844 } });
    const page = await context.newPage(), errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    let heldPath = null, releaseHeld, arrived;
    const hold = path => { heldPath = path; return new Promise(resolve => { arrived = resolve; }); };
    await page.addInitScript(now => {
      const D = Date; window.fixtureClock = now;
      window.Date = class extends D { constructor(...args) { super(...(args.length ? args : [window.fixtureClock])); } static now() { return window.fixtureClock; } };
    }, cases.ask_price.now);
    await page.route('**/*', async route => {
      const r = route.request(), url = new URL(r.url());
      assert.equal(url.origin, LOCAL_ORIGIN, 'test must never contact production');
      requests.push(url.pathname);
      // The response is computed before the delay to exercise an already-authorized old response.
      const response = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(),
        ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
    // Synthetic fixture proof; the separate owner-link suite exercises real WebAuthn.
    if (['/api/login','/api/register'].includes(new URL(r.url()).pathname) && response.ok && r.postDataJSON()?.username === 'entitled_fixture') {
      const token = response.headers.get('Set-Cookie').split(';')[0].slice('session='.length);
      await seedBtcOwnerSession(preview.apiEnv, token, 'entitled_fixture');
    }
      const body = Buffer.from(await response.arrayBuffer());
      if (url.pathname === heldPath) {
        heldPath = null;
        await new Promise(resolve => { releaseHeld = resolve; arrived(); });
      }
      await route.fulfill({ status: response.status, body, headers: Object.fromEntries(response.headers) });
    });
    const phase = async name => {
      market = cases[name].payload; clock = cases[name].now;
      await page.evaluate(now => { window.fixtureClock = now; }, clock);
      await page.evaluate(() => btcOptions.refresh());
    };
    const login = async username => {
      await page.locator('#auth-username').fill(username);
      await page.locator('#auth-password').fill(password);
      assert.equal(await page.locator('#auth-submit').textContent(), 'Log In');
      await page.locator('#auth-submit').click();
      await page.waitForFunction(username => document.querySelector('#user-name').textContent === username, username);
    };
    try {
      market = cases.ask_price.payload; clock = cases.ask_price.now;
      await page.goto(LOCAL_ORIGIN + '/?chain=BTC');
      await page.locator('.btc-login').waitFor();
      assert.equal(await page.locator('#underlying-select').inputValue(), 'BTC');
      assert.equal(await page.locator('#btc-select').count(), 0, 'orphan control removed');
      assert.equal(await page.locator('.btc-contract').count(), 0);
      for (const id of ['qqq-iv', 'status-bar', 'oi-skew-meter', 'spy-oi-skew-meter', 'ibit-oi-skew-meter', 'price-strip', 'tab-history', 'heatmap-wrap']) {
        assert.equal(await page.locator('#' + id).isVisible(), false, id);
      }
      await page.locator('.btc-login').click();
      assert.equal(await page.locator('#auth-username').evaluate(el => el === document.activeElement), true);
      await login('wrong_fixture');
      await page.locator('.btc-owner-link').waitFor();
      assert.equal(await page.locator('.btc-contract').count(), 0);
      await page.locator('#auth-logout').click();
      await login('entitled_fixture');
      await page.locator('.btc-buy:not([disabled])').waitFor();
      assert.equal(await page.locator('#underlying-select').inputValue(), 'BTC');
      assert.match(page.url(), /chain=BTC/);
      assert.equal(requests.includes('/api/register'), false, 'login uses a pre-existing fixture account');
      assert.match(await page.locator('.btc-sell-reason').textContent(), /No position/);
      assert.match(await page.locator('.btc-ask-time').textContent(), /observed side change/);
      for (const selector of ['#underlying-select', '.btc-contract', '.btc-buy', '.btc-sell', '.btc-ticket']) {
        const box = await page.locator(selector).boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width + 1, `${width}: ${selector}`);
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no page overflow');
      assert.equal(await page.locator('#footer').isVisible(), false, 'equity OI legend hidden');
      if (process.env.BTC_MOBILE_EVIDENCE) {
        mkdirSync(process.env.BTC_MOBILE_EVIDENCE, { recursive: true });
        await page.screenshot({ path: `${process.env.BTC_MOBILE_EVIDENCE}/btc-${width}-chain.png`, fullPage: true });
      }
      // Direct row affordance opens the actual existing ticket for review and submission.
      await page.locator('.btc-buy').click();
      assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(), false);
      await page.locator('#trade-ticket .btn-buy').click();
      await page.waitForFunction(() => document.querySelector('.btc-position')?.textContent.includes('Owned: 1'));
      assert.equal(await page.evaluate(() => localStorage.getItem(SESSION_CACHE_KEY).includes('BTCOPT:')), false, 'private BTC rows never enter display cache');
      await page.reload();
      await page.locator('.btc-contract').waitFor();
      assert.equal(await page.locator('#underlying-select').inputValue(), 'BTC');
      assert.match(await page.locator('.btc-position').textContent(), /Owned: 1/);
      await phase('aged_replay');
      assert.equal(await page.locator('.btc-buy').isDisabled(), true);
      assert.equal(await page.locator('.btc-sell').isDisabled(), true);
      assert.match(await page.locator('.btc-sell-reason').textContent(), /stale/);
      await phase('close_bid');
      assert.equal(await page.locator('.btc-sell').isDisabled(), false);
      await page.locator('.btc-sell').click();
      await page.locator('#trade-ticket .btn-sell').click();
      await page.waitForFunction(() => document.querySelector('.btc-position')?.textContent === 'No open position');
      // Provider-timed freshness uses the same UI action path as observed clocks.
      const timed = structuredClone(cases.close_bid.payload), ts = new Date(clock).toISOString();
      Object.assign(timed.quotes[0], { bid_ts: ts, ask_ts: ts }); market = timed;
      await page.evaluate(() => btcOptions.refresh());
      assert.match(await page.locator('.btc-ask-time').textContent(), /provider side time/);
      assert.equal(await page.locator('.btc-buy').isDisabled(), false);
      // A response begun under BTC cannot restore rows after selecting equity.
      let waiting = hold('/api/btc-options');
      const lateBtc = page.evaluate(() => btcOptions.refresh()); await waiting;
      await page.locator('#underlying-select').selectOption('SPY');
      releaseHeld(); await lateBtc;
      assert.equal(await page.locator('.btc-contract').count(), 0);
      assert.equal(await page.locator('#tab-history').isVisible(), true);
      await page.locator('#underlying-select').selectOption('QQQ');
      assert.equal(await page.locator('#qqq-iv').isVisible(), true);
      // Likewise, a delayed non-QQQ snapshot cannot repaint the Bitcoin header.
      waiting = hold('/browser-data/intraday/spy/latest.json');
      await page.locator('#underlying-select').selectOption('SPY'); await waiting;
      await page.locator('#underlying-select').selectOption('BTC');
      releaseHeld();
      await page.locator('.btc-contract').waitFor();
      assert.equal(await page.locator('#title').textContent(), 'Bitcoin futures options');
      assert.equal(await page.locator('#underlying-select').inputValue(), 'BTC');
      // A private response already computed for the old account is discarded at logout.
      waiting = hold('/api/btc-options');
      const lateAccount = page.evaluate(() => btcOptions.refresh()); await waiting;
      await page.locator('#auth-logout').click();
      assert.equal(await page.locator('.btc-contract').count(), 0);
      releaseHeld(); await lateAccount;
      assert.equal(await page.locator('.btc-contract').count(), 0);
      await page.locator('.btc-login').click(); await login('wrong_fixture');
      await page.locator('.btc-owner-link').waitFor();
      assert.equal(await page.locator('.btc-contract').count(), 0);
      assert.equal(await page.evaluate(() => btcOptions.quotes.size), 0);
      // An order accepted for the old account may finish after logout; its reply
      // must not append a fill to the replacement account or throw on null session.
      await page.locator('#auth-logout').click();
      await login('entitled_fixture');
      await page.locator('.btc-buy:not([disabled])').waitFor();
      waiting = hold('/api/paper-trade');
      await page.locator('.btc-buy').click();
      await page.locator('#trade-ticket .btn-buy').click(); await waiting;
      await page.locator('.btc-dismiss').click();
      await page.locator('#auth-logout').click();
      await page.locator('.btc-login').click(); await login('wrong_fixture');
      releaseHeld();
      await page.locator('.btc-owner-link').waitFor();
      await page.waitForFunction(() => pendingExecutions.size === 0);
      assert.equal(await page.evaluate(() => session.trades.length), 0, 'old-account fill cannot populate new account');
      assert.deepEqual(errors, []);
      if (process.env.BTC_MOBILE_EVIDENCE) {
        mkdirSync(process.env.BTC_MOBILE_EVIDENCE, { recursive: true });
        await page.screenshot({ path: `${process.env.BTC_MOBILE_EVIDENCE}/btc-${width}-account.png`, fullPage: true });
      }
      console.log(`PASS ${width}px production entry: selector/auth/refresh/actions/ownership/freshness/equity restoration/view and account races`);
    } finally { await context.close(); preview.close(); }
  }
} finally { await browser.close(); global.fetch = originalFetch; global.Date = OriginalDate; }
