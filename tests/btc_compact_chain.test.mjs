// Real production entry, disposable API/auth; synthetic display data only.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { setupBtcOwner, seedBtcOwnerSession } from './helpers/btc_owner_fixture.mjs';
import { seedAccounts } from './helpers/test_env.mjs';
import { derivePasswordHash } from '../backend/auth.js';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';
const scenario = btcClockScenarios().find(s => s.name === 'close_bid');
const originalFetch = global.fetch;
global.fetch = async (url, opts) => new URL(url).pathname === '/btc-options' ? Response.json(scenario.payload) : fixtureNetwork(url, opts);
const browser = await chromium.launch({ headless: true });
const evidence = process.env.BTC_COMPACT_EVIDENCE;
try {
  for (const width of [320, 375, 390]) {
    const preview = createPreview(); setupBtcOwner(preview.apiEnv); preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
    const salt = btoa('compact-fixture'), password = 'synthetic-password';
    await seedAccounts(preview.apiEnv, [{ username: 'compact_fixture', salt, hash: await derivePasswordHash(password, salt), iterations: 100000, balance_cash: 10000, trades: [] }]);
    const context = await browser.newContext({ viewport: { width, height: 667 } });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(now => {
      delete URLSearchParams.prototype.size; // Older Safari still supports toString().
      const D = Date; window.fixtureClock = now;
      window.Date = class extends D { constructor(...args) { super(...(args.length ? args : [window.fixtureClock])); } static now() { return window.fixtureClock; } };
    }, scenario.now);
    await page.route('**/*', async route => {
      const r = route.request(); assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
      const response = await preview.fetch(new Request(r.url(), { method:r.method(), headers:await r.allHeaders(), ...(r.postDataBuffer() ? {body:r.postDataBuffer()} : {}) }));
      if (new URL(r.url()).pathname === '/api/login' && response.ok) await seedBtcOwnerSession(preview.apiEnv, response.headers.get('Set-Cookie').split(';')[0].slice(8), 'compact_fixture');
      await route.fulfill({ status:response.status, headers:Object.fromEntries(response.headers), body:Buffer.from(await response.arrayBuffer()) });
    });
    try {
      await page.goto(LOCAL_ORIGIN + '/?chain=BTC');
      await page.locator('.btc-login').click();
      await page.locator('#auth-username').fill('compact_fixture'); await page.locator('#auth-password').fill(password);
      await page.locator('#auth-submit').click(); await page.locator('.btc-contract').waitFor();
      // Freeze only polling after real login; exercise the shipped renderer and ticket.
      await page.evaluate(payload => {
        btcOptions.generation++; // Discard any real-login refresh already in flight.
        btcOptions.refresh = async () => {};
        const contracts = [], quotes = [];
        let n = 0;
        for (const month of ['2026-11', '2026-12']) for (const strike of [85000, 86000, 87000, 88000, 89000, 90000, 91000, 92000]) for (const type of ['call','put']) {
          if (strike === 92000 && type === 'put') continue;
          const c = { ...payload.contracts[0], contract_key:'BTCOPT:' + (++n).toString(16).padStart(40,'0'), strike, type, future_month:month, future_symbol:month === '2026-11' ? '/BTCX6' : '/BTCZ6', exp:month + '-27' };
          const ts = new Date().toISOString();
          contracts.push(c); quotes.push({ ...payload.quotes[0], symbol:c.contract_key, contract:c, bid:120 + n, ask:122 + n, bid_size:2, ask_size:3, bid_ts:ts, ask_ts:ts, bid_received_ts:ts, ask_received_ts:ts });
        }
        // Deliberately shuffled input; pairing must never cross futures months.
        btcOptions.data = { ...payload, expiry:null, contracts:contracts.reverse(), quotes, entries_enabled:true, entry_keys:contracts.map(c=>c.contract_key), session:'open' };
        btcOptions.quotes = new Map(quotes.map(q=>[q.symbol,q]));
        session.trades = contracts.filter(c=>c.strike===85000).map(c=>({sym:c.contract_key, instrument_type:'futures_option', contract:c, multiplier:5, side:'buy', qty:1, price:100, fees:2.5, strike:c.strike, type:c.type, exp:c.exp}));
        btcOptions.render(); window.scrollTo(0,0);
      }, scenario.payload);
      const rects = await page.locator('.btc-strike-row').evaluateAll(rows => rows.map(row => {
        const r = row.getBoundingClientRect(); return {strike:row.dataset.strike, top:r.top,bottom:r.bottom,left:r.left,right:r.right,complete:r.top>=0 && r.bottom<=innerHeight};
      }));
      assert.ok(rects.filter(r=>r.complete).length >= 5, JSON.stringify({width,rects}));
      assert.equal(new Set(rects.filter(r=>r.complete).map(r=>r.strike)).size, rects.filter(r=>r.complete).length);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
      assert.equal(await page.locator('.btc-series').count(), 2);
      assert.equal(await page.locator('.btc-empty').count(), 2);
      assert.deepEqual(await page.locator('.btc-series').first().locator('.btc-strike').allTextContents(), ['85,000','86,000','87,000','88,000','89,000','90,000','91,000','92,000']);
      if (evidence) {
        mkdirSync(evidence,{recursive:true});
        await page.screenshot({path:`${evidence}/btc-compact-${width}x667.png`});
        writeFileSync(`${evidence}/btc-compact-${width}x667.json`,JSON.stringify({width,height:667,source:'site/index.html via disposable preview; synthetic quotes and holdings',rects},null,2));
      }
      for (const type of ['call','put']) for (const action of ['buy','sell']) {
        const leg = page.locator('.btc-series').first().locator(`.btc-contract[data-type="${type}"]`).first();
        const symbol = await leg.getAttribute('data-symbol');
        await leg.locator('.btc-'+action).click();
        assert.equal(await page.locator('#trade-ticket').getAttribute('data-btc'),symbol);
        assert.equal(await page.locator(`#trade-ticket .btn-${action}`).evaluate(el=>el===document.activeElement),true);
        assert.equal(await page.locator(`#trade-ticket .btn-${action}`).isDisabled(),false);
        assert.match(await page.locator('.btc-ticket-details').textContent(),/Owned: 1.*Bid size 2.*ask size 3/);
        await page.evaluate(()=>closeTicket());
      }
      await page.evaluate(()=>{ window.fixtureClock += 16000; btcOptions.updateActions(); });
      assert.equal(await page.locator('.btc-buy:not(:disabled),.btc-sell:not(:disabled)').count(),0);
      assert.match(await page.locator('.btc-buy').first().getAttribute('aria-label'),/stale/);
      await page.locator('.btc-ticket').first().click();
      assert.equal(await page.locator('#trade-ticket .btn-buy').isDisabled(),true);
      assert.equal(await page.locator('#trade-ticket .btn-sell').isDisabled(),true);
      assert.match(await page.locator('.btc-ticket-details').textContent(),/stale/);
      await page.evaluate(() => {
        closeTicket();
        const data = btcOptions.data;
        data.expiries = [...new Set(data.contracts.map(c => c.exp))].sort();
        data.expiry = data.expiries[0];
        data.center = 89000;
        btcOptions.render();
        btcOptions.refresh = () => { data.expiry = btcOptions.expiry; btcOptions.render(); };
      });
      for (const expiry of ['2026-11-27', '2026-12-27']) {
        await page.getByLabel('Standard monthly expiry').selectOption(expiry);
        assert.equal(await page.locator('.btc-series').count(), 1);
        assert.equal(await page.locator('.btc-contract').count(), 15);
        for (const edge of ['first', 'last']) {
          const row = page.locator('.btc-strike-row')[edge]();
          await row.scrollIntoViewIfNeeded();
          const top = await row.evaluate(el => el.getBoundingClientRect().top);
          await page.evaluate(() => btcOptions.refresh());
          assert.equal(await row.evaluate(el => el.getBoundingClientRect().top), top);
        }
      }
      if (width === 375) {
        // Exercise the ordinary signed-in API and polling path at production chain size.
        const months = ['2026-11-27', '2026-12-24'];
        const listings = months.map((exp, month) => Array.from({ length: 414 }, (_, i) => ({
          ...scenario.payload.contracts[0], exp, strike: 10000 + Math.floor(i / 2) * 1000,
          type: i % 2 ? 'put' : 'call', contract_key: 'BTCOPT:' + (month * 414 + i).toString(16).padStart(40, '0')
        })));
        let outage = false, marketCenter = false;
        global.fetch = async (url, opts) => {
          const u = new URL(url);
          if (u.pathname !== '/btc-options') return fixtureNetwork(url, opts);
          if (outage) return new Response('', { status: 503 });
          const month = u.searchParams.get('expiry') === months[1] ? 1 : 0;
          const contracts = listings[month];
          const display_prices = { [contracts[0].contract_key]: { mark: 50700, as_of: new Date(scenario.now).toISOString() } };
          const keys = new Set((u.searchParams.get('symbols') || '').split(','));
          const quotes = contracts.filter(c => keys.has(c.contract_key) && c !== contracts[0]).map(c => ({
            ...scenario.payload.quotes[0], contract: c, symbol: c.contract_key,
            ask_ts: new Date(scenario.now).toISOString(), ask_received_ts: new Date(scenario.now).toISOString()
          }));
          quotes.push({ ...scenario.payload.quotes[0], symbol: contracts[0].contract_key, contract: contracts[0], bid: 0, ask: 0 });
          return Response.json({ ...scenario.payload, display_prices, contracts, quotes, entry_keys: contracts.map(c => c.contract_key),
            expiry: months[month], expiries: months, subscribed_keys: quotes.map(q => q.symbol),
            center: marketCenter ? 59000 : 113000, center_from_future: marketCenter });
        };
        try {
          await page.addInitScript(() => {
            const interval = window.setInterval.bind(window);
            window.setInterval = (fn, ms, ...args) => ms === 5000 ? 0 : interval(fn, ms, ...args);
          });
          await page.reload();
          const rows = page.locator('.btc-strike-row');
          await page.waitForFunction(() => document.querySelectorAll('.btc-strike-row').length === 207);
          assert.deepEqual(new Set(await page.locator('.btc-contract').evaluateAll(els => els.map(el => el.dataset.symbol))),
            new Set(listings[0].map(c => c.contract_key)));
          marketCenter = true;
          await page.evaluate(() => btcOptions.refresh());
          const center = await rows.nth(49).boundingBox();
          assert.ok(center.y >= 0 && center.y + center.height <= 667);
          for (const row of [rows.first(), rows.last()]) {
            await row.scrollIntoViewIfNeeded();
            const top = await row.evaluate(el => el.getBoundingClientRect().top);
            await page.waitForFunction(key => btcOptions.quotes.has(key), await row.locator('.btc-contract').last().getAttribute('data-symbol'));
            await page.evaluate(() => btcOptions.refresh());
            assert.equal(await row.evaluate(el => el.getBoundingClientRect().top), top);
            outage = true;
            await page.evaluate(() => btcOptions.refresh());
            assert.equal(await row.locator('.btc-buy').first().isDisabled(), true);
            outage = false;
            await page.evaluate(() => btcOptions.refresh());
            assert.equal(await row.evaluate(el => el.getBoundingClientRect().top), top);
            const leg = row.locator('.btc-contract').last();
            await leg.locator('.btc-buy').click();
            assert.equal(await page.locator('#trade-ticket').getAttribute('data-btc'), await leg.getAttribute('data-symbol'));
            await page.evaluate(() => closeTicket());
          }
          assert.equal(await rows.first().locator('.btc-reference-mark').first().isVisible(), true);
          assert.equal(await rows.first().locator('.btc-reference-mark').first().textContent(), 'Mark 50,700*');
          assert.equal(await rows.first().locator('.btc-buy').first().isDisabled(), true);
          marketCenter = false;
          await page.getByLabel('Standard monthly expiry').selectOption(months[1]);
          await page.waitForFunction(exp => btcOptions.data.expiry === exp, months[1]);
          await rows.first().scrollIntoViewIfNeeded();
          const top = await rows.first().evaluate(el => el.getBoundingClientRect().top);
          marketCenter = true;
          await page.evaluate(() => btcOptions.refresh());
          assert.equal(await rows.first().evaluate(el => el.getBoundingClientRect().top), top);
        } finally {
          global.fetch = async (url, opts) => new URL(url).pathname === '/btc-options' ? Response.json(scenario.payload) : fixtureNetwork(url, opts);
        }
      }
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}x667: ${rects.filter(r=>r.complete).length} COMPLETE distinct strike rows, paired calls/puts, correct buy/owned close tickets, stale denial, empty legs, no overflow`);
    } finally { await context.close(); preview.close(); }
  }
} finally { await browser.close(); global.fetch = originalFetch; }
