// G3 generation contract across the panel, the backend proxy, the gateway and the Python publisher,
// ported to the G6 contract (news/v2/views/index.json, news/v2/views/p/<gen>.<n>/..., tombstones).
// NOT RUN: written and ported for static review only (no test execution).
//   node tests/news_generation_contract.test.js
// Covers: one exact key grammar in four places; the reader lease (sleep, clock changes, disagreement);
// refresh-required instead of mixing (lease end on visibility recheck, generation mismatch, page 404,
// request deadline); the fetcher deadline with fake timers; the backend's no-store index, News-only
// upstream deadline (504) and unchanged non-News behaviour.
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const panel = require('../site/js/news-panel.js');
const Q = require('./news_panel/qualify.js').loadQualified();
const REPO = path.join(__dirname, '..');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const G = Q.gen;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const tick = () => new Promise(r => setImmediate(r));
async function settle() { for (let i = 0; i < 8; i++) await tick(); }

class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.listeners = {}; this.className = ''; this._text = ''; this.disabled = false; this.value = ''; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t) { (this.listeners[t] || []).forEach(fn => fn()); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => ' ' + c.textContent).join(''); }
  all(p, out = []) { if (p(this)) out.push(this); this.children.forEach(c => c.all(p, out)); return out; }
  byTag(t) { return this.all(n => n.tagName === t.toUpperCase()); }
}
const doc = { createElement: t => new Node(t) };
const events = () => { const l = {}; return { addEventListener: (t, f) => (l[t] = l[t] || []).push(f), fire: t => (l[t] || []).forEach(f => f()) }; };
const clocks = () => { const c = { m: 1000, w: Date.parse('2026-11-09T10:20:00Z') }; c.mono = () => c.m; c.wall = () => c.w; return c; };
const load = key => JSON.parse(JSON.stringify(Q.byKey(key)));
const FIRST = Q.index.views[0].first_page;

const KEYS = [
  ['news/v2/views/index.json', true],
  ['news/v1/views/index.json', false],                                  // legacy index: no longer served
  ['news/v1/views/g/20261109T100500Z-0123456789abcdef/collector/oil/2026-11-09/page-0001.json', false],   // legacy page
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef/collector/oil/2026-11-09/page-0001.json', false],   // no incarnation
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef.0/collector/oil/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef.01/collector/oil/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef.9999/collector/oil/2026-11-09/page-0001.json', true],
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef.10000/collector/oil/2026-11-09/page-0001.json', false],
  [`news/v2/views/p/${G}/collector/oil/2026-11-09/page-0001.json`, true],
  [`news/v2/views/p/${G}/publication/ai_buildout/2024-02-29/page-0020.json`, true],
  [`news/v2/views/p/${G}/archive/all/2026-11-09/page-0021.json`, false],
  [`news/v2/views/p/${G}/archive/all/2026-11-09/page-0000.json`, false],
  [`news/v2/views/p/${G}/archive/all/2026-02-30/page-0001.json`, false],
  [`news/v2/views/p/${G}/archive/fed/2026-11-09/page-0001.json`, false],
  ['news/v2/views/p/20261109T100500Z-0123456789ABCDEF.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T100500Z-0123456789abcde.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T100500Z-0123456789abcdef0.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261131T100500Z-0123456789abcdef.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T240000Z-0123456789abcdef.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20261109T235960Z-0123456789abcdef.1/archive/all/2026-11-09/page-0001.json', false],
  ['news/v2/views/p/20240229T235959Z-0123456789abcdef.1/archive/all/2024-02-29/page-0001.json', true],
  ['news/v1/views/collector/oil/2026-11-09/page-0001.json', false],   // unqualified: builder output only
  [`news/v2/views/p/${G}/index.json`, false], [`news/v2/views/p/${G}/`, false], ['news/v2/views/p/', false],
  ['news/v1/views/today.json', false], ['news/v1/latest.json', false],
];

test('one key grammar: panel, backend, gateway and the Python publisher agree', async () => {
  const backend = await import('../backend/browser-data.js');
  const gateway = await import('../frontend/preview-policy.mjs');
  const py = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-c',
    'import json,sys; from market_data.news.publish import is_public_view_key as k; ' +
    'print(json.dumps([k(x) for x in json.loads(sys.stdin.read())]))'],
    { cwd: REPO, input: JSON.stringify(KEYS.map(k => k[0])) }).toString());
  KEYS.forEach(([key, want], i) => {
    assert.equal(panel.isNewsViewKey(key), want, 'panel ' + key);
    assert.equal(backend.isPublicBrowserDataKey(key), want, 'backend ' + key);
    assert.equal(gateway.publicReadAllowed(new URL(APP + '/browser-data/' + key)), want, 'gateway ' + key);
    assert.equal(py[i], want, 'python ' + key);
  });
});

