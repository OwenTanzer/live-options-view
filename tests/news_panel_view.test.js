// The News panel (site/js/news-panel.js, the only copy) rendering the
// committed fixture views (tests/fixtures/news/views/, SYNTHETIC). A minimal
// fake DOM records what the panel builds; its innerHTML setter throws, so any
// markup injection path fails the test. M4b adds the view-key check and the
// checked fetcher (absence vs failure states, keys from pages refused before
// any request) and the site wiring: the ?v= hash, script order, the hidden
// panel, one panel copy and one stylesheet, and loadNewsPanel on both the
// /browser-data proxy and the public R2 base. M4b.1: malformed pages and render
// errors after a valid page are visible failures that drop the ready state and
// page name, and a valid page recovers. Run: node tests/news_panel_view.test.js
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');

const PANEL_FILE = path.join(__dirname, '../site/js/news-panel.js');
const panel = require(PANEL_FILE);
const VIEWS = path.join(__dirname, 'fixtures/news/views');
// G3: the panel reads published (generation-qualified) views; the committed builder fixtures are
// qualified by the tests-only helper (same gen derivation as views.qualify_generation). NOT RUN when
// these G3 edits were made.
const Q = require('./news_panel/qualify.js').loadQualified();
const q = key => Q.q(key);
const load = key => JSON.parse(JSON.stringify(Q.byKey(key)));
const clone = o => JSON.parse(JSON.stringify(o));

