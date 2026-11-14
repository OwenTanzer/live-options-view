// B2 (readability quest): the boot section moved out of index.html into
// site/js/boot.js, loaded in the same position — the last inline section
// (only the stale-page protection stays inline, because BUILD_ID must live
// in index.html itself). The test pins the ?v= hash in the page to the
// module's content (like shared.js), asserts the section no longer lives
// inline, and drives boot in a vm context with stubbed cross-section
// functions: the cached-session shortcut, the auth form submit / toggle /
// logout handlers, the tab + history wiring, the fetch phase, the poller
// setup, and the five intervals it arms. Boot is an async IIFE, so loading
// the module runs it — the harness awaits a tick before asserting.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/boot.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/boot.js?v=' + moduleHash), 'boot script URL must match its content hash on every hosting path');
assert.ok(!html.includes('(async function boot()'), 'boot must live in js/boot.js, not inline in index.html');
assert.ok(!html.includes('renderChainTitle(chainSelection.symbol)'), 'boot body must live in js/boot.js, not inline in index.html');
assert.ok(html.includes('// ── boot'), 'boot banner must stay in index.html as the section marker');

// --- harness: stub DOM + every cross-section function boot calls. ---
function makeEl(id) {
  return {
    id,
    value: '',
    textContent: '',
    disabled: false,
    onchange: null,
    onsubmit: null,
    onclick: null,
    reset() { this._resetCalls = (this._resetCalls || 0) + 1; },
  };
}
const EL_IDS = ['underlying-select', 'auth-form', 'auth-username', 'auth-password',
  'auth-error', 'auth-submit', 'auth-toggle', 'auth-logout', 'margin-call-continue',
  'tab-live', 'tab-history', 'tab-bots', 'tab-squeeze', 'tab-news', 'hist-date'];