test('the lease ends on time, on sleep, on a backward or jumping wall clock, and on disagreement', () => {
  const L = panel.LEASE_MS;
  const check = (dm, dw) => { const c = clocks(); const valid = panel.startLease(c, L); c.m += dm; c.w += dw; return valid(); };
  assert.equal(check(0, 0), true);
  assert.equal(check(L - 1, L - 1), true);
  assert.equal(check(L, L), false, 'lease length');
  assert.equal(check(1000, 3 * 3600 * 1000), false, 'sleep: monotonic paused, wall advanced');
  assert.equal(check(60000, -1), false, 'wall clock went backwards');
  assert.equal(check(60000, 60000 + panel.CLOCK_AGREE_MS), false, 'clocks disagree');
});

function mountWith(pages, opts) {
  const root = new Node('div');
  const fetched = [];
  const fetchJson = key => {
    fetched.push(key);
    if (key in pages) return typeof pages[key] === 'function' ? pages[key]() : Promise.resolve(pages[key]);
    return Promise.resolve(load(key));
  };
  return { root, fetched, done: panel.mount(doc, root, fetchJson, panel.INDEX_KEY, opts) };
}

test('an ended lease, rechecked on visibility, asks for a refresh and requests nothing more', async () => {
  const c = clocks();
  const ev = events();
  const first = load(FIRST);
  first.next_page = FIRST.replace('page-0001', 'page-0002');
  first.page_count = 2;
  const { root, fetched, done } = mountWith({ [FIRST]: first }, { clocks: c, events: ev });
  await done;
  assert.equal(root.getAttribute('data-state'), 'ready');
  c.m += 1000; c.w += 4 * 3600 * 1000;           // the laptop slept for four hours
  ev.fire('visibilitychange');
  assert.equal(root.getAttribute('data-state'), 'refresh-required');
  assert.ok(root.textContent.includes('Reload'));
  assert.deepEqual(fetched, [panel.INDEX_KEY, FIRST]);
});

test('a page from another generation is never shown', async () => {
  const other = load(FIRST);
  other.gen = '20990101T000000Z-0000000000000000.1';
  const { root, done } = mountWith({ [FIRST]: other }, { clocks: clocks() });
  await done;
  assert.equal(root.getAttribute('data-state'), 'refresh-required');
  assert.ok(!root.textContent.includes('collector mode'), 'nothing of the foreign page was rendered');
});

test('Reload starts again from the fixed index', async () => {
  const other = load(FIRST);
  other.gen = '20990101T000000Z-0000000000000000.1';
  let served = other;
  const { root, fetched, done } = mountWith({ [FIRST]: () => Promise.resolve(served) }, { clocks: clocks() });
  await done;
  served = load(FIRST);
  root.byTag('button').find(b => b.textContent === 'Reload').fire('click');
  await settle();
  assert.equal(root.getAttribute('data-state'), 'ready');
  assert.deepEqual(fetched, [panel.INDEX_KEY, FIRST, panel.INDEX_KEY, FIRST]);
});

test('the fetcher deadline aborts the request and discards a late body', async () => {
  const timers = [];
  const fake = { set: (f, ms) => { timers.push({ f, ms }); return timers.length; }, clear: () => {} };
  let seen = null;
  let release = null;
  const fetchImpl = (url, init) => { seen = init; return new Promise(r => { release = r; }); };
  const fetchJson = panel.makeFetcher('/browser-data', fetchImpl, () => 1, 20000, fake);
  const pending = fetchJson(FIRST);
  await tick();
  assert.equal(timers[0].ms, 20000);
  assert.equal(seen.cache, 'no-store');
  timers[0].f();                                  // the deadline passes
  await assert.rejects(pending, e => e.newsKind === 'deadline');
  if (typeof AbortController === 'function') assert.equal(seen.signal.aborted, true);
  release({ status: 200, ok: true, text: async () => JSON.stringify(load(FIRST)) });   // too late: ignored
  await settle();
});

test('a page past its deadline asks for a refresh instead of mixing', async () => {
  const deadline = Object.assign(new Error(FIRST + ': no complete response within 20 s'), { newsKind: 'deadline' });
  const { root, done } = mountWith({ [FIRST]: () => Promise.reject(deadline) }, { clocks: clocks() });
  await done;
  assert.equal(root.getAttribute('data-state'), 'refresh-required');
});

