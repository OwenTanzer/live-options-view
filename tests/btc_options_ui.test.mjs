import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { computeBookFromTrades } from '../backend/common.js';
import { btcClockScenarios } from './helpers/btc_clock_fixture.mjs';

const now = Date.parse('2026-11-21T18:00:00Z'), sym = 'BTCOPT:' + 'a'.repeat(40);
const c = { future_symbol: '/BTCX6', future_month: '2026-11', contract_key: sym, strike: 90000,
  exp: '2026-11-27', stops_trading_at: '2026-11-27T16:00:00Z', type: 'call' };
const ts = new Date(now).toISOString();
const q = { symbol: sym, contract: c, available: true, bid: 120, ask: 121,
  bid_size: 1, ask_size: 2, bid_ts: ts, ask_ts: ts, bid_received_ts: ts, ask_received_ts: ts };
const trades = [{ sym, contract: c, instrument_type: 'futures_option', multiplier: 5,
  side: 'buy', qty: 1, price: 100, fees: 2.5 }, { sym, contract: c, instrument_type: 'futures_option',
  multiplier: 5, side: 'sell', qty: 1, price: 120, fees: 2.5 }];
function el() { return { children: [], dataset: {}, hidden: false, textContent: '', classList: { add() {} }, querySelectorAll() { return []; },
  setAttribute() {}, replaceChildren() { this.children = []; }, appendChild(c) { this.children.push(c); }, append(c) { this.children.push(c); } }; }
const elements = Object.fromEntries(['underlying-select', 'btc-panel', 'btc-chain', 'no-data', 'title', 'paper-hint'].map(k => [k, el()]));
const intervals = new Map(), listeners = new Map();
let resolveRequest, book = {}, ticketClosed = 0;
const context = vm.createContext({ console, Date: class extends Date { static now() { return now; } },
  document: { addEventListener: (name, cb) => listeners.set(name, cb), body: el(), getElementById: id => elements[id], createElement: el, querySelectorAll: () => [] },
  sessionVerified: true, URL, URLSearchParams, location: { href: 'https://fixture.invalid/' }, history: { replaceState() {} },
  UNDERLYING_KEY: 'chain_underlying', localStorage: { setItem() {} }, showTab() {},
  session: { username: 'fixture', trades }, ticketEl: null, pendingExecutions: new Set(),
  computeBook: () => book, closeTicket: () => ticketClosed++, clearChain() {}, renderSelectedChain() {}, renderPaper() {},
  setInterval: (cb, ms) => intervals.set(ms, cb), openTicket() {}, fetch: () => new Promise(resolve => { resolveRequest = resolve; }) });
vm.runInContext(readFileSync(new URL('../site/js/btc-options.js', import.meta.url), 'utf8'), context);
const btc = vm.runInContext('btcOptions', context);
let refreshes = 0;
const actualRefresh = btc.refresh;
btc.refresh = () => refreshes++;
context.document.hidden = true;
intervals.get(5000)();
listeners.get('visibilitychange')();
assert.equal(refreshes, 0);
context.document.hidden = false;
listeners.get('visibilitychange')();
assert.equal(refreshes, 1, 'resume immediately');
intervals.get(5000)();
assert.equal(refreshes, 2, 'retain foreground cadence');
btc.access = 'unauthorized'; btc.retryAccessAt = now + 300_000;
for (let i = 0; i < 12; i++) intervals.get(5000)();
assert.equal(refreshes, 2, 'ordinary ticks respect the denied session cooldown');
listeners.get('visibilitychange')();
assert.equal(refreshes, 3, 'foreground return immediately retries authorization');
btc.retryAccessAt = now;
intervals.get(5000)();
assert.equal(refreshes, 4, 'periodic recovery for externally granted access');
btc.access = null;
btc.refresh = actualRefresh;
btc.data = { session: 'open' }; btc.quotes.set(sym, q);
assert.equal(btc.mark(sym), 120);
btc.quotes.set(sym, { ...q, market_session: 'maintenance' });
assert.equal(btc.mark(sym), null, 'closed CME holding while viewing USDC');
btc.data.session = 'maintenance';
const linear = 'USDC:BTC_USDC-27NOV26-90000-C';
btc.quotes.set(linear, { ...q, contract: { ...c, exchange:'Deribit' }, market_session:'open' });
assert.equal(btc.mark(linear), 120, 'open USDC holding while viewing CME');
btc.data.session = 'open';
btc.quotes.set(sym, { ...q, bid_size: 0 }); assert.equal(btc.mark(sym), null);
btc.quotes.set(sym, { ...q, bid_ts: '2026-11-21T17:59:00Z' }); assert.equal(btc.mark(sym), null);
btc.quotes.set(sym, { ...q, bid: 122 }); assert.equal(btc.mark(sym), null);
btc.active = true;
const pending = btc.refresh();
btc.select(false); resolveRequest(Response.json({ session: 'open', quotes: [q], contracts: [c] }));
await pending;
assert.equal(btc.quotes.size, 0, 'in-flight selection response cannot repopulate old quotes');
assert.ok(ticketClosed > 0);
book = { [sym]: { instrument_type: 'futures_option', pos: 1 } };
const heldRefresh = btc.refresh();
resolveRequest(Response.json({ session: 'open', quotes: [q], contracts: [c] }));
await heldRefresh;
assert.equal(btc.mark(sym), 120, 'held contracts refresh away from BTC view');

// Exercise the exact browser book code against the server implementation.
const paper = readFileSync(new URL('../site/js/paper-trading.js', import.meta.url), 'utf8');
const bookSource = paper.slice(paper.indexOf('function computeBook('), paper.indexOf('async function executeTrade('));
vm.runInContext('function occRoot() { return ""; }\n' + bookSource, context);
context.trades = trades;
const client = JSON.parse(JSON.stringify(vm.runInContext('computeBook(trades)', context)))[sym];
const server = computeBookFromTrades(trades)[sym];
for (const field of ['pos', 'avg', 'realized', 'realized_gross', 'fees', 'open_fees']) assert.equal(client[field], server[field], field);
assert.equal(client.realized, 95);
console.log('PASS BTC UI side availability, selection races, held refresh and client/server fee parity');

for (const s of btcClockScenarios()) {
  context.Date = class extends Date { static now() { return s.now; } };
  btc.data = s.payload;
  const row = s.payload.quotes[0]; btc.quotes.set(row.symbol, row);
  for (const [action, side] of [['buy', 'ask'], ['sell', 'bid']]) {
    assert.equal(btc.side(row.symbol, side) != null, s[action], `${s.name}/${side}`);
    if (s[action]) assert.match(btc.clockLabel(row.symbol, side), /observed side change/);
  }
}
console.log('PASS actual collector wire scenarios agree with UI eligibility and provenance');
