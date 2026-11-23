// Multi-underlying browser acceptance (#182, extended for #219/#178): QQQ ->
// SPY -> IBIT -> QQQ switching without cross-contamination, desktop + narrow,
// with screenshots.
// Optional rendered integration: NODE_PATH=<playwright installation> node tests/multi_underlying_browser.test.js
// Synthetic snapshots; no production requests, authentication or trading.
// Screenshots land in docs/evidence/multi-underlying/ (committed as evidence).
//
// #219: IBIT is published on a MIXED strike ladder (whole dollars 30–39 and
// 51–59, half dollars 39.5–50) — the exact strikes in the November 10
// snapshot. The renderer used to synthesize `atm + offset`, so 30 of the 41
// displayed strikes had no row and rendered two empty cells. This run asserts
// the rendered ladder is exactly the published one and that no cell is empty.
// #178: IBIT's snapshot is frozen at 16:14 ET, so the header must call it a
// retained after-hours reading rather than looking like a current price.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const EVIDENCE = path.join(__dirname, '../docs/evidence/multi-underlying');
const CLOCK = '2026-11-09T21:20:00Z';   // 13:20 PT / 16:20 ET, after the close

function row(root, yymmdd, strike, type, exp, spot) {
  const occ = `${root}${yymmdd}${type === 'call' ? 'C' : 'P'}${String(Math.round(strike * 1000)).padStart(8, '0')}`;
  return {
    OptionSymbol: occ, Strike: strike, Type: type, Expiration: exp,
    OpenInterest: type === 'call' ? 1200 : 900, Volume: 45,
    Bid: 1.1, Mid: 1.2, Ask: 1.3, Last: 1.25, IV: 0.22,
    Delta: type === 'call' ? 0.5 : -0.5, Gamma: 0.01, Theta: -0.05, Vega: 0.1,
    UnderlyingPrice: spot,
  };
}
// The strikes IBIT actually published on 2026-11-10.
const IBIT_LADDER = [30, 31, 32, 33, 34, 35, 36, 37, 38, 39,
  39.5, 40, 40.5, 41, 41.5, 42, 42.5, 43, 43.5, 44, 44.5, 45, 45.5, 46,
  46.5, 47, 47.5, 48, 48.5, 49, 49.5, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59];
const STRIKES = {
  // A realistic contiguous $1 ladder either side of the ±20 window, so the
  // QQQ/SPY runs prove nothing moved for them.
  QQQ: Array.from({ length: 81 }, (_, i) => 636 + i),
  SPY: Array.from({ length: 71 }, (_, i) => 701 + i),
  IBIT: IBIT_LADDER,
};
function snapshot(symbol, { date, expiration, spot, ts, snapshotTime }) {
  const yymmdd = expiration.slice(2).replaceAll('-', '');
  const rows = STRIKES[symbol].flatMap(strike => [
    row(symbol, yymmdd, strike, 'call', expiration, spot),
    row(symbol, yymmdd, strike, 'put', expiration, spot),
  ]);
  return {
    symbol, timestamp: ts, snapshot_time: snapshotTime, date, expiration, tier: '0DTE_Regular',
    underlying_price: spot, snapshot_key: `intraday/${symbol.toLowerCase()}/snapshot.csv`, rows,
  };
}
// Synthetic OI calibration so the heatmap colors (and the evidence
// screenshots) are not stuck on the "calibration failed" warning. Offsets
// cover the ATM window the heatmap renders around the snapshot's strikes.
const OIRANGES_CSV = 'Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj\n' +
  Array.from({ length: 41 }, (_, i) => `0DTE_Regular,${i - 20},100,500,1000,5000,100,500,1000,5000,1.0,1.0`).join('\n');
const snapshots = {
  'intraday/latest.json': snapshot('QQQ', {
    date: '2026-11-09', expiration: '2026-11-09', spot: 656.51,
    ts: '11/9/2026 13:19:00', snapshotTime: '16:19 ET',
  }),
  'intraday/spy/latest.json': snapshot('SPY', {
    date: '2026-11-09', expiration: '2026-11-09', spot: 736.0,
    ts: '11/9/2026 13:19:20', snapshotTime: '16:19 ET',
  }),
  // Frozen at 16:14 ET six minutes before the page clock: still the published
  // chain, no longer a current one (#178).
  'intraday/ibit/latest.json': snapshot('IBIT', {
    date: '2026-11-09', expiration: '2026-11-09', spot: 41.45,
    ts: '11/9/2026 13:14:00', snapshotTime: '16:14 ET',
  }),
};
const PUBLISHED_IBIT = new Set(STRIKES.IBIT.map(String));
const OCC = /^([A-Z]+)(\d{6})([CP])(\d{8})$/;
// Every /api/live-quotes request the page makes, as the symbol list it asked
// for: this is the evidence that quote interest follows published contracts.
const quoteLog = [];