// --- fake DOM ------------------------------------------------------------------
class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.disabled = false; this.value = ''; this.selected = false;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type) { (this.listeners[type] || []).forEach(fn => fn()); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML must not be used: ' + v); }
  get innerHTML() { throw new Error('innerHTML must not be read'); }
  all(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.all(pred, out)); return out; }
  byClass(c) { return this.all(n => n.className.split(' ').includes(c)); }
  byTag(t) { return this.all(n => n.tagName === t.toUpperCase()); }
}
const doc = { createElement: tag => new Node(tag) };
function render(page, opts) {
  const root = new Node('div');
  panel.renderPage(doc, root, page, opts);
  return root;
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

const index = load('news/v2/views/index.json');
const pageKeys = index.views.map(v => v.first_page);
const pages = Object.fromEntries(pageKeys.map(k => [k, load(k)]));
const oil = pages[q('news/v1/views/collector/oil/2026-11-09/page-0001.json')];
const pub = pages[q('news/v1/views/publication/all/2026-11-09/page-0001.json')];

test('the index and every fixture page validate and render', () => {
  assert.deepEqual(panel.validateIndex(index), []);
  assert.equal(index.views.length, 9);
  for (const [key, page] of Object.entries(pages)) {
    assert.deepEqual(panel.validatePage(page), [], key);
    const root = render(page);
    assert.ok(root.textContent.includes(page.data_label), key + ' shows the data label');
    assert.ok(root.byClass('np-synthetic').length === 1, key);
  }
});

test('the synthetic, retrospective and unmapped labels stay visible', () => {
  const text = render(pub).textContent;
  assert.ok(text.includes('SYNTHETIC fixture data'));
  assert.ok(text.includes('RETROSPECTIVE (hindsight)'));
  assert.ok(render(oil).textContent.includes('instrument: unmapped'));
  assert.ok(!render(oil).textContent.includes('RETROSPECTIVE'));
});

test('timing, revisions, later context and stale prices are shown', () => {
  const root = render(oil);
  const text = root.textContent;
  assert.ok(text.includes('event 2026-11-09 10:05:00Z (observed_at)'));
  assert.ok(text.includes('revision 1 (1 known at event)'));
  assert.ok(text.includes('Later context (not known at event): revised 10:10:00'));
  assert.ok(root.byClass('np-price-stale').length > 0, 'a stale price is visible');
  assert.ok(text.includes('Price stale'), 'the stale price interval is listed');
  assert.ok(text.includes('missed interval 45 min'));
  assert.ok(text.includes('Since the last observed run'));
});

test('no span in the fixture is called quiet, and an empty page never is', () => {
  for (const page of Object.values(pages)) {
    assert.ok(!render(page).textContent.includes('Observed, no items'), page.mode + '/' + page.stream);
  }
  const empty = clone(oil);
  empty.rows = [];
  const text = render(empty).textContent;
  assert.ok(text.includes('No items on this page'));
  assert.ok(!text.includes('Observed, no items'));
});

test('"Observed, no items" appears exactly where the builder marked a segment quiet', () => {
  const page = clone(oil);
  page.coverage.segments[1].quiet = true;
  const root = render(page);
  const quiet = root.all(n => n.getAttribute('data-quiet') === 'true');
  assert.equal(quiet.length, 1);
  assert.equal(root.textContent.split('Observed, no items').length - 1, 1);
  // A zero row count alone is not quiet.
  const zero = clone(oil);
  zero.coverage.segments.forEach(s => { s.observations_in_span = 0; s.events_in_span = 0; s.quiet = false; });
  assert.ok(!render(zero).textContent.includes('Observed, no items'));
});

test('span counts keep new observations and row events apart', () => {
  const page = clone(oil);
  const seg = page.coverage.segments[2];
  seg.observations_in_span = 0; seg.events_in_span = 1; seg.quiet = false;
  const cell = render(page).byClass('np-seg')[2];
  assert.ok(cell.textContent.includes('0 new observed · 1 events'));
  assert.ok(!cell.textContent.includes('Observed, no items'));
  for (const s of oil.coverage.segments) {
    assert.ok(Number.isInteger(s.observations_in_span) && Number.isInteger(s.events_in_span));
  }
});

test('a retrospective classification is later context with its time, not "streams at event"', () => {
  const cards = render(pub).byClass('np-row');
  const eia = cards[pub.rows.findIndex(r => r.source_id === 'eia_today_in_energy')];
  assert.equal(pub.rows.find(r => r.source_id === 'eia_today_in_energy').classification_at_event.timing, 'retrospective');
  assert.ok(eia.textContent.includes('streams at event: not known'));
  assert.ok(eia.textContent.includes('Later context (not known at event): classified 10:12:00 (retrospective): ai_buildout, oil'));
  assert.ok(!render(pub).textContent.includes('streams at event: ai'), 'no publication row claims streams at event');
  const text = render(oil).textContent;
  assert.ok(text.includes('streams at event: oil') && !text.includes('(retrospective)'));
});

test('the source filter is page-local, labelled, and leaves coverage alone', () => {
  const full = render(oil);
  const filtered = render(oil, { sourceFilter: 'eia_today_in_energy' });
  const n = oil.rows.filter(r => r.source_id === 'eia_today_in_energy').length;
  assert.ok(n > 0 && n < oil.rows.length);
  assert.ok(filtered.textContent.includes(`${n} of ${oil.rows.length} rows on this page`));
  assert.ok(filtered.textContent.includes('Coverage above stays for the whole stream'));
  assert.equal(filtered.byClass('np-row').length, n);
  const segs = r => r.byClass('np-seg').map(s => s.textContent);
  assert.deepEqual(segs(filtered), segs(full));
  const none = render(oil, { sourceFilter: 'techcrunch' });
  assert.ok(none.textContent.includes('No rows from techcrunch on this page'));
  assert.ok(!none.textContent.includes('Observed, no items'));
});

test('the filter control re-renders through its change handler', () => {
  const root = render(oil);
  const [select] = root.byTag('select');
  select.value = 'oilprice_com';
  select.fire('change');
  const n = oil.rows.filter(r => r.source_id === 'oilprice_com').length;
  assert.ok(root.textContent.includes(`${n} of ${oil.rows.length} rows on this page`));
});

test('incomplete pages and coverage show the INCOMPLETE banner with counts', () => {
  assert.equal(render(oil).byClass('np-incomplete').length, 0);
  const page = clone(oil);
  page.complete = false; page.view.complete = false;
  page.coverage.coverage_complete = false;
  page.overflow_reasons = ['coverage_over_count_limits', 'rows_beyond_max_pages'];
  page.omitted.segments_omitted = 3; page.omitted.rows_omitted = 7; page.view.pages_omitted = 2;
  const [banner] = render(page).byClass('np-incomplete');
  assert.equal(banner.getAttribute('role'), 'alert');
  for (const s of ['INCOMPLETE', 'omitted coverage is not a quiet interval', 'rows beyond max pages',
    'segments omitted: 3', 'rows omitted: 7', 'pages omitted: 2']) {
    assert.ok(banner.textContent.includes(s), s);
  }
});

test('incomplete coverage alone still raises the banner', () => {
  const page = clone(oil);
  page.coverage.coverage_complete = false;  // even if a page claimed complete: true
  const [banner] = render(page).byClass('np-incomplete');
  assert.ok(banner && banner.textContent.includes('omitted coverage is not a quiet interval'));
});

test('navigation buttons follow prev/next keys', () => {
  const page = clone(oil);
  page.page_number = 2; page.page_count = 3;
  page.prev_page = 'news/v1/views/collector/oil/2026-11-09/page-0001.json';
  page.next_page = 'news/v1/views/collector/oil/2026-11-09/page-0003.json';
  const seen = [];
  const root = render(page, { onNavigate: k => seen.push(k) });
  const [prev, next] = root.byTag('button');
  assert.equal(prev.disabled || next.disabled, false);
  prev.fire('click'); next.fire('click');
  assert.deepEqual(seen, [page.prev_page, page.next_page]);
  assert.ok(root.textContent.includes('Page 2 of 3'));
  const [p1, n1] = render(oil).byTag('button');
  assert.equal(p1.disabled && n1.disabled, true);
});

test('render-time URL check matches the builder rules', () => {
  const good = ['https://example.com/a?b=1#c', 'http://example.com:8080/x'];
  const bad = ['javascript:alert(1)', 'data:text/html,x', 'ftp://example.com/', 'https://user:pw@example.com/',
    'https://user@example.com/', 'https://exa mple.com/', 'https://example.com/\u0000', 'https://example.com/‮',
    'http:///nohost', 'https://example.com:99999/', '', null, 42, 'https://example.com/' + 'a'.repeat(2048),
    '//example.com/x', 'https://example.com\t/x', 'HTTPS://example.com@evil.test/',
    'https://example.com\\@evil.test/', 'http:\\\\example.com/'];
  good.forEach(u => assert.equal(panel.safeUrl(u), new URL(u).href, u));
  bad.forEach(u => assert.equal(panel.safeUrl(u), null, String(u).slice(0, 40)));
});

test('links get rel="noopener noreferrer"; rejected links render as text only', () => {
  const page = clone(oil);
  page.rows[0].canonical_url = 'https://example.com/story';
  page.rows[1].canonical_url = 'javascript:alert(1)';  // a builder bypass must still be refused
  page.rows[1].url_rejected = null;
  page.rows[2].title = '<img src=x onerror=alert(1)>';
  page.rows[2].canonical_url = null; page.rows[2].url_rejected = 'scheme_not_http';
  const root = render(page);
  const links = root.byTag('a');
  assert.equal(links.length, oil.rows.filter((r, i) => i > 2 && panel.safeUrl(r.canonical_url)).length + 1);
  for (const a of links) {
    assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
    assert.ok(/^https?:\/\//.test(a.getAttribute('href')));
  }
  const cards = root.byClass('np-row');
  assert.equal(cards[1].byTag('a').length, 0);
  assert.ok(cards[1].textContent.includes('link rejected'));
  assert.ok(cards[2].textContent.includes('<img src=x onerror=alert(1)>'), 'markup stays literal text');
  assert.ok(cards[2].textContent.includes('link rejected: scheme not http'));
  assert.equal(root.all(n => n.tagName === 'IMG').length, 0);
});

test('an invalid page is refused, not partially rendered', () => {
  const root = render({ schema_version: 'other' });
  assert.ok(root.textContent.startsWith('Cannot render this page'));
  assert.equal(root.byClass('np-row').length, 0);
});

test('an index cut before its first entry still shows INCOMPLETE with its omissions', async () => {
  const body = execFileSync(process.env.PYTHON || 'python', [path.join(__dirname, 'test_news_views.py'), '--truncated-index'],
    { cwd: path.join(__dirname, '..') });
  const truncated = JSON.parse(body.toString('utf8'));
  assert.deepEqual([truncated.views.length, truncated.views_omitted, truncated.views_total, truncated.complete], [0, 9, 9, false]);
  Object.assign(truncated, { schema_version: 'news-view-index/v2', state: 'published', gen: Q.gen, logical_gen: Q.logicalGen,
    commit: { nonce: '0'.repeat(32) }, pages_sha256: '0'.repeat(64) });   // the published form names its generation
  const root = new Node('div');
  await panel.mount(doc, root, () => Promise.resolve(truncated), 'news/v2/views/index.json');
  const [banner] = root.byClass('np-incomplete');
  assert.ok(banner, 'banner present');
  assert.equal(banner.getAttribute('role'), 'alert');
  assert.ok(banner.textContent.includes('INCOMPLETE index') && banner.textContent.includes('9 of 9 views omitted (index bytes)'));
  assert.ok(root.textContent.includes('No view is listed: the index was cut before its first entry'));
  assert.ok(!root.textContent.includes('No views in this index'));
  const complete = new Node('div');
  await panel.mount(doc, complete, () => Promise.resolve(Object.assign({}, truncated, { complete: true, views_omitted: 0,
    views_total: 0, overflow_reasons: [] })), 'news/v2/views/index.json');
  assert.equal(complete.byClass('np-incomplete').length, 0);
  assert.ok(complete.textContent.includes('No views in this index'));
});

test('the latest view choice wins when responses arrive in reverse order', async () => {
  const COLL = q('news/v1/views/collector/oil/2026-11-09/page-0001.json');
  const PUB = q('news/v1/views/publication/all/2026-11-09/page-0001.json');
  const pending = {};
  const fetchJson = key => key.endsWith('index.json') || key === index.views[0].first_page
    ? Promise.resolve(load(key))
    : new Promise((resolve, reject) => { pending[key] = { resolve: () => resolve(load(key)), reject }; });
  const root = new Node('div');
  await panel.mount(doc, root, fetchJson, 'news/v2/views/index.json');
  const pick = key => { const [picker] = root.byTag('select'); picker.value = key; picker.fire('change'); };
  pick(COLL);
  pick(PUB);
  pending[PUB].resolve();
  await new Promise(r => setImmediate(r));
  assert.equal(root.getAttribute('data-page'), PUB);
  pending[COLL].resolve();  // the older request arrives last
  await new Promise(r => setImmediate(r));
  assert.equal(root.getAttribute('data-page'), PUB);
  assert.ok(root.textContent.includes('RETROSPECTIVE (hindsight)'));
  assert.ok(!root.textContent.includes('collector mode'));
  // A stale failure is ignored too; a failure of the latest choice is shown.
  pick(COLL); pick(PUB);
  pending[PUB].resolve();
  await new Promise(r => setImmediate(r));
  pending[COLL].reject(new Error('late failure'));  // the older request fails after the newer one rendered
  await new Promise(r => setImmediate(r));
  assert.ok(!root.textContent.includes('late failure') && root.getAttribute('data-page') === PUB);
  assert.ok(root.textContent.includes('RETROSPECTIVE (hindsight)'));
  pick(COLL);
  pending[COLL].reject(new Error('network down'));
  await new Promise(r => setImmediate(r));
  assert.ok(root.textContent.includes('Could not load "' + COLL + '": network down'));
  assert.equal(root.getAttribute('data-state'), 'failed');
});

test('mount loads the index, renders the first view and switches views', async () => {
  const root = new Node('div');
  const fetched = [];
  const fetchJson = key => { fetched.push(key); return Promise.resolve(load(key)); };
  await panel.mount(doc, root, fetchJson, 'news/v2/views/index.json');
  assert.equal(root.getAttribute('data-page'), index.views[0].first_page);
  const [picker] = root.byTag('select');
  picker.value = q('news/v1/views/publication/all/2026-11-09/page-0001.json');
  picker.fire('change');
  await new Promise(r => setImmediate(r));
  assert.equal(root.getAttribute('data-page'), picker.value);
  assert.ok(root.textContent.includes('RETROSPECTIVE'));
  assert.deepEqual(fetched.slice(0, 2), ['news/v2/views/index.json', index.views[0].first_page]);
  const failed = new Node('div');
  await panel.mount(doc, failed, () => Promise.resolve({ schema_version: 'x' }), 'news/v2/views/index.json');
  assert.ok(failed.textContent.includes('Could not load news views'));
});

// --- M4b: view keys, the checked fetcher, and the site wiring ---------------------
const INDEX_KEY = 'news/v2/views/index.json';
const BAD_KEYS = [
  'news/v1/views/collector/oil/2026-02-30/page-0001.json',   // not a calendar date
  'news/v1/views/collector/oil/2026-13-01/page-0001.json',
  'news/v1/views/collector/oil/2026-11-09/page-0000.json',   // pages are 0001-0020
  'news/v1/views/collector/oil/2026-11-09/page-0021.json',
  'news/v1/views/collector/oil/2026-11-09/page-001.json',
  'news/v1/views/hindsight/oil/2026-11-09/page-0001.json',   // unknown mode
  'news/v1/views/collector/gold/2026-11-09/page-0001.json',  // unknown stream
  'news/v1/views/collector/oil/2026-11-09/page-0001.json?x=1',
  'news/v1/views/collector/oil/2026-11-09/page-0001.json#x',
  'news/v1/views/collector/oil/2026-11-09/../../../index.json',
  'news/v1/views/collector/oil/2026-11-09/page-0001.JSON',
  '/news/v1/views/index.json', 'news/v1/views/', 'news/v1/views', 'news/v1/index.json',
  'news/oil/v1/latest.json', 'manifest.json', 'intraday/latest.json',
  'https://example.com/news/v1/views/index.json', '//example.com/x', '../../../secret', '',
  'news/v1/views/collector/oil/2026-11-09/page-0001.json\n', 'news/v1/views/collector/oil/２０２６-11-09/page-0001.json',
  null, undefined, 7, {},
  // G3/G6: unqualified pages are builder output, never published; the incarnation segment is exact;
  // legacy news/v1 published keys are refused.
  'news/v1/views/index.json',
  'news/v2/views/p/20261109T100500Z-0123456789abcdef/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261109T100500Z-0123456789abcdef.0/collector/oil/2026-11-09/page-0001.json',
  'news/v1/views/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261109T100500Z-0123456789ABCDEF.1/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261109T100500Z-0123456789abcde.1/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261131T100500Z-0123456789abcdef.1/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261109T240000Z-0123456789abcdef.1/collector/oil/2026-11-09/page-0001.json',
  'news/v2/views/p/20261109T100500Z-0123456789abcdef.1/collector/oil/2026-11-09/page-0021.json',
  'news/v2/views/p/20261109T100500Z-0123456789abcdef.1/index.json',
];
const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, text: async () => body });
function fixtureFetch(calls, overrides = {}) {
  return async (url, opts) => {
    calls.push({ url, opts });
    const key = url.replace(/^.*?(news\/v2\/views\/)/, '$1').replace(/\?_=\d+$/, '');
    if (key in overrides) return overrides[key]();
    const text = Q.text(key);   // the published (qualified) fixture
    return text !== null ? resp(200, text) : resp(404, 'not found');
  };
}
const tick = () => new Promise(r => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await tick(); }

