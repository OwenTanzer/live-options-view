// B2 (readability quest): the paper-trading section moved out of index.html
// into site/js/paper-trading.js, loaded in the same position. The test pins
// the ?v= hash in the page to the module's content (like shared.js), asserts
// the section no longer lives inline, and drives the book / execution /
// settlement / render paths with a stub DOM + stub fetch + the real shared.js
// and shared/heatmap.js: average-cost computeBook (including a flip through
// multipliers), the execution request retry + error identity, executeTrade's
// normalization / no-session / pending-dedupe / fill paths, settleExpired's
// no-op / settle / 410-margin-call / transient-failure paths, snapshot ingest
// (spot marks + the liveQuotes subscribe callback) and renderPaper's
// positions / summary / log rendering. Values returned from the vm context
// are compared field by field, not deepEqual — deepEqual fails across the
// vm realm boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/paper-trading.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../site/shared.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/paper-trading.js?v=' + moduleHash), 'paper-trading script URL must match its content hash on every hosting path');
for (const fn of ['cacheSessionDisplay', 'readSessionDisplayCache', 'newExecutionRequestId',
  'requestExecution', 'openPositionSymbols', 'visibleContractSymbols',
  'updateQuoteInterests', 'renderTransportHealth', 'renderQuoteHealth',
  'fetchSpyHealth', 'ingestSnapshotQuotes', 'publishSnapshotQuotes',
  'computeBook', 'executeTrade', 'announceOrderStatus', 'showExecutionError',
  'setTradePending', 'hasExpiredPositions', 'settleExpired', 'closePosition',
  'instrumentLabel', 'fmtMoney', 'fmtPriceMoney', 'pnlClass', 'escapeHtml',
  'renderPaper']) {
  assert.ok(!html.includes(`function ${fn}`), `${fn} must live in js/paper-trading.js, not inline in index.html`);
}
assert.ok(!html.includes('let session = null'), 'session must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('let spotMarks = {}'), 'spotMarks must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('const SESSION_CACHE_KEY'), 'SESSION_CACHE_KEY must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('const liveQuotes = new LiveQuoteService()'), 'liveQuotes must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('let lastData = null'), 'lastData must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('const chainSelection = new ChainSelection'), 'chainSelection must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('const chainState = {}'), 'chainState must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('let ticketEl = null'), 'ticketEl must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('const pendingExecutions = new Set()'), 'pendingExecutions must live in js/paper-trading.js, not inline in index.html');
assert.ok(!html.includes('let positionMarks = {}'), 'positionMarks must live in js/paper-trading.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── paper trading'), 'paper trading banner must stay in index.html as the section marker');

// --- stub DOM: paper panel elements with a real innerHTML setter. ---
function makeEl() {
  const el = {
    textContent: '', value: '', disabled: false,
    dataset: {}, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }),
    remove() {},
  };
  let _innerHTML = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => _innerHTML,
    set: v => { _innerHTML = String(v); el.children.length = 0; },
    enumerable: true, configurable: true,
  });
  return el;
}
const els = {};
for (const id of ['no-data', 'paper-positions', 'paper-summary', 'paper-hint',
                  'paper-log-body', 'user-name', 'paper-order-status',
                  'quote-health', 'spy-health']) {
  els[id] = makeEl();
}

