// Optional rendered check of M5 pilot output (local only): two fixture pilot cycles are published
// into a temporary public directory, then the site's only panel copy renders them in the tests-only
// harness. Prices are unavailable (no price input connected) and must say so; nothing may be blank.
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_publish_browser.test.js
//   NEWS_PANEL_EVIDENCE=<dir>  also saves desktop and narrow-width screenshots there.
// Every request is served from the repository or the published directory; anything else is aborted.
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'news-publish-'));
  const pub = path.join(tmp, 'public');
  // G7: the shipped (inactive) deploy/news-pilot.json names news/v2/views/ and is used as is.
  const cfgPath = path.join(REPO, 'market_data/news/deploy/news-pilot.json');
  const args = ['--config', cfgPath, '--private', path.join(tmp, 'private'), '--public', pub, '--spool', path.join(tmp, 'spool')];
  for (const clock of ['2026-11-09T10:05:00Z', '2026-11-09T10:20:00Z']) {
    execFileSync(process.env.PYTHON || 'python', ['-m', 'market_data.news', 'pilot-cycle', ...args, '--clock', clock],
      { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
  }
  // G3 (NOT RUN when written): the pilot publishes immutable generations; the live index names one.
  const gen = JSON.parse(fs.readFileSync(path.join(pub, 'news/v2/views/index.json'), 'utf8')).gen;
  const published = key => key.replace('news/v1/views/', `news/v2/views/p/${gen}/`);
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
        else if (url.pathname === '/site/js/news-panel.js' || url.pathname === '/site/index.html') {
          file = path.join(REPO, url.pathname);
        }
        else if (url.pathname.startsWith('/published/')) file = path.join(pub, url.pathname.slice('/published/'.length));
        if (!file || !file.startsWith(REPO) && !file.startsWith(pub) || !fs.existsSync(file)) {
          return route.fulfill({ status: 404, body: 'not found' });
        }
        return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] || 'application/octet-stream' });
      });
      await page.goto(`${ORIGIN}/tests/news_panel/harness.html?root=/published/`);
      await page.evaluate(() => window.newsPanelReady);
      const pick = async unqualified => {
        const key = published(unqualified);
        await page.selectOption('select[aria-label^="View"]', key);
        await page.waitForFunction(k => document.querySelector('#news-panel').getAttribute('data-page') === k, key);
      };
      const text = () => page.locator('#news-panel').innerText();

      for (const key of ['news/v1/views/collector/oil/2026-11-09/page-0001.json',
        'news/v1/views/archive/ai_buildout/2026-11-09/page-0001.json']) {
        await pick(key);
        assert.equal(await page.locator('#news-panel').getAttribute('data-state'), 'ready', `${name} ${key} ready`);
        const t = await text();
        assert.ok(t.includes('PILOT news: prices unavailable (no price input connected)'), `${name} ${key}: label`);
        assert.ok(!t.includes('SYNTHETIC'), `${name} ${key}: not labelled synthetic`);
        assert.ok(t.includes('price missing: none available yet'), `${name} ${key}: row price missing`);
        assert.ok(/Price missing 00:00:00 → 10:20:00/.test(t), `${name} ${key}: whole-window missing interval`);
        assert.ok(!/price (matched|stale):/.test(t), `${name} ${key}: no matched or stale price`);
        const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
        assert.ok(sw <= iw, `${name} ${key}: page scrolls horizontally (${sw} > ${iw})`);
        if (EVIDENCE) {
          fs.mkdirSync(EVIDENCE, { recursive: true });
          const label = key.split('/').slice(3, 5).join('-');
          await page.screenshot({ path: path.join(EVIDENCE, `pilot-${name}-${label}.png`), fullPage: true });
        }
      }
      // An empty earlier day in the 7-day window is "no items on this page", never "observed, no items".
      await pick('news/v1/views/collector/oil/2026-11-05/page-0001.json');
      const empty = await text();
      assert.ok(!empty.includes('Observed, no items'), `${name}: an unobserved day is not called quiet`);
      assert.deepEqual(errors, [], `${name}: page errors`);
      await page.close();
    }
    assert.deepEqual(foreign, [], 'no request left the harness origin');
  } finally {
    await browser.close();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log('news publish browser test passed' + (EVIDENCE ? `; screenshots in ${EVIDENCE}` : ''));
})().catch(e => { console.error(e); process.exit(1); });
