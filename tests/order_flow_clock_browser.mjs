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
const browser = await chromium.launch({headless: true, ...(process.env.CHROME_BIN ? {executablePath:process.env.CHROME_BIN} : {channel:'chromium'})});
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
      const allowed = /^\/(?:order-flow\.html|order-flow-(?:replay(?:-strikes)?|capture-20261123)\.json|shared\/(?:order-flow|one-clock)[a-z-]*\.js)$/;
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
    // Read each optional view on a short screen. A wheel should move the panel
    // when there is more content, then continue down the page at its boundary.
    await page.setViewportSize({width,height:600});
    const panel = page.locator('#optional-tape #view');
    for (const id of ['sales','chart','footprint','ladder','events']) {
      await page.locator('[data-view="'+id+'"]').click();
      await page.locator('#optional-tape #view[data-view="'+id+'"]').waitFor();
      await panel.focus();
      const before = await panel.evaluate(el=>({overflowY:el.scrollHeight>el.clientHeight+2,
        overflowX:el.scrollWidth>el.clientWidth+2}));
      if (width===390 && id==='sales') assert.ok(before.overflowX,id);
      if (['chart','ladder'].includes(id)) assert.equal(before.overflowX,false);
      if (before.overflowY) {
        if (width===390) await page.keyboard.press('PageDown');
        else {
          await panel.hover();
          await page.mouse.wheel(0,160);
        }
        await page.waitForFunction(()=>document.querySelector('#optional-tape').shadowRoot.querySelector('#view').scrollTop>0);
      }
      if (before.overflowX) {
        await page.keyboard.press('ArrowRight');
        await page.waitForFunction(()=>document.querySelector('#optional-tape').shadowRoot.querySelector('#view').scrollLeft>0);
      }
      if (width===1440) {
        await panel.evaluate(el=>el.scrollTop=el.scrollHeight);
        await panel.hover();
        const pageTop = await page.evaluate(()=>scrollY);
        await page.mouse.wheel(0,300);
        // A wheel gesture can remain latched to the inner scroller on its final frame.
        if (await page.evaluate(top=>scrollY<=top,pageTop)) {
          await page.waitForTimeout(180);
          await page.mouse.wheel(0,300);
        }
        await page.waitForFunction(top=>scrollY>top,pageTop);
      }
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),id);
    }
    await page.locator('[data-view="classic"]').click();
    await page.setViewportSize({width,height:900});
    await page.locator('#seek').fill('350');
    await page.waitForFunction(previous => document.querySelector('#flow-history').getAttribute('href') !== previous, first);
    // A wheel over B must change the actual price axis at the reviewer's replay cursor.
    await page.goto(link('QQQ',1790343060000)+'&tape=chart');
    const range = page.locator('#optional-tape #chart-strikes');
    await range.getByText('Strikes $740–$744 of $709–$775',{exact:true}).waitFor();
    const graph = page.locator('#optional-tape #cv');
    await graph.hover();
    const pageY = await page.evaluate(()=>scrollY);
    await page.mouse.wheel(0,240);
    await range.getByText('Strikes $737–$741 of $709–$775',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>scrollY),pageY);
    await panel.evaluate(el=>el.scrollTop=el.scrollHeight);
    const geometry=await panel.evaluate(el=>{
      const cv=el.querySelector('canvas').getBoundingClientRect(), viewport=el.getBoundingClientRect();
      return [el.scrollWidth-el.clientWidth,el.scrollHeight-el.clientHeight,cv.top-viewport.top,cv.height-el.clientHeight];
    });
    assert.deepEqual(geometry,[0,4960,0,0]);
    await range.getByText('Strikes $709–$713 of $709–$775',{exact:true}).waitFor();
    assert.ok(await page.getByRole('button',{name:'Chain only · Individual trades unavailable at these strikes'}).isDisabled());
    await panel.evaluate(el=>el.scrollTop=31*80);
    await page.getByRole('button',{name:'Inspect 2564 prints at these strikes · 37 tickets ≥ $10k'}).waitFor();
    await page.mouse.wheel(0,-240);
    await range.getByText('Strikes $743–$747 of $709–$775',{exact:true}).waitFor();
    // Browse the complete archived chain in both the footprint and ladder.
    await page.locator('[data-view="footprint"]').click();
    const headers=panel.locator('thead th');
    await headers.nth(67).waitFor({state:'attached'});
    assert.equal(await headers.nth(1).textContent(),'775');
    assert.equal(await headers.last().textContent(),'709');
    await panel.evaluate(el=>el.scrollLeft=el.scrollWidth);
    const edges=await panel.evaluate(el=>{
      const heads=el.querySelectorAll('th'), rect=el.getBoundingClientRect();
      return [Math.abs(heads[0].getBoundingClientRect().left-rect.left),
        heads[heads.length-1].getBoundingClientRect().right-rect.left-el.clientWidth];
    });
    assert.ok(edges.every(n=>Math.abs(n)<=2));
    assert.equal(await panel.locator('tbody tr').first().locator('td').last().getAttribute('title'),'Individual trades unavailable');
    await page.locator('[data-view="ladder"]').click();
    const levels=panel.locator('.lrow');
    await levels.nth(66).waitFor({state:'attached'});
    assert.equal(await levels.first().locator('.k').textContent(),'775');
    assert.equal(await levels.last().locator('.k').textContent(),'709');
    await panel.evaluate(el=>el.scrollTop=el.scrollHeight);
    assert.equal(await levels.last().locator('.unrecorded').count(),2);
    assert.ok(await panel.locator('.ladder-head').evaluate(el=>Math.abs(el.getBoundingClientRect().top-el.parentElement.getBoundingClientRect().top)<=1));
    await page.locator('[data-view="sales"]').click();
    await page.locator('#optional-tape .controls input').check();
    await panel.locator('[data-record]').nth(3322).waitFor({state:'attached'});
    assert.equal(await panel.locator('[data-record]').count(),3323);
    await page.locator('[data-view="events"]').click();
    await panel.locator('.ev[data-i]').first().waitFor();
    const eventPrints=await panel.evaluate(el=>{
      const groups=[...el.querySelectorAll('.ev[data-i]')];
      for(const group of groups) group.click();
      return [...el.querySelectorAll('.ev .list>div')].map(row=>row.textContent.match(/archive row (\d+)/)?.[1]);
    });
    assert.equal(eventPrints.length,3323);
    assert.equal(new Set(eventPrints).size,3323);
    await page.goto(origin+'/order-flow.html?source=capture&tape=chart');
    await statusContains('887 prints in view');
    await range.getByText('Strikes $649–$653 of $500–$900',{exact:true}).waitFor();
    const chartViewport=page.locator('#optional-tape #view');
    await chartViewport.evaluate(el=>el.scrollTop-=240);
    await range.getByText('Strikes $652–$656 of $500–$900',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Inspect 254 prints at these strikes · 3 tickets ≥ $10k'}).click();
    const inspected=[];
    for (;;) {
      inspected.push(...await page.locator('#print-inspector li').allTextContents());
      if (await page.locator('#inspect-next').isDisabled()) break;
      await page.locator('#inspect-next').click();
    }
    assert.equal(inspected.length,254);
    for (const sequence of [20949,20951]) assert.ok(inspected.some(row=>row.includes('source sequence '+sequence)));
    await page.locator('#inspect-close').click();
    await chartViewport.focus();
    await page.keyboard.press('End');
    await range.getByText('Strikes $500–$520 of $500–$900',{exact:true}).waitFor();
    await page.keyboard.press('Home');
    await range.getByText('Strikes $880–$900 of $500–$900',{exact:true}).waitFor();
    await page.locator('[data-view="footprint"]').click();
    const strikeColumns=page.locator('#optional-tape .grid thead th');
    await strikeColumns.nth(201).waitFor({state:'attached'});
    assert.equal(await strikeColumns.first().textContent(),'ET');
    assert.equal(await strikeColumns.last().textContent(),'500');
    await page.locator('[data-view="ladder"]').click();
    await page.locator('#optional-tape .lrow').nth(200).waitFor({state:'attached'});
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
    // Two otherwise identical tickets and two identical small prints must survive every view.
    const sample = {...fixture.feed.rows[0], type:'P', t:fixture.displayed_t, price:1};
    fixture.feed.cap = 4;
    fixture.feed.rows = [1,2,3,4].map(seq => ({...sample, seq, size:seq <= 2 ? 100 : 1}));
    fixtureChain = structuredClone(fixture.chain);
    await page.goto(origin + '/order-flow.html?source=live');
    await statusContains('4 prints in view');
    const picker = page.locator('#tape-view'), optional = page.locator('#optional-tape');
    const choose = id => picker.locator('[data-view="'+id+'"]').click();
    assert.equal(await picker.locator('[aria-pressed=true]').getAttribute('data-view'), 'classic');
    await choose('sales');
    await optional.locator('.coverage-cap').waitFor();
    assert.match(await optional.locator('.coverage-cap').textContent(), /Covers only from .*display cap/);
    await optional.locator('.run').click();
    assert.match(await optional.locator('.inner').textContent(), /sequence 4.*sequence 3/);
    await optional.getByLabel('All prints').check();
    await optional.locator('.row[data-record]').nth(3).waitFor();
    assert.equal(await optional.locator('.row[data-record]').count(),4);
    assert.equal(await optional.locator('.row.big').count(),2);
    await choose('chart');
    await optional.getByRole('button',{name:'Inspect chart prints (4)'}).click();
    assert.equal(await page.locator('#print-inspector li').count(),4);
    await page.locator('#inspect-close').click();
    await choose('footprint');
    await optional.locator('td.hasbig').click();
    assert.equal(await optional.locator('#detail .list div').count(),4);
    await choose('ladder');
    const puts = optional.locator('[data-k="745"][data-t="P"]');
    await puts.click();
    assert.equal(await optional.locator('#detail .list div').count(),4);
    await optional.locator('#dslider').fill('30000');
    assert.match(await optional.locator('#now').textContent(),/0 prints/);
    assert.equal(await optional.locator('.chip').count(),0);
    await optional.locator('#dlive').click();
    assert.match(await optional.locator('#now').textContent(),/4 prints/);
    await choose('events');
    await optional.locator('.ev[data-i]').first().click();
    assert.equal(await optional.locator('.ev .list div').count(),4);
    assert.match(await optional.locator('.ev .list').textContent(),/sequence 4.*sequence 3.*sequence 2.*sequence 1/);
    await page.locator('#filter').selectOption('Calls');
    await statusContains('0 prints in view');
    assert.equal(await optional.locator('.ev[data-i]').count(),0);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
    await choose('classic');
    assert.ok(await page.locator('#classic-tape').isVisible());
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