test('the view-key check accepts exactly the builder key space', () => {
  // The index records the builder's modes, streams and page cap: the panel's check must match them.
  assert.deepEqual(panel.MODES, index.modes);
  assert.deepEqual(panel.STREAMS, index.streams);
  assert.equal(panel.MAX_PAGES, index.limits.pages_per_view);
  assert.ok(panel.isNewsViewKey(INDEX_KEY));
  for (const v of index.views) {
    assert.ok(panel.isNewsViewKey(v.first_page), v.first_page);
    for (let n = 1; n <= panel.MAX_PAGES; n++) {
      assert.ok(panel.isNewsViewKey(v.first_page.replace('page-0001', 'page-' + String(n).padStart(4, '0'))));
    }
  }
  for (const k of BAD_KEYS) assert.equal(panel.isNewsViewKey(k), false, JSON.stringify(k));
});

test('the fetcher checks the key before every request', async () => {
  const calls = [];
  const fetchJson = panel.makeFetcher('/browser-data/', fixtureFetch(calls), () => 1762686900123.7);
  for (const k of BAD_KEYS) {
    await assert.rejects(fetchJson(k), e => e.newsKind === 'refused' && /not a published news view key/.test(e.message));
  }
  assert.equal(calls.length, 0, 'a refused key is never requested');
  const got = await fetchJson(INDEX_KEY);
  assert.equal(got.schema_version, 'news-view-index/v2', 'the published (qualified) index');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/browser-data/news/v2/views/index.json?_=1762686900123');
  assert.equal(calls[0].opts.credentials, 'omit');
  assert.equal(calls[0].opts.cache, 'no-store', 'G3: no cached index or page');
  assert.ok(typeof AbortController !== 'function' || calls[0].opts.signal, 'G3: every request can be aborted at its deadline');
  const r2 = [];
  await panel.makeFetcher('https://pub.example.r2.dev', fixtureFetch(r2), () => 5)(index.views[0].first_page);
  assert.equal(r2[0].url, 'https://pub.example.r2.dev/' + index.views[0].first_page + '?_=5');
});

