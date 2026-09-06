const assert = require('node:assert/strict');

(async () => {
  const { default: worker, derivePasswordHash, randomSaltBase64 } = await import('../worker.js');
  const users = new Map(), sessions = new Map(), objects = new Map();
  let revision = 0, failFinalization = false, quotes = 0;
  const kv = store => ({
    get: async key => store.get(key) ?? null,
    put: async (key, value) => { store.set(key, value); },
    delete: async key => { store.delete(key); },
    list: async ({ prefix }) => ({ keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) }),
  });
  const env = { USERS: kv(users), SESSIONS: kv(sessions), BOT_REGISTRATION_KEY: 'fixture-operator',
    LIVE_QUOTE_ORIGIN: 'https://fixture.invalid', LIVE_QUOTE_KEY: 'fixture-key',
    PAPER_TRADES: {
      async get(key) {
        const x = objects.get(key);
        return x ? { etag: x.etag, json: async () => JSON.parse(x.value) } : null;
      },
      async put(key, value, options = {}) {
        if (failFinalization && JSON.parse(value).status === 'rejected') {
          failFinalization = false;
          throw new Error('injected R2 finalization failure');
        }
        const current = objects.get(key);
        if (options.onlyIf?.etagDoesNotMatch === '*' && current) return null;
        if (options.onlyIf?.etagMatches && current?.etag !== options.onlyIf.etagMatches) return null;
        const etag = `e${++revision}`;
        objects.set(key, { value, etag });
        return { etag };
      },
    },
  };
  const realFetch = global.fetch;
  global.fetch = async () => {
    quotes++;
    return Response.json({ quotes: [{ symbol: 'AAPL', instrument_class: 'equity',
      bid: 99, ask: 100, bid_ts: new Date().toISOString(), ask_ts: new Date().toISOString() }] });
  };
  const call = (path, body, cookie = '', headers = {}) => worker.fetch(new Request(`https://fixture.invalid${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', Cookie: cookie, ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }), env);
  const password = 'fixture-password';
  async function seed(username, isBot = true) {
    const salt = randomSaltBase64();
    const record = { username, salt, hash: await derivePasswordHash(password, salt, 1000), iterations: 1000,
      version: 0, balance_cash: 5, trades: [], is_bot: isBot };
    users.set(`user:${username}`, JSON.stringify(record));
    if (isBot) users.set(`bot:${username}`, JSON.stringify({ username }));
    sessions.set(`sess:${username}`, JSON.stringify({ username }));
    return `session=${username}`;
  }
  const intent = id => ({ execution_request_id: id, sym: 'AAPL', instrument_type: 'share', side: 'buy', qty: 1 });
  try {
    const cookie = await seed('fixture_bot');
    const id = '12345678-1234-4234-8234-123456789abc';
    failFinalization = true;
    assert.equal((await call('/api/paper-trade', intent(id), cookie)).status, 503);
    const closed = JSON.parse(users.get('user:fixture_bot'));
    assert.equal(closed.account_closed, true);
    assert.equal(closed.closure_reason, 'insufficient_balance');
    assert.equal(closed.balance_cash, 5);
    assert.deepEqual(closed.trades, []);
    assert.equal(closed.closure_execution.execution_request_id, id);
    const quoteCount = quotes;
    const replay = await call('/api/paper-trade', intent(id), cookie);
    assert.equal(replay.status, 400);
    assert.equal((await replay.json()).account_closed, true);
    assert.equal(quotes, quoteCount, 'closure recovery must not obtain a new quote');
    assert.equal(JSON.parse(objects.get(`paper-trades/requests/${id}.json`).value).status, 'rejected');
    assert.equal((await call('/api/paper-trade', intent(id), cookie)).status, 400);
    const next = intent('22345678-1234-4234-8234-123456789abc');
    assert.equal((await call('/api/paper-trade', next, cookie)).status, 403);
    assert.equal((await call('/api/paper-trade', { ...next, side: 'sell' }, cookie)).status, 403);
    assert.equal(quotes, quoteCount, 'closed accounts cannot attempt further executions');
    assert.equal((await call('/api/me', undefined, cookie)).status, 200, 'history remains readable');
    assert.equal((await (await call('/api/me', undefined, cookie)).json()).account_closed, true);
    const login = await call('/api/login', { username: 'fixture_bot', password });
    assert.equal(login.status, 200, 'restart can authenticate without re-registration');
    assert.equal((await call('/api/register', { username: 'fixture_bot', password }, '',
      { 'X-Bot-Registration-Key': 'fixture-operator' })).status, 409);
    assert.equal(JSON.parse(users.get('user:fixture_bot')).balance_cash, 5, 'cannot receive a fresh starting balance');

    const human = await seed('fixture_human', false);
    const h = await call('/api/paper-trade', intent('32345678-1234-4234-8234-123456789abc'), human);
    assert.equal(h.status, 400);
    assert.equal(JSON.parse(users.get('user:fixture_human')).account_closed, undefined);
    for (const username of ['crassus_freuding_phelps', 'crassus_trumpwhisp_phelps', 'crassus_newton_phelps']) {
      assert.equal((await call('/api/register', { username, password })).status, 400);
      assert.equal((await call('/api/register', { username, password }, '',
        { 'X-Bot-Registration-Key': 'wrong' })).status, 403);
      assert.equal((await call('/api/register', { username, password }, '',
        { 'X-Bot-Registration-Key': 'fixture-operator' })).status, 201);
      assert.equal((await call('/api/bot-metadata', { username, alias: 'Phelps fixture', strategy_id: 'momentum_qqq' }, '',
        { 'X-Bot-Registration-Key': 'fixture-operator' })).status, 200);
    }
    const roster = await (await call('/api/bots')).json();
    assert.equal(roster.bots.find(b => b.username === 'fixture_bot').account_closed, true);
    console.log('PASS: terminal bot closure, crash-safe rejection replay, preserved history, no reopening, bot username compatibility');
  } finally { global.fetch = realFetch; }
})().catch(error => { console.error(error); process.exitCode = 1; });
