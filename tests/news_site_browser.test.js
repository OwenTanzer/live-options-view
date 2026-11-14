// Optional rendered check of the News tab in the full site page (MediaFlow M4b).
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_site_browser.test.js
//   NEWS_SITE_EVIDENCE=<dir>  also saves 1280 px and 390 px screenshots there.
// Loads the real site/index.html on both data bases:
//   proxy  the production origin, so BROWSER_DATA is /browser-data and every
//          /browser-data/ request runs through the real Worker (backend/index.js)
//          with its upstream fetch answered from the committed fixture views;
//   r2     another origin, so BROWSER_DATA is the public R2 origin, answered
//          from the same fixture views directly.
// All data is SYNTHETIC (tests/fixtures/news/views/). Every request must stay on
// the page origin or the R2 origin; nothing reaches the network. Only News
// requests are checked against the view-key rule; the shell's other data
// requests get 404s, which the shell already tolerates. Covers: the panel is
// hidden and makes no request before the tab opens; it renders on both bases;
// keys from a tampered index, picker and prev/next are refused and never
// requested; a 404 index shows "No news views published yet"; a 500 index
// and a missing page show failures; a malformed allowed page after a valid one
// (null coverage/window/rows, wrong schema) is a visible failure with no page
// error or stale ready state, then a valid page recovers (M4b.1); no
// horizontal scroll at either width, and
// at 390 px the coverage table stacks into cards instead of scrolling sideways.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let pw;
try { pw = require('playwright'); } catch (e) { pw = require('playwright-core'); }

const REPO = path.resolve(__dirname, '..');
const SITE = path.join(REPO, 'site');
const VIEWS = path.join(REPO, 'tests/fixtures/news/views');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const OTHER = 'http://news-site.test';
const R2 = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const EVIDENCE = process.env.NEWS_SITE_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const INDEX_KEY = 'news/v2/views/index.json';
// G3 (NOT RUN when written): the bucket holds the published form of the fixture (the fixed index plus
// one immutable generation), produced by the tests-only views.qualify_generation mirror.
const Q = require('./news_panel/qualify.js').loadQualified(VIEWS);
const FIRST = Q.q('news/v1/views/collector/all/2026-11-09/page-0001.json');  // index.views[0]
const OIL = Q.q('news/v1/views/collector/oil/2026-11-09/page-0001.json');
const BAD_FIRST = 'news/v1/views/collector/all/2026-02-30/page-0001.json';
const BAD_PICK = 'news/v1/views/collector/oil/2026-11-09/page-0021.json';
const BAD_NEXT = '../../../../auth/remember_token.json';
const panel = require(path.join(SITE, 'js/news-panel.js'));
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };

// What the R2 bucket holds for a scenario: fixture views, optionally tampered or failing.
let scenario = {};
function upstream(key) {
  if (key in scenario) return scenario[key]();
  const text = Q.text(key);
  if (text === null) return { status: 404, body: 'not found' };
  return { status: 200, body: text, type: 'application/json' };
}
const json = o => () => ({ status: 200, body: JSON.stringify(o), type: 'application/json' });
const status = n => () => ({ status: n, body: 'upstream ' + n });
const fixture = key => JSON.parse(Q.text(key));