test('backend: the index is never cacheable, News upstream has a deadline, other keys are unchanged', async () => {
  const { default: worker } = await import('../backend/index.js');
  const env = new Proxy({}, { get() { throw new Error('No bindings may be accessed'); } });
  const original = { fetch: global.fetch, setTimeout: global.setTimeout };
  const calls = [];
  let hang = false;
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    if (hang) {
      if (options.signal && options.signal.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
      return new Promise(() => {});
    }
    return new Response('{}', { status: 200, headers: { 'Cache-Control': 'public, max-age=600', 'Content-Type': 'application/json' } });
  };
  const request = key => worker.fetch(new Request(APP + '/browser-data/' + key), env);
  try {
    const index = await request('news/v2/views/index.json');
    assert.equal(index.headers.get('cache-control'), 'no-store');
    const page = await request(FIRST);
    assert.equal(page.headers.get('cache-control'), 'public, max-age=600', 'immutable pages keep their caching');
    assert.ok(calls.at(-1).options.signal, 'News requests carry a deadline signal');
    const other = await request('manifest.json');
    assert.equal(other.headers.get('cache-control'), 'public, max-age=600');
    assert.equal(calls.at(-1).options.signal, undefined, 'non-News requests are unchanged');
    // The 15 s deadline, fired at once: a hanging News upstream becomes 504.
    hang = true;
    global.setTimeout = (f, ms) => { if (ms === 15000) { f(); return 0; } return original.setTimeout(f, ms); };
    const late = await request(FIRST);
    assert.equal(late.status, 504);
    assert.equal(await late.text(), 'News upstream deadline exceeded');
  } finally {
    global.fetch = original.fetch;
    global.setTimeout = original.setTimeout;
  }
});

// --- G6: tombstones and the v2 index (NOT RUN) -----------------------------------------------------
const { tombstone, INDEX_KEY: PUBLISHED_INDEX } = require('./news_panel/qualify.js');

function mountIndex(index) {
  const root = new Node('div');
  const asked = [];
  const fetchJson = key => { asked.push(key); return key === PUBLISHED_INDEX ? Promise.resolve(index) : Promise.reject(new Error('unexpected ' + key)); };
  return { root, asked, done: panel.mount(doc, root, fetchJson, PUBLISHED_INDEX, { clocks: clocks() }) };
}

test('G6: a withdrawn tombstone shows "not currently published" and requests no page', async () => {
  const { root, asked, done } = mountIndex(tombstone('withdrawn'));
  await done;
  assert.equal(root.getAttribute('data-state'), 'withdrawn');
  assert.ok(root.textContent.includes('News views are not currently published'), root.textContent);
  assert.ok(!root.textContent.includes('Could not load'));
  assert.deepEqual(asked, [PUBLISHED_INDEX], 'only the index was requested');
});

test('G6: an unpublished tombstone shows the same state as a 404 on the index', async () => {
  const { root, asked, done } = mountIndex(tombstone('unpublished'));
  await done;
  assert.equal(root.getAttribute('data-state'), 'absent');
  assert.ok(root.textContent.includes('No news views published yet'));
  assert.deepEqual(asked, [PUBLISHED_INDEX]);
});

test('G6: only an exact tombstone counts; anything else is a visible failure, never an empty panel', async () => {
  assert.equal(panel.tombstoneState(tombstone('withdrawn')), 'withdrawn');
  assert.equal(panel.tombstoneState(tombstone('unpublished')), 'unpublished');
  const bad = [
    Object.assign(tombstone(), { state: 'published' }),
    Object.assign(tombstone(), { schema_version: 'news-view-index/v1' }),
    Object.assign(tombstone(), { commit: { nonce: 'short' } }),
    Object.assign(tombstone(), { commit: null }),
    Object.assign(tombstone(), { gen: G }),
    Object.assign(tombstone(), { views: [] }),
    null, [], 'withdrawn',
  ];
  for (const index of bad) {
    assert.equal(panel.tombstoneState(index), null, JSON.stringify(index));
    const { root, done } = mountIndex(index);
    await done;
    assert.equal(root.getAttribute('data-state'), 'failed', JSON.stringify(index));
  }
});

test('G6: the published index must be v2, published, nonced and consistent with its incarnation', () => {
  assert.deepEqual(panel.validateIndex(load(PUBLISHED_INDEX)), []);
  const broken = {
    'v1 schema': i => { i.schema_version = 'news-view-index/v1'; },
    'no state': i => { delete i.state; },
    'no commit': i => { delete i.commit; },
    'bad nonce': i => { i.commit = { nonce: 'ABC' }; },
    'logical generation as gen': i => { i.gen = i.logical_gen; },
    'another logical_gen': i => { i.logical_gen = '20990101T000000Z-0000000000000000'; },
    'gen with a leading zero': i => { i.gen = i.logical_gen + '.01'; },
  };
  for (const [name, change] of Object.entries(broken)) {
    const index = load(PUBLISHED_INDEX);
    change(index);
    assert.notDeepEqual(panel.validateIndex(index), [], name);
  }
  assert.equal(panel.isGen(G), true);
  assert.equal(panel.isGen(Q.logicalGen), false, 'a generation without an incarnation number is not accepted');
  assert.equal(panel.genOf(FIRST), G);
});

(async () => {
  for (const [name, fn] of tests) {
    await fn();
    console.log('ok -', name);
  }
  console.log(`\n${tests.length} news generation contract tests passed`);
})().catch(e => { console.error(e); process.exit(1); });
