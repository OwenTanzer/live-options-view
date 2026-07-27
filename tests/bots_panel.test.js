// Exercises the Automated tab's rendering logic from docs/index.html.
//
// The panel's code lives in index.html's inline script, so it's extracted by
// its section markers and evaluated against a minimal DOM/quote shim. That
// keeps the test honest -- it runs the shipped source rather than a copy that
// can drift -- at the cost of depending on those two comment banners staying
// put. If this file starts failing with "could not locate", that's why.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'docs', 'index.html'), 'utf8');
const START = '// ── automated (bot) panel';
const END = '// ── historical panel';
const from = html.indexOf(START);
const to = html.indexOf(END, from);
assert.ok(from !== -1 && to !== -1, 'could not locate the automated-panel section in docs/index.html');
const source = html.slice(from, to);

// ── DOM + collaborator shim ───────────────────────────────────────────────────
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, textContent: '', innerHTML: '', classList: { toggle() {} } };
  return els[id];
}
const quotes = new Map();
const liveQuotes = {
  botSymbols: null,
  get: (s) => quotes.get(s) || null,
  setBotPositions(syms) { this.botSymbols = [...syms]; },
};

const scope = {
  document: { getElementById: el },
  fetch: async () => { throw new Error('not used in this test'); },
  setInterval: () => 1,
  clearInterval: () => {},
  liveQuotes,
  fmtQ: (v) => (v == null ? '' : v < 10 ? v.toFixed(2) : v.toFixed(1)),
  fmtMoney: (v) => `${v < 0 ? '−' : v > 0 ? '+' : ''}$${Math.abs(v).toFixed(2)}`,
  pnlClass: (v) => (v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : ''),
  escapeHtml: (s) => String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  )),
};

const names = Object.keys(scope);
const factory = new Function(...names, `${source}\nreturn { renderBots, botEquity, setBotsPolling, get botsData(){return botsData}, set botsData(v){botsData=v} };`);
const panel = factory(...names.map(n => scope[n]));

const pos = (sym, qty, avg) =>
  ({ sym, strike: 694, type: 'call', exp: '2026-07-27', qty, avg_price: avg });
const bot = (alias, username, cash, positions = [], extra = {}) =>
  ({ alias, username, balance_cash: cash, starting_balance: 10000,
     trade_count: positions.length, first_trade_ts: null, last_trade_ts: null,
     positions, created_at: null, ...extra });

// ── botEquity ─────────────────────────────────────────────────────────────────
{
  quotes.clear();
  quotes.set('X', { mid: 4.0 });

  // Flat account: equity is just cash.
  assert.deepEqual(panel.botEquity(bot('A', 'a', 10000)), { equity: 10000, openValue: 0, unmarked: 0 });

  // Long 2 @ mark 4.00 -> 2 * 4.00 * 100 = 800 on top of cash.
  const long = panel.botEquity(bot('B', 'b', 9000, [pos('X', 2, 3.0)]));
  assert.equal(long.equity, 9800);
  assert.equal(long.unmarked, 0);

  // Short positions reduce equity by the cost to buy them back.
  const short = panel.botEquity(bot('C', 'c', 11000, [pos('X', -3, 5.0)]));
  assert.equal(short.equity, 11000 - 1200);

  // An unquoted contract is counted as unmarked rather than valued at zero --
  // the panel must never present a confident total built from missing marks.
  const partial = panel.botEquity(bot('D', 'd', 9000, [pos('X', 1, 3.0), pos('NOQUOTE', 5, 1.0)]));
  assert.equal(partial.unmarked, 1, 'a contract with no quote is reported unmarked');
  assert.equal(partial.equity, 9400, 'equity excludes the unmarked leg rather than treating it as worthless');
}

// ── renderBots ────────────────────────────────────────────────────────────────
{
  quotes.clear();
  quotes.set('X', { mid: 4.0 });

  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      bot('Winner', 'crassus_win', 12000, [pos('X', 1, 1.0)]),
      bot('Loser', 'crassus_lose', 8000),
    ],
  };
  panel.renderBots();

  const out = els['bots-grid'].innerHTML;
  assert.match(out, /Winner/);
  assert.match(out, /Loser/);
  assert.match(els['bots-status'].textContent, /2 accounts/);

  // The leader border marks exactly the top account by equity.
  assert.equal((out.match(/bot-card leader/g) || []).length, 1, 'exactly one leader is highlighted');
  const winnerCard = out.slice(Math.max(0, out.indexOf('Winner') - 200), out.indexOf('Loser'));
  assert.match(winnerCard, /leader/, 'the highest-equity account is the one marked leader');

  // A flat account renders the explicit flat state, not an empty table.
  assert.match(out, /flat — no open positions/);

  // Bot contracts are registered for quoting.
  assert.deepEqual(liveQuotes.botSymbols, ['X']);
}

// ── XSS: a hostile alias must not execute ─────────────────────────────────────
{
  quotes.clear();
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [bot('<img src=x onerror=alert(1)>', 'crassus_evil', 10000)],
  };
  panel.renderBots();
  const out = els['bots-grid'].innerHTML;
  assert.equal(out.includes('<img src=x'), false, 'a hostile alias must be escaped, not injected');
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
}

// ── empty roster ──────────────────────────────────────────────────────────────
{
  panel.botsData = { as_of: '2026-07-27T15:00:00Z', bots: [] };
  panel.renderBots();
  assert.equal(els['bots-grid'].innerHTML, '');
  assert.match(els['bots-status'].textContent, /no automated accounts registered yet/);
}

console.log('PASS automated tab equity, leader, escaping, and empty states');
