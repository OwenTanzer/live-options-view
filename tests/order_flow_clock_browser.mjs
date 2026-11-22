// Run from the repo root with Playwright installed. Serve tracked fixture assets in-process.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';

const origin = 'http://fixture.test';
const archiveCutoff = Date.parse('2026-09-25T15:42:14.321Z');
function link(instrument = 'QQQ', cutoff = archiveCutoff) {
  const params = new URLSearchParams({one_clock_session: '2026-09-25',
    one_clock_instrument: instrument, one_clock_at: String(cutoff)});
  return origin + '/order-flow.html?' + params;
}
const browser = await chromium.launch({headless: true, ...(process.env.CHROME_BIN ? {executablePath:process.env.CHROME_BIN} : {channel:'chromium'})});
try {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const errors = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const allowed = /^\/(?:order-flow\.html|order-flow-replay\.json|shared\/(?:order-flow|one-clock)[a-z-]*\.js)$/;
      if (url.origin !== origin || !allowed.test(url.pathname)) {
        unexpected.push(url.href);
        return route.abort();
      }
      const contentType = url.pathname.endsWith('.html') ? 'text/html' :
        url.pathname.endsWith('.json') ? 'application/json' : 'text/javascript';
      const body = await readFile(new URL('../site' + url.pathname, import.meta.url));
      await route.fulfill({body, contentType});
    });
    const statusContains = text => page.waitForFunction(
      expected => document.querySelector('#state').textContent.includes(expected), text);
    await page.goto(link());
    await statusContains('Reconstructed archive');
    assert.ok(await page.locator('#ladder tr').count() > 0);
    assert.equal(await page.locator('#play').textContent(), 'Play');
    const chain = page.locator('#flow-history');
    const first = await chain.getAttribute('href');
    assert.equal(new URL(first, origin).searchParams.get('one_clock_at'), String(archiveCutoff));
    await page.locator('#seek').fill('350');
    await page.waitForFunction(previous => document.querySelector('#flow-history').getAttribute('href') !== previous, first);
    for (const [url, message] of [
      [link('SPY'), 'unavailable at this cursor'],
      [link('QQQ', Date.parse('2026-09-25T01:00:00-04:00')), 'Outside archive coverage'],
      [link() + '&one_clock_at=bad', 'Invalid One Clock cursor']
    ]) {
      await page.goto(url);
      await statusContains(message);
      assert.ok(await chain.isHidden());
      assert.ok(await page.locator('#play').isDisabled());
    }
    await page.goto(origin + '/order-flow.html?source=simulation');
    await statusContains('Simulated market');
    await page.locator('#sim-jump').click();
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    await page.close();
  }
  console.log('Order Flow shared cursor passed desktop and mobile archive and simulation checks.');
} finally {
  await browser.close();
}