function runBoot({ cachedSession = null, submitAuthImpl = null } = {}) {
  const els = {};
  for (const id of EL_IDS) els[id] = makeEl(id);
  const calls = {
    initPriceStrip: 0, renderChainTitle: [], selectUnderlying: [], renderAuthGate: 0,
    renderPaper: 0, submitAuth: [], cacheSessionDisplay: 0, updateQuoteInterests: 0,
    renderSelectedChain: 0, hideMarginCallScreen: 0, showTab: [], loadHistoryDate: [],
    setStatus: [], loadRanges: 0, fetchLatest: 0, fetchPrices: 0, fetchSpyHealth: 0,
    refreshSessionFromServer: 0, renderQuoteHealth: 0, renderTransportHealth: [],
    startCountdown: 0, fetchSteoCalibration: 0, fetchSqueezeLatest: 0, startQuotePanelRefresh: 0,
    updatePriceStrip: [], intervals: [], fetches: [], localStorageRemoves: [],
  };
  const pollers = [];
  class LiveQuotePoller {
    constructor(source, opts) { this.source = source; this.opts = opts; this.started = false; this.healthFns = []; pollers.push(this); }
    subscribeHealth(fn) { this.healthFns.push(fn); }
    start() { this.started = true; }
  }
  let tickerSubscribeCb = null;
  const sandbox = {
    console,
    document: { getElementById: id => els[id] },
    localStorage: { getItem: () => null, removeItem: key => { calls.localStorageRemoves.push(key); } },
    fetch: async (url, options) => { calls.fetches.push({ url, options }); return { ok: true }; },
    setInterval: (fn, ms) => { calls.intervals.push({ fn, ms }); return calls.intervals.length; },
    chainSelection: { symbol: 'QQQ' },
    session: null,
    authMode: 'login',
    liveQuotes: { _liveQuotes: true },
    liveQuotePoller: null,
    tickerPoller: null,
    ranges: null,
    SQUEEZE_POLL_MS: 300_000,
    SESSION_CACHE_KEY: 'qqq-session-display-v1',
    SHARE_QUOTE_MAX_AGE_MS: 90_000,
    LiveQuotePoller,
    initPriceStrip: () => { calls.initPriceStrip++; },
    startQuotePanelRefresh: () => { calls.startQuotePanelRefresh++; },
    renderChainTitle: symbol => { calls.renderChainTitle.push(symbol); },
    selectUnderlying: value => { calls.selectUnderlying.push(value); },
    readSessionDisplayCache: () => cachedSession,
    renderAuthGate: () => { calls.renderAuthGate++; },
    renderPaper: () => { calls.renderPaper++; },
    submitAuth: submitAuthImpl || (async (u, p) => { calls.submitAuth.push([u, p]); return { user: u }; }),
    cacheSessionDisplay: () => { calls.cacheSessionDisplay++; },
    updateQuoteInterests: () => { calls.updateQuoteInterests++; },
    renderSelectedChain: () => { calls.renderSelectedChain++; },
    hideMarginCallScreen: () => { calls.hideMarginCallScreen++; },
    showTab: tab => { calls.showTab.push(tab); },
    loadHistoryDate: value => { calls.loadHistoryDate.push(value); },
    setStatus: (a, b) => { calls.setStatus.push([a, b]); },
    loadRanges: async () => { calls.loadRanges++; sandbox.ranges = { '0DTE_Regular': {} }; },
    fetchLatest: async () => { calls.fetchLatest++; },
    fetchPrices: async () => { calls.fetchPrices++; },
    fetchSpyHealth: async () => { calls.fetchSpyHealth++; },
    refreshSessionFromServer: async () => { calls.refreshSessionFromServer++; },
    renderQuoteHealth: () => { calls.renderQuoteHealth++; },
    renderTransportHealth: (...args) => { calls.renderTransportHealth.push(args); },
    tickerState: {
      subscribe: cb => { tickerSubscribeCb = cb; },
      snapshot: () => ({ QQQ: 600.1 }),
    },
    updatePriceStrip: snap => { calls.updatePriceStrip.push(snap); },
    startCountdown: () => { calls.startCountdown++; },
    fetchSteoCalibration: () => { calls.fetchSteoCalibration++; },
    fetchSqueezeLatest: () => { calls.fetchSqueezeLatest++; },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(moduleSource, context, { filename: 'site/js/boot.js' });
  const flush = () => new Promise(r => setTimeout(r, 20));
  return { els, calls, pollers, sandbox, context, flush, getTickerSubscribeCb: () => tickerSubscribeCb };
}

(async () => {
  // No cached session: the instant-render shortcut is skipped, the fetch
  // phase runs, then the pollers start and the intervals arm.
  const boot = runBoot();
  await boot.flush();
  assert.equal(boot.calls.initPriceStrip, 1, 'boot must init the price strip first');
  assert.deepEqual(boot.calls.renderChainTitle, ['QQQ'], 'boot must title the chain for the selected underlying');
  assert.equal(boot.calls.renderPaper, 0, 'no cached session: nothing to render before hydration');
  assert.equal(boot.calls.loadRanges, 1, 'boot must load calibration data before the snapshot');
  assert.deepEqual(boot.calls.setStatus.map(s => s[1]), ['Loading calibration data…', 'Fetching live snapshot…'], 'boot must narrate the two phases');
  assert.equal(boot.calls.fetchLatest, 1, 'boot must fetch the live snapshot');
  assert.equal(boot.calls.fetchPrices, 1, 'boot must fetch prices');
  assert.equal(boot.calls.fetchSpyHealth, 1, 'boot must fetch SPY health');
  assert.equal(boot.calls.refreshSessionFromServer, 1, 'boot must hydrate the session from the server');
  assert.equal(boot.calls.renderSelectedChain, 1, 'boot must render the chain after hydration');
  assert.equal(boot.pollers.length, 2, 'boot must build two pollers (live quotes + ticker state)');
  assert.equal(boot.pollers[0].source, boot.sandbox.liveQuotes, 'first poller watches liveQuotes');
  assert.ok(boot.pollers.every(p => p.started), 'both pollers must start');
  assert.equal(boot.pollers[0].healthFns.length, 1, 'live poller subscribes renderQuoteHealth');
  assert.equal(boot.pollers[1].opts.maxQuoteAgeMs, 90_000, 'ticker poller gets the share-quote max age');
  assert.equal(boot.calls.startCountdown, 1, 'boot must start the 60s refresh cycle');
  assert.equal(boot.calls.fetchSteoCalibration, 1, 'boot must fetch STEO once at startup');
  assert.equal(boot.calls.fetchSqueezeLatest, 1, 'boot must fetch the squeeze panel once at startup');
  assert.equal(boot.calls.startQuotePanelRefresh, 1, 'boot must start the single panel refresh controller');
  assert.deepEqual(boot.calls.intervals.map(i => i.ms), [15_000, 60_000, 300_000, 60_000],
    'boot must arm fetchSpyHealth / STEO / squeeze / ranges-retry intervals; the panel owns prices');
  assert.deepEqual(boot.calls.updatePriceStrip, [{ QQQ: 600.1 }], 'boot must paint the price strip once from the snapshot');

  // The ticker subscribe callback repaints the strip and re-renders paper
  // only when a session exists.
  boot.getTickerSubscribeCb()();
  assert.equal(boot.calls.updatePriceStrip.length, 2, 'ticker update must repaint the strip');
  assert.equal(boot.calls.renderPaper, 0, 'ticker update without a session must not render paper');

  // Tab + history wiring: each button routes to its tab, the date picker
  // loads the chosen archive date, the underlying selector switches.
  boot.els['tab-bots'].onclick();
  boot.els['tab-squeeze'].onclick();
  boot.els['tab-history'].onclick();
  boot.els['tab-live'].onclick();
  boot.els['tab-news'].onclick();
  assert.deepEqual(boot.calls.showTab, ['bots', 'squeeze', 'history', 'live', 'news'], 'tab buttons must route to their tabs');
  boot.els['hist-date'].value = '2026-09-18';
  boot.els['hist-date'].onchange({ target: boot.els['hist-date'] });
  assert.deepEqual(boot.calls.loadHistoryDate, ['2026-09-18'], 'history date picker must load the chosen date');
  boot.els['underlying-select'].onchange({ target: { value: 'SPY' } });
  assert.deepEqual(boot.calls.selectUnderlying, ['SPY'], 'underlying selector must switch the chain');

  // Auth toggle flips login <-> register and re-renders the gate.
  assert.equal(boot.sandbox.authMode, 'login', 'auth starts in login mode');
  boot.els['auth-toggle'].onclick();
  assert.equal(boot.sandbox.authMode, 'register', 'toggle must switch to register');
  boot.els['auth-toggle'].onclick();
  assert.equal(boot.sandbox.authMode, 'login', 'toggle must switch back to login');

  // Margin-call continue hides the screen.
  boot.els['margin-call-continue'].onclick();
  assert.equal(boot.calls.hideMarginCallScreen, 1, 'margin-call continue must hide the screen');

  // Logout: POSTs /api/logout, clears the session + cache, re-renders.
  // (Field-by-field: the options object is built inside the vm realm, so
  // deepEqual across the realm boundary fails on prototypes.)
  await boot.els['auth-logout'].onclick();
  assert.equal(boot.calls.fetches.length, 1, 'logout must fetch exactly once');
  assert.equal(boot.calls.fetches[0].url, '/api/logout', 'logout must POST /api/logout');
  assert.equal(boot.calls.fetches[0].options.method, 'POST', 'logout must POST');
  assert.equal(boot.calls.fetches[0].options.credentials, 'same-origin', 'logout must send same-origin credentials');
  assert.equal(boot.sandbox.session, null, 'logout must clear the session');
  assert.deepEqual(boot.calls.localStorageRemoves, ['qqq-session-display-v1'], 'logout must drop the cached session display');
  assert.equal(boot.calls.renderSelectedChain, 2, 'logout must re-render the chain');

  // Auth submit, success path: credentials routed, session cached + shown,
  // paper + chain re-rendered, form reset, button re-enabled.
  boot.els['auth-username'].value = '  owen  ';
  boot.els['auth-password'].value = 'secret';
  await boot.els['auth-form'].onsubmit({ preventDefault: () => {} });
  assert.deepEqual(boot.calls.submitAuth, [['owen', 'secret']], 'submit must trim the username and pass both credentials');
  assert.equal(boot.calls.cacheSessionDisplay, 1, 'successful login must cache the session display');
  assert.equal(boot.els['auth-form']._resetCalls, 1, 'successful login must reset the form');
  assert.equal(boot.els['auth-submit'].disabled, false, 'submit button must be re-enabled after login');
  assert.equal(boot.calls.renderPaper, 1, 'successful login must render paper');
  assert.equal(boot.sandbox.session.user, 'owen', 'successful login must set the session');

  // Auth submit, failure path: the error is shown, no session is set.
  const failing = runBoot({ submitAuthImpl: async () => { throw new Error('bad password'); } });
  await failing.flush();
  failing.els['auth-username'].value = 'owen';
  failing.els['auth-password'].value = 'wrong';
  await failing.els['auth-form'].onsubmit({ preventDefault: () => {} });
  assert.equal(failing.els['auth-error'].textContent, 'bad password', 'failed login must show the error');
  assert.equal(failing.sandbox.session, null, 'failed login must not set a session');
  assert.equal(failing.els['auth-submit'].disabled, false, 'submit button must be re-enabled after failure');

  // Cached session: boot renders paper instantly (display-only) before the
  // fetch phase, then re-renders after hydration.
  const cached = runBoot({ cachedSession: { user: 'owen' } });
  await cached.flush();
  assert.equal(cached.calls.renderPaper, 2, 'cached session: paper renders instantly and again after hydration');
  assert.equal(cached.sandbox.session.user, 'owen', 'cached session must be picked up at boot');
  cached.getTickerSubscribeCb()();
  assert.equal(cached.calls.renderPaper, 3, 'ticker update with a session must re-render paper');
})().catch(e => { console.error(e); process.exit(1); });