test('only a 404 on index.json means "not published"; everything else is a failure', async () => {
  const P1 = index.views[0].first_page;
  const kind = async (key, override) => {
    const f = panel.makeFetcher('/browser-data', fixtureFetch([], { [key]: override }), () => 1);
    try { await f(key); return 'ok'; } catch (e) { return e.newsKind; }
  };
  assert.equal(await kind(INDEX_KEY, () => resp(404, '')), 'absent');
  assert.equal(await kind(P1, () => resp(404, '')), 'gone');   // G3: a retired generation's page
  assert.equal(await kind(INDEX_KEY, () => resp(500, '')), 'http');
  assert.equal(await kind(INDEX_KEY, () => resp(403, '')), 'http');
  assert.equal(await kind(INDEX_KEY, () => { throw new TypeError('Failed to fetch'); }), 'network');
  assert.equal(await kind(INDEX_KEY, () => resp(200, '<html>')), 'json');
  assert.equal(await kind(P1, () => resp(200, '{"rows": [')), 'json');

  const mountWith = async override => {
    const root = new Node('div');
    await panel.mount(doc, root, panel.makeFetcher('/browser-data', fixtureFetch([], { [INDEX_KEY]: override }), () => 1),
      INDEX_KEY);
    return root;
  };
  const absent = await mountWith(() => resp(404, 'not found'));
  assert.equal(absent.getAttribute('data-state'), 'absent');
  assert.equal(absent.byClass('np-absent').length, 1);
  assert.ok(absent.textContent.includes('No news views published yet'));
  assert.ok(!absent.textContent.includes('Could not load'));
  for (const [override, text] of [[() => resp(500, ''), 'HTTP 500'], [() => resp(503, ''), 'HTTP 503'],
    [() => { throw new TypeError('Failed to fetch'); }, 'request failed (Failed to fetch)'],
    [() => resp(200, 'not json'), 'not valid JSON']]) {
    const failed = await mountWith(override);
    assert.equal(failed.getAttribute('data-state'), 'failed', text);
    assert.ok(failed.textContent.includes('Could not load news views: ') && failed.textContent.includes(text), text);
    assert.ok(!failed.textContent.includes('No news views published yet'), text);
  }
  // A missing page is never "not published": since G3 it means the generation was retired, so the
  // reader is asked to reload the index (refresh-required), never shown another generation.
  const calls = [];
  const root = new Node('div');
  await panel.mount(doc, root, panel.makeFetcher('/browser-data',
    fixtureFetch(calls, { [index.views[0].first_page]: () => resp(404, '') }), () => 1), INDEX_KEY);
  assert.equal(root.getAttribute('data-state'), 'refresh-required');
  assert.ok(root.textContent.includes('HTTP 404') && !root.textContent.includes('No news views published yet'));
  assert.ok(root.byTag('button').some(b => b.textContent === 'Reload'));
});

