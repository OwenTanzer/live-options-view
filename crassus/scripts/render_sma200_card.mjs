// Local-only render of the shipped Automated card with an SMA(200) fixture.
// Usage:
//   NODE_PATH=<playwright node_modules> TEST_CHROMIUM_PATH=<chromium> \
//     node crassus/scripts/render_sma200_card.mjs
// No network, account or production state is used.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const html = fs.readFileSync(path.join(root, 'site/index.html'), 'utf8');
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const source = fs.readFileSync(path.join(root, 'site/js/bots-panel.js'), 'utf8');
const out = path.join(root, 'crassus/scripts/fixtures');

const bot = {
  alias: 'SPY 200D',
  username: 'crassus_spy200',
  strategy_id: 'spy_sma200_double_or_out',
  balance_cash: -10000,
  starting_balance: 10000,
  trade_count: 2,
  first_trade_ts: '2026-01-02T15:30:00Z',
  last_trade_ts: '2026-01-05T15:31:00Z',
  account_closed: false,
  positions: [{ sym: 'SPY', instrument_type: 'share', multiplier: 1, qty: 40, avg_price: 500 }],
  strategy_state: {
    strategy: 'spy_sma200_double_or_out', symbol: 'SPY', window: 200,
    signal_date: '2026-01-02', close: 601.25, sma: 590.1,
    prior_close: 588.4, prior_sma: 589.7, signal: 'bullish_cross',
    target: '2x_long', position: '2x_long', position_qty: 40,
    equity: 10000, ask: 500, pending: false,
    reason: 'Bullish crossover 2026-01-02: buying 40 SPY shares at the next regular-session ask 500.00 for 2.0x entry equity.',
    adjustment_basis: 'adjusted_close',
    execution_convention: 'next_regular_session_executable_quote',
  },
};

const pageHtml = `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head>
<body style="background:var(--bg);color:var(--fg);padding:16px;font-family:system-ui,sans-serif">
<h1 style="font-size:18px;margin-bottom:12px">Automated · local SMA(200) fixture</h1>
<section id="bots-panel"><div id="bots-status"></div><div id="bots-by-strategy"></div><div id="bots-grid"></div></section>
<script>${source}</script>
</body></html>`;

const browser = await chromium.launch({ headless: true,
  ...(process.env.TEST_CHROMIUM_PATH ? { executablePath: process.env.TEST_CHROMIUM_PATH } : {}) });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
await context.route('**/*', route => route.fulfill({
  contentType: 'text/html', body: pageHtml,
}));
const page = await context.newPage();
await page.goto('http://sma200-fixture.invalid/');
await page.evaluate(({ fixture, moduleSource }) => {
  const scope = {
    document,
    liveQuotes: { get: () => ({ mid: 520, observedAt: new Date().toISOString() }), setBotPositions() {} },
    fetch: async () => { throw new Error('fixture must not fetch'); },
    setInterval: () => { throw new Error('fixture must not start a timer'); },
    clearInterval() {},
    fmtQ: v => String(v),
    fmtMoney: v => `${v < 0 ? '−' : v > 0 ? '+' : ''}$${Math.abs(v).toFixed(2)}`,
    pnlClass: v => v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : '',
    escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    occRoot: () => 'QQQ',
  };
  const panel = new Function(...Object.keys(scope), moduleSource + ';return {renderBots,set data(v){botsData=v}};')(...Object.values(scope));
  const data = { bots: [fixture], as_of: '2026-01-05T16:00:00Z' };
  const before = JSON.stringify(data);
  panel.data = data;
  panel.renderBots();
  if (JSON.stringify(data) !== before) throw new Error('fixture mutated');
}, { fixture: bot, moduleSource: source });
await page.screenshot({ path: path.join(out, 'sma200-card-desktop.png'), fullPage: true });
await page.setViewportSize({ width: 390, height: 900 });
await page.screenshot({ path: path.join(out, 'sma200-card-narrow.png'), fullPage: true });
fs.writeFileSync(path.join(out, 'sma200-card.html'), pageHtml);
console.log(JSON.stringify({
  status: 'rendered',
  desktop: path.join(out, 'sma200-card-desktop.png'),
  narrow: path.join(out, 'sma200-card-narrow.png'),
  cardText: await page.locator('.bot-card').innerText(),
}));
await browser.close();
