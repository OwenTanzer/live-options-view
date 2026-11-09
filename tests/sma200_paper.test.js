const assert = require('node:assert/strict');

(async () => {
  const { default: worker, derivePasswordHash, randomSaltBase64, updateBotIndex } = await import('../backend/index.js');
  const users = new Map(), sessions = new Map(), objects = new Map();
  let revision = 0;
  const kv = store => ({
    get: async key => store.get(key) ?? null,
    put: async (key, value) => { store.set(key, value); },
    delete: async key => { store.delete(key); },
    list: async ({ prefix }) => ({ keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) }),
  });
  const bucket = {
    async get(key) {
      const item = objects.get(key);
      return item ? { etag: item.etag, json: async () => JSON.parse(item.value) } : null;
    },
    async put(key, value, options = {}) {
      const current = objects.get(key);
      if (options.onlyIf?.etagDoesNotMatch === '*' && current) return null;
      if (options.onlyIf?.etagMatches && current?.etag !== options.onlyIf.etagMatches) return null;
      const etag = `e${++revision}`;
      objects.set(key, { value, etag });
      return { etag };
    },
  };
  const env = {
    USERS: kv(users), SESSIONS: kv(sessions), PAPER_TRADES: bucket,
    BOT_REGISTRATION_KEY: 'fixture-operator',
    LIVE_QUOTE_ORIGIN: 'https://fixture.invalid', LIVE_QUOTE_KEY: 'fixture-key',
  };
  const realFetch = global.fetch;
  let quote = { bid: 499, ask: 500 };
  global.fetch = async () => Response.json({ quotes: [{
    symbol: 'SPY', instrument_class: 'equity', bid: quote.bid, ask: quote.ask,
    bid_ts: new Date().toISOString(), ask_ts: new Date().toISOString(),
  }] });
  const call = (path, body = undefined, cookie = '', headers = {}) => worker.fetch(new Request(`https://fixture.invalid${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', Cookie: cookie, ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }), env);
  const password = 'fixture-password';
  async function seed(username, extra = {}) {
    const salt = randomSaltBase64();
    const record = {
      username, salt, hash: await derivePasswordHash(password, salt, 1000), iterations: 1000,
      version: 0, balance_cash: 10_000, starting_balance: 10_000, trades: [], is_bot: true,
      strategy_id: 'spy_sma200_double_or_out', ...extra,
    };
    users.set(`user:${username}`, JSON.stringify(record));
    sessions.set(`sess:${username}`, JSON.stringify({ username }));
    return `session=${username}`;
  }
  const uuid = n => `12345678-1234-4234-8234-1234567890${String(n).padStart(2, '0')}`;
  try {
    const cookie = await seed('sma_fixture', { paper_margin_limit: 2, paper_financing_rate: 0.06 });
    const buy = await call('/api/paper-trade', {
      execution_request_id: uuid(1), sym: 'SPY', instrument_type: 'share', side: 'buy', qty: 40,
    }, cookie);
    assert.equal(buy.status, 201, await buy.text());
    const bought = JSON.parse(users.get('user:sma_fixture'));
    assert.equal(bought.balance_cash, -10_000, '2x share entry creates a paper loan');
    assert.equal(bought.trades.length, 1);
    assert.equal(bought.trades[0].instrument_type, 'share');
    assert.equal(bought.trades[0].multiplier, 1);
    assert.equal(bought.trades[0].qty, 40);

    // Backdate the entry to exercise one year of 6% financing on the loan.
    bought.trades[0].ts = new Date(Date.now() - 365 * 86_400_000).toISOString();
    users.set('user:sma_fixture', JSON.stringify(bought));
    quote = { bid: 510, ask: 511 };
    const sell = await call('/api/paper-trade', {
      execution_request_id: uuid(2), sym: 'SPY', instrument_type: 'share', side: 'sell', qty: 40,
    }, cookie);
    assert.equal(sell.status, 201, await sell.text());
    const sold = JSON.parse(users.get('user:sma_fixture'));
    assert.equal(sold.trades.length, 2);
    assert.equal(sold.trades[1].side, 'sell');
    assert.ok(Math.abs(sold.trades[1].financing_cost - 600) < 1, JSON.stringify(sold.trades[1]));
    assert.ok(sold.balance_cash > 0, 'sale repays the loan and financing');

    // State metadata is public-roster material and must round-trip as a flat
    // allowlisted object; arbitrary nested data is rejected.
    const meta = await call('/api/bot-metadata', {
      username: 'sma_fixture', strategy_id: 'spy_sma200_double_or_out',
      strategy_state: { signal_date: '2026-01-02', signal: 'bullish_cross',
        close: 600, sma: 590, target: '2x_long', position: '2x_long',
        reason: 'fixture signal', nested: { bad: true } },
    }, '', { 'X-Bot-Registration-Key': 'fixture-operator' });
    assert.equal(meta.status, 400, 'nested strategy state must be rejected');
    const ok = await call('/api/bot-metadata', {
      username: 'sma_fixture',
      strategy_state: { signal_date: '2026-01-02', signal: 'bullish_cross',
        close: 600, sma: 590, target: '2x_long', position: '2x_long',
        reason: 'fixture signal' },
    }, '', { 'X-Bot-Registration-Key': 'fixture-operator' });
    assert.equal(ok.status, 200, await ok.text());
    const state = JSON.parse(users.get('user:sma_fixture')).strategy_state;
    assert.equal(state.signal, 'bullish_cross');
    assert.equal(state.target, '2x_long');
    await updateBotIndex(env, ['sma_fixture'], { ready: true });
    const roster = await (await call('/api/bots')).json();
    const card = roster.bots.find(b => b.username === 'sma_fixture');
    assert.equal(card.strategy_state.signal, 'bullish_cross');
    assert.equal(card.paper_margin_limit, 2);

    // A human or a levered non-flat account cannot borrow through this path.
    const human = await seed('human_fixture', { is_bot: false, paper_margin_limit: 2 });
    const humanBuy = await call('/api/paper-trade', {
      execution_request_id: uuid(3), sym: 'SPY', instrument_type: 'share', side: 'buy', qty: 40,
    }, human);
    assert.equal(humanBuy.status, 400);

    const levered = await seed('sma_extra', { paper_margin_limit: 2, paper_financing_rate: 0.06 });
    quote = { bid: 499, ask: 500 };
    assert.equal((await call('/api/paper-trade', {
      execution_request_id: uuid(4), sym: 'SPY', instrument_type: 'share', side: 'buy', qty: 40,
    }, levered)).status, 201);
    const extra = await call('/api/paper-trade', {
      execution_request_id: uuid(5), sym: 'SPY', instrument_type: 'share', side: 'buy', qty: 1,
    }, levered);
    assert.equal(extra.status, 400, 'a leveraged bot cannot add a second non-flat entry');
    assert.equal(JSON.parse(users.get('user:sma_extra')).account_closed, true);
    console.log('PASS: SPY share, 2x paper margin, financing, flat-entry guard and strategy-state allowlist');
  } finally {
    global.fetch = realFetch;
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