test('keys taken from the index, the picker and prev/next are refused before any request', async () => {
  // G3: an index whose first_page (picker option) is not a key of its own generation is refused whole,
  // before any page request; a prev/next link that leaves the generation shows refresh-required and is
  // never requested.
  const BAD = 'news/v1/views/collector/oil/2026-02-30/page-0001.json';
  for (const [label, change] of [['first_page', i => { i.views[0].first_page = BAD; }],
    ['picker option', i => { i.views[1].first_page = 'https://example.com/x.json'; }],
    ['other generation', i => { i.views[0].first_page = i.views[0].first_page.replace(Q.gen, '20990101T000000Z-0000000000000000.1'); }]]) {
    const calls = [];
    const tampered = clone(index);
    change(tampered);
    const root = new Node('div');
    await panel.mount(doc, root, panel.makeFetcher('/browser-data',
      fixtureFetch(calls, { [INDEX_KEY]: () => resp(200, JSON.stringify(tampered)) }), () => 1), INDEX_KEY);
    assert.deepEqual(calls.map(c => c.url), ['/browser-data/' + INDEX_KEY + '?_=1'], label);
    assert.equal(root.getAttribute('data-state'), 'failed', label);
    assert.ok(root.textContent.includes('Could not load news views: ') &&
      root.textContent.includes('is not in generation ' + Q.gen), label);
  }
  // prev/next from a page: each in a fresh mount (refresh-required ends that mount).
  const FIRST = index.views[0].first_page;
  for (const [label, field, key] of [['older →', 'next_page', '../../../../secret.json'],
    ['← newer', 'prev_page', FIRST.replace('page-0001', 'page-0021')],
    ['older →', 'next_page', FIRST.replace(Q.gen, '20990101T000000Z-0000000000000000.1').replace('page-0001', 'page-0002')]]) {
    const calls = [];
    const page = clone(load(FIRST));
    page[field] = key;
    const root = new Node('div');
    await panel.mount(doc, root, panel.makeFetcher('https://pub.example.r2.dev',
      fixtureFetch(calls, { [FIRST]: () => resp(200, JSON.stringify(page)) }), () => 1), INDEX_KEY);
    const btn = root.byTag('button').find(b => b.textContent === label);
    assert.ok(btn && !btn.disabled, label);
    btn.fire('click');
    await settle();
    assert.equal(calls.length, 2, label + ' ' + key + ' was not requested');
    assert.equal(root.getAttribute('data-state'), 'refresh-required', key);
    assert.ok(root.textContent.includes('left generation ' + Q.gen), key);
  }
});

