// Run from the repo root with Playwright installed. Serve tracked fixture assets in-process.
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';

const origin = 'http://fixture.test';
const archiveCutoff = Date.parse('2026-09-25T15:42:14.321Z');
function link(instrument = 'QQQ', cutoff = archiveCutoff) {
  const params = new URLSearchParams({one_clock_session: '2026-09-25',
    one_clock_instrument: instrument, one_clock_at: String(cutoff)});
  return origin + '/order-flow.html?' + params;
}
const fixture = JSON.parse(await readFile(new URL('./fixtures/order_flow_live_20260925.json', import.meta.url)));
const provider = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const browser = await chromium.launch({headless: true, channel: 'chrome'});
try {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const errors = [], unexpected = [];
    let fixtureChain;
    await page.clock.setFixedTime(new Date(fixture.displayed_t));
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (fixtureChain && url.origin === provider) {
        const data = url.pathname === '/intraday/latest.json' ? fixtureChain :
          url.pathname === '/order-flow/qqq/v1/recent.json' ? fixture.feed : null;
        if (data) return route.fulfill({json: data});
      }
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
    for (const state of ['fresh', 'expired', 'missing', 'zero']) {
      fixtureChain = structuredClone(fixture.chain);
      if (state === 'expired') fixtureChain.timestamp = new Date(fixture.displayed_t - 120001).toISOString();
      if (state === 'missing' || state === 'zero') {
        fixtureChain.underlying_price = state === 'missing' ? null : 0;
        for (const row of fixtureChain.rows) row.UnderlyingPrice = fixtureChain.underlying_price;
      }
      await page.goto(origin + '/order-flow.html?source=live');
      await page.waitForFunction(() => document.querySelector('#fresh').textContent.includes('s old'));
      const rows = page.locator('#ladder tr[data-strike]');
      assert.equal(await rows.count(), 8, state);
      assert.match(await page.locator('#strike-range').textContent(),
        state === 'fresh' ? /Following.*QQQ \$744\.40/ : /Waiting for fresh spot/, state);
      await rows.first().click();
      const details = await page.locator('#details').textContent();
      assert.match(details, /recorded prints [1-9]/, state);
      if (state !== 'fresh') assert.match(details, /spot no coverage/, state);
      if (state === 'missing' || state === 'zero') assert.match(details, /gamma 0\.[0-9]+/, state);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    await page.close();
  }
  const history = await browser.newPage();
  await history.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (url.pathname.startsWith('/api/')) return route.fulfill({status: 404, json: {}});
    const file = url.pathname === '/' ? '/index.html' : url.pathname;
    if (!/^\/(?:[\w.-]+\/)*[\w.-]+\.(?:js|css|html)$/.test(file)) return route.abort();
    try {
      return await route.fulfill({path: fileURLToPath(new URL('../site' + file, import.meta.url))});
    } catch {
      return route.abort();
    }
  });
  await history.goto(link().replace('/order-flow.html?', '/?') + '#history-clock');
  const outgoing = history.locator('#history-order-flow-link');
  await outgoing.waitFor({state:'visible'});
  const submit = history.locator('#history-clock-form button[type=submit]');
  const instrument = history.locator('#history-clock-instrument');
  await instrument.fill('AAPL');
  await submit.click();
  await history.getByText('Chain archive is available for QQQ, SPY and IBIT. Other sources keep their own coverage.').waitFor();
  assert.ok(await outgoing.isHidden(), 'AAPL has no Order Flow archive');
  assert.equal(await outgoing.getAttribute('href'), null);
  await instrument.fill('QQQ');
  await submit.click();
  await outgoing.waitFor({state:'visible'});
  assert.equal(new URL(await outgoing.getAttribute('href'), origin).searchParams.get('one_clock_at'), String(archiveCutoff));
  await history.close();
  console.log('Order Flow shared cursor passed desktop and mobile archive and simulation checks.');
} finally {
  await browser.close();
}
