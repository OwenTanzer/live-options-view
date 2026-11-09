// G4 isolated paper lifecycle against the real Worker modules.
//
// register -> enter -> ATM changes while the old contract is held -> deferred
// exit -> restart/reconnect -> new ATM entry -> settle -> multi-client roster.
// Also exercises a failed registration and an interrupted membership update.
// All storage is in memory; no network, account or production access.
const assert = require('node:assert/strict');

(async () => {
  const { default: worker, computeBookFromTrades, updateBotIndex, BOT_INDEX_KEY } =
    await import('../backend/index.js');

  const users = new Map(), sessions = new Map(), objects = new Map();
  let revision = 0, listCalls = 0, r2Gets = 0, r2Puts = 0;
  let failIndexWrites = false;
  const kv = store => ({
    get: async key => store.get(key) ?? null,
    put: async (key, value) => { store.set(key, value); },
    delete: async key => { store.delete(key); },
    list: async ({ prefix }) => {
      listCalls++;
      return { keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) };
    },
  });
  const bucket = {
    async get(key) {
      r2Gets++;
      const current = objects.get(key);
      return current ? { etag: current.etag, json: async () => JSON.parse(current.value) } : null;
    },
    async put(key, value, options = {}) {
      r2Puts++;
      if (failIndexWrites && key === BOT_INDEX_KEY) throw new Error('injected membership interruption');
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
    BOT_REGISTRATION_KEY: 'fixture-operator', BOT_INDEX_READS: 'r2',
    LIVE_QUOTE_ORIGIN: 'https://fixture.invalid', LIVE_QUOTE_KEY: 'fixture-key',
  };
  await updateBotIndex(env, [], { ready: true });

  const password = 'fixture-password';
  const call = (path, body, cookie = '', headers = {}) => worker.fetch(new Request(`https://fixture.invalid${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', Cookie: cookie, ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }), env);
  const cookieOf = response => (response.headers.get('Set-Cookie') || '').split(';')[0];
  const position = (username, symbol) => {
    const record = JSON.parse(users.get(`user:${username}`));
    return computeBookFromTrades(record.trades)[symbol];
  };
  const requestId = n => `12345678-1234-4234-8234-1234567890${String(n).padStart(2, '0')}`;
  const contract = (strike) => `QQQ261109C00${strike}000`;

  const realFetch = global.fetch;
  global.fetch = async (url) => {
    const symbols = new URL(url).searchParams.get('symbols').split(',');
    const now = new Date().toISOString();
    return Response.json({ quotes: symbols.map(symbol => ({
      symbol, bid: 2.4, ask: 2.5, bid_ts: now, ask_ts: now,
      strike: Number(symbol.slice(10, 15)), type: 'call', exp: '2026-11-09',
    })) });
  };

  try {
    // Registration and first entry (711 strike).
    const registration = await call('/api/register', {
      username: 'lifecycle_bot', password, alias: 'Lifecycle', strategy_id: 'smoke_atm_roundtrip',
    }, '', { 'X-Bot-Registration-Key': 'fixture-operator' });
    assert.equal(registration.status, 201);
    let cookie = cookieOf(registration);
    assert.equal((await call('/api/paper-trade', {
      execution_request_id: requestId(1), sym: contract(711), instrument_type: 'option', side: 'buy', qty: 1,
    }, cookie)).status, 201);
    assert.equal(position('lifecycle_bot', contract(711)).pos, 1);

    // ATM changes to 712 while 711 is held. The accepted #86 fixture proves
    // the strategy stands down; this lifecycle fixture defers the exit.
    const beforeRestart = JSON.parse(users.get('user:lifecycle_bot'));
    assert.equal(beforeRestart.trades.length, 1);

    // Restart/reconnect: a fresh login sees the same book and history.
    const login = await call('/api/login', { username: 'lifecycle_bot', password });
    assert.equal(login.status, 200);
    cookie = cookieOf(login);
    const afterRestart = await (await call('/api/me', undefined, cookie)).json();
    assert.equal(computeBookFromTrades(afterRestart.trades)[contract(711)].pos, 1);

    // Deferred exit executes against the held 711 contract, not the new ATM.
    assert.equal((await call('/api/paper-trade', {
      execution_request_id: requestId(2), sym: contract(711), instrument_type: 'option', side: 'sell', qty: 1,
    }, cookie)).status, 201);
    assert.equal(position('lifecycle_bot', contract(711)).pos, 0);

    // After the old position is flat, the new ATM 712 can be entered.
    assert.equal((await call('/api/paper-trade', {
      execution_request_id: requestId(3), sym: contract(712), instrument_type: 'option', side: 'buy', qty: 1,
    }, cookie)).status, 201);
    assert.equal(position('lifecycle_bot', contract(712)).pos, 1);

    // Settlement on the next session date: 712 is OTM at a 400 spot, so it
    // settles worthless and the book returns flat.
    const settle = await call('/api/settle', { as_of: '2026-11-10', spot_marks: { '2026-11-09': 400 } }, cookie);
    assert.equal(settle.status, 200);
    const settled = JSON.parse(users.get('user:lifecycle_bot'));
    assert.equal(computeBookFromTrades(settled.trades)[contract(712)].pos, 0);

    // Multi-client roster reads: balances stay fresh and no enumeration occurs.
    const listsBefore = listCalls;
    for (let i = 0; i < 12; i++) {
      const roster = await (await call('/api/bots')).json();
      assert.equal(roster.bots.length, 1);
      assert.equal(roster.bots[0].username, 'lifecycle_bot');
    }
    assert.equal(listCalls, listsBefore, 'roster refresh must not enumerate KV');

    // Interrupted membership update: account creation succeeds, index fails,
    // retry login repairs membership without a duplicate account.
    failIndexWrites = true;
    const failed = await call('/api/register', {
      username: 'repair_bot', password, alias: 'Repair', strategy_id: 'smoke_atm_roundtrip',
    }, '', { 'X-Bot-Registration-Key': 'fixture-operator' });
    assert.equal(failed.status, 503);
    assert.ok(users.has('user:repair_bot'));
    failIndexWrites = false;
    assert.equal((await call('/api/login', { username: 'repair_bot', password })).status, 200);
    const rosterAfterRepair = await (await call('/api/bots')).json();
    assert.equal(rosterAfterRepair.bots.filter(b => b.username === 'repair_bot').length, 1);

    // Failed registration with a wrong key creates nothing.
    assert.equal((await call('/api/register', {
      username: 'rejected_bot', password, alias: 'Rejected', strategy_id: 'smoke_atm_roundtrip',
    }, '', { 'X-Bot-Registration-Key': 'wrong' })).status, 403);
    assert.equal(users.has('user:rejected_bot'), false);

    console.log(JSON.stringify({
      lifecycle: [
        'registered', 'entered_711', 'atm_changed_712_held_711', 'deferred_exit_711',
        'restart_reconciled', 'entered_712', 'settled_flat', 'multi_client_roster',
        'interrupted_membership_repaired', 'wrong_key_rejected',
      ],
      balances: {
        after_settlement: settled.balance_cash,
        expected: 10000 - 250 + 240 - 250,
      },
      operation_counts: {
        kv_list_calls_total: listCalls,
        roster_list_calls: listCalls - listsBefore,
        r2_gets: r2Gets,
        r2_puts: r2Puts,
      },
    }, null, 2));
    console.log('10 lifecycle checks passed.');
  } finally {
    global.fetch = realFetch;
  }
})().catch((error) => { console.error(error); process.exit(1); });