test('site wiring: one hashed script between squeeze and tab switching, a hidden panel, one copy, one stylesheet', () => {
  const repo = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(repo, 'site/index.html'), 'utf8');
  const src = fs.readFileSync(PANEL_FILE);
  const hash = createHash('sha256').update(src).digest('hex').slice(0, 16);
  assert.ok(html.includes('<script src="js/news-panel.js?v=' + hash + '"></script>'),
    'news-panel script URL must match its content hash on every hosting path');
  const at = s => { const i = html.indexOf(s); assert.ok(i > 0, s); return i; };
  assert.ok(at('js/squeeze-panel.js?v=') < at('js/news-panel.js?v=') && at('js/news-panel.js?v=') < at('js/tab-switching.js?v='));
  assert.equal(html.split('<script src="js/news-panel.js').length, 2, 'exactly one script tag');
  assert.ok(html.includes('// ── news panel (MediaFlow M4b)'), 'section banner stays in index.html');
  assert.ok(/<div id="news-panel" class="hidden"[^>]*><\/div>/.test(html), 'the News panel starts hidden and empty');
  assert.ok(html.includes('<button id="tab-news" class="tab-btn">News</button>'));
  assert.ok(/#squeeze-panel\.hidden,\s*#news-panel\.hidden \{ display: none; \}/.test(html));
  // One panel copy: the harness loads the site file.
  assert.ok(!fs.existsSync(path.join(__dirname, 'news_panel/news-panel.js')), 'no tests-only copy of the panel');
  const harness = fs.readFileSync(path.join(__dirname, 'news_panel/harness.html'), 'utf8');
  assert.ok(harness.includes('<script src="../../site/js/news-panel.js"></script>'));
  assert.ok(!/\.np-/.test(harness.replace(/<!--[\s\S]*?-->/g, '')), 'the harness carries no panel CSS of its own');
  // One stylesheet, scoped: every panel rule in index.html sits under #news-panel.
  const block = /\/\* ── news panel[\s\S]*?\/\* ── end news panel ── \*\//.exec(html);
  assert.ok(block, 'marked CSS block the harness copies');
  const css = block[0].replace(/\/\*[\s\S]*?\*\//g, '').replace(/@media[^{]*\{/g, '');
  const selectors = css.split('}').map(x => x.split('{')[0].trim()).filter(Boolean).flatMap(x => x.split(','));
  assert.ok(selectors.length > 40);
  for (const sel of selectors) assert.ok(sel.trim().startsWith('#news-panel'), 'unscoped selector: ' + sel.trim());
  assert.ok(!/\.np-/.test(html.replace(block[0], '')), 'no panel rules outside the block');
});

test('loadNewsPanel mounts from NEWS_DATA on the proxy and a dedicated public News base', async () => {
  for (const base of ['/browser-data', 'https://pub-0123456789abcdef0123456789abcdef.r2.dev']) {
    const calls = [];
    const container = new Node('div');
    const sandbox = {
      // The market base is a different origin: News must never read it.
      NEWS_DATA: base, BROWSER_DATA: 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev', console, setImmediate, setTimeout, clearTimeout,
      document: Object.assign({ getElementById: id => (id === 'news-panel' ? container : null) }, doc),
      fetch: fixtureFetch(calls),
      URL, Date: class extends Date { static now() { return 1762686900000; } },
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(PANEL_FILE, 'utf8'), sandbox, { filename: 'site/js/news-panel.js' });
    assert.equal(typeof sandbox.NewsPanel.mount, 'function');
    await vm.runInContext('loadNewsPanel()', sandbox);
    assert.deepEqual(calls.map(c => c.url), [base + '/' + INDEX_KEY + '?_=1762686900000',
      base + '/' + index.views[0].first_page + '?_=1762686900000']);
    assert.ok(calls.every(c => c.opts.credentials === 'omit'));
    assert.equal(container.getAttribute('data-state'), 'ready');
    assert.ok(container.textContent.includes('SYNTHETIC fixture data'));
  }
});

test('loadNewsPanel with no News origin requests nothing and fails visibly (never a false empty state)', async () => {
  for (const sandboxBase of [{ NEWS_DATA: null }, { NEWS_DATA: '' }, {}]) {
    const calls = [];
    const container = new Node('div');
    const sandbox = Object.assign({
      BROWSER_DATA: 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev', console, setImmediate, setTimeout, clearTimeout,
      document: Object.assign({ getElementById: id => (id === 'news-panel' ? container : null) }, doc),
      fetch: fixtureFetch(calls),
      URL, Date: class extends Date { static now() { return 1762686900000; } },
    }, sandboxBase);
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(PANEL_FILE, 'utf8'), sandbox, { filename: 'site/js/news-panel.js' });
    await vm.runInContext('loadNewsPanel()', sandbox);
    assert.deepEqual(calls, [], JSON.stringify(sandboxBase));
    assert.equal(container.getAttribute('data-state'), 'failed');
    assert.ok(container.textContent.includes('no public News origin is configured'), container.textContent);
    assert.ok(!container.textContent.includes('No news views published yet'));
  }
  const fetchJson = panel.makeFetcher(null, () => { throw new Error('must not fetch'); }, () => 0);
  await assert.rejects(fetchJson(INDEX_KEY), e => e.newsKind === 'unconfigured');
});

