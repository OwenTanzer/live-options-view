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

  console.log('PASS worker.js auth/trade/settlement logic');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
