// Optional rendered check of the tests-only News panel harness (M4a; since M4b it
// loads site/js/news-panel.js and the panel CSS block from site/index.html).
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_panel_browser.test.js
//   NEWS_PANEL_EVIDENCE=<dir>  also saves desktop and narrow-width screenshots there.
// Views are real builder output (python tests/test_news_views.py --write-harness DIR):
// m3/ is the committed fixture, quiet/ has covered quiet spans, delayed/ has a row event inside a
// span with no new observations, edge/ overflows its limits and carries a javascript: link and
// markup in a title, truncated/ is an index cut before its first entry. All SYNTHETIC. Every request is served
// from the repository or that directory; anything else is aborted and fails the test.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let pw;
try { pw = require('playwright'); } catch (e) { pw = require('playwright-core'); }

const REPO = path.resolve(__dirname, '..');
const ORIGIN = 'http://news-harness.test';
const EVIDENCE = process.env.NEWS_PANEL_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' };

(async () => {
  const built = fs.mkdtempSync(path.join(os.tmpdir(), 'news-panel-built-'));
  execFileSync(process.env.PYTHON || 'python', [path.join(REPO, 'tests/test_news_views.py'), '--write-harness', built],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
  // G3 (NOT RUN when written): the panel reads published generations, so each builder set is qualified
  // (tests/news_panel/qualify.js, the views.qualify_generation mirror) before it is served.
  const { qualifyFiles, listKeys } = require('./news_panel/qualify.js');
  const views = fs.mkdtempSync(path.join(os.tmpdir(), 'news-panel-views-'));
  const Qs = {};
  for (const set of fs.readdirSync(built)) {
    const dir = path.join(built, set);
    const raw = {};
    for (const key of listKeys(dir)) raw[key] = fs.readFileSync(path.join(dir, key));
    if (!raw['news/v1/views/index.json']) continue;
    const { files, q } = qualifyFiles(raw);
    Qs[set] = q;
    for (const [key, obj] of Object.entries(files)) {
      const file = path.join(views, set, key);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, JSON.stringify(obj));
    }
  }
  let current = null;
  const browser = await pw.chromium.launch({ headless: true });
  const foreign = [];
  try {
    for (const [name, viewport] of Object.entries(VIEWPORTS)) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== ORIGIN) { foreign.push(url.href); return route.abort(); }
        let file = null;
        if (url.pathname.startsWith('/tests/news_panel/')) file = path.join(REPO, url.pathname);
        // The harness loads the site's only panel copy and copies its CSS block from index.html.
        else if (url.pathname === '/site/js/news-panel.js' || url.pathname === '/site/index.html') {
          file = path.join(REPO, url.pathname);
        }
        else if (url.pathname.startsWith('/views/')) file = path.join(views, url.pathname.slice('/views/'.length));
        if (!file || !file.startsWith(REPO) && !file.startsWith(views) || !fs.existsSync(file)) {
          return route.fulfill({ status: 404, body: 'not found' });
        }
        return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] || 'application/octet-stream' });
      });
      const open = async set => {
        current = set;
        await page.goto(`${ORIGIN}/tests/news_panel/harness.html?root=/views/${set}/`);
        await page.evaluate(() => window.newsPanelReady);
      };
      const pick = async unqualified => {
        const key = unqualified.startsWith('news/v2/views/p/') ? unqualified : Qs[current](unqualified);
        await page.selectOption('select[aria-label^="View"]', key);
        await page.waitForFunction(k => document.querySelector('#news-panel').getAttribute('data-page') === k, key);
      };
      const noHorizontalScroll = async label => {
        const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
        assert.ok(sw <= iw, `${name} ${label}: page scrolls horizontally (${sw} > ${iw})`);
      };
      const shot = async label => {
        if (EVIDENCE) {
          fs.mkdirSync(EVIDENCE, { recursive: true });
          await page.screenshot({ path: path.join(EVIDENCE, `${name}-${label}.png`), fullPage: true });
        }
      };
      const text = () => page.locator('#news-panel').innerText();

      // m3: the committed fixture, collector/oil.
      await open('m3');
      await pick('news/v1/views/collector/oil/2026-11-09/page-0001.json');
      let t = await text();
      for (const s of ['SYNTHETIC fixture data', 'collector mode', 'instrument: unmapped', 'missed interval 45 min',
        'Later context (not known at event)', 'Since the last observed run']) assert.ok(t.includes(s), `${name} m3: ${s}`);
      assert.ok(!t.includes('Observed, no items'), `${name} m3 calls nothing quiet`);
      assert.ok(await page.locator('.np-price-stale').count() > 0);
      assert.equal(await page.locator('.np-incomplete').count(), 0);
      await noHorizontalScroll('m3');
      await shot('m3-collector-oil');
      await page.selectOption('select[aria-label^="Source filter"]', 'eia_today_in_energy');
      t = await text();
      assert.ok(/\d+ of \d+ rows on this page/.test(t) && t.includes('Coverage above stays for the whole stream'));
      await shot('m3-collector-oil-filtered');

      await pick('news/v1/views/publication/all/2026-11-09/page-0001.json');
      t = await text();
      assert.ok(t.includes('RETROSPECTIVE (hindsight)'));
      assert.ok(t.includes('classified 10:12:00 (retrospective): ai_buildout, oil'), `${name} retrospective classification`);
      assert.ok(!/streams at event: (?!not known)/.test(t), `${name} publication rows claim no streams at event`);
      await noHorizontalScroll('m3 publication');
      await shot('m3-publication-all');

      // quiet: covered spans with a pre-pagination zero count.
      await open('quiet');
      await pick('news/v1/views/collector/oil/2026-11-09/page-0001.json');
      assert.equal(await page.locator('tr[data-quiet="true"]').count(), 2, `${name} quiet spans`);
      assert.equal(((await text()).match(/Observed, no items/g) || []).length, 2);
      await pick('news/v1/views/collector/ai_buildout/2026-11-09/page-0001.json');
      assert.equal(await page.locator('tr[data-quiet="true"]').count(), 0, 'a partial scope is never quiet');
      await pick('news/v1/views/publication/oil/2026-11-09/page-0001.json');
      assert.equal(await page.locator('tr[data-quiet="true"]').count(), 0, 'hindsight is never quiet');
      await pick('news/v1/views/collector/oil/2026-11-09/page-0001.json');
      await noHorizontalScroll('quiet');
      await shot('quiet-collector-oil');

      // delayed: archive mode, a row event at 10:12 in a span with no new observations.
      await open('delayed');
      await pick('news/v1/views/archive/oil/2026-11-09/page-0001.json');
      const segs = await page.locator('tr.np-seg').evaluateAll(rs => rs.map(r => [r.getAttribute('data-quiet'), r.innerText]));
      assert.deepEqual(segs.map(s => s[0]), ['false', 'true', 'false'], `${name} delayed quiet flags`);
      assert.ok(segs[2][1].includes('0 new observed · 1 events'), `${name} delayed counts`);
      assert.ok((await text()).includes('event 2026-11-09 10:12:00Z'), `${name} delayed event row`);
      await noHorizontalScroll('delayed');
      await shot('delayed-archive-oil');

      // truncated: an index with no entries is still visibly incomplete.
      await open('truncated');
      const ib = page.locator('.np-incomplete[role="alert"]');
      assert.equal(await ib.count(), 1, `${name} truncated banner`);
      assert.ok((await ib.innerText()).includes('9 of 9 views omitted (index bytes)'));
      assert.ok((await text()).includes('No view is listed: the index was cut before its first entry'));
      await noHorizontalScroll('truncated');
      await shot('truncated-index');

      // edge: overflow, a rejected link and markup in a title.
      await open('edge');
      await pick('news/v1/views/collector/all/2026-11-09/page-0001.json');
      const banner = page.locator('.np-incomplete[role="alert"]');
      assert.equal(await banner.count(), 1);
      const b = await banner.innerText();
      for (const s of ['INCOMPLETE', 'omitted coverage is not a quiet interval', 'rows beyond max pages',
        'segments omitted: 3', 'rows omitted: 2', 'pages omitted: 2']) assert.ok(b.includes(s), `${name} banner: ${s}`);
      assert.ok((await text()).includes('Page 1 of 2'));
      t = await text();
      assert.ok(t.includes('<img src=x onerror=alert(1)>'), 'markup renders as text');
      assert.ok(t.includes('link rejected: scheme not http'));
      assert.equal(await page.locator('#news-panel img').count(), 0);
      assert.equal(await page.locator('a[href^="javascript:"]').count(), 0);
      await noHorizontalScroll('edge');
      await shot('edge-page1');
      await page.getByRole('button', { name: 'older →' }).click();
      await page.waitForFunction(() => /page-0002\.json$/.test(document.querySelector('#news-panel').getAttribute('data-page')));
      assert.ok((await text()).includes('Page 2 of 2'));
      for (const rel of await page.locator('#news-panel a').evaluateAll(as => as.map(a => a.rel))) {
        assert.equal(rel, 'noopener noreferrer');
      }
      assert.ok(await page.getByRole('button', { name: 'older →' }).isDisabled());
      await noHorizontalScroll('edge page 2');
      await shot('edge-page2');

      assert.deepEqual(errors, [], `${name}: page errors`);
      await page.close();
      console.log(`ok - ${name} ${viewport.width}x${viewport.height}`);
    }
    assert.deepEqual(foreign, [], 'no request left the harness origin');
  } finally {
    await browser.close();
    fs.rmSync(views, { recursive: true, force: true });
    fs.rmSync(built, { recursive: true, force: true });
  }
  console.log('news panel browser test passed' + (EVIDENCE ? `; screenshots in ${EVIDENCE}` : ''));
})().catch(e => { console.error(e); process.exit(1); });