// M4b.1: a malformed page after a valid one is a visible failure, not a blank "ready" panel.
const MALFORMED = {
  'coverage null': p => { p.coverage = null; },
  'window null': p => { p.window = null; },
  'rows null': p => { p.rows = null; },
  'wrong schema': p => { p.schema_version = 'news-view/v0'; },
  'view null': p => { p.view = null; },
  'segment null': p => { p.coverage.segments = [null]; },
  'row null': p => { p.rows = [null]; },
};
const unhandled = [];
process.on('unhandledRejection', e => { unhandled.push(String(e && e.message || e)); });

test('validatePage refuses the structures the renderer cannot draw', () => {
  for (const key of pageKeys) assert.deepEqual(panel.validatePage(pages[key]), [], key);
  for (const [name, mutate] of Object.entries(MALFORMED)) {
    const page = clone(oil);
    mutate(page);
    assert.ok(panel.validatePage(page).length > 0, name);
  }
});

test('valid page -> malformed page -> valid page on both bases: visible failure, no stale ready, recovery', async () => {
  const FIRST = index.views[0].first_page;
  const NEXT = FIRST.replace('page-0001', 'page-0002');
  for (const base of ['/browser-data', 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev']) {
    const first = clone(load(FIRST));
    first.next_page = NEXT;
    first.page_count = 2;
    let next = null;
    const calls = [];
    const fetchJson = panel.makeFetcher(base, fixtureFetch(calls, {
      [FIRST]: () => resp(200, JSON.stringify(first)),
      [NEXT]: () => resp(200, JSON.stringify(next)),
    }), () => 1);
    const root = new Node('div');
    await panel.mount(doc, root, fetchJson, INDEX_KEY);
    assert.equal(root.getAttribute('data-state'), 'ready');
    for (const [name, mutate] of Object.entries(MALFORMED)) {
      next = clone(load(FIRST));
      Object.assign(next, { page_number: 2, page_count: 2, prev_page: FIRST, next_page: null });
      mutate(next);
      const btn = root.byTag('button').find(b => b.textContent === 'older →');
      assert.ok(btn && !btn.disabled, name);
      btn.fire('click');
      await settle();
      const label = `${base} ${name}`;
      assert.equal(root.getAttribute('data-state'), 'failed', label);
      assert.equal(root.getAttribute('data-page'), null, label + ': no stale page name');
      assert.equal(root.getAttribute('data-pending'), null, label);
      const shown = root.byClass('np-incomplete').map(n => n.textContent).join(' ');
      assert.ok(shown.includes('Could not load "' + NEXT + '": not a valid news view page ('), label + ': ' + shown);
      assert.equal(root.byClass('np-head').length, 0, label + ': the previous page is not left on screen');
      // Recovery: the picker still works and a valid page renders as ready.
      const [picker] = root.byTag('select');
      picker.value = FIRST;
      picker.fire('change');
      await settle();
      assert.equal(root.getAttribute('data-state'), 'ready', label + ': recovers');
      assert.equal(root.getAttribute('data-page'), FIRST, label);
      assert.equal(root.byClass('np-incomplete').length, 0, label + ': failure cleared on recovery');
    }
  }
  assert.deepEqual(unhandled, [], 'no unhandled rejection');
});

test('a render failure that validation does not catch still takes the failure path', async () => {
  const FIRST = index.views[0].first_page;
  const NEXT = FIRST.replace('page-0001', 'page-0002');
  const first = clone(load(FIRST));
  first.next_page = NEXT;
  const bad = clone(load(FIRST));
  // Valid structure, but a getter that throws while drawing (stands in for any renderer bug).
  const fetchJson = key => key === NEXT
    ? Promise.resolve(Object.defineProperty(clone(bad), 'hindsight', { get() { throw new Error('boom'); } }))
    : Promise.resolve(key === FIRST ? first : load(key));
  const root = new Node('div');
  await panel.mount(doc, root, fetchJson, INDEX_KEY);
  root.byTag('button').find(b => b.textContent === 'older →').fire('click');
  await settle();
  assert.equal(root.getAttribute('data-state'), 'failed');
  assert.equal(root.getAttribute('data-page'), null);
  assert.ok(root.textContent.includes('Could not load "' + NEXT + '": the page could not be rendered (boom)'));
  assert.deepEqual(unhandled, []);
});

test('newest selection still wins when the older response is malformed', async () => {
  const COLL = q('news/v1/views/collector/oil/2026-11-09/page-0001.json');
  const PUB = q('news/v1/views/publication/all/2026-11-09/page-0001.json');
  const pending = {};
  const fetchJson = key => key.endsWith('index.json') || key === index.views[0].first_page
    ? Promise.resolve(load(key))
    : new Promise(resolve => { pending[key] = resolve; });
  const root = new Node('div');
  await panel.mount(doc, root, fetchJson, INDEX_KEY);
  const pick = key => { const [picker] = root.byTag('select'); picker.value = key; picker.fire('change'); };
  pick(COLL); pick(PUB);
  pending[PUB](load(PUB));
  await settle();
  pending[COLL](Object.assign(clone(load(COLL)), { coverage: null }));  // older and malformed, arrives last
  await settle();
  assert.equal(root.getAttribute('data-state'), 'ready');
  assert.equal(root.getAttribute('data-page'), PUB);
  assert.ok(root.textContent.includes('RETROSPECTIVE (hindsight)') && !root.textContent.includes('Could not load'));
  // The newest choice malformed: shown as a failure.
  pick(COLL);
  pending[COLL](Object.assign(clone(load(COLL)), { window: null }));
  await settle();
  assert.equal(root.getAttribute('data-state'), 'failed');
  assert.equal(root.getAttribute('data-page'), null);
  assert.deepEqual(unhandled, []);
});

(async () => {
  for (const [name, fn] of tests) {
    await fn();
    console.log('ok -', name);
  }
  console.log(`
${tests.length} news panel view tests passed`);
})().catch(e => { console.error(e); process.exit(1); });