(async () => {
  const { default: worker } = await import('../backend/index.js');
  const env = new Proxy({}, { get() { throw new Error('No bindings may be accessed'); } });
  const originalFetch = global.fetch;
  const upstreamCalls = [];
  global.fetch = async (url, options) => {
    const u = new URL(url);
    assert.equal(u.origin, R2, 'the Worker only fetches the public R2 origin');
    assert.equal(options.credentials, 'omit');
    const key = u.pathname.slice(1);
    upstreamCalls.push(key);
    const r = upstream(key);
    return new Response(r.body, { status: r.status, headers: r.type ? { 'Content-Type': r.type } : {} });
  };
  const browser = await pw.chromium.launch({ headless: true });
  const results = [];
  try {
    for (const [base, pageOrigin] of [['proxy', APP], ['r2', OTHER]]) {
      for (const [name, viewport] of Object.entries(VIEWPORTS)) {
        const label = `${base} ${name}`;
        const page = await browser.newPage({ viewport });
        const errors = [];
        const foreign = [];
        let news = [];  // every News view request the page made: { base, key, query }
        let researchReqs = [];  // R1: the research view follows the panel index (tested in news_research_site_browser)
        page.on('pageerror', e => errors.push(e.message));
        page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
        await page.route('**/*', async route => {
          const url = new URL(route.request().url());
          if (url.origin === pageOrigin && url.pathname.startsWith('/browser-data/')) {
            const key = url.pathname.slice('/browser-data/'.length);
            if (key.includes('research')) researchReqs.push({ base: 'proxy', key, query: url.search });
            else if (key.startsWith('news/')) news.push({ base: 'proxy', key, query: url.search });
            const res = await worker.fetch(new Request(url, { headers: { Origin: APP } }), env);
            return route.fulfill({ status: res.status, body: Buffer.from(await res.arrayBuffer()),
              headers: Object.fromEntries(res.headers) });
          }
          if (url.origin === R2) {
            const key = url.pathname.slice(1);
            if (key.includes('research')) researchReqs.push({ base: 'r2', key, query: url.search });
            else if (key.startsWith('news/')) news.push({ base: 'r2', key, query: url.search });
            const r = upstream(key);
            return route.fulfill({ status: r.status, body: r.body, contentType: r.type || 'text/plain',
              headers: { 'Access-Control-Allow-Origin': '*' } });
          }
          if (url.origin === pageOrigin) {
            const file = path.join(SITE, url.pathname === '/' ? 'index.html' : url.pathname);
            if (file.startsWith(SITE + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
              return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] || 'application/octet-stream' });
            }
            return route.fulfill({ status: 404, body: '{}', contentType: 'application/json' });
          }
          foreign.push(url.href);
          return route.abort();
        });
        const text = () => page.locator('#news-panel').innerText();
        const state = () => page.getAttribute('#news-panel', 'data-state');
        const waitState = async (want, why) => {
          await page.waitForFunction(w => {
            const s = document.querySelector('#news-panel').getAttribute('data-state');
            return s === w && !document.querySelector('#news-panel').hasAttribute('data-pending');
          }, want, { timeout: 10000 }).catch(async () => {
            throw new Error(`${label} ${why}: data-state ${await state()} (wanted ${want}): ${(await text()).slice(0, 300)}`);
          });
        };
        const noHorizontalScroll = async what => {
          const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
          assert.ok(sw <= iw, `${label} ${what}: page scrolls horizontally (${sw} > ${iw})`);
          if (viewport.width < 600) {
            // At phone width the coverage rows stack into cards: no table scrolls sideways inside its wrapper.
            const wide = await page.evaluate(() => [...document.querySelectorAll('#news-panel .np-table-wrap')]
              .filter(w => w.scrollWidth > w.clientWidth + 1).map(w => w.scrollWidth + '>' + w.clientWidth));
            assert.deepEqual(wide, [], `${label} ${what}: coverage table scrolls sideways at ${viewport.width} px`);
          }
        };
        const shot = async what => {
          if (!EVIDENCE) return;
          fs.mkdirSync(EVIDENCE, { recursive: true });
          await page.screenshot({ path: path.join(EVIDENCE, `${base}-${viewport.width}-${what}.png`), fullPage: true });
        };
        const openNews = async () => {
          news = [];
          researchReqs = [];
          await page.goto(pageOrigin + '/');
          await page.waitForSelector('#tab-news');
          assert.ok(await page.locator('#news-panel').isHidden(), `${label}: the News panel starts hidden`);
          await page.waitForTimeout(300);
          assert.deepEqual(news, [], `${label}: no News request before the tab opens`);
          await page.click('#tab-news');
          assert.ok(await page.locator('#news-panel').isVisible());
          assert.ok(await page.locator('#live-panel').isHidden(), `${label}: the live panel hides`);
        };
        const newsKeys = () => news.map(n => n.key);
        const checkNewsRequests = () => {
          for (const n of news) {
            assert.equal(n.base, base, `${label}: News request on the wrong base: ${n.key}`);
            assert.ok(panel.isNewsViewKey(n.key), `${label}: requested a key outside the view rule: ${n.key}`);
            assert.ok(/^\?_=[0-9]{1,20}$/.test(n.query), `${label}: query ${n.query}`);
          }
        };

        // 1. The fixture views render.
        scenario = {};
        await openNews();
        await waitState('ready', 'fixture');
        let t = await text();
        for (const s of ['SYNTHETIC fixture data', 'collector mode', 'stream all', 'Page 1 of 1']) {
          assert.ok(t.includes(s), `${label} fixture: ${s}`);
        }
        assert.deepEqual(newsKeys(), [INDEX_KEY, FIRST]);
        await noHorizontalScroll('fixture');
        await shot('1-fixture');
        await page.selectOption('#news-panel select[aria-label^="View"]', OIL);
        await page.waitForFunction(k => document.querySelector('#news-panel').getAttribute('data-page') === k, OIL);
        t = await text();
        for (const s of ['collector mode', 'stream oil', 'missed interval 45 min', 'Later context (not known at event)']) {
          assert.ok(t.includes(s), `${label} collector/oil: ${s}`);
        }
        assert.deepEqual(newsKeys(), [INDEX_KEY, FIRST, OIL]);
        await noHorizontalScroll('collector-oil');
        await shot('2-collector-oil');
        // Leaving and reopening the tab does not refetch.
        await page.click('#tab-live');
        await page.click('#tab-news');
        await page.waitForTimeout(200);
        assert.equal(news.length, 3, `${label}: reopening the tab does not mount again`);
        checkNewsRequests();

        // 2. Rejected keys from the index, the picker and prev/next are never requested. G3: an index
        // whose first_page (picker option) is outside its generation is refused whole at mount; a next
        // link that leaves the generation asks for a refresh instead of being followed.
        for (const [what, bad] of [['first_page', BAD_FIRST], ['picker option', BAD_PICK]]) {
          const idx = fixture(INDEX_KEY);
          idx.views[what === 'first_page' ? 0 : 1].first_page = bad;
          scenario = { [INDEX_KEY]: json(idx) };
          await openNews();
          await waitState('failed', 'tampered ' + what);
          t = await text();
          assert.ok(t.includes('Could not load news views: ') && t.includes('is not in generation ' + Q.gen),
            `${label} ${what}: ${t.slice(0, 200)}`);
          assert.deepEqual(newsKeys(), [INDEX_KEY]);
        }
        await noHorizontalScroll('refused-first-page');
        await shot('3-refused-first-page');

        const tamperedPage = fixture(FIRST);
        tamperedPage.next_page = BAD_NEXT;
        scenario = { [FIRST]: json(tamperedPage) };
        await openNews();
        await waitState('ready', 'tampered next');
        await page.click('#news-panel button:has-text("older →")');
        await waitState('refresh-required', 'next');
        t = await text();
        assert.ok(t.includes('left generation ' + Q.gen) && t.includes('Reload'), `${label}: next refused with a refresh`);
        assert.deepEqual(newsKeys(), [INDEX_KEY, FIRST], `${label}: refused keys were not requested`);
        await noHorizontalScroll('refused-next');
        await shot('4-refused-next');
        checkNewsRequests();

        // 2b. M4b.1: valid page -> malformed allowed next page -> valid page. Each malformed page is a
        // visible failure with no page error, no stale ready state or page name, and the panel recovers.
        const NEXT = FIRST.replace('page-0001', 'page-0002');
        const first = fixture(FIRST);
        Object.assign(first, { next_page: NEXT, page_count: 2 });
        const malformed = {
          'coverage-null': p => { p.coverage = null; },
          'window-null': p => { p.window = null; },
          'rows-null': p => { p.rows = null; },
          'wrong-schema': p => { p.schema_version = 'news-view/v0'; },
        };
        scenario = { [FIRST]: json(first) };
        await openNews();
        await waitState('ready', 'before malformed');
        for (const [what, mutate] of Object.entries(malformed)) {
          const bad = fixture(FIRST);
          Object.assign(bad, { page_number: 2, page_count: 2, prev_page: FIRST, next_page: null });
          mutate(bad);
          scenario[NEXT] = json(bad);
          await page.click('#news-panel button:has-text("older →")');
          await waitState('failed', what);
          assert.equal(await page.getAttribute('#news-panel', 'data-page'), null, `${label} ${what}: stale data-page`);
          const failure = page.locator('#news-panel .np-incomplete');
          assert.ok(await failure.isVisible(), `${label} ${what}: failure is visible`);
          t = await failure.innerText();
          assert.ok(t.includes(`Could not load "${NEXT}": not a valid news view page (`), `${label} ${what}: ${t}`);
          assert.equal(await page.locator('#news-panel .np-head').count(), 0, `${label} ${what}: old page left on screen`);
          await noHorizontalScroll('malformed-' + what);
          if (what === 'coverage-null') await shot('8-malformed-coverage-null');
          await page.selectOption('#news-panel select[aria-label^="View"]', FIRST);
          await waitState('ready', what + ' recovery');
          assert.equal(await page.getAttribute('#news-panel', 'data-page'), FIRST);
          assert.equal(await page.locator('#news-panel .np-incomplete').count(), 0, `${label} ${what}: failure cleared`);
          if (what === 'wrong-schema') await shot('9-recovered');
        }
        assert.deepEqual(newsKeys().filter(k => k === NEXT).length, 4, `${label}: each malformed page requested once`);
        assert.deepEqual(errors, [], `${label}: no page error from a malformed page`);
        checkNewsRequests();

        // 3. Absence and failure are distinct.
        scenario = { [INDEX_KEY]: status(404) };
        await openNews();
        await waitState('absent', 'index 404');
        t = await text();
        assert.ok(t.includes('No news views published yet') && !t.includes('Could not load'), `${label} absent: ${t}`);
        await shot('5-not-published');

        scenario = { [INDEX_KEY]: status(500) };
        await openNews();
        await waitState('failed', 'index 500');
        t = await text();
        assert.ok(t.includes('Could not load news views: ') && t.includes('HTTP 500'), `${label} 500: ${t}`);
        assert.ok(!t.includes('No news views published yet'));
        await shot('6-index-500');

        scenario = { [FIRST]: status(404) };
        await openNews();
        await waitState('refresh-required', 'missing page');   // G3: a retired generation's page
        t = await text();
        assert.ok(t.includes('HTTP 404') && t.includes('Reload'), `${label} missing page: ${t}`);
        assert.ok(!t.includes('No news views published yet'));
        await noHorizontalScroll('missing-page');
        await shot('7-missing-page');
        checkNewsRequests();

        // R1: these fixture indexes carry no research entry, so the research view requests nothing (and the
        // retired news/v2/research/view.json key is never requested).
        assert.deepEqual(researchReqs, [], `${label}: no research request without a research entry in the index`);
        assert.deepEqual(foreign, [], `${label}: requests left the page and R2 origins`);
        assert.deepEqual(errors, [], `${label}: page errors`);
        results.push(label);
        console.log(`ok - ${label} ${viewport.width}x${viewport.height}`);
        await page.close();
      }
    }
    // The proxy only ever fetched view keys from R2 for News; nothing else reached the bucket for a refused key.
    for (const key of upstreamCalls.filter(k => k.startsWith('news/'))) assert.ok(panel.isNewsViewKey(key), key);
    for (const key of [BAD_FIRST, BAD_PICK]) assert.ok(!upstreamCalls.includes(key));
  } finally {
    global.fetch = originalFetch;
    await browser.close();
  }
  assert.equal(results.length, 4);
  console.log('news site browser test passed' + (EVIDENCE ? '; screenshots in ' + EVIDENCE : ''));
})().catch(e => { console.error(e); process.exit(1); });
