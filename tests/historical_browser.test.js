// Optional rendered integration: NODE_PATH=<playwright installation> node tests/historical_browser.test.js
// Synthetic public archives; no production requests, authentication or trading.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const APP = 'https://live-options-view.otmooper12.workers.dev';
(async () => {
  const { default: worker } = await import('../backend/index.js');
  const browser = await chromium.launch({headless:true, channel:'chromium'});
  const originalFetch = global.fetch;
  const dates = ['2026-10-26','2026-10-27'];
  const archives = {'manifest.json':JSON.stringify({dates})};
  for (const date of dates) {
    const key = date.replaceAll('-','');
    archives[`raw/qqq_chain_${key}.csv`] = 'Expiration,Type,Strike,UnderlyingPrice,OpenInterest,Volume,DTE\n' + ['call','put'].flatMap(type => [499,500,501].map(strike=>`${date},${type},${strike},500,1000,20,0`)).join('\n');
    archives[`prices/qqq_intraday_${key}.csv`] = `datetime,open,high,low,close\n${date}T13:30:00Z,500,502,499,501\n${date}T13:35:00Z,501,503,500,502`;
  }
  global.fetch = async url => {
    const key = new URL(url).pathname.slice(1);
    return new Response(archives[key] || '',{status: key in archives ? 200:404});
  };
  try {
    const page = await browser.newPage();
    await page.clock.setFixedTime(new Date('2026-11-08T12:00:00Z'));
    const staleWarning = ' \u2014 archive stale: newest date 2026-10-27 is 8 trading days old';
    const errors = [];
    page.on('pageerror',e=>errors.push(e.message));
    const requests = [];
    await page.route('**/*',async route => {
      const url = new URL(route.request().url());
      requests.push(url.href);
      if(url.origin === APP && url.pathname.startsWith('/browser-data/')) {
        const response = await worker.fetch(new Request(url),new Proxy({}, {get(){throw Error('binding access');}}));
        return route.fulfill({status:response.status,body:Buffer.from(await response.arrayBuffer()),headers:Object.fromEntries(response.headers)});
      }
      if(url.origin === APP) {
        const path = 'site' + (url.pathname === '/' ? '/index.html':url.pathname);
        if(fs.existsSync(path) && fs.statSync(path).isFile()) return route.fulfill({path});
      }
      return route.fulfill({status:404,body:'{}',contentType:'application/json'});
    });
    await page.goto(APP);
    await page.locator('#tab-history').click();
    await page.waitForFunction(expected => document.querySelector('#hist-status').textContent === expected,
      'Trade date 2026-10-27' + staleWarning);
    assert.equal(await page.locator('#hist-date option').count(),2);
    assert.ok(await page.locator('#hist-heatmap-wrap').isVisible());
    assert.ok(await page.locator('#hist-sidebar').isVisible());
    await page.waitForFunction(()=>document.querySelectorAll('.similar-row').length===1);
    assert.equal(await page.locator('#widget-intraday-label').textContent(),'2026-10-27');
    const sessionButton = page.locator('#hist-strip button[data-date="2026-10-26"]');
    await sessionButton.focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#hist-date').value === '2026-10-26');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-date')), '2026-10-26',
      'keyboard selection retains focus on the archived session button');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-date')), '2026-10-27');
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.querySelector('#hist-strip-wrap').getBoundingClientRect().right <= innerWidth),
      'session navigation fits a 390px viewport');
    if (process.env.HIST_EVIDENCE) await page.screenshot({path:process.env.HIST_EVIDENCE,fullPage:true});
    await page.locator('#hist-date').selectOption('2026-10-26');
    await page.waitForFunction(expected => document.querySelector('#hist-status').textContent === expected,
      'Trade date 2026-10-26' + staleWarning);
    assert.equal(await page.locator('#widget-intraday-label').textContent(),'2026-10-26');
    assert.ok(requests.some(u=>u.includes('/browser-data/prices/qqq_intraday_')));
    assert.ok(!requests.some(u=>/r2\.dev\/(manifest|raw|prices)/.test(u)));
    assert.deepEqual(errors,[]);

    archives['manifest.json']='{"dates":[]}';
    await page.reload(); await page.locator('#tab-history').click();
    await page.waitForFunction(()=>document.querySelector('#hist-status').textContent === 'Archive is empty.');
    assert.ok(await page.locator('#hist-date').isDisabled());
    assert.ok(!await page.locator('#hist-sidebar').isVisible());
    archives['manifest.json']='bad JSON';
    await page.reload(); await page.locator('#tab-history').click();
    await page.waitForFunction(()=>document.querySelector('#hist-status').textContent.startsWith('Failed to load archive index:'));
    assert.equal(await page.locator('#hist-date option').count(),0);
    archives['manifest.json']=JSON.stringify({dates});
    await page.locator('#tab-live').click(); await page.locator('#tab-history').click();
    await page.waitForFunction(expected => document.querySelector('#hist-status').textContent === expected,
      'Trade date 2026-10-27' + staleWarning);
    console.log('Chromium: Historical dates, chain, sidebar/similarity, date switch, empty/malformed manifest and retry passed (synthetic production-origin routing, not deployed acceptance)');
  } finally { global.fetch = originalFetch; await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});
