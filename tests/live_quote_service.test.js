// B3 slice 3 (Nov 2026): the live-quote transport + ticker state
// (TRADEABLE_SHARE_SYMBOLS / isTradeableShareSymbol / LiveQuoteService /
// tickerSessionState / SHARE_QUOTE_MAX_AGE_MS / freshShareQuote /
// TickerStateStore / sameSet / LiveQuotePoller / parseRetryAfter) moved
// verbatim out of site/shared.js into site/shared/quotes.js. Pins the
// module's ?v= hash in the page, asserts the ten names no longer live in
// shared.js, pins the entry's export surface (the eight names that were
// always among the 26 are still re-exported — the same function objects —
// while TRADEABLE_SHARE_SYMBOLS and sameSet stay browser globals by design),
// and drives the moved code directly.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'quotes.js'), 'utf8');
const quotes = require('../site/shared/quotes.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/quotes.js?v=' + moduleHash),
  'quotes script URL must match its content hash on every hosting path');

for (const name of ['LiveQuoteService', 'LiveQuotePoller', 'TickerStateStore', 'tickerSessionState', 'parseRetryAfter', 'freshShareQuote']) {
  assert.ok(!shared.includes(`function ${name}(`) && !shared.includes(`class ${name} `), `${name} must no longer live in shared.js`);
  assert.equal(typeof quotes[name], 'function', `quotes.js must export ${name}`);
}
for (const decl of ['const TRADEABLE_SHARE_SYMBOLS = new Set([', 'const SHARE_QUOTE_MAX_AGE_MS = 15_000;', 'function sameSet(', 'function isTradeableShareSymbol(']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
}

// The entry's export list is the behavior evidence for the split: exactly the
// same 26 names shared.js exported before B3 slice 3, with the eight
// live-quote names re-exported from the new home — the same function objects,
// not copies — and TRADEABLE_SHARE_SYMBOLS / sameSet still browser-only.
assert.deepEqual(Object.keys(entry).sort(), [
  'CHAIN_UNDERLYINGS', 'ChainSelection', 'LiveQuotePoller', 'LiveQuoteService',
  'SHARE_QUOTE_MAX_AGE_MS', 'TickerStateStore', 'chainLatestPath',
  'computeAtmWindow', 'describeSqueezeAcquisitionFailure', 'findRevision',
  'fmtSteoDelta', 'formatMomentum', 'formatSqueezeScanStatus',
  'formatSqueezeScheduleStatus', 'formatSqueezeFirstSeen', 'formatVwapRvol',
  'freshShareQuote', 'isTradeableShareSymbol', 'normalizePaperOrder',
  'normalizeShareOrder', 'occRoot', 'parseRetryAfter', 'relativeOiRanges',
  'summarizeSpyHealth', 'tickerSessionState', 'validateChainPayload',
].sort());
for (const name of ['LiveQuoteService', 'LiveQuotePoller', 'TickerStateStore', 'tickerSessionState', 'SHARE_QUOTE_MAX_AGE_MS', 'freshShareQuote', 'parseRetryAfter', 'isTradeableShareSymbol']) {
  assert.equal(entry[name], quotes[name], `the entry must re-export the same ${name}`);
}
assert.equal(quotes.TRADEABLE_SHARE_SYMBOLS.has('QQQ'), true, 'QQQ is a tradeable share symbol');
assert.equal(quotes.isTradeableShareSymbol('SPY'), false, 'SPY is display-only, not a tradeable share');

const {
  LiveQuoteService, LiveQuotePoller, TickerStateStore, tickerSessionState, parseRetryAfter,
  SHARE_QUOTE_MAX_AGE_MS, freshShareQuote,
} = quotes;

const service = new LiveQuoteService();
let notifications = 0;
service.subscribe(() => notifications++);
service.setVisibleContracts(['VISIBLE']);
service.setOpenPositions(['OPEN']);
service.publish([
  { symbol: 'VISIBLE', bid: 1, ask: 1.2, strike: 500, type: 'call', exp: '2026-07-16' },
  { symbol: 'OPEN', bid: 2, ask: 2.4, strike: 501, type: 'put', exp: '2026-07-16' },
  { symbol: 'IGNORED', bid: 3, ask: 3.4, strike: 502, type: 'call', exp: '2026-07-16' },
], { source: 'r2-snapshot', observedAt: '2026-07-16T12:00:00Z' });

