import { origin as usdcOrigin, usdcFixture } from './helpers/usdc_fixture.mjs';
// Production HTML and real WebAuthn, all routed to disposable local bindings.
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { chromium } from 'playwright';
import { createPreview, fixtureNetwork } from '../scripts/preview-local.mjs';
import { setupBtcOwner } from './helpers/btc_owner_fixture.mjs';
import { seedAccounts } from './helpers/test_env.mjs';
import { derivePasswordHash } from '../backend/auth.js';

const origin = 'https://options.moopertonic.net', originalFetch = global.fetch;
const browser = await chromium.launch({ headless: true });
global.fetch = async (url, options) => new URL(url).origin === usdcOrigin ? usdcFixture(url) : fixtureNetwork(url, options);
try {
  for (const width of [390, 320]) {
    const preview = createPreview(); setupBtcOwner(preview.apiEnv); preview.apiEnv.BTC_USDC_DATA_ORIGIN = usdcOrigin;
    // Exercise the real RP/origin with an entirely local API and asset binding.
    preview.env.ENVIRONMENT = 'production';
    for (const key of ['ISOLATION', 'PREVIEW_ORIGIN', 'API_ENVIRONMENT', 'PUBLIC_DATA_ENVIRONMENT']) delete preview.env[key];
    const password = 'synthetic-paper-password', salt = btoa('synthetic-salt');
    const hash = await derivePasswordHash(password, salt);
    await seedAccounts(preview.apiEnv, ['my_normal_paper', 'second_paper'].map(username => ({ username,
      hash, salt, iterations: 100000, balance_cash: 10000, trades: [] })));
    const invite = randomBytes(32).toString('base64url');
    await preview.apiEnv.DB.prepare("INSERT INTO trade_journal_members(member,invite_hash,invite_until) VALUES('Owen',?,unixepoch()+3600)")
      .bind(createHash('sha256').update(invite).digest('hex')).run();
    const page = await browser.newPage({ viewport: { width, height: 844 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('WebAuthn.enable');
    await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal',
      hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
    let verifyCount = 0, holdOptions = false, arrived, release;
    await page.route('**/*', async route => {
      const r = route.request(), url = new URL(r.url());
      if (url.origin === 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev') {
        // Existing equity widgets request public archives directly. Supply only
        // local synthetic documents; never pass this request to the network.
        return route.fulfill({ status: 404, body: '' });
      }
      assert.equal(url.origin, origin, 'all requests stay in the local harness');
      const result = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(),
        ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
      const body = await result.text();
      if (url.pathname === '/api/btc-options/owner-link') {
        const input = r.postDataJSON();
        assert.equal(input.invite, undefined); assert.equal(input.username, undefined);
        assert.equal(result.headers.has('Set-Cookie'), false, 'link does not create journal or paper sessions');
        if (input.action === 'verify') verifyCount++;
        if (input.action === 'options') {
          assert.deepEqual(JSON.parse(body).allowCredentials || [], []);
          if (holdOptions) { holdOptions = false; await new Promise(resolve => { release = resolve; arrived(); }); }
        }
      }
      await route.fulfill({ status: result.status, body, headers: Object.fromEntries(result.headers) });
    });
    const login = async name => {
      await page.locator('#auth-username').fill(name); await page.locator('#auth-password').fill(password);
      await page.locator('#auth-submit').click(); await page.locator('.btc-owner-link').waitFor();
    };
    try {
      // Fixture enrollment through the already-existing journal ceremony, then
      // deliberately leave the journal authenticated to prove it grants no BTC.
      await page.goto(origin + '/trade-journal.html');
      await page.locator('#access summary').click(); await page.locator('#invitation').fill(invite);
      await page.locator('#enroll').click(); await page.locator('#journal:not([hidden]) #restore').waitFor();
      await page.goto(origin + '/?chain=BTC'); await page.locator('.btc-login').waitFor();
      await login('my_normal_paper');
      assert.equal(await page.locator('.btc-contract').count(), 0);
      assert.equal(await page.getByRole('button', { name: 'Switch paper account' }).count(), 0);
      assert.match(await page.locator('.btc-status').textContent(), /my_normal_paper/);
      assert.equal(verifyCount, 0, 'no auto-link by paper name or journal session');
      await page.locator('.btc-owner-link').click(); await page.locator('.btc-contract').waitFor();
      assert.equal(verifyCount, 1);
      assert.equal(await page.locator('#user-name').textContent(), 'my_normal_paper');
      assert.equal(await page.locator('#underlying-select').inputValue(), 'BTC');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.reload(); await page.locator('.btc-contract').waitFor(); assert.equal(verifyCount, 1);
      await page.locator('.btc-owner-unlink').click(); await page.locator('.btc-owner-link').waitFor();
      assert.equal(await page.locator('.btc-contract').count(), 0);
      await page.locator('.btc-owner-link').click(); await page.locator('.btc-contract').waitFor();
      await page.locator('#auth-logout').click(); await page.locator('.btc-login').waitFor();
      await login('my_normal_paper'); assert.equal(await page.locator('.btc-contract').count(), 0, 'login does not carry an old proof');
      const priorCount = verifyCount;
      const waiting = new Promise(resolve => { arrived = resolve; }); holdOptions = true;
      await page.locator('.btc-owner-link').click(); await waiting;
      await page.locator('#auth-logout').click(); await page.locator('.btc-login').waitFor();
      await login('second_paper'); release();
      await page.waitForFunction(() => !btcOwnerLink.busy);
      assert.equal(verifyCount, priorCount, 'old ceremony cannot verify for replacement account');
      assert.equal(await page.locator('.btc-contract').count(), 0);
      await page.locator('.btc-owner-link').click(); await page.locator('.btc-contract').waitFor();
      // Credential revocation takes effect at the next read, including browser state.
      await preview.apiEnv.DB.prepare("DELETE FROM trade_journal_members WHERE member='Owen'").run();
      await page.evaluate(() => btcOptions.refresh());
      await page.locator('.btc-owner-link').waitFor();
      assert.equal(await page.locator('.btc-contract').count(), 0);
      assert.equal(await page.evaluate(() => btcOptions.quotes.size), 0);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px existing owner passkey: deliberate session link, no auto-link, refresh, unlink/logout, account race, revocation`);
    } finally { await page.close(); preview.close(); }
  }
} finally { global.fetch = originalFetch; await browser.close(); }