// --- harness: the page's shared scope, as the module sees it. ---
// `botsTimer`/`renderBots` come from the bots panel, `showMarginCallScreen`
// from the auth module, `closeTicket` from the trade-ticket module and
// `renderSelectedChain` from the data-fetch-cycle module — all runtime-only,
// so the sandbox stubs them here. `tickerState` comes from the price-strip
// module; the stub returns no quote, so share rows render unmarked.
let fetchCalls = [];
let nextResponse = null; // a response object, or an Error to throw
let fetchImpl = null; // when set, decides the response per call
const saved = {};
const calls = { renderBots: 0, marginCall: [], closeTicket: 0, renderSelectedChain: 0 };
const sandbox = {
  console,
  BROWSER_DATA: '/browser-data',
  document: {
    getElementById: id => els[id],
    createElement: () => makeEl(),
  },
  localStorage: {
    getItem: k => (k in saved ? saved[k] : null),
    setItem: (k, v) => { saved[k] = String(v); },
    removeItem: k => { delete saved[k]; },
  },
  crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
  botsTimer: null,
  renderBots: () => { calls.renderBots++; },
  showMarginCallScreen: reason => { calls.marginCall.push(reason); },
  closeTicket: () => { calls.closeTicket++; },
  renderSelectedChain: () => { calls.renderSelectedChain++; },
  // `selectedChainData` comes from the data-fetch-cycle module; the stub
  // returns no chain, so updateQuoteInterests falls back to the open
  // positions alone unless the test passes data explicitly.
  selectedChainData: () => null,
  tickerState: { get: () => null },
  fetch: async (url, options) => {
    fetchCalls.push({ url: String(url), options });
    if (fetchImpl) return fetchImpl(url, options);
    if (nextResponse instanceof Error) throw nextResponse;
    return nextResponse;
  },
};
const context = vm.createContext(sandbox);
// B3 slice 2 (Nov 2026): fmtQ / computeAtmWindow moved out of shared.js
// into site/shared/heatmap.js — load the shipped module too, in page order,
// so renderPaper / visibleContractSymbols read the real ones.
// B3 slice 3 (Nov 2026): LiveQuoteService / freshShareQuote moved to
// site/shared/quotes.js — same, after heatmap.js, so the module's top-level
// `const liveQuotes = new LiveQuoteService()` constructs the real service.
// B3 slice 4 (Nov 2026): the panel formatters + order validators moved to
// site/shared/format.js — same, after quotes.js, so normalizePaperOrder is
// the real one when the trade paths run.
// B3 slice 5 (Nov 2026): the chain underlying names moved to
// site/shared/chain.js — same, after format.js, in page order.
vm.runInContext(sharedSource, context, { filename: 'site/shared.js' });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/heatmap.js'), 'utf8'), context, { filename: 'site/shared/heatmap.js' });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/quotes.js'), 'utf8'), context, { filename: 'site/shared/quotes.js' });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/format.js'), 'utf8'), context, { filename: 'site/shared/format.js' });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/shared/chain.js'), 'utf8'), context, { filename: 'site/shared/chain.js' });
vm.runInContext(moduleSource, context, { filename: 'site/js/paper-trading.js' });
const get = name => vm.runInContext(name, context);
const set = (name, value) => vm.runInContext(`${name} = (${JSON.stringify(value)})`, context);
const response = (data, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => data,
});
const trade = (over = {}) => ({
  ts: '2026-09-23T13:00:00Z', sym: 'QQQ260918C600000', strike: 600, type: 'call',
  exp: '2026-09-18', side: 'buy', qty: 1, price: 5, ...over,
});

assert.equal(typeof get('computeBook'), 'function', 'module must define computeBook');
assert.equal(typeof get('executeTrade'), 'function', 'module must define executeTrade');
assert.equal(typeof get('settleExpired'), 'function', 'module must define settleExpired');
assert.equal(typeof get('renderPaper'), 'function', 'module must define renderPaper');
assert.equal(typeof get('ingestSnapshotQuotes'), 'function', 'module must define ingestSnapshotQuotes');
assert.equal(get('session'), null, 'session starts null (logged out)');
assert.equal(get('SESSION_CACHE_KEY'), 'paperbook_session_cache');
assert.equal(typeof get('liveQuotes'), 'object', 'module must construct the shared liveQuotes service');
assert.equal(get('lastData'), null, 'lastData starts null');
assert.equal(typeof get('chainSelection'), 'object', 'module must construct the shared chainSelection');

