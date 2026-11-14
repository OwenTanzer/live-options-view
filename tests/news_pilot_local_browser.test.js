// G7: rendered local check of the News tab against stores written by the real publisher.
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_pilot_local_browser.test.js
//   NEWS_G7_EVIDENCE=<dir>  also saves screenshots there.
// tests/news_g7_local_pilot.py runs the pilot through the real CLI against disposable local stores
// (fixture feeds, the shipped inactive news-pilot.json) and copies the public store after each stage:
// a timed-out page PUT, the page cap, a late-landing PUT, an index write whose answer is lost, its
// settlement, withdrawal, and a page that reappears after withdrawal. Each stage is loaded in the real
// site/index.html on both data bases:
//   proxy  the production origin: /browser-data/ runs through the real Worker (backend/index.js), whose
//          upstream fetch is answered from that stage's public directory;
//   r2     another origin, reading the public R2 origin answered from the same directory.
// Checks: the panel shows what the live index says (ready on the live incarnation, or the withdrawn
// state); it requests only view keys, only under the live incarnation, never a quarantined or
// withdrawn namespace (a late page named by no index stays unread); no page errors, no foreign
// requests, no horizontal scroll. Local only: nothing reaches a provider or the network.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let pw;
try { pw = require('playwright'); } catch (e) { pw = require('playwright-core'); }

const REPO = path.resolve(__dirname, '..');
const SITE = path.join(REPO, 'site');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const OTHER = 'http://news-site.test';
const R2 = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const EVIDENCE = process.env.NEWS_G7_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const INDEX_KEY = 'news/v2/views/index.json';
const panel = require(path.join(SITE, 'js/news-panel.js'));
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };

(async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'news-g7-'));
  execFileSync(process.env.PYTHON || 'python', [path.join(REPO, 'tests/news_g7_local_pilot.py'), out],
    { cwd: REPO, stdio: ['ignore', 'inherit', 'inherit'] });
  const stages = JSON.parse(fs.readFileSync(path.join(out, 'stages.json'), 'utf8'));
  assert.ok(stages.length >= 9);

  let storeDir = null;   // the public directory of the stage being served
  const upstream = key => {
    const file = path.join(storeDir, ...key.split('/'));
    if (!file.startsWith(storeDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return { status: 404, body: 'not found' };
    }
    return { status: 200, body: fs.readFileSync(file), type: 'application/json' };
  };
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
  let checks = 0;
  try {
    for (const stage of stages) {
      storeDir = path.join(out, 'stages', stage.stage);
      const viewports = ['S1-published', 'S7-withdrawn'].includes(stage.stage) ? Object.entries(VIEWPORTS)
        : [['desktop', VIEWPORTS.desktop]];
      for (const [base, pageOrigin] of [['proxy', APP], ['r2', OTHER]]) {
        for (const [vname, viewport] of viewports) {
          const label = `${stage.stage} ${base} ${vname}`;
          const page = await browser.newPage({ viewport });
          const errors = [], foreign = [], news = [], researchReqs = [];
          page.on('pageerror', e => errors.push(e.message));
          page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
          await page.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.origin === pageOrigin && url.pathname.startsWith('/browser-data/')) {
              const key = url.pathname.slice('/browser-data/'.length);
              if (key.includes('research')) researchReqs.push({ base: 'proxy', key });
              else if (key.startsWith('news/')) news.push({ base: 'proxy', key });
              const res = await worker.fetch(new Request(url, { headers: { Origin: APP } }), env);
              return route.fulfill({ status: res.status, body: Buffer.from(await res.arrayBuffer()),
                headers: Object.fromEntries(res.headers) });
            }
            if (url.origin === R2) {
              const key = url.pathname.slice(1);
              if (key.includes('research')) researchReqs.push({ base: 'r2', key });
              else if (key.startsWith('news/')) news.push({ base: 'r2', key });
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
          await page.goto(pageOrigin + '/');
          await page.waitForSelector('#tab-news');
          await page.click('#tab-news');
          await page.waitForFunction(w => {
            const p = document.querySelector('#news-panel');
            return p.getAttribute('data-state') === w && !p.hasAttribute('data-pending');
          }, stage.expect, { timeout: 10000 }).catch(async () => {
            const s = await page.getAttribute('#news-panel', 'data-state');
            const t = (await page.locator('#news-panel').innerText()).slice(0, 300);
            throw new Error(`${label}: data-state ${s} (wanted ${stage.expect}): ${t}`);
          });
          const text = await page.locator('#news-panel').innerText();
          const keys = news.map(n => n.key);
          if (stage.expect === 'ready') {
            assert.equal(await page.getAttribute('#news-panel', 'data-page'), stage.first_page, `${label}: first page`);
            assert.deepEqual(keys, [INDEX_KEY, stage.first_page], `${label}: requests`);
            assert.ok(stage.first_page.startsWith(`news/v2/views/p/${stage.live_gen}/`), `${label}: live incarnation`);
          } else {
            assert.ok(text.includes('News views are not currently published'), `${label}: ${text.slice(0, 200)}`);
            assert.deepEqual(keys, [INDEX_KEY], `${label}: a tombstone names no page`);
          }
          for (const n of news) {
            assert.equal(n.base, base, `${label}: wrong base for ${n.key}`);
            assert.ok(panel.isNewsViewKey(n.key), `${label}: not a view key: ${n.key}`);
            for (const p of stage.forbidden_prefixes) assert.ok(!n.key.startsWith(p), `${label}: read ${n.key}`);
          }
          const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
          assert.ok(sw <= iw, `${label}: page scrolls horizontally (${sw} > ${iw})`);
          if (EVIDENCE) {
            fs.mkdirSync(EVIDENCE, { recursive: true });
            await page.screenshot({ path: path.join(EVIDENCE, `${stage.stage}-${base}-${viewport.width}.png`), fullPage: false });
          }
          // R1: these stores have no research ledger, so the index says research state "none": nothing is requested.
          assert.deepEqual(researchReqs, [], `${label}: no research request (no research page is named)`);
          assert.deepEqual(foreign, [], `${label}: requests left the page and R2 origins`);
          assert.deepEqual(errors, [], `${label}: page errors`);
          checks++;
          console.log(`ok - ${label}: ${stage.expect}${stage.live_gen ? ' ' + stage.live_gen : ''}`);
          await page.close();
        }
      }
    }
    for (const key of upstreamCalls.filter(k => k.startsWith('news/'))) assert.ok(panel.isNewsViewKey(key), key);
  } finally {
    global.fetch = originalFetch;
    await browser.close();
  }
  if (EVIDENCE) fs.copyFileSync(path.join(out, 'stages.json'), path.join(EVIDENCE, 'stages.json'));
  if (EVIDENCE) fs.copyFileSync(path.join(out, 'commands.json'), path.join(EVIDENCE, 'commands.json'));
  console.log(`news pilot local browser test passed: ${checks} rendered checks over ${stages.length} stages` +
    (EVIDENCE ? '; screenshots in ' + EVIDENCE : ''));
})().catch(e => { console.error(e); process.exit(1); });