assert.equal(service.get('VISIBLE').mid, 1.1);
assert.equal(service.get('OPEN').mid, 2.2);
assert.equal(service.get('VISIBLE').source, 'r2-snapshot');
assert.equal(service.get('VISIBLE').observedAt, '2026-07-16T12:00:00Z');
assert.equal(service.get('IGNORED'), null);
assert.equal(notifications, 1);

service.publish([
  { symbol: 'VISIBLE', bid: 0.5, ask: 0.7, strike: 500, type: 'call', exp: '2026-07-16' },
], { source: 'older-snapshot', observedAt: '2026-07-16T11:59:00Z' });
assert.equal(service.get('VISIBLE').bid, 1, 'an older snapshot must not overwrite a newer quote');

service.setVisibleContracts([]);
assert.equal(service.get('VISIBLE'), null);
assert.equal(service.get('OPEN').mid, 2.2);
assert.equal(notifications, 2, 'dropping a contract via setVisibleContracts should notify subscribers');

service.setOpenPositions([]);
assert.equal(service.get('OPEN'), null);
assert.equal(notifications, 3, 'dropping a contract via setOpenPositions should notify subscribers');

// Interest-set changes that prune nothing shouldn't fire a spurious notification.
service.setVisibleContracts([]);
assert.equal(notifications, 3, 'a no-op prune should not notify subscribers');

assert.equal(parseRetryAfter('5', 0), 5000);
assert.equal(parseRetryAfter('Thu, 01 Jan 1970 00:00:10 GMT', 2000), 8000);

const regular = Date.parse('2026-07-17T14:30:00Z'); // 10:30 ET
const premarket = Date.parse('2026-07-17T12:00:00Z'); // 08:00 ET
assert.equal(tickerSessionState('equity', regular), 'live');
assert.equal(tickerSessionState('equity', premarket), 'extended');
assert.equal(tickerSessionState('index', premarket), 'closed');
assert.equal(tickerSessionState('crypto', premarket), 'live');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-17T21:01:00Z')), 'closed', 'Friday after 17:00 ET');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-19T21:59:00Z')), 'closed', 'Sunday before reopen');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-19T22:01:00Z')), 'live', 'Sunday after reopen');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-20T21:30:00Z')), 'closed', 'weekday maintenance');

const tickers = new TickerStateStore(
  { QQQ: 'equity', VIX: 'index', 'BTC/USD': 'crypto' },
  { nowFn: () => premarket, staleAfterMs: 30_000 },
);
tickers.publish([
  { symbol: 'QQQ', price: 500, source: 'dxlink', quote_ts: '2026-07-17T11:59:59Z' },
  { symbol: 'VIX', price: 18, source: 'dxlink', quote_ts: '2026-07-17T11:58:00Z' },
  { symbol: 'BTC/USD', price: 120000, source: 'dxlink', quote_ts: '2026-07-17T11:59:59Z' },
]);
assert.equal(tickers.get('QQQ').state, 'extended');
assert.equal(tickers.get('VIX').state, 'stale', 'one stale symbol must not change another ticker');
assert.equal(tickers.get('BTC/USD').state, 'live');
tickers.publish([
  { symbol: 'VIX', price: 17.5, source: 'yfinance', quote_ts: null },
]);
assert.equal(tickers.get('VIX').state, 'fallback', 'YFinance retrieval cannot imply a live market event');
tickers.publishFallback({ QQQ: { price: 490 } }, '2026-07-17T12:00:00Z');
assert.equal(tickers.get('QQQ').price, 500, 'fallback must not replace a healthy live quote');
tickers.publish([
  { symbol: 'QQQ', price: 490, source: 'yfinance', quote_ts: null },
]);
assert.equal(tickers.get('QQQ').price, 500, 'undated YFinance must not replace fresh DXLink');
assert.equal(tickers.get('QQQ').source, 'dxlink', 'fresh live source retains priority');

const shareNow = Date.parse('2026-07-30T14:30:10Z');
const shareTickers = new TickerStateStore(
  { AAPL: 'equity' },
  { nowFn: () => shareNow, staleAfterMs: SHARE_QUOTE_MAX_AGE_MS },
);
shareTickers.publish([{
  symbol: 'AAPL', price: 100, bid: 110, ask: 111, source: 'dxlink',
  quote_ts: '2026-07-30T14:30:05Z',
  bid_ts: '2026-07-30T14:30:04Z', ask_ts: '2026-07-30T14:30:05Z',
}]);
const freshShare = freshShareQuote(shareTickers.get('AAPL'), shareNow);
assert.equal(freshShare.mid, 110.5, 'share marks use the fresh two-sided midpoint, not an older last trade');
assert.equal(shareTickers.get('AAPL').bid_ts, '2026-07-30T14:30:04Z', 'ticker state preserves bid timestamps');
assert.equal(shareTickers.get('AAPL').ask_ts, '2026-07-30T14:30:05Z', 'ticker state preserves ask timestamps');
assert.equal(freshShareQuote({ ...shareTickers.get('AAPL'), ask_ts: null }, shareNow), null,
  'a two-sided share quote requires the ask timestamp');
