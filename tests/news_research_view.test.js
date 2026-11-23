// P1: site/js/news-research.js rendering the research projections built by
// tests/news_research_scenarios.py (prepared records A-F and the SYNTHETIC timeline) in a minimal fake DOM
// whose innerHTML throws, so any markup path fails. Checks each stage's required and forbidden text,
// failure states for malformed views, and that only http(s) links become anchors.
// Run: node tests/news_research_view.test.js
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const research = require(path.join(__dirname, '../site/js/news-research.js'));

class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.className = ''; this._text = ''; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this.listeners = this.listeners || {})[type] = fn; }
  click() { if (this.listeners && this.listeners.click) this.listeners.click(); }
  insertBefore(c, ref) {
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    c.parent = this; return c;
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set innerHTML(v) { throw new Error('innerHTML must not be used: ' + v); }
  get innerHTML() { throw new Error('innerHTML must not be read'); }
  all(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.all(pred, out)); return out; }
}
function makeDoc() {
  const head = new Node('head');
  return { head, createElement: t => new Node(t), getElementById: id => head.all(n => n.id === id)[0] || null };
}

const out = fs.mkdtempSync(path.join(os.tmpdir(), 'news-research-'));
execFileSync(process.env.PYTHON || 'python', [path.join(__dirname, 'news_research_scenarios.py'), out],
  { stdio: ['ignore', 'pipe', 'inherit'] });
const stages = JSON.parse(fs.readFileSync(path.join(out, 'stages.json'), 'utf8'));
const load = name => JSON.parse(fs.readFileSync(path.join(out, name), 'utf8'));

let n = 0;
function test(name, fn) { fn(); n++; console.log('ok - ' + name); }

for (const s of stages) {
  test(`stage ${s.stage}: ${s.mode} @ ${s.cutoff}`, () => {
    const doc = makeDoc(); const root = new Node('div');
    assert.equal(research.render(doc, root, load(s.file)), 'ready');
    const text = root.textContent;
    for (const m of s.must) assert.ok(text.includes(m), `${s.stage}: missing "${m}"`);
    for (const m of s.must_not) assert.ok(!text.includes(m), `${s.stage}: must not show "${m}"`);
    assert.equal(root.getAttribute('data-items'), String(s.items));
    for (const a of root.all(x => x.tagName === 'A')) {
      assert.ok(/^https?:\/\//.test(a.href), `${s.stage}: anchor ${a.href}`);
      assert.equal(a.rel, 'noopener noreferrer');
    }
    assert.equal(doc.head.all(x => x.id === 'news-research-style').length, 1, 'one style block');
  });
}

test('every time line names its kind: publication / EDGAR acceptance / access / capture commit / annotation commit', () => {
  const root = new Node('div');
  research.render(makeDoc(), root, load('synthetic-edgar.json'));
  const box = root.all(x => x.getAttribute && x.getAttribute('data-kind') === 'research_capture')
    .find(b => b.textContent.includes('13F'));
  const t = box.textContent;
  for (const m of ['EDGAR acceptance 2026-11-16T21:05:12Z', 'accessed 2026-11-17T14:00:00Z', 'capture committed 2026-11-17T14:30:05Z',
    'no collector known-at', 'made 2026-11-17T14:30:00Z', 'committed 2026-11-17T14:30:05Z', 'measured 2026-09-30',
    'filing_due 2026-11-16 (date precision) (not a measurement time)']) {
    assert.ok(t.includes(m), m);
  }
});

test('prepared records keep evidence, status, context and unread hops distinct', () => {
  const root = new Node('div');
  research.render(makeDoc(), root, load('prepared-research.json'));
  const t = root.textContent;
  for (const m of ['bot-blocked 2026-11-12T16:59:58Z', 'HTTP 401 (login)', 'login required', 'not attempted', 'not available to us',
    'redemption', 'unverified', 'offered', 'Context (stated figures, not pressure quantities)',
    'flagship fund AUM (context; never multiplied or added)', 'measured unknown', 'not captured',
    'upper bound: write time of the design note', 'published 2026-11-06 (date precision)',
    'published 2026-11-09T10:30-05:00 (minute precision)', 'PREPARED']) {
    assert.ok(t.includes(m), m);
  }
  const e = root.all(x => x.getAttribute && x.getAttribute('data-lineage') === 'E')[0].textContent;
  assert.ok(e.includes('RETROSPECTIVE hypothesis') && e.includes('thesis assertion'));
  const d = root.all(x => x.getAttribute && x.getAttribute('data-lineage') === 'D')[0].textContent;
  assert.ok(d.includes('CONTEMPORANEOUS hypothesis') && d.includes('bid_deadline 2026-11-19'));
});

test('malformed views are failures, not empty pages', () => {
  const good = load('prepared-research.json');
  const bad = [null, {}, { ...good, schema_version: 'x' }, { ...good, mode: 'live' }, { ...good, items: null },
    { ...good, items: [{ source: {}, annotations: [{}] }] }];
  for (const v of bad) {
    const root = new Node('div');
    assert.equal(research.render(makeDoc(), root, v), 'failed');
    assert.equal(root.getAttribute('data-state'), 'failed');
    assert.ok(root.textContent.includes('Could not show research records'));
  }
});

test('only http(s) URLs become links', () => {
  assert.equal(research.safeUrl('javascript:alert(1)'), null);
  assert.equal(research.safeUrl('data:text/html,x'), null);
  assert.equal(research.safeUrl('//evil.example'), null);
  assert.equal(research.safeUrl('https://example.test/a'), 'https://example.test/a');
  const v = load('synthetic-capture-only.json');
  v.items[0].source.url = 'javascript:alert(1)';
  const root = new Node('div');
  research.render(makeDoc(), root, v);
  assert.equal(root.all(x => x.tagName === 'A').length, 0);
  assert.ok(root.textContent.includes('SYNTHETIC: fund reported cutting gross exposure'));
});

// --- R1 wiring: the research view follows the News panel's index ------------------------------------
const panel = require(path.join(__dirname, '../site/js/news-panel.js'));
const tick = () => new Promise(r => setImmediate(r));
const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, text: async () => body });
const INC = '20261113T190000Z-0123456789abcdef.1';
const BUILT = '2026-11-13T19:00:00Z';
const PAGE = `news/v2/views/p/${INC}/research/view.json`;

