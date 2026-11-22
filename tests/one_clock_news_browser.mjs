// Run with the News reader alone or with ONE_CLOCK_DASHBOARD=1 in a paired dashboard checkout.
import assert from 'node:assert/strict';
import {existsSync, statSync} from 'node:fs';
import {createRequire} from 'node:module';
import {chromium} from 'playwright';
const require = createRequire(import.meta.url);
const fixture = require('./news_panel/qualify.js').loadQualified();
const paired = process.env.ONE_CLOCK_DASHBOARD === '1';
const origin = 'https://dashboard.test';
const browser = await chromium.launch({headless:true,
  ...(process.env.CHROME_BIN ? {executablePath:process.env.CHROME_BIN} : {channel:'chromium'})});
try {
  for (const width of [1440,390]) {
    const page = await browser.newPage({viewport:{width,height:844}});
    const keys = [];
    // Hold the first live index (even if the transport delivers it late). Every other
    // request is intercepted. Fixtures simulate the shape of a recorded publication.
    await page.addInitScript(() => {
      const transport = window.fetch;
      window.newsProbe = {held:false, aborted:false};
      window.fetch = function(url, options) {
        if (String(url).includes('news/v2/views/') && !window.newsProbe.held) {
          window.newsProbe.held = true;
          options.signal.addEventListener('abort', () => {window.newsProbe.aborted = true;});
          return new Promise(resolve => {window.releaseNews = index => resolve(new Response(JSON.stringify(index)));});
        }
        return transport.apply(this, arguments);
      };
    });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const at = url.pathname.indexOf('news/v2/views/');
      if (at >= 0) {
        const key = url.pathname.slice(at);
        keys.push(key);
        const view = structuredClone(fixture.byKey(key));
        if (!view) return route.fulfill({status:404,json:{}});
        if (view.rows && view.mode === 'archive') {
          view.data_label = 'PILOT news: prices unavailable (no price input connected)';
          for (const row of view.rows) if (row.title.includes('Tanker') && row.revision_number === 1)
            row.archived_at = '2026-11-09T10:06:00.006684Z';
        }
        return route.fulfill({json:view});
      }
      const path = 'site' + (url.pathname === '/' ? '/index.html' : url.pathname);
      if (url.origin === origin && existsSync(path) && statSync(path).isFile()) return route.fulfill({path});
      return route.fulfill({status:404,json:{}});
    });
    await page.goto(origin);
    if (paired) await page.waitForFunction(() => !!window.oneClockDashboard);
    await page.locator('#tab-news').click();
    await page.waitForFunction(() => window.newsProbe.held);
    await page.locator('#tab-history').click();
    await page.waitForFunction(() => window.newsProbe.aborted, null, {timeout:4000});
    await page.evaluate(async index => {
      window.releaseNews(index);
      await new Promise(requestAnimationFrame);
    }, fixture.index);
    assert.deepEqual(keys, [], 'a late hidden index must never open research or collector pages');
    if (paired) {
      await page.locator('#clock-mode').selectOption('replay');
      for (const [fraction, expected] of [['006',false],['007',true],['006',false]]) {
        await page.locator('#history-clock-date').fill('2026-11-09');
        await page.locator('#history-clock-time').fill('05:06:00.' + fraction);
        await page.locator('#history-clock-form button').click();
        const cutoff = Date.parse('2026-11-09T10:06:00.'+fraction+'Z');
        assert.equal(await page.evaluate(() => window.oneClockDashboard.snapshot().cutoff),cutoff);
        await page.locator('#tab-news').click();
        await page.waitForFunction(() => document.querySelector('#news-panel').textContent.includes('retained headlines'));
        const result = await page.locator('#news-panel').innerText();
        assert.equal(result.includes('Tanker'), expected, fraction);
        assert.ok(await page.locator('#news-research').isHidden());
        await page.locator('#tab-history').click();
      }
      assert.ok(keys.some(key => key.includes('/archive/')));
      assert.ok(keys.every(key => key.endsWith('/index.json') || key.includes('/archive/')), keys.join('\n'));
      const hiddenReadCount = keys.length;
      await page.locator('#clock-mode').selectOption('live');
      await page.evaluate(() => new Promise(requestAnimationFrame));
      assert.equal(keys.length, hiddenReadCount);
    }
    await page.locator('#tab-news').click();
    await page.waitForFunction(() => document.querySelector('#news-panel [data-state="ready"]'));
    assert.ok(keys.some(key => key.includes('/collector/')));
    console.log(width+'px hidden live index aborted, late result inert, visible live reader restored'+
      (paired ? ', archived receipt visible at .007 and absent at .006 after rewind' : ''));
    await page.close();
  }
} finally { await browser.close(); }
