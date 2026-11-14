// P1: rendered check of the research view (site/js/news-research.js) in the tests-only harness
// (tests/news_panel/research.html), for every stage built by tests/news_research_scenarios.py.
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_research_browser.test.js
//   NEWS_RESEARCH_EVIDENCE=<dir>  also saves desktop and narrow screenshots there.
// Every request is served from the repository or the built directory; anything else is aborted and
// fails the test. Checks: ready state, each stage's required/forbidden text, no horizontal scroll at
// 1280 and 390 px, no page errors, links only http(s).
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let pw;
try { pw = require('playwright'); } catch (e) { pw = require('playwright-core'); }

const REPO = path.resolve(__dirname, '..');
const ORIGIN = 'http://news-research.test';
const EVIDENCE = process.env.NEWS_RESEARCH_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' };

(async () => {
  const built = fs.mkdtempSync(path.join(os.tmpdir(), 'news-research-built-'));
  execFileSync(process.env.PYTHON || 'python', [path.join(REPO, 'tests/news_research_scenarios.py'), built],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
  const stages = JSON.parse(fs.readFileSync(path.join(built, 'stages.json'), 'utf8'));
  const browser = await pw.chromium.launch({ headless: true });
  let checks = 0;
  try {
    for (const [vname, viewport] of Object.entries(VIEWPORTS)) {
      const page = await browser.newPage({ viewport });
      const errors = [], foreign = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== ORIGIN) { foreign.push(url.href); return route.abort(); }
        const rel = decodeURIComponent(url.pathname);
        const base = rel.startsWith('/built/') ? built : REPO;
        const file = path.join(base, rel.startsWith('/built/') ? rel.slice('/built/'.length) : rel.slice(1));
        if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
          return route.fulfill({ status: 404, body: 'not found' });
        }
        return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] || 'application/octet-stream' });
      });
      for (const s of stages) {
        const label = `${s.stage} ${vname}`;
        await page.goto(`${ORIGIN}/tests/news_panel/research.html?src=/built/${s.file}`);
        await page.waitForFunction(() => {
          const b = document.getElementById('news-research');
          return b && (b.getAttribute('data-state') === 'ready' || b.getAttribute('data-state') === 'failed');
        }, null, { timeout: 10000 });
        assert.equal(await page.getAttribute('#news-research', 'data-state'), 'ready', label);
        const text = await page.locator('#news-research').innerText();
        for (const m of s.must) assert.ok(text.includes(m), `${label}: missing "${m}"`);
        for (const m of s.must_not) assert.ok(!text.includes(m), `${label}: must not show "${m}"`);
        const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
        assert.ok(sw <= iw, `${label}: page scrolls horizontally (${sw} > ${iw})`);
        const hrefs = await page.$$eval('#news-research a', as => as.map(a => a.getAttribute('href')));
        for (const h of hrefs) assert.ok(/^https?:\/\//.test(h), `${label}: link ${h}`);
        if (EVIDENCE) {
          fs.mkdirSync(EVIDENCE, { recursive: true });
          await page.screenshot({ path: path.join(EVIDENCE, `${vname}-${s.stage}.png`), fullPage: true });
        }
        checks++;
        console.log(`ok - ${label}: ${s.items} items`);
      }
      assert.deepEqual(foreign, [], `${vname}: requests left the harness origin`);
      assert.deepEqual(errors, [], `${vname}: page errors`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  if (EVIDENCE) fs.copyFileSync(path.join(built, 'stages.json'), path.join(EVIDENCE, 'stages.json'));
  console.log(`news research browser test passed: ${checks} rendered checks` + (EVIDENCE ? '; screenshots in ' + EVIDENCE : ''));
})().catch(e => { console.error(e); process.exit(1); });
