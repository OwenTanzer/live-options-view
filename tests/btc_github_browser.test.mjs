// Both shipped auth paths, fixed gateway callback, and production entry HTML.
// All cookies, accounts, identity responses and quotes are synthetic/local.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createPreview, fixtureNetwork } from '../scripts/preview-local.mjs';
import { setupBtcOwner } from './helpers/btc_owner_fixture.mjs';
import { seedAccounts } from './helpers/test_env.mjs';
import { derivePasswordHash } from '../backend/auth.js';
import { CREDIT_ORIGIN, SITE, OWNER_ID } from '../backend/kalshi-access.js';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';
const scenario = btcClockScenarios().find(s => s.name === 'ask_price');
const realFetch = global.fetch, RealDate = Date;
let identity = OWNER_ID, status = 200, identityCalls = 0;
global.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [scenario.now])); } static now() { return scenario.now; } };
global.fetch = async (input, options = {}) => {
  const url = new URL(input);
  if (url.origin === CREDIT_ORIGIN) {
    assert.equal(options.redirect, 'manual');
    if (url.pathname === '/api/private/identity') {
      identityCalls++; assert.equal(options.headers.Cookie, '__Host-cds-session=synthetic_github_session_0123456789');
      return Response.json({ github_id: identity }, { status });
    }
    if (url.pathname === '/credit-tape/auth/login') return new Response(null, { status: 303,
      headers: { Location: SITE + '/credit-tape/auth/callback?code=fixture&state=fixture' } });
    if (url.pathname === '/credit-tape/auth/callback') return new Response(null, { status: 303, headers: {
      Location: '/credit-tape/', 'Set-Cookie': '__Host-cds-session=synthetic_github_session_0123456789; Secure; HttpOnly; SameSite=Lax; Path=/',
    } });
    throw Error('Unexpected identity route');
  }
  if (url.pathname === '/btc-options') return Response.json(scenario.payload);
  return fixtureNetwork(input, options);
};
const browser = await chromium.launch({ headless: true });
try {
  for (const width of [390, 320]) {
    identity = OWNER_ID; status = 200;
    const preview = createPreview(); setupBtcOwner(preview.apiEnv); preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
    preview.apiEnv.ENVIRONMENT = preview.env.ENVIRONMENT = 'production'; delete preview.apiEnv.ISOLATION;
    for (const k of ['ISOLATION', 'PREVIEW_ORIGIN', 'API_ENVIRONMENT', 'PUBLIC_DATA_ENVIRONMENT']) delete preview.env[k];
    const password = 'synthetic-normal-password', salt = btoa('synthetic-salt'), hash = await derivePasswordHash(password, salt);
    await seedAccounts(preview.apiEnv, ['normal_book', 'Owen'].map(username => ({ username, hash, salt, iterations: 100000, balance_cash: 10000, trades: [] })));
    const page = await browser.newPage({ viewport: { width, height: 844 } }), errors = [], actions = [];
    page.on('pageerror', e => errors.push(e.message));
    const install = async p => {
      await p.addInitScript(now => { const D = Date; window.Date = class extends D {
        constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; }
      }; }, scenario.now);
      await p.route('**/*', async route => {
        const r = route.request(), u = new URL(r.url());
        if (u.origin === 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev') return route.fulfill({ status: 404, body: '' });
        assert.equal(u.origin, SITE, 'never contact real OAuth or production');
        if (u.pathname === '/api/btc-options/owner-link') actions.push(r.postDataJSON().action);
        const response = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(),
          ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
        const headers = Object.fromEntries(response.headers);
        // Playwright accepts newline-separated Set-Cookie values; preserve both
        // upstream session and the gateway's return-hint clearing cookie.
        if (response.headers.has('Set-Cookie')) headers['set-cookie'] = response.headers.getSetCookie().join('\n');
        if ([302,303].includes(response.status)) {
          // Playwright routing can skip subsequent HTTP redirect hops. Start a
          // fresh, intercepted navigation instead; API tests pin the actual 303.
          const target = new URL(response.headers.get('Location'), SITE);
          assert.equal(target.origin, SITE); delete headers.location;
          headers['content-type'] = 'text/html';
          return route.fulfill({ status: 200, headers, body: `<script>location.replace(${JSON.stringify(target.href)})</script>` });
        }
        await route.fulfill({ status: response.status, headers, body: await response.text() });
      });
    };
    const login = async (p, name) => {
      await p.locator('#auth-username').fill(name); await p.locator('#auth-password').fill(password);
      await p.locator('#auth-submit').click();
      await p.waitForFunction(name => document.querySelector('#user-name').textContent === name, name);
    };
    await install(page);
    try {
      await page.goto(SITE + '/?chain=BTC'); await page.locator('.btc-github-login').waitFor();
      if (width === 320) await login(page, 'normal_book');
      await page.locator('.btc-github-login').click();
      await page.waitForURL('**/?chain=BTC');
      if (width === 390) { await page.locator('.btc-login').waitFor(); assert.equal(await page.locator('.btc-contract').count(), 0); await login(page, 'normal_book'); }
      await page.locator('.btc-buy:not([disabled])').waitFor();
      assert.equal(await page.locator('#user-name').textContent(), 'normal_book');
      assert.equal(actions.includes('options'), false, 'verified GitHub requires no passkey ceremony');
      assert.equal(actions.includes('verify'), false);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator('.btc-buy').click(); await page.locator('#trade-ticket .btn-buy').click();
      await page.waitForFunction(() => document.querySelector('.btc-position')?.textContent.includes('Owned: 1'));
      await page.reload(); await page.locator('.btc-sell:not([disabled])').waitFor();
      assert.equal(await page.evaluate(() => localStorage.getItem(SESSION_CACHE_KEY).includes('BTCOPT:')), false);
      const reads = identityCalls;
      status = 401;
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
      assert.equal(await page.locator('.btc-contract').count(), 0, 'BFCache restoration clears private rows before revalidation');
      await page.waitForFunction(() => typeof btcOptions !== 'undefined' && btcOptions.access === 'unauthorized');
      assert.equal(await page.locator('.btc-contract').count(), 0);
      assert.equal(await page.evaluate(() => session.trades.some(t => t.instrument_type === 'futures_option')), false);
      assert.ok(identityCalls > reads, 'revocation is checked remotely, not cached');
      status = 200; identity = OWNER_ID+1;
      await page.locator('.btc-github-check').click();
      await page.waitForFunction(() => !btcOwnerLink.busy && !!btcOwnerLink.message);
      assert.equal(await page.locator('.btc-contract').count(), 0);
      identity = OWNER_ID;
      await page.locator('.btc-github-check').click(); await page.locator('.btc-sell:not([disabled])').waitFor();
      await page.locator('.btc-sell').click(); await page.locator('#trade-ticket .btn-sell').click();
      await page.waitForFunction(() => document.querySelector('.btc-position')?.textContent === 'No open position');
      await page.locator('.btc-owner-unlink').click(); await page.locator('.btc-github-login').waitFor();
      await page.reload(); await page.locator('.btc-github-login').waitFor();
      assert.equal(await page.locator('.btc-contract').count(), 0, 'reload respects deliberate unlink');
      // A separate browser with an ordinary matching-name account gets no data.
      const other = await browser.newPage({ viewport: { width, height: 844 } }); await install(other);
      try {
        await other.goto(SITE + '/?chain=BTC'); await other.locator('.btc-login').waitFor(); await login(other, 'Owen');
        await other.locator('.btc-github-login').waitFor(); assert.equal(await other.locator('.btc-contract').count(), 0);
        assert.equal(await other.evaluate(() => session.trades.length), 0);
      } finally { await other.close(); }
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px GitHub/paper login both orders, fixed BTC return, seamless owner, buy/close, foreign identity, expiry, cache and unlink`);
    } finally { await page.close(); preview.close(); }
  }
} finally { await browser.close(); global.fetch = realFetch; global.Date = RealDate; }