// --- pure formatters. ---
assert.equal(get('fmtMoney')(12.345), '+$12.35');
assert.equal(get('fmtMoney')(-3), '−$3.00');
assert.equal(get('fmtMoney')(0), '$0.00');
assert.equal(get('fmtPriceMoney')(600.1), '$600.10');
assert.equal(get('pnlClass')(1), 'pnl-up');
assert.equal(get('pnlClass')(-1), 'pnl-down');
assert.equal(get('pnlClass')(0), '');
assert.equal(get('escapeHtml')('<a href="x">&\'</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
assert.equal(get('instrumentLabel')({ instrument_type: 'share' }, 'QQQ'), 'QQQ');
assert.equal(get('instrumentLabel')({ strike: 600, type: 'call' }, 'QQQ260918C600000'), 'QQQ 600 C');
assert.equal(get('instrumentLabel')({ strike: 595, type: 'put' }, 'QQQ260918P595000'), 'QQQ 595 P');

// --- computeBook: average cost, realized on a partial close, flip through zero. ---
const book = get('computeBook')([
  trade({ qty: 2, price: 5 }),
  trade({ qty: 3, price: 6 }),
]);
assert.equal(book['QQQ260918C600000'].pos, 5);
assert.equal(book['QQQ260918C600000'].avg, 5.6);
assert.equal(book['QQQ260918C600000'].multiplier, 100);
const book2 = get('computeBook')([
  trade({ qty: 2, price: 5 }),
  trade({ qty: 3, price: 6 }),
  trade({ side: 'sell', qty: 2, price: 7 }),
]);
assert.equal(book2['QQQ260918C600000'].pos, 3);
assert.equal(book2['QQQ260918C600000'].avg, 5.6);
assert.ok(Math.abs(book2['QQQ260918C600000'].realized - 280) < 1e-9, 'partial close realizes (7 − 5.6) × 2 × 100');
const book3 = get('computeBook')([
  trade({ qty: 2, price: 5 }),
  trade({ qty: 3, price: 6 }),
  trade({ side: 'sell', qty: 5, price: 8 }),
]);
assert.equal(book3['QQQ260918C600000'].pos, 0);
assert.ok(Math.abs(book3['QQQ260918C600000'].realized - 1200) < 1e-9, 'closing the whole book realizes (8 − 5.6) × 5 × 100');
const book4 = get('computeBook')([
  trade({ qty: 2, price: 5 }),
  trade({ qty: 3, price: 6 }),
  trade({ side: 'sell', qty: 7, price: 8 }),
]);
assert.equal(book4['QQQ260918C600000'].pos, -2);
assert.equal(book4['QQQ260918C600000'].avg, 8, 'flipping through zero re-averages at the flip price');
assert.ok(Math.abs(book4['QQQ260918C600000'].realized - 1200) < 1e-9, 'the flip realizes the closing leg (8 − 5.6) × 5 × 100');
// Shares use a multiplier of 1, not 100.
const book5 = get('computeBook')([
  { ts: '2026-09-23T13:00:00Z', sym: 'QQQ', instrument_type: 'share', side: 'buy', qty: 10, price: 100 },
  { ts: '2026-09-23T13:01:00Z', sym: 'QQQ', instrument_type: 'share', side: 'sell', qty: 4, price: 110 },
]);
assert.equal(book5['QQQ'].pos, 6);
assert.equal(book5['QQQ'].realized, 40);
// With no argument the book is built from the session's trades.
set('session', { username: 'owen', balance_cash: 10000, trades: [trade({ qty: 2, price: 5 })] });
assert.equal(get('computeBook')()['QQQ260918C600000'].pos, 2);
set('session', null);

// --- session display cache round-trips through localStorage. ---
set('session', { username: 'owen', balance_cash: 10000, trades: [] });
get('cacheSessionDisplay')();
assert.equal(saved['paperbook_session_cache'], JSON.stringify({ username: 'owen', balance_cash: 10000, trades: [] }));
assert.equal(get('readSessionDisplayCache')().username, 'owen');
set('session', null);

// --- newExecutionRequestId uses the platform UUID. ---
assert.equal(get('newExecutionRequestId')(), '00000000-0000-4000-8000-000000000001');

// --- renderTransportHealth / renderQuoteHealth write the health line. ---
get('renderQuoteHealth')({ state: 'live', quoteAgeMs: 4000, message: 'ok' });
assert.equal(els['quote-health'].className, 'transport-health live');
assert.equal(els['quote-health'].textContent, 'Options: live · 4s');
get('renderTransportHealth')('quote-health', 'Options', { state: 'stale', returned: 1, requested: 3, message: 'partial' });
assert.equal(els['quote-health'].textContent, 'Options: stale · 1/3');
// A closed quote feed is a named state, not a red failure: after the session
// stop the producer refuses option requests while tickers stay live, and the
// line has to say which of those happened.
get('renderTransportHealth')('quote-health', 'Options', { state: 'closed', returned: 0, requested: 82, message: 'closed for this session' });
assert.equal(els['quote-health'].className, 'transport-health closed');
assert.equal(els['quote-health'].textContent, 'Options: session closed');

const SYM = 'QQQ260918C600000';
const INFO = { sym: SYM, strike: 600, type: 'call', exp: '2026-09-18' };

(async () => {
  // --- requestExecution: a lost first response retries once, same identity. ---
  let attempts = 0;
  fetchImpl = async (url, options) => {
    attempts++;
    if (attempts === 1) throw new Error('socket hangup');
    return response({ order_id: 'ord-1', balance_cash: 9500 });
  };
  const filled = await get('requestExecution')(INFO, { side: 'buy', qty: 1, order_type: 'market' }, 'req-1');
  assert.equal(filled.order_id, 'ord-1');
  assert.equal(attempts, 2, 'a thrown fetch retries once with the same request');
  assert.equal(Object.isFrozen(filled), true, 'execution results are frozen');
  assert.equal(fetchCalls.at(-1).options.body.includes('"execution_request_id":"req-1"'), true);

  // --- requestExecution: a rejected execution throws with its order identity. ---
  fetchImpl = async () => response({ error: 'Insufficient funds', order_id: 'ord-2', status: 'rejected' }, 400);
  await assert.rejects(
    get('requestExecution')(INFO, { side: 'buy', qty: 1, order_type: 'market' }, 'req-2'),
    err => { assert.equal(err.message, 'Insufficient funds'); assert.equal(err.orderId, 'ord-2'); assert.equal(err.orderStatus, 'rejected'); return true; });

  // --- executeTrade: a normalization error announces + never fetches. ---
  fetchCalls = [];
  fetchImpl = null;
  nextResponse = response({});
  set('session', { username: 'owen', balance_cash: 10000, trades: [] });
  await get('executeTrade')(INFO, 'buy', 0);
  assert.equal(fetchCalls.length, 0, 'an invalid quantity never reaches the Worker');
  assert.equal(els['paper-order-status'].textContent, 'Quantity must be a positive whole number.');

  // --- executeTrade: with no session, nothing is sent. ---
  set('session', null);
  await get('executeTrade')(INFO, 'buy', 1);
  assert.equal(fetchCalls.length, 0, 'logged out, no execution is attempted');

  // --- executeTrade: a fill appends the trade, updates cash, re-renders. ---
  set('session', { username: 'owen', balance_cash: 10000, trades: [] });
  nextResponse = response({ ...trade({ qty: 1, price: 5.2 }), order_id: 'ord-3', order_type: 'market', balance_cash: 9480 });
  const ticketBefore = calls.closeTicket;
  const chainBefore = calls.renderSelectedChain;
  await get('executeTrade')(INFO, 'buy', 1);
  assert.equal(fetchCalls.at(-1).url, '/api/paper-trade');
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).sym, SYM);
  assert.equal(get('session').trades.length, 1);
  assert.equal(get('session').balance_cash, 9480);
  assert.equal(get('session').trades[0].order_id, 'ord-3');
  assert.equal(calls.closeTicket, ticketBefore + 1, 'the ticket closes on a fill');
  assert.equal(calls.renderSelectedChain, chainBefore + 1, 'position badges refresh on a fill');
  assert.ok(els['paper-positions'].innerHTML.includes('QQQ 600 C'), 'the new position renders');
  assert.ok(els['paper-summary'].innerHTML.includes('Cash'), 'the summary renders');
  assert.ok(els['paper-log-body'].innerHTML.includes('BUY 1'), 'the fill is logged');

  // --- executeTrade: a second call for the same symbol while one is in flight is dropped. ---
  let release;
  fetchImpl = () => new Promise(resolve => { release = () => resolve(response({ ...trade({ qty: 1, price: 5.3 }), order_id: 'ord-4', order_type: 'market', balance_cash: 9427 })); });
  const first = get('executeTrade')(INFO, 'buy', 1);
  const callsBefore = fetchCalls.length;
  await get('executeTrade')(INFO, 'buy', 1);
  assert.equal(fetchCalls.length, callsBefore, 'the duplicate in-flight order is dropped, not double-filled');
  release();
  await first;
  assert.equal(get('session').trades.length, 2, 'the first order still fills once');
  fetchImpl = null;

  // --- executeTrade: a rejected execution announces the rejection, keeps the book. ---
  nextResponse = response({ error: 'Insufficient funds', order_id: 'ord-5', status: 'rejected' }, 400);
  await get('executeTrade')(INFO, 'buy', 1);
  assert.equal(get('session').trades.length, 2, 'a rejected order adds no trade');
  assert.ok(els['paper-order-status'].textContent.includes('rejected order ord-5'), 'the rejection names the order');
  assert.ok(els['paper-hint'].textContent.includes('Execution rejected'), 'the hint shows the rejection');

  // --- closePosition: sells the whole open book for the symbol. ---
  fetchCalls = [];
  nextResponse = response({ ...trade({ side: 'sell', qty: 2, price: 5.4 }), order_id: 'ord-6', order_type: 'market', balance_cash: 10507 });
  get('closePosition')(SYM);
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  assert.equal(fetchCalls.at(-1).url, '/api/paper-trade');
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).side, 'sell');
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).qty, 2);
  assert.equal(get('session').trades.length, 3, 'the close fills and lands in the book');

  // --- closePosition: closes a SPY position by its own OCC symbol, not QQQ's. ---
  const SPY_SYM = 'SPY261106C00700000';
  set('session', { username: 'owen', balance_cash: 10000, trades: [trade({ sym: SPY_SYM, strike: 700, type: 'call', exp: '2026-11-06', qty: 2, price: 5 })] });
  fetchCalls = [];
  nextResponse = response({ ...trade({ sym: SPY_SYM, strike: 700, type: 'call', exp: '2026-11-06', side: 'sell', qty: 2, price: 5.4 }), order_id: 'ord-9', order_type: 'market', balance_cash: 10507 });
  get('closePosition')(SPY_SYM);
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).sym, SPY_SYM, 'the close targets the SPY contract');
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).side, 'sell');
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).qty, 2);
  assert.equal(get('session').trades.length, 2, 'the SPY close fills and lands in the book');

  // --- executeTrade: a fill after the user switched tickets leaves the new ticket alone. ---
  set('session', { username: 'owen', balance_cash: 10000, trades: [] });
  vm.runInContext("ticketEl = document.createElement('div')", context);
  const firstTicket = get('ticketEl');
  let releaseFill;
  fetchImpl = () => new Promise(resolve => { releaseFill = () => resolve(response({ ...trade({ qty: 1, price: 5.2 }), order_id: 'ord-7', order_type: 'market', balance_cash: 9480 })); });
  const pendingFill = get('executeTrade')(INFO, 'buy', 1);
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  // The user switches chains and opens a new ticket while the order is in flight.
  vm.runInContext("ticketEl = document.createElement('div')", context);
  const newTicket = get('ticketEl');
  assert.notEqual(newTicket, firstTicket);
  const closeBefore = calls.closeTicket;
  releaseFill();
  await pendingFill;
  assert.equal(get('session').trades.length, 1, 'the fill still lands after a switch');
  assert.equal(calls.closeTicket, closeBefore, 'the new ticket is not closed by the old order filling');
  assert.equal(get('ticketEl'), newTicket, 'the new ticket stays open');
  vm.runInContext('ticketEl = null', context);
  fetchImpl = null;

  // --- executeTrade: a rejection after the user switched tickets does not write into the new ticket. ---
  set('session', { username: 'owen', balance_cash: 10000, trades: [] });
  els['paper-hint'].textContent = '';
  vm.runInContext("ticketEl = document.createElement('div')", context);
  let releaseReject;
  fetchImpl = () => new Promise(resolve => { releaseReject = () => resolve(response({ error: 'Insufficient funds', order_id: 'ord-8', status: 'rejected' }, 400)); });
  const pendingReject = get('executeTrade')(INFO, 'buy', 1);
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  vm.runInContext("ticketEl = document.createElement('div')", context);
  const switchedTicket = get('ticketEl');
  releaseReject();
  await pendingReject;
  assert.equal(get('session').trades.length, 0, 'a rejected order adds no trade');
  assert.equal(switchedTicket.children.length, 0, 'the new ticket gets no status written into it');
  assert.ok(els['paper-hint'].textContent.includes('Execution rejected'), 'the rejection surfaces in the hint, not the wrong ticket');
  assert.ok(els['paper-order-status'].textContent.includes('rejected order ord-8'), 'the global status names the rejected order');
  vm.runInContext('ticketEl = null', context);
  fetchImpl = null;

  // --- hasExpiredPositions / settleExpired: nothing expired, no network call. ---
  fetchCalls = [];
  set('session', { username: 'owen', balance_cash: 10507, trades: [trade({ exp: '2026-09-23' })] });
  vm.runInContext("lastDataDate = '2026-09-23'", context);
  assert.equal(get('hasExpiredPositions')(), false, 'an option expiring today is not expired');
  await get('settleExpired')();
  assert.equal(fetchCalls.length, 0, 'no expired positions means no settlement call');

  // --- settleExpired: an expired long settles through /api/settle. ---
  set('session', { username: 'owen', balance_cash: 10507, trades: [trade({ exp: '2026-09-18' })] });
  assert.equal(get('hasExpiredPositions')(), true);
  nextResponse = response({ settled: [trade({ exp: '2026-09-18', side: 'sell', qty: 1, price: 0, note: 'expired worthless' })], balance_cash: 10507 });
  await get('settleExpired')();
  assert.equal(fetchCalls.at(-1).url, '/api/settle');
  assert.deepEqual(Object.keys(JSON.parse(fetchCalls.at(-1).options.body)).sort(), ['as_of', 'spot_marks']);
  assert.equal(JSON.parse(fetchCalls.at(-1).options.body).as_of, '2026-09-23');
  assert.equal(get('session').trades.length, 2, 'the settlement trade lands in the book');

  // --- settleExpired: a 410 margin call surfaces the reason, no trade lands. ---
  set('session', { username: 'owen', balance_cash: 10507, trades: [trade({ exp: '2026-09-18' })] });
  nextResponse = response({ reason: 'A settlement obligation exceeded the account balance' }, 410);
  await get('settleExpired')();
  assert.equal(calls.marginCall.length, 1, 'the margin-call screen is shown');
  assert.equal(calls.marginCall[0], 'A settlement obligation exceeded the account balance');
  assert.equal(get('session').trades.length, 1, 'a margin-called settlement adds no trade');

  nextResponse = response({settled: []});
  await get('settleExpired')();
  const emptyCount = fetchCalls.length;
  await get('settleExpired')();
  assert.equal(fetchCalls.length, emptyCount, 'unchanged empty settlement is coalesced');
  get('spotMarks')['2026-09-18'] = 500;
  await get('settleExpired')();
  assert.equal(fetchCalls.length, emptyCount + 1, 'a new mark retries immediately');
  vm.runInContext('lastEmptySettlement.retryAt = 0', context);

  // --- settleExpired: a transient 500 leaves the book alone for the next cycle. ---
  nextResponse = response({}, 500);
  await get('settleExpired')();
  assert.equal(get('session').trades.length, 1, 'a failed settlement is retried next cycle, not half-applied');

  // --- ingestSnapshotQuotes: records the date + spot mark, publishes quotes. ---
  set('session', { username: 'owen', balance_cash: 10507, trades: [trade({ exp: '2026-09-23' })] });
  vm.runInContext("lastData = { date: '2026-09-23', underlying_price: 600.1, rows: [] }", context);
  const chainBefore2 = calls.renderSelectedChain;
  get('ingestSnapshotQuotes')({
    date: '2026-09-23', underlying_price: 600.1, timestamp: '2026-09-23T13:04:00Z',
    rows: [{ OptionSymbol: SYM, Bid: 1.1, Ask: 1.2, Mid: 1.15, Strike: 600, Type: 'call', Expiration: '2026-09-18' }],
  });
  assert.equal(get('lastDataDate'), '2026-09-23');
  assert.equal(get('spotMarks')['QQQ:2026-09-23'], 600.1, 'the snapshot spot is kept per underlying for settlement pricing');
  assert.equal(get('positionMarks')[SYM], 1.15, 'the held symbol keeps its last mark once the quote feed moves on');
  assert.ok(calls.renderSelectedChain > chainBefore2, 'the subscribe callback re-renders the chain on a new quote');
  get('ingestSnapshotQuotes')({
    symbol: 'SPY', date: '2026-09-23', underlying_price: 670.2, timestamp: '2026-09-23T13:04:00Z', rows: [],
  });
  assert.equal(get('spotMarks')['SPY:2026-09-23'], 670.2, 'a SPY snapshot marks its own underlying, never QQQ');
  assert.equal(get('spotMarks')['QQQ:2026-09-23'], 600.1, 'the QQQ mark is untouched by a SPY snapshot');

  // --- updateQuoteInterests: the open position + visible contracts are wanted. ---
  vm.runInContext(`lastData = ${JSON.stringify({ underlying_price: 600.1, rows: [{ Strike: 600, Type: 'call', OptionSymbol: SYM }] })}`, context);
  get('updateQuoteInterests')();
  assert.ok(get('liveQuotes').interestSymbols().includes(SYM), 'the held contract stays in the quote interest set');

  // --- fetchSpyHealth: a current publication renders live. ---
  const now = new Date().toISOString();
  nextResponse = response({ updated_at: now, spy: { collection_status: 'collecting', last_snapshot_upload_time: now, spot: { status: 'live' } } });
  await get('fetchSpyHealth')();
  assert.equal(els['spy-health'].textContent, 'SPY: live');
  assert.equal(els['spy-health'].className, 'transport-health live');

  // --- fetchSpyHealth: a failed load renders unavailable, not a throw. ---
  nextResponse = new Error('network down');
  await get('fetchSpyHealth')();
  assert.equal(els['spy-health'].textContent, 'SPY: unavailable');

  // --- renderPaper: mixed QQQ + SPY positions render distinctly, regardless of the selected chain. ---
  set('session', { username: 'owen', balance_cash: 10000, trades: [
    trade({ qty: 2, price: 5 }),
    trade({ sym: 'SPY261106C00700000', strike: 700, type: 'call', exp: '2026-11-06', qty: 1, price: 3 }),
  ] });
  get('renderPaper')();
  assert.ok(els['paper-positions'].innerHTML.includes('QQQ 600 C'), 'the QQQ position renders');
  assert.ok(els['paper-positions'].innerHTML.includes('SPY 700 C'), 'the SPY position renders beside it, not merged into QQQ');

  // --- renderPaper: no trades renders the empty state. ---
  set('session', { username: 'owen', balance_cash: 10000, trades: [] });
  get('renderPaper')();
  assert.equal(els['paper-positions'].innerHTML, '');
  assert.ok(els['paper-summary'].innerHTML.includes('Cash'));
  assert.ok(!els['paper-summary'].innerHTML.includes('Realized'), 'no trades means no realized line');
  assert.equal(els['paper-hint'].style.display, '');
  assert.ok(els['paper-log-body'].innerHTML.includes('No trades yet.'));
  assert.equal(els['user-name'].textContent, 'owen');

  // --- renderPaper: logged out renders nothing. ---
  set('session', null);
  els['paper-positions'].innerHTML = 'stale';
  get('renderPaper')();
  assert.equal(els['paper-positions'].innerHTML, 'stale', 'renderPaper is a no-op while logged out');

  // --- setTradePending: with no ticket open, nothing happens. ---
  assert.equal(get('ticketEl'), null);
  const statusBefore = els['paper-order-status'].textContent;
  get('setTradePending')(true, 'Submitting market order...');
  assert.equal(els['paper-order-status'].textContent, statusBefore, 'the status line is untouched without a ticket');
})();
