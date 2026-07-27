const assert = require('node:assert/strict');
const {
  LiveQuoteService, LiveQuotePoller, TickerStateStore, tickerSessionState, parseRetryAfter,
} = require('../docs/shared.js');

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

  console.log('PASS live quote service polling, interests, health, and deduplication');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
