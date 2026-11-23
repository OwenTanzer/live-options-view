import { setupBtcOwner, seedBtcOwnerSession } from './helpers/btc_owner_fixture.mjs';
// Opt-in live market data, local owner fixture and disposable paper account.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const interactive = process.argv.includes('--interactive');
const preview = createPreview();
setupBtcOwner(preview.apiEnv);
preview.apiEnv.BTC_OPTIONS_ENTRIES = '1';
const oldFetch = global.fetch;
assert.ok(process.env.BTC_USDC_DATA_ORIGIN, 'Set BTC_USDC_DATA_ORIGIN to the live quote origin for this test');
preview.apiEnv.BTC_USDC_DATA_ORIGIN = new URL(process.env.BTC_USDC_DATA_ORIGIN).origin;
global.fetch = async (url,options) => {
  const u=new URL(url);
  if(u.origin===preview.apiEnv.BTC_USDC_DATA_ORIGIN && u.pathname.startsWith('/api/v2/public/')) return oldFetch(url,options);
  if(u.pathname==='/btc-options') return Response.json({session:'open',contracts:[],quotes:[],expiries:[],subscribed_keys:[],entry_keys:[]});
  return fixtureNetwork(url,options);
};
let browser;
try {
  browser = await chromium.launch({ headless: !interactive });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
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
  await page.getByLabel('Bitcoin option product').selectOption('usdc');
  await page.waitForFunction(() => btcOptions.data?.contracts?.some(c=>c.product==='BTC_USDC'));
  if (interactive) {
    console.log('USDC live-data preview open. Paper balances are disposable. Close this browser to stop.');
    await page.waitForEvent('close', { timeout: 0 });
  } else {
    const key = await page.evaluate(() => {
      const d=btcOptions.data;
      const near=d.contracts.filter(c=>Math.abs(c.strike/d.center-1)<0.025 && d.display_prices[c.contract_key]?.bid>0 && d.display_prices[c.contract_key]?.ask>0);
      return near.sort((a,b)=>Math.abs(a.strike-d.center)-Math.abs(b.strike-d.center))[0]?.contract_key;
    });
    assert.ok(key,'near-money two-sided contract');
    const row=page.locator('.btc-contract').filter({has:page.locator('[data-btc-action="ticket"][data-symbol="'+key+'"]')});
    await row.locator('.btc-ticket').click();
    await page.waitForFunction(()=>document.querySelector('#trade-ticket .btn-buy')?.disabled===false);
    await page.locator('#trade-ticket .btn-buy').click();
    await page.waitForFunction(()=>session.trades.some(t=>t.side==='buy' && t.contract?.product==='BTC_USDC'));
    await row.locator('.btc-ticket').click();
    await page.waitForFunction(()=>document.querySelector('#trade-ticket .btn-sell')?.disabled===false);
    await page.locator('#trade-ticket .btn-sell').click();
    await page.waitForFunction(()=>session.trades.some(t=>t.side==='sell' && t.contract?.product==='BTC_USDC'));
    await page.reload();
    await page.waitForFunction(()=>typeof session!=='undefined' && session?.trades?.filter(t=>t.sym.startsWith('USDC:')).length===2);
    console.log('LIVE SOURCE PAPER RECEIPT',await page.evaluate(()=>session.trades.filter(t=>t.sym.startsWith('USDC:')).map(t=>({sym:t.sym,side:t.side,sizeBTC:t.multiplier,price:t.price,fees:t.fees,cash_delta:t.cash_delta,time:t.quote_ts,bid_size:t.source_quote.bid_size,ask_size:t.source_quote.ask_size}))));
    assert.deepEqual(errors,[]);
  }
} finally {await browser?.close();preview.close();global.fetch=oldFetch;}
