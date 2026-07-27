const assert = require('node:assert/strict');

const UUID = '12345678-1234-4234-8234-123456789abc';

(async () => {
  // worker.js is a Cloudflare Worker module (`export default { fetch }`),
  // loaded here via dynamic import rather than require() since it's ESM.
  const {
    validateTradeIntent, executionIntentMatches, computeBookFromTrades,
    settlementPriceServer, validateSettleRequest, constantTimeEqual,
    derivePasswordHash, randomSaltBase64, parseCookies,
    USERNAME_RE, MIN_PASSWORD_LEN, MAX_PASSWORD_LEN, STARTING_BALANCE,
    netPositions, handleBots, handleBotMetadata,
  } = await import('../worker.js');

  // ── validateTradeIntent ────────────────────────────────────────────────────
  assert.equal(
    validateTradeIntent({ execution_request_id: UUID, sym: 'QQQ260717C00600000', side: 'buy', qty: 1 }),
    null,
    'a well-formed trade intent should pass without account_id/account_name/install_id',
  );
  assert.match(validateTradeIntent({}), /Body must be a JSON object|execution_request_id/);
  assert.match(validateTradeIntent({ execution_request_id: 'not-a-uuid' }), /UUID/);
  assert.match(
    validateTradeIntent({ execution_request_id: UUID, sym: '', side: 'buy', qty: 1 }),
    /sym must be a non-empty string/,
  );
  assert.match(
    validateTradeIntent({ execution_request_id: UUID, sym: 'QQQ', side: 'hold', qty: 1 }),
    /side must be "buy" or "sell"/,
  );
  assert.match(
    validateTradeIntent({ execution_request_id: UUID, sym: 'QQQ', side: 'buy', qty: 0 }),
    /qty must be a positive integer/,
  );

  // ── executionIntentMatches ─────────────────────────────────────────────────
  const intent = { execution_request_id: UUID, sym: 'QQQ', side: 'buy', qty: 2 };
  assert.equal(executionIntentMatches({ ...intent }, intent), true);
  assert.equal(executionIntentMatches({ ...intent, qty: 3 }, intent), false, 'a mismatched qty must not match');
  assert.equal(executionIntentMatches({ ...intent, sym: 'SPY' }, intent), false);

  // ── computeBookFromTrades ───────────────────────────────────────────────────
  {
    const trades = [
      { sym: 'QQQ260717C00600000', strike: 600, type: 'call', exp: '2026-07-17', side: 'buy', qty: 2, price: 1.0 },
      { sym: 'QQQ260717C00600000', strike: 600, type: 'call', exp: '2026-07-17', side: 'sell', qty: 1, price: 1.5 },
    ];
    const book = computeBookFromTrades(trades);
    const b = book['QQQ260717C00600000'];
    assert.equal(b.pos, 1, 'one contract should remain open after buying 2 and selling 1');
    assert.equal(b.avg, 1.0, 'average cost basis should be unaffected by a partial close');
    assert.equal(b.realized, (1.5 - 1.0) * 1 * 100, 'realized PnL should reflect the closed portion only');
  }

  // ── settlementPriceServer ───────────────────────────────────────────────────
  assert.equal(settlementPriceServer({ pos: 1, strike: 600, type: 'call', exp: '2026-07-17' }, {}), 0,
    'a long position always settles worthless -- no exercise capacity in this book');
  assert.equal(
    settlementPriceServer({ pos: -1, strike: 600, type: 'call', exp: '2026-07-17' }, { '2026-07-17': 610 }),
    10,
    'an ITM short call should settle at intrinsic value (spot - strike)',
  );
  assert.equal(
    settlementPriceServer({ pos: -1, strike: 600, type: 'put', exp: '2026-07-17' }, { '2026-07-17': 610 }),
    0,
    'an OTM short put should settle worthless',
  );
  assert.equal(
    settlementPriceServer({ pos: -1, strike: 600, type: 'call', exp: '2026-07-17' }, {}),
    0,
    'a short position with no recorded spot for its expiration falls back to worthless',
  );

  // ── validateSettleRequest ───────────────────────────────────────────────────
  assert.equal(validateSettleRequest({ as_of: '2026-07-17', spot_marks: { '2026-07-17': 610 } }), null);
  assert.match(validateSettleRequest({ as_of: 'not-a-date', spot_marks: {} }), /as_of/);
  assert.match(validateSettleRequest({ as_of: '2026-07-17', spot_marks: { '2026-07-17': -5 } }), /non-negative/);
  assert.match(validateSettleRequest({ as_of: '2026-07-17', spot_marks: 'nope' }), /spot_marks must be an object/);

  // ── constantTimeEqual ───────────────────────────────────────────────────────
  assert.equal(constantTimeEqual('abc', 'abc'), true);
  assert.equal(constantTimeEqual('abc', 'abd'), false);
  assert.equal(constantTimeEqual('abc', 'abcd'), false, 'different lengths must not match');

  // ── username/password bounds ────────────────────────────────────────────────
  assert.equal(USERNAME_RE.test('trader_1'), true);
  assert.equal(USERNAME_RE.test('ab'), false, 'usernames under 3 chars should be rejected');
  assert.equal(USERNAME_RE.test('has a space'), false);
  assert.equal(STARTING_BALANCE, 10_000);
  assert.ok(MIN_PASSWORD_LEN >= 8);
  assert.ok(MAX_PASSWORD_LEN > MIN_PASSWORD_LEN);

  // ── parseCookies ────────────────────────────────────────────────────────────
  {
    const request = { headers: { get: () => 'session=abc123; other=xyz' } };
    const cookies = parseCookies(request);
    assert.equal(cookies.session, 'abc123');
    assert.equal(cookies.other, 'xyz');
  }
  {
    const request = { headers: { get: () => null } };
    assert.deepEqual(parseCookies(request), {}, 'a missing Cookie header should yield an empty object');
  }

  // ── password hashing round trip ─────────────────────────────────────────────
  const salt = randomSaltBase64();
  const hash = await derivePasswordHash('correct horse battery staple', salt);
  const sameHash = await derivePasswordHash('correct horse battery staple', salt);
  const wrongHash = await derivePasswordHash('wrong password', salt);
  assert.equal(constantTimeEqual(hash, sameHash), true, 'the same password and salt must re-derive the same hash');
  assert.equal(constantTimeEqual(hash, wrongHash), false, 'a different password must derive a different hash');

  // ── netPositions ────────────────────────────────────────────────────────────
  {
    const t = (sym, side, qty, price, extra = {}) =>
      ({ sym, side, qty, price, strike: 600, type: 'call', exp: '2026-07-17', ...extra });

    assert.deepEqual(netPositions([]), [], 'no trades yields no positions');

    const flat = netPositions([t('A', 'buy', 2, 1.0), t('A', 'sell', 2, 1.5)]);
    assert.deepEqual(flat, [], 'a round trip nets flat and is dropped, not shown as a zero row');

    const [long] = netPositions([t('A', 'buy', 2, 1.0), t('A', 'buy', 2, 2.0)]);
    assert.equal(long.qty, 4);
    assert.equal(long.avg_price, 1.5, 'avg_price averages across the fills building the position');

    const [short] = netPositions([t('A', 'sell', 3, 2.0)]);
    assert.equal(short.qty, -3, 'a naked sell shows as a negative position');
    assert.equal(short.avg_price, 2.0);

    const multi = netPositions([t('A', 'buy', 1, 1.0), t('B', 'buy', 2, 3.0)]);
    assert.equal(multi.length, 2, 'distinct symbols stay distinct');

    // ── regressions: netPositions must agree with the real position book ──────
    // An earlier cost accumulator never reset on a flat round trip, so a
    // reopened contract reported a blend of the closed and current positions.
    // The smoke strategy closes and reopens the same contract every cycle.
    {
      const reopen = [t('A', 'buy', 2, 1.0), t('A', 'sell', 2, 1.5), t('A', 'buy', 1, 3.0)];
      const [p] = netPositions(reopen);
      assert.equal(p.qty, 1);
      assert.equal(p.avg_price, 3.0, 'a reopened position costs what it was reopened at, not a blend');
      assert.equal(p.avg_price, computeBookFromTrades(reopen).A.avg, 'roster avg matches the settlement book');
    }
    // Partial close leaves the surviving lot at its original basis.
    {
      const partial = [t('A', 'buy', 4, 2.0), t('A', 'sell', 1, 5.0)];
      const [p] = netPositions(partial);
      assert.equal(p.qty, 3);
      assert.equal(p.avg_price, 2.0, 'a partial close does not re-base the remaining position');
      assert.equal(p.avg_price, computeBookFromTrades(partial).A.avg);
    }
    // Flipping through flat re-bases at the flipping fill rather than mixing sides.
    {
      const flip = [t('A', 'buy', 1, 1.0), t('A', 'sell', 3, 4.0)];
      const [p] = netPositions(flip);
      assert.equal(p.qty, -2, 'selling through flat leaves a short');
      assert.equal(p.avg_price, 4.0, 'the flipped side is based on the flipping fill');
      assert.equal(p.avg_price, computeBookFromTrades(flip).A.avg);
    }
    // Whatever the trade history, the roster and the settlement book must agree
    // on both size and basis for every open contract.
    {
      const messy = [
        t('A', 'buy', 3, 1.0), t('A', 'sell', 1, 2.0), t('A', 'buy', 2, 4.0),
        t('A', 'sell', 4, 3.0), t('A', 'sell', 2, 6.0), t('B', 'sell', 1, 0.5),
      ];
      const book = computeBookFromTrades(messy);
      for (const p of netPositions(messy)) {
        assert.equal(p.qty, book[p.sym].pos, `qty agrees with the book for ${p.sym}`);
        assert.equal(p.avg_price, Number(book[p.sym].avg.toFixed(4)), `avg agrees with the book for ${p.sym}`);
      }
    }
  }

  // ── handleBotMetadata ───────────────────────────────────────────────────────
  {
    const store = {
      'bot:crassus_bob': JSON.stringify({ username: 'crassus_bob' }),
      'user:crassus_bob': JSON.stringify({
        username: 'crassus_bob', alias: 'Bob', is_bot: true,
        strategy_id: 'smoke_atm_roundtrip', balance_cash: 10000, trades: [], version: 0,
      }),
      'user:realperson': JSON.stringify({ username: 'realperson', balance_cash: 42, trades: [], version: 0 }),
    };
    const env = {
      BOT_REGISTRATION_KEY: 'operator-key',
      USERS: {
        list: async ({ prefix }) => ({
          keys: Object.keys(store).filter(k => k.startsWith(prefix)).map(name => ({ name })),
        }),
        get: async (k) => store[k] ?? null,
        put: async (k, v) => { store[k] = v; },
      },
    };
    // A real Request: readJsonBody streams request.body, so a plain stub object
    // would exercise a different path than production.
    const req = (body, key = 'operator-key') => new Request('https://example.test/api/bot-metadata', {
      method: 'POST',
      headers: key === null ? {} : { 'X-Bot-Registration-Key': key },
      body: JSON.stringify(body),
    });

    // Moving a bot to a new strategy re-attributes its future performance.
    const moved = await handleBotMetadata(req({ username: 'crassus_bob', strategy_id: 'reddit_sentiment_qqq' }), env);
    assert.equal(moved.status, 200);
    assert.equal((await moved.json()).strategy_id, 'reddit_sentiment_qqq');
    const roster = await (await handleBots({}, env)).json();
    assert.equal(roster.bots[0].strategy_id, 'reddit_sentiment_qqq',
      'the roster reflects the re-synced strategy, not the one captured at registration');

    // Without the operator key it is not reachable at all.
    assert.equal((await handleBotMetadata(req({ username: 'crassus_bob', strategy_id: 'x' }, 'wrong'), env)).status, 403);
    assert.equal((await handleBotMetadata(req({ username: 'crassus_bob' }, null), env)).status, 403);

    // A human account can never be edited into the public roster this way.
    assert.equal((await handleBotMetadata(req({ username: 'realperson', strategy_id: 'x' }), env)).status, 409);
    assert.equal((await handleBotMetadata(req({ username: 'nobody_here', strategy_id: 'x' }), env)).status, 404);

    // Malformed strategy ids are rejected rather than stored.
    assert.equal((await handleBotMetadata(req({ username: 'crassus_bob', strategy_id: 'Not Valid!' }), env)).status, 400);
  }

  // ── handleBots ──────────────────────────────────────────────────────────────
  {
    const makeEnv = (entries) => ({
      USERS: {
        list: async ({ prefix }) => ({
          keys: Object.keys(entries).filter(k => k.startsWith(prefix)).map(name => ({ name })),
        }),
        get: async (key) => entries[key] ?? null,
      },
    });

    const botRecord = {
      username: 'crassus_bob', alias: 'Bob', is_bot: true, strategy_id: 'reddit_sentiment_qqq',
      salt: 'SALT', hash: 'HASH',
      iterations: 100000, balance_cash: 9500, starting_balance: 10000,
      trades: [{ sym: 'A', side: 'buy', qty: 1, price: 5, strike: 600, type: 'call', exp: '2026-07-17', ts: '2026-07-27T12:00:00Z' }],
      createdAt: '2026-07-27T00:00:00Z', version: 1,
    };
    const humanRecord = {
      username: 'realperson', balance_cash: 42, trades: [], salt: 'S', hash: 'H', version: 0,
    };

    const env = makeEnv({
      'bot:crassus_bob': JSON.stringify({ username: 'crassus_bob' }),
      'user:crassus_bob': JSON.stringify(botRecord),
      'user:realperson': JSON.stringify(humanRecord),
    });

    const body = await (await handleBots({}, env)).json();
    assert.equal(body.bots.length, 1, 'only indexed bot accounts appear in the roster');
    const [bot] = body.bots;
    assert.equal(bot.username, 'crassus_bob');
    assert.equal(bot.alias, 'Bob');
    assert.equal(bot.strategy_id, 'reddit_sentiment_qqq', 'the roster reports which strategy a bot runs');
    assert.equal(bot.balance_cash, 9500);
    assert.equal(bot.trade_count, 1);
    assert.equal(bot.positions.length, 1);

    // The whole point of the projection: secrets must never reach the client.
    for (const leaked of ['salt', 'hash', 'iterations', 'version', 'password']) {
      assert.equal(leaked in bot, false, `/api/bots must not expose ${leaked}`);
    }
    assert.equal(JSON.stringify(body).includes('realperson'), false,
      'a human account must never appear in the public bot roster');
    assert.equal(JSON.stringify(body).includes('HASH'), false, 'no password hash may be serialized');

    // A record that lost its is_bot flag is excluded even while indexed.
    const demoted = makeEnv({
      'bot:crassus_bob': JSON.stringify({ username: 'crassus_bob' }),
      'user:crassus_bob': JSON.stringify({ ...botRecord, is_bot: false }),
    });
    assert.equal((await (await handleBots({}, demoted)).json()).bots.length, 0,
      'the record, not the index, is the authority on bot-ness');

    // A bot registered before strategy_id existed reports null, not undefined,
    // so the client can render a definite "no strategy recorded" state.
    const legacy = makeEnv({
      'bot:crassus_bob': JSON.stringify({ username: 'crassus_bob' }),
      'user:crassus_bob': JSON.stringify({ ...botRecord, strategy_id: undefined }),
    });
    assert.equal((await (await handleBots({}, legacy)).json()).bots[0].strategy_id, null);

    // A liquidated bot leaves a dangling index entry; the roster tolerates it.
    const dangling = makeEnv({ 'bot:crassus_gone': JSON.stringify({ username: 'crassus_gone' }) });
    assert.equal((await (await handleBots({}, dangling)).json()).bots.length, 0,
      'an index entry with no account behind it is skipped, not fatal');
  }

  console.log('PASS worker.js auth/trade/settlement logic');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
