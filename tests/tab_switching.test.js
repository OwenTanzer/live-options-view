// B2 (readability quest): the tab-switching section moved out of index.html
// into site/js/tab-switching.js, loaded in the same position. The test pins
// the ?v= hash in the page to the module's content (like shared.js), asserts
// the section no longer lives inline, and drives showTab with a stub DOM:
// panel visibility, button active state, the one-shot history load, the bots
// polling toggle, the one-shot News panel mount (M4b), and the unknown-tab
// fallback to live.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/tab-switching.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/tab-switching.js?v=' + moduleHash), 'tab-switching script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function showTab'), 'showTab must live in js/tab-switching.js, not inline in index.html');
assert.ok(!html.includes('const TABS = {'), 'TABS must live in js/tab-switching.js, not inline in index.html');
assert.ok(html.includes('// ── tab switching'), 'tab-switching banner must stay in index.html as the section marker');

// --- stub DOM: panels + tab buttons with a recording classList. ---
function makeEl() {
  const classes = new Set();
  return {
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    _classes: classes,
  };
}
const els = {};
for (const id of ['live-panel', 'history-panel', 'bots-panel', 'squeeze-panel', 'news-panel',
                  'tab-live', 'tab-history', 'tab-bots', 'tab-squeeze', 'tab-news']) {
  els[id] = makeEl();
}
// The live tab starts visible / active in the shipped markup.
els['tab-live']._classes.add('active');
let manifestCalls = 0;
let newsLoads = 0;
let pollingCalls = [];
const sandbox = {
  console,
  document: { getElementById: id => els[id] },
  loadHistoryManifest: () => { manifestCalls++; },
  loadNewsPanel: () => { newsLoads++; },
  setBotsPolling: on => { pollingCalls.push(on); },
};
const context = vm.createContext(sandbox);
vm.runInContext(moduleSource, context, { filename: 'site/js/tab-switching.js' });
const get = name => vm.runInContext(name, context);

assert.equal(typeof get('showTab'), 'function', 'module must define showTab');
assert.deepEqual(Object.keys(get('TABS')).sort(), ['bots', 'history', 'live', 'news', 'squeeze'], 'TABS must map the five tabs');

const visible = id => !els[id]._classes.has('hidden');
const active = id => els[id]._classes.has('active');
const onlyVisible = id => ['live-panel', 'history-panel', 'bots-panel', 'squeeze-panel', 'news-panel'].every(p => visible(p) === (p === id));
const onlyActive = id => ['tab-live', 'tab-history', 'tab-bots', 'tab-squeeze', 'tab-news'].every(b => active(b) === (b === id));

// Live: the default tab. No manifest fetch, bots polling off.
vm.runInContext("showTab('live')", context);
assert.ok(onlyVisible('live-panel'), 'live tab shows only the live panel');
assert.ok(onlyActive('tab-live'), 'live tab marks only the live button active');
assert.equal(manifestCalls, 0, 'live tab must not load the history manifest');
assert.deepEqual(pollingCalls, [false], 'live tab turns bots polling off');

// History: loads the manifest exactly once, then the guard holds.
vm.runInContext("showTab('history')", context);
assert.ok(onlyVisible('history-panel'), 'history tab shows only the history panel');
assert.ok(onlyActive('tab-history'), 'history tab marks only the history button active');
assert.equal(manifestCalls, 1, 'first history visit loads the manifest');
assert.deepEqual(pollingCalls, [false, false], 'history tab keeps bots polling off');
vm.runInContext("showTab('history')", context);
assert.equal(manifestCalls, 1, 'reopening history must not reload the manifest');
assert.deepEqual(pollingCalls, [false, false, false], 'reopening history still keeps bots polling off');

// Bots: polling on while the tab is on screen.
vm.runInContext("showTab('bots')", context);
assert.ok(onlyVisible('bots-panel'), 'bots tab shows only the bots panel');
assert.ok(onlyActive('tab-bots'), 'bots tab marks only the bots button active');
assert.deepEqual(pollingCalls, [false, false, false, true], 'bots tab turns bots polling on');

// Squeeze: polling back off.
vm.runInContext("showTab('squeeze')", context);
assert.ok(onlyVisible('squeeze-panel'), 'squeeze tab shows only the squeeze panel');
assert.ok(onlyActive('tab-squeeze'), 'squeeze tab marks only the squeeze button active');
assert.deepEqual(pollingCalls, [false, false, false, true, false], 'leaving bots turns polling off');

// News: mounts the panel exactly once, and keeps bots polling off.
assert.equal(newsLoads, 0, 'no News request before the tab is opened');
vm.runInContext("showTab('news')", context);
assert.ok(onlyVisible('news-panel'), 'news tab shows only the news panel');
assert.ok(onlyActive('tab-news'), 'news tab marks only the news button active');
assert.equal(newsLoads, 1, 'first news visit mounts the panel');
assert.deepEqual(pollingCalls, [false, false, false, true, false, false], 'news tab keeps bots polling off');
vm.runInContext("showTab('live')", context);
vm.runInContext("showTab('news')", context);
assert.equal(newsLoads, 1, 'reopening news must not mount the panel again');
assert.equal(manifestCalls, 1, 'news never loads the history manifest');
pollingCalls = pollingCalls.slice(0, 5);

// Unknown tab falls back to live — and does not re-trigger the manifest.
vm.runInContext("showTab('bogus')", context);
assert.ok(onlyVisible('live-panel'), 'unknown tab falls back to the live panel');
assert.ok(onlyActive('tab-live'), 'unknown tab falls back to the live button');
assert.equal(manifestCalls, 1, 'fallback to live must not reload the manifest');
assert.equal(newsLoads, 1, 'fallback to live must not mount the news panel');
assert.deepEqual(pollingCalls, [false, false, false, true, false, false], 'fallback to live turns bots polling off');
console.log('tab switching: hash pinned, extraction verified, live/history/bots/squeeze/news/unknown passed');