(async () => {
  const { default: worker } = await import('../backend/index.js');
  const browser = await chromium.launch({ headless: true, channel: 'chromium' });
  const originalFetch = global.fetch;
  const live = { ...snapshots };
  global.fetch = async url => {
    const key = new URL(url).pathname.slice(1);
    if (key === 'derived/OIranges.csv') return new Response(OIRANGES_CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } });
    if (key in live) return new Response(JSON.stringify(live[key]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    return new Response('', { status: 404 });
  };
  fs.mkdirSync(EVIDENCE, { recursive: true });
  const quoteResponse = symbols => {
    const quotes = symbols.map(symbol => {
      const m = OCC.exec(symbol);
      if (!m) return null;
      const now = new Date(CLOCK).toISOString();
      return { symbol, bid: 0.45, ask: 0.48, quote_ts: now, strike: Number(m[4]) / 1000,
        type: m[3] === 'C' ? 'call' : 'put', exp: `20${m[2].slice(0, 2)}-${m[2].slice(2, 4)}-${m[2].slice(4, 6)}` };
    }).filter(Boolean);
    return { quotes, requested: symbols.length, returned: quotes.length, server_ts: new Date(CLOCK).toISOString() };
  };
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.clock.setFixedTime(new Date(CLOCK));
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin === APP && url.pathname === '/api/live-quotes') {
        const symbols = (url.searchParams.get('symbols') || '').split(',').filter(Boolean);
        quoteLog.push({ path: url.pathname, symbols });
        return route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify(quoteResponse(symbols)) });
      }
      if (url.origin === APP && url.pathname.startsWith('/browser-data/')) {
        const response = await worker.fetch(new Request(url), new Proxy({}, { get() { throw Error('binding access'); } }));
        return route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
      }
      if (url.origin === APP) {
        const filePath = 'site' + (url.pathname === '/' ? '/index.html' : url.pathname);
        if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) return route.fulfill({ path: filePath });
      }
      return route.fulfill({ status: 404, body: '{}', contentType: 'application/json' });
    });
    await page.goto(APP);
    // QQQ (default): 0DTE title, QQQ spot, QQQ rows, and a truthful header.
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('656.51'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr td.call-cell').length > 0);
    const qqqHeader = await page.evaluate(() => ({
      age: document.querySelector('#price-age').textContent,
      state: document.querySelector('#price-age').dataset.state,
      tier: document.querySelector('#tier-badge').textContent,
    }));
    assert.equal(qqqHeader.state, 'live', 'a one-minute-old QQQ snapshot is live');
    assert.match(qqqHeader.age, /^snapshot 16:19 ET · After hours · 1m ago$/, 'QQQ header carries time, session and age');
    assert.equal(qqqHeader.tier, 'Regular tier', 'the tier badge is labeled as a tier, not a session');
    assert.equal(await emptyCells(page), 0, 'no empty QQQ cells');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-qqq.png') });
    // SPY: 0DTE title, SPY spot, no QQQ rows left.
    await page.locator('#underlying-select').selectOption('SPY');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'SPY 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('736.00'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    assert.equal(await emptyCells(page), 0, 'no empty SPY cells');
    assert.equal((await strikeColumn(page)).length, 41, 'SPY renders the ±20 window of its published ladder');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-spy.png') });
    // IBIT: the mixed-ladder chain, still 0DTE, now a retained after-hours read.
    const beforeIbitQuotes = quoteLog.length;
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'IBIT 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('41.45'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    const ibitStrikes = await strikeColumn(page);
    assert.deepEqual(ibitStrikes,
      [53, 52, 51, 50, 49.5, 49, 48.5, 48, 47.5, 47, 46.5, 46, 45.5, 45, 44.5, 44, 43.5, 43, 42.5, 42,
        41.5, 41, 40.5, 40, 39.5, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30],
      'IBIT renders the published mixed ladder around the 41.5 ATM strike, highest first');
    assert.ok(ibitStrikes.every(s => PUBLISHED_IBIT.has(String(s))), 'no rendered strike was invented');
    assert.equal(await emptyCells(page), 0, 'every IBIT call and put cell is populated (#219)');
    const ibitHeader = await page.evaluate(() => ({
      age: document.querySelector('#price-age').textContent,
      state: document.querySelector('#price-age').dataset.state,
      title: document.querySelector('#price').title,
    }));
    assert.equal(ibitHeader.state, 'retained', 'a chain frozen at 16:14 ET is not presented as current');
    assert.equal(ibitHeader.age, 'snapshot 16:14 ET · After hours · 6m ago · retained');
    assert.match(ibitHeader.title, /not current prices/);
    const ibitQuotes = await waitForQuoteRequest(r => r.symbols.length && r.symbols.every(s => s.startsWith('IBIT')), beforeIbitQuotes);
    assert.ok(ibitQuotes.symbols.every(s => PUBLISHED_IBIT.has(String(Number(OCC.exec(s)[4]) / 1000))),
      'every requested IBIT contract is on the published ladder');
    assert.equal(new Set(ibitQuotes.symbols).size, ibitQuotes.symbols.length, 'no contract is requested twice');
    assert.equal(ibitQuotes.symbols.length, ibitStrikes.length * 2,
      'the quote request covers both sides of exactly the rendered strikes');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-ibit.png') });
    // Back to QQQ: the QQQ snapshot returns, uncontaminated by SPY/IBIT.
    const beforeQqqReturnQuotes = quoteLog.length;
    await page.locator('#underlying-select').selectOption('QQQ');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('656.51'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length === 41);
    const backToQqq = await waitForQuoteRequest(r => r.symbols.length && r.symbols.every(s => s.startsWith('QQQ')), beforeQqqReturnQuotes);
    assert.equal(backToQqq.symbols.filter(s => s.startsWith('IBIT')).length, 0, 'QQQ quote interest carries no IBIT contracts');
    assert.equal((await strikeColumn(page)).length, 41, 'QQQ ladder restored to its ±20 window');
    // Narrow (390px): the IBIT mixed-ladder chain renders populated without overflow.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'IBIT 0DTE Live OI');
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    assert.equal(await emptyCells(page), 0, 'the narrow IBIT chain is populated too');
    assert.deepEqual(await strikeColumn(page), ibitStrikes, 'desktop and mobile render the same strike ladder');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, 'narrow IBIT chain must not overflow horizontally');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'narrow-ibit.png') });
    await page.locator('#underlying-select').selectOption('SPY');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'SPY 0DTE Live OI');
    assert.equal(await emptyCells(page), 0, 'the narrow SPY chain is populated too');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'narrow-spy.png') });
    await page.locator('#underlying-select').selectOption('QQQ');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    assert.equal(await emptyCells(page), 0, 'the narrow QQQ chain is populated too');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'narrow-qqq.png') });
    // IBIT not published (404): explicit unavailable, nothing rendered as IBIT.
    delete live['intraday/ibit/latest.json'];
    await page.reload();
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'unavailable');
    assert.ok((await page.locator('#no-data').textContent()).length > 0, 'a missing IBIT snapshot explains itself');
    assert.equal(await page.evaluate(() => document.querySelector('#price-age').textContent), '',
      'a cleared chain leaves no stale snapshot line behind');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-ibit-missing.png') });
    assert.deepEqual(errors, [], 'no page errors across the switching run');
    console.log('Chromium: QQQ -> SPY -> IBIT -> QQQ switching, #219 mixed IBIT ladder populated at 1280px and 390px, '
      + 'published-contract quote interest, #178 retained header, and missing-IBIT unavailable passed '
      + '(synthetic snapshots, evidence in docs/evidence/multi-underlying/)');
  } finally { global.fetch = originalFetch; await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });

function strikeColumn(page) {
  return page.$$eval('#heatmap-body tr td.strike-cell', cells => cells.map(c => parseFloat(c.textContent)));
}
function emptyCells(page) {
  return page.$$eval('#heatmap-body tr', trs => trs.filter(tr => {
    const call = tr.querySelector('td.call-cell');
    const put = tr.querySelector('td.put-cell');
    return !call || !put || !call.textContent.trim() || !put.textContent.trim();
  }).length);
}
// Quote interest is published through the poller, one fetch behind the render,
// so wait for the request that proves it rather than reading a stale snapshot
// of the request log.
async function waitForQuoteRequest(predicate, fromIndex, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const match = quoteLog.slice(fromIndex).find(predicate);
    if (match) return match;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`no matching live-quotes request within ${timeoutMs}ms (saw ${JSON.stringify(quoteLog.slice(fromIndex).map(r => r.symbols.length))})`);
}
