// R1/R2: the News tab of the real site/index.html, with the research view following the news index.
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_research_site_browser.test.js
//   NEWS_RESEARCH_SITE_EVIDENCE=<dir>  also saves screenshots there.
// Each case's disposable stores are built by the real CLI with the shipped inactive config: the prepared
// research bundle is imported into the private store (research-import, fixed clocks), then pilot-cycle
// publishes the news views AND the research page as pages of one generation
// (news/v2/views/p/<gen>.<n>/research/view.json, named by the index's "research" entry). Nothing writes
// the retired news/v2/research/view.json key. Each case is served on both data bases:
//   proxy  the production origin: /browser-data/ runs through the real Worker (backend/index.js), its
//          upstream answered from the store;
//   r2     another origin, reading the public R2 origin answered from the same store.
// Checks: nothing is requested before the News tab opens; the research view shows exactly the page the
// index names, in the index's incarnation (or absent / withdrawn / refresh-required / failed while the
// news views keep their own state); the fixed-view note; no private ledger, legacy or retired key is
// ever requested; the proxy refuses ledger and retired keys; it hides with the tab and is not refetched;
// no page errors, foreign requests or horizontal scroll.
// Local only: fixture feeds, temporary directories, nothing reaches a provider or the network.
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
const EVIDENCE = process.env.NEWS_RESEARCH_SITE_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const RETIRED_KEY = 'news/v2/research/view.json';
const INDEX_KEY = 'news/v2/views/index.json';
const RESEARCH_PAGE = /^news\/v2\/views\/p\/([0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.[1-9][0-9]{0,3})\/research\/view\.json$/;
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
const PY = process.env.PYTHON || 'python';
const CFG = path.join(REPO, 'market_data/news/deploy/news-pilot.json');
const BUNDLE = path.join(REPO, 'tests/fixtures/news/research/positioning_p1_prepared.json');
const FIXED = 'Fixed view: research mode at this publication\'s build time. No other mode or cutoff is published.';

function cli(args) {
  execFileSync(PY, ['-m', 'market_data.news', ...args], { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
}
// One store pair per case: { ledger: import the prepared bundle first, clock: the pilot cycle, withdraw }.
function buildStores(dir, spec) {
  const priv = path.join(dir, 'private'), pub = path.join(dir, 'public');
  if (spec.ledger) {
    cli(['research-import', '--store', priv, '--bundle', BUNDLE, '--clock', '2026-11-13T18:00:00Z',
      '--commit-clock', '2026-11-13T18:00:05Z']);
  }
  cli(['pilot-cycle', '--config', CFG, '--private', priv, '--public', pub, '--spool', path.join(dir, 'spool'),
    '--clock', spec.clock]);
  if (spec.withdraw) {
    cli(['publish-stop', '--config', CFG, '--private', priv, '--public', pub, '--clock', spec.clock, '--reason', 'R1 test']);
    cli(['publish-withdraw', '--config', CFG, '--private', priv, '--public', pub, '--clock', spec.clock]);
  }
  assert.ok(!fs.existsSync(path.join(pub, 'news/v1')), 'the public store holds no private keys');
  assert.ok(!fs.existsSync(path.join(pub, ...RETIRED_KEY.split('/'))), 'nothing writes the retired research key');
  return pub;
}

// store: which stores; override: what the research page answers; panel/state: expected data-state values.
const CASES = [
  { name: 'prepared-research', store: 'prepared', panel: 'ready', state: 'ready', both: true, requests: 1,
    must: [FIXED, 'Cutoff 2026-11-13T19:00:00Z', 'RESEARCH', 'PREPARED', 'accessed 2026-11-12T16:59:58Z',
      'capture committed 2026-11-13T18:00:05Z', 'no collector known-at', 'RETROSPECTIVE hypothesis',
      'CONTEMPORANEOUS hypothesis', 'attributed report', 'thesis assertion', 'not evidence', 'upper bound',
      'capture revision 1'] },
  { name: 'before-commit', store: 'before', panel: 'ready', state: 'ready', requests: 1,
    must: [FIXED, 'Cutoff 2026-11-13T18:00:04Z', 'No research records visible'] },
  { name: 'no-ledger', store: 'empty', panel: 'ready', state: 'absent', both: true, requests: 0,
    must: ['No research view published: no verified research run at this build'] },
  { name: 'withdrawn', store: 'withdrawn', panel: 'withdrawn', state: 'withdrawn', requests: 0,
    must: ['Research records are not currently published'] },
  { name: 'page-gone', store: 'prepared', override: { status: 404 }, panel: 'ready', state: 'refresh-required', requests: 1,
    must: ['This research view was replaced or has expired', 'no longer published', 'Reload news and research'],
    reload: true },
  { name: 'upstream-500', store: 'prepared', override: { status: 500 }, panel: 'ready', state: 'failed', requests: 1,
    must: ['Could not load research records', 'HTTP 500'] },
  { name: 'malformed', store: 'prepared', override: { body: '{"schema_version": "news-research-view/v1", "items": ' },
    panel: 'ready', state: 'failed', requests: 1, must: ['Could not load research records', 'not valid JSON'] },
];

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'news-research-site-'));
  const stores = {
    prepared: buildStores(path.join(tmp, 'prepared'), { ledger: true, clock: '2026-11-13T19:00:00Z' }),
    before: buildStores(path.join(tmp, 'before'), { ledger: true, clock: '2026-11-13T18:00:04Z' }),
    empty: buildStores(path.join(tmp, 'empty'), { ledger: false, clock: '2026-11-13T19:00:00Z' }),
    withdrawn: buildStores(path.join(tmp, 'withdrawn'), { ledger: true, clock: '2026-11-13T19:00:00Z', withdraw: true }),
  };
  const indexOf = name => JSON.parse(fs.readFileSync(path.join(stores[name], ...INDEX_KEY.split('/')), 'utf8'));
  const prepared = indexOf('prepared');
  assert.equal(prepared.research.state, 'published');
  assert.equal(RESEARCH_PAGE.exec(prepared.research.page)[1], prepared.gen, 'the research page is in the index incarnation');
  assert.equal(indexOf('withdrawn').state, 'withdrawn');

  let current = null;   // the case being served
  const upstream = key => {
    const store = stores[current.store];
    if (RESEARCH_PAGE.test(key) && current.override) {
      if (current.override.status) return { status: current.override.status, body: 'upstream ' + current.override.status };
      return { status: 200, body: current.override.body, type: 'application/json' };
    }
    const file = path.join(store, ...key.split('/'));
    if (!file.startsWith(store + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return { status: 404, body: 'not found' };
    return { status: 200, body: fs.readFileSync(file), type: 'application/json',
      cache: key === INDEX_KEY ? 'public, max-age=60' : 'public, max-age=31536000, immutable' };
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
    const headers = {};
    if (r.type) headers['Content-Type'] = r.type;
    if (r.cache) headers['Cache-Control'] = r.cache;
    return new Response(r.body, { status: r.status, headers });
  };

  // The Worker: the research page is an immutable generation page (its upstream caching is kept; only the
  // index is forced no-store); the retired key, the private ledger and other research keys are refused.
  current = CASES[0];
  let res = await worker.fetch(new Request(APP + '/browser-data/' + prepared.research.page + '?_=1', { headers: { Origin: APP } }), env);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=31536000, immutable');
  res = await worker.fetch(new Request(APP + '/browser-data/' + INDEX_KEY + '?_=1', { headers: { Origin: APP } }), env);
  assert.equal(res.headers.get('cache-control'), 'no-store', 'the index stays no-store');
  const before = upstreamCalls.length;
  for (const key of [RETIRED_KEY, 'news/v1/research/runs/2026-11-13/r/annotations.jsonl.gz', 'news/v1/research/_writer/lease.json',
    'news/v2/research/', 'news/v2/research/other.json', prepared.research.page.replace('/view.json', '/other.json'),
    prepared.research.page + '.bak']) {
    res = await worker.fetch(new Request(APP + '/browser-data/' + key, { headers: { Origin: APP } }), env);
    assert.equal(res.status, 404, key);
  }
  assert.equal(upstreamCalls.length, before, 'refused keys never reach upstream');
  const afterProbe = upstreamCalls.length;

  const browser = await pw.chromium.launch({ headless: true });
  let checks = 0;
  try {
    for (const c of CASES) {
      current = c;
      const idx = indexOf(c.store);
      for (const [base, pageOrigin] of [['proxy', APP], ['r2', OTHER]]) {
        const views = c.both ? Object.entries(VIEWPORTS) : [['desktop', VIEWPORTS.desktop]];
        for (const [vname, viewport] of views) {
          const label = `${c.name} ${base} ${vname}`;
          const page = await browser.newPage({ viewport });
          const errors = [], foreign = [];
          const news = [];
          page.on('pageerror', e => errors.push(e.message));
          page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
          await page.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.origin === pageOrigin && url.pathname.startsWith('/browser-data/')) {
              const key = url.pathname.slice('/browser-data/'.length);
              if (key.startsWith('news/')) news.push({ base: 'proxy', key, query: url.search });
              const r = await worker.fetch(new Request(url, { headers: { Origin: APP } }), env);
              return route.fulfill({ status: r.status, body: Buffer.from(await r.arrayBuffer()), headers: Object.fromEntries(r.headers) });
            }
            if (url.origin === R2) {
              const key = url.pathname.slice(1);
              if (key.startsWith('news/')) news.push({ base: 'r2', key, query: url.search });
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
          await page.waitForTimeout(300);
          assert.deepEqual(news, [], `${label}: no News request before the tab opens`);
          assert.ok(await page.locator('#news-research').isHidden(), `${label}: research hidden with the News tab`);
          await page.click('#tab-news');
          const waitState = async (sel, want) => page.waitForFunction(([s, w]) => {
            const b = document.querySelector(s);
            return b && b.getAttribute('data-state') === w && !b.hasAttribute('data-pending');
          }, [sel, want], { timeout: 10000 }).catch(async () => {
            throw new Error(`${label}: ${sel} data-state ${await page.getAttribute(sel, 'data-state')} (wanted ${want}): ` +
              (await page.locator(sel).innerText()).slice(0, 300));
          });
          await waitState('#news-panel', c.panel);
          await waitState('#news-research', c.state);
          assert.ok(await page.locator('#news-research').isVisible(), `${label}: research visible on the News tab`);
          if (c.panel === 'ready') {
            const panelText = await page.locator('#news-panel').innerText();
            assert.ok(panelText.includes('prices unavailable'), `${label}: the news views still say prices are unavailable`);
          }
          const text = await page.locator('#news-research').innerText();
          for (const m of c.must) assert.ok(text.includes(m), `${label}: missing "${m}" in ${text.slice(0, 400)}`);
          const research = news.filter(n => n.key.includes('research'));
          assert.equal(research.length, c.requests, `${label}: research requests ${JSON.stringify(research)}`);
          if (c.requests) {
            assert.equal(research[0].key, idx.research.page, `${label}: exactly the page the index names`);
            assert.equal(RESEARCH_PAGE.exec(research[0].key)[1], idx.gen, `${label}: in the index incarnation`);
            assert.equal(research[0].base, base);
            assert.ok(/^\?_=[0-9]{1,20}$/.test(research[0].query), `${label}: query ${research[0].query}`);
            if (c.state === 'ready') assert.equal(await page.getAttribute('#news-research', 'data-page'), idx.research.page);
          }
          assert.equal(news.filter(n => n.key === INDEX_KEY).length, 1, `${label}: one index read, shared with the research view`);
          assert.ok(news.every(n => !n.key.startsWith('news/v1/') && n.key !== RETIRED_KEY), `${label}: no private, legacy or retired key`);
          const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
          assert.ok(sw <= iw, `${label}: page scrolls horizontally (${sw} > ${iw})`);
          for (const h of await page.$$eval('#news-research a', as => as.map(a => a.getAttribute('href')))) {
            assert.ok(/^https?:\/\//.test(h), `${label}: link ${h}`);
          }
          if (EVIDENCE) {
            fs.mkdirSync(EVIDENCE, { recursive: true });
            await page.screenshot({ path: path.join(EVIDENCE, `${c.name}-${base}-${viewport.width}.png`), fullPage: true });
          }
          if (c.reload) {
            // Chej 11-13: the research recovery is actionable while the news views stay ready. Its button
            // reloads the one shared index: news views and research both follow the new read.
            const indexReads = news.filter(n => n.key === INDEX_KEY).length;
            await page.click('#news-research button[data-action="reload-shared-index"]');
            await page.waitForFunction(() => {
              const b = document.querySelector('#news-research');
              return b && b.getAttribute('data-state') === 'refresh-required' && !b.hasAttribute('data-pending');
            }, null, { timeout: 10000 });
            await waitState('#news-panel', 'ready');
            assert.equal(news.filter(n => n.key === INDEX_KEY).length, indexReads + 1, `${label}: one fresh index read for both`);
            assert.equal(news.filter(n => n.key.includes('research')).length, c.requests + 1, `${label}: research refetched once from the new index`);
          }
          // Leaving the tab hides the research view; reopening does not refetch.
          await page.click('#tab-live');
          assert.ok(await page.locator('#news-research').isHidden(), `${label}: research hides with the News tab`);
          await page.click('#tab-news');
          await page.waitForTimeout(200);
          assert.equal(news.filter(n => n.key.includes('research')).length, c.reload ? c.requests + 1 : c.requests, `${label}: not refetched on reopen`);
          assert.deepEqual(foreign, [], `${label}: requests left the page and R2 origins`);
          assert.deepEqual(errors, [], `${label}: page errors`);
          checks++;
          console.log(`ok - ${label}: panel ${c.panel}, research ${c.state}`);
          await page.close();
        }
      }
    }
    for (const key of upstreamCalls.slice(afterProbe).filter(k => k.startsWith('news/'))) {
      assert.ok(key.startsWith('news/v2/views/'), 'upstream ' + key);
    }
  } finally {
    global.fetch = originalFetch;
    await browser.close();
  }
  console.log(`news research site browser test passed: ${checks} rendered checks over ${CASES.length} cases` +
    (EVIDENCE ? '; screenshots in ' + EVIDENCE : ''));
})().catch(e => { console.error(e); process.exit(1); });