assert.equal(freshShareQuote({ ...shareTickers.get('AAPL'), bid_ts: '2026-07-30T14:29:00Z' }, shareNow), null,
  'a stale side disables the whole share quote');

(async () => {
  const live = new LiveQuoteService();
  live.setVisibleContracts(['QQQ260717C00600000']);
  let resolveFetch;
  let calls = 0;
  const responsePromise = new Promise(resolve => { resolveFetch = resolve; });
  const poller = new LiveQuotePoller(live, {
    fetchFn: () => {
      calls++;
      return responsePromise;
    },
    nowFn: () => Date.parse('2026-07-17T14:30:05Z'),
    randomFn: () => 0.5,
    isHidden: () => false,
  });
  poller.running = true;
  const first = poller.poll();
  poller.poll();
  assert.equal(calls, 1, 'overlapping poll calls should share one in-flight request');
  resolveFetch({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({
      server_ts: '2026-07-17T14:30:05Z',
      quotes: [{
        symbol: 'QQQ260717C00600000', bid: 1, ask: 1.2, strike: 600,
        type: 'call', exp: '2026-07-17', quote_ts: '2026-07-17T14:30:04Z',
      }],
    }),
  });
  await first;
  assert.equal(live.get('QQQ260717C00600000').source, 'dxlink-live');
  assert.equal(live.get('QQQ260717C00600000').observedAt, '2026-07-17T14:30:04Z');
  assert.equal(poller.health.state, 'live');
  assert.equal(poller.health.quoteAgeMs, 1000);
  assert.equal(poller._normalInterval(), 5000);
  poller.stop();

  const hiddenPoller = new LiveQuotePoller(new LiveQuoteService(), { isHidden: () => true });
  assert.equal(hiddenPoller._normalInterval(), 60_000);
  hiddenPoller.dispose();

  // ── botPositions is an independent interest slot ────────────────────────────
  // The Automated tab and the viewer's own book share one quote transport, so
  // registering bot contracts must not evict the user's positions (or vice
  // versa) -- that would blank out marks on whichever tab rendered second.
  {
    const svc = new LiveQuoteService();
    svc.setOpenPositions(['MINE']);
    svc.setBotPositions(['BOT_A', 'BOT_B']);
    svc.publish([
      { symbol: 'MINE',  bid: 1, ask: 1.2, strike: 500, type: 'call', exp: '2026-07-16' },
      { symbol: 'BOT_A', bid: 2, ask: 2.4, strike: 501, type: 'put',  exp: '2026-07-16' },
      { symbol: 'OTHER', bid: 9, ask: 9.9, strike: 502, type: 'call', exp: '2026-07-16' },
    ], { source: 'test' });

    assert.equal(svc.get('MINE').mid, 1.1, 'user positions survive bot registration');
    assert.equal(svc.get('BOT_A').mid, 2.2, 'bot positions are quoted');
    assert.equal(svc.get('OTHER'), null, 'unregistered symbols are still ignored');
    assert.deepEqual(svc.interestSymbols(), ['BOT_A', 'BOT_B', 'MINE']);

    // Leaving the Automated tab drops bot-only quotes but keeps the user's.
    svc.setBotPositions([]);
    assert.equal(svc.get('BOT_A'), null, 'clearing bot interest prunes bot-only quotes');
    assert.equal(svc.get('MINE').mid, 1.1, 'clearing bot interest leaves the user book intact');
  }

  // ── A closed quote lane is only closed on explicit producer evidence ────────
  // The producer reports `offline` in three different situations: no session has
  // started yet, a session died (auth circuit, token refresh, unhandled
  // exception), and the deliberate end of a trading session. Only the last one
  // may be shown as closed; hiding a trading-hours outage behind that label
  // would be worse than a red badge. These cases run against a real HTTP
  // server, so the bodies, statuses and Retry-After headers are the wire ones.
  {
    const NOW = Date.parse('2026-11-10T23:30:00Z');
    const bodies = {
      '/closed': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'offline', session: 'session_closed', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/waiting': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'offline', session: 'waiting_for_session', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/failed': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'offline', session: 'session_failed', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/uninitialized': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'offline', session: 'uninitialized', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/connecting': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'connecting', session: 'open', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/legacy': { status: 503, retry: '5', body: {
        error: 'Live quote feed unavailable',
        health: { state: 'offline', last_quote_at: null, server_ts: '2026-11-10T23:30:00Z' } } },
      '/live': { status: 200, retry: null, body: {
        server_ts: '2026-11-10T23:30:00Z',
        health: { state: 'live', session: 'open', server_ts: '2026-11-10T23:30:00Z' },
        quotes: [{ symbol: 'QQQ261110C00677000', bid: 1, ask: 1.2, strike: 677,
          type: 'call', exp: '2026-11-10', quote_ts: '2026-11-10T23:30:00Z',
          bid_ts: '2026-11-10T23:30:00Z', ask_ts: '2026-11-10T23:30:00Z' }] } },
    };
    const server = http.createServer((req, res) => {
      const spec = bodies[new URL(req.url, 'http://127.0.0.1').pathname] || bodies['/legacy'];
      const headers = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
      if (spec.retry) headers['Retry-After'] = spec.retry;
      res.writeHead(spec.status, headers);
      res.end(JSON.stringify(spec.body));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const once = async (route) => {
      const svc = new LiveQuoteService();
      svc.setVisibleContracts(['QQQ261110C00677000']);
      const poller = new LiveQuotePoller(svc, {
        endpoint: `${origin}${route}`, nowFn: () => NOW, randomFn: () => 0.5, isHidden: () => false,
      });
      poller.running = true;
      await poller.poll();
      const health = { ...poller.health };
      poller.stop();
      return health;
    };

    const closed = await once('/closed');
    assert.equal(closed.state, 'closed', 'a deliberate session end is the only thing shown as closed');
    assert.equal(closed.sessionState, 'session_closed');
    assert.equal(closed.serviceState, 'offline');
    assert.equal(Date.parse(closed.retryAt) - NOW, 60_000, 'a closed lane is probed once a minute');
    assert.equal(closed.message, 'The option quote feed is closed for this session');

    const failed = await once('/failed');
    assert.equal(failed.state, 'unavailable', 'a session failure is a failure, not a closed lane');
    assert.equal(failed.sessionState, 'session_failed');
    assert.equal(Date.parse(failed.retryAt) - NOW, 5_000, 'the producer Retry-After still governs the retry');

    // A cold restart after hours sits in the scheduler's deliberate wait for the
    // next session: closed, and labelled as waiting rather than as an outage.
    const waiting = await once('/waiting');
    assert.equal(waiting.state, 'closed', 'a deliberate wait for the next session is a closed lane');
    assert.equal(waiting.sessionState, 'waiting_for_session');
    assert.equal(Date.parse(waiting.retryAt) - NOW, 60_000, 'and it is probed once a minute');

    const uninitialized = await once('/uninitialized');
    assert.equal(uninitialized.state, 'unavailable', 'a collector that has not opened a session is not "closed"');

    const connecting = await once('/connecting');
    assert.equal(connecting.state, 'unavailable', 'an in-session feed outage stays a failure');
    assert.equal(connecting.serviceState, 'connecting');

    const legacy = await once('/legacy');
    assert.equal(legacy.state, 'unavailable', 'an older producer with no session field is never assumed closed');

    // Repeated failures still escalate, so a real outage is visible and backed off.
    {
      const svc = new LiveQuoteService();
      svc.setVisibleContracts(['QQQ261110C00677000']);
      const poller = new LiveQuotePoller(svc, {
        endpoint: `${origin}/failed`, nowFn: () => NOW, randomFn: () => 0.5, isHidden: () => false,
      });
      poller.running = true;
      await poller.poll();
      await poller.poll();
      assert.equal(poller.health.state, 'backing-off', 'a failing lane still walks the backoff ladder');
      assert.equal(poller.failureCount, 2);
      poller.stop();
    }

    const live = await once('/live');
    assert.equal(live.state, 'live', 'a real answer clears the lane');
    assert.equal(live.sessionState, 'open');
    await new Promise(resolve => server.close(resolve));
  }

  console.log('PASS live quote service polling, interests, health, closed-vs-failed lane handling over real HTTP, and deduplication');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