async function asyncTests() {
  const good = JSON.parse(fs.readFileSync(path.join(out, 'prepared-research.json'), 'utf8'));
  // The published form of a projection: the builder adds gen, build_as_of and the fixed cutoff.
  const page = Object.assign({}, good, { gen: INC, build_as_of: BUILT, cutoff: BUILT, mode: 'research', page_kind: 'research' });
  const entry = (over) => Object.assign({ schema_version: 'news-research-view/v1', mode: 'research', cutoff: BUILT, fixed: true,
    runs: 1, unverified_runs: 0, state: 'published', page: PAGE, items: page.items.length }, over || {});
  const index = (research) => ({ gen: INC, build_as_of: BUILT, views: [], research });
  function setup(respond, opts) {
    const calls = [];
    const fetchJson = panel.makeFetcher('/browser-data/', async (url, o) => { calls.push({ url, o }); return respond(url); },
      () => 7, (opts && opts.deadline) || 20000, opts && opts.timers);
    const root = new Node('div');
    const onIndex = research.follow(makeDoc(), root);
    return { calls, fetchJson, root, onIndex };
  }

  {
    const t = setup(() => resp(200, JSON.stringify(page)));
    assert.equal(t.root.getAttribute('data-state'), 'loading');
    const got = await t.onIndex({ state: 'published', index: index(entry()), fetchJson: t.fetchJson, leaseValid: () => true });
    assert.equal(got, 'ready');
    assert.deepEqual(t.calls.map(c => c.url), [`/browser-data/${PAGE}?_=7`], 'exactly the page the index names');
    assert.equal(t.calls[0].o.cache, 'no-store');
    assert.equal(t.calls[0].o.credentials, 'omit');
    assert.equal(t.root.getAttribute('data-page'), PAGE);
    assert.ok(t.root.textContent.includes(research.FIXED_NOTE), 'the fixed-view limitation is shown');
    assert.ok(t.root.textContent.includes('Cutoff ' + BUILT));
    n++; console.log('ok - follow: the index research page is fetched through the panel fetcher and rendered with the fixed-view note');
  }
  for (const [what, ev, state, text] of [
    ['no research entry', { state: 'published', index: index(undefined) }, 'absent', 'No research view published'],
    ['state none', { state: 'published', index: index(entry({ state: 'none', page: null, items: 0, runs: 0 })) }, 'absent', 'no verified research run'],
    ['state none with unverified runs', { state: 'published', index: index(entry({ state: 'none', page: null, items: 0, runs: 0, unverified_runs: 2 })) }, 'absent', '2 research run(s) failed verification'],
    ['omitted', { state: 'published', index: index(entry({ state: 'omitted', page: null, reason: 'research_view_bytes', bytes: 300000 })) }, 'omitted', 'research_view_bytes, 300000 bytes'],
    ['page in another incarnation', { state: 'published', index: index(entry({ page: PAGE.replace('.1/', '.2/') })) }, 'failed', 'not in generation'],
    ['not the build cutoff', { state: 'published', index: index(entry({ cutoff: '2026-11-13T18:00:00Z' })) }, 'failed', 'not the build cutoff'],
    ['not the fixed mode', { state: 'published', index: index(entry({ mode: 'publication' })) }, 'failed', 'not the fixed research view'],
    ['withdrawn tombstone', { state: 'withdrawn' }, 'withdrawn', 'Research records are not currently published'],
    ['unpublished tombstone', { state: 'unpublished' }, 'absent', 'No research view published'],
    ['index 404', { state: 'absent' }, 'absent', 'No research view published'],
    ['index failed', { state: 'failed' }, 'unavailable', 'news index could not be loaded'],
    ['panel refresh', { state: 'refresh', reason: 'the index lease ended' }, 'refresh-required', 'Use the Reload button in the News view above'],
  ]) {
    const t = setup(() => { throw new Error('must not fetch'); });
    await t.onIndex(Object.assign({ fetchJson: t.fetchJson, leaseValid: () => true }, ev));
    assert.equal(t.root.getAttribute('data-state'), state, what);
    assert.ok(t.root.textContent.includes(text), `${what}: ${t.root.textContent}`);
    assert.equal(t.calls.length, 0, `${what}: nothing requested`);
    n++; console.log(`ok - follow: ${what} -> ${state}, no request`);
  }
  for (const [what, respond, lease, state, text] of [
    ['page 404 (gone)', () => resp(404, 'nf'), true, 'refresh-required', 'no longer published'],
    ['page 500', () => resp(500, 'x'), true, 'failed', 'HTTP 500'],
    ['bad json', () => resp(200, '{'), true, 'failed', 'not valid JSON'],
    ['page from another incarnation', () => resp(200, JSON.stringify(Object.assign({}, page, { gen: INC.replace('.1', '.2') }))), true, 'refresh-required', 'not from generation'],
    ['page at another cutoff', () => resp(200, JSON.stringify(Object.assign({}, page, { cutoff: '2026-11-13T18:00:00Z' }))), true, 'refresh-required', 'not from generation'],
    ['lease ended while loading', () => resp(200, JSON.stringify(page)), false, 'refresh-required', 'lease ended'],
    ['wrong schema', () => resp(200, JSON.stringify(Object.assign({}, page, { schema_version: 'x' }))), true, 'failed', 'Could not show research records'],
  ]) {
    const t = setup(respond);
    let reloads = 0;
    await t.onIndex({ state: 'published', index: index(entry()), fetchJson: t.fetchJson, leaseValid: () => lease,
      reload: () => { reloads++; } });
    assert.equal(t.root.getAttribute('data-state'), state, what);
    assert.ok(t.root.textContent.includes(text), `${what}: ${t.root.textContent}`);
    const buttons = t.root.all(x => x.tagName === 'BUTTON');
    if (state === 'refresh-required') {
      // Chej 11-13: actionable recovery even while the news views stay ready: the shared-index reload.
      assert.equal(buttons.length, 1, `${what}: one reload control`);
      assert.equal(buttons[0].getAttribute('data-action'), 'reload-shared-index');
      assert.ok(!t.root.textContent.includes('Reload the News view above'), `${what}: no instruction to a control that may not exist`);
      buttons[0].click();
      buttons[0].click();
      assert.equal(reloads, 1, `${what}: the button reloads the shared index exactly once`);
      assert.equal(buttons[0].disabled, true, `${what}: the control is disabled at once`);
      assert.equal(t.root.getAttribute('data-state'), 'loading', `${what}: the control is cleared while loading`);
      assert.equal(t.root.all(x => x.tagName === 'BUTTON').length, 0, `${what}: no live control while loading`);
    } else {
      assert.equal(buttons.length, 0, `${what}: no reload control`);
    }
    n++; console.log(`ok - follow: ${what} -> ${state}${state === 'refresh-required' ? ' (shared-index reload control)' : ''}`);
  }
  {
    let fire = null;
    const timers = { set: f => { fire = f; return 1; }, clear: () => {} };
    const t = setup(() => new Promise(() => {}), { timers });
    const p = t.onIndex({ state: 'published', index: index(entry()), fetchJson: t.fetchJson, leaseValid: () => true });
    await tick(); fire();
    assert.equal(await p, 'refresh-required');
    assert.ok(t.root.textContent.includes('no complete response within 20 s'));
    assert.ok(t.root.textContent.includes('Reload the page to see the current publication'),
      'without a shared reload the wording points at a browser reload');
    n++; console.log('ok - follow: a research request past its deadline asks for a refresh');
  }
  {
    // A late answer for an earlier index never replaces what a later event showed.
    let release = null;
    const t = setup(() => new Promise(r => { release = () => r(resp(200, JSON.stringify(page))); }));
    const first = t.onIndex({ state: 'published', index: index(entry()), fetchJson: t.fetchJson, leaseValid: () => true });
    await tick();
    t.onIndex({ state: 'refresh', reason: 'replaced' });
    release();
    assert.equal(await first, null);
    assert.equal(t.root.getAttribute('data-state'), 'refresh-required');
    n++; console.log('ok - follow: a stale research response never renders over a later state');
  }
  {
    assert.equal(panel.isNewsViewKey(PAGE), true);
    assert.equal(panel.researchGenOf(PAGE), INC);
    assert.equal(panel.genOf(PAGE), null, 'a research key never stands in for a view page');
    for (const k of ['news/v2/research/view.json', `news/v2/views/p/${INC}/research/other.json`,
      'news/v1/research/runs/2026-11-13/x/manifest.json', `news/v2/views/p/${INC.replace('.1', '.0')}/research/view.json`]) {
      assert.equal(panel.isNewsViewKey(k), false, k);
    }
    n++; console.log('ok - panel key rule: the research page of an incarnation only; the retired key and the ledger are refused');
  }
  {
    const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
    const { createHash } = require('node:crypto');
    const hash = f => createHash('sha256').update(fs.readFileSync(path.join(__dirname, '../site/js', f))).digest('hex').slice(0, 16);
    assert.ok(html.includes('<script src="js/news-research.js?v=' + hash('news-research.js') + '"></script>'), 'research stamp');
    assert.ok(html.includes('<script src="js/news-panel.js?v=' + hash('news-panel.js') + '"></script>'), 'panel stamp');
    const at = s => html.indexOf(s);
    assert.ok(at('js/news-research.js?v=') > 0 && at('js/news-research.js?v=') < at('js/news-panel.js?v='), 'loaded before the panel');
    assert.equal((html.match(/id="news-research"/g) || []).length, 1, 'one research container');
    const between = html.slice(at('id="news-panel"'), at('id="news-research"'));
    assert.ok(at('id="news-panel"') < at('id="news-research"') && between.split('\n').length <= 2,
      'the research container directly follows #news-panel (the sibling rule hides it with the tab)');
    assert.ok(html.includes('#news-panel.hidden + #news-research { display: none; }'));
    assert.ok(!/news\/v[12]\/research/.test(html), 'the page names neither the ledger nor the retired key');
    const panelSrc = fs.readFileSync(path.join(__dirname, '../site/js/news-panel.js'), 'utf8');
    assert.ok(/function loadNewsPanel\(\) \{[\s\S]{0,400}NewsResearch\.follow\(document, box\)[\s\S]{0,400}onIndex: onIndex/.test(panelSrc));
    assert.ok(!/news\/v[12]\/research/.test(panelSrc));
    const researchSrc = fs.readFileSync(path.join(__dirname, '../site/js/news-research.js'), 'utf8');
    assert.ok(!/fetch\(/.test(researchSrc), 'the research module makes no request of its own');
    assert.ok(!/loadNewsResearch/.test(researchSrc + panelSrc), 'the standalone research loader is gone');
    n++; console.log('ok - site wiring: stamps, order, one container after #news-panel, follows the panel index, no own key');
  }
}

asyncTests().then(() => console.log(`${n} news research view tests passed`))
  .catch(e => { console.error(e); process.exit(1); });
