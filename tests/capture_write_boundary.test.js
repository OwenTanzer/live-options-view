// G4a: the Worker's R2 writes are confined to the operational prefixes.
//
// WHY this test exists: `env.PAPER_TRADES` is bound (wrangler.toml) to
// `qqq-options-chain-data`, the bucket the collector publishes into. The
// Worker's own state occupies exactly two key shapes; everything else in that
// bucket is capture that the site, the MCP server and Crassus read. These
// checks drive the boundary directly -- accepted keys, refused capture keys,
// traversal that starts with an allowed prefix, log records, and unchanged
// error/return propagation -- and then assert the bound call sites actually
// route through it.
//
// The last check is the deliberate limitation: calling the raw binding still
// works. This boundary is code over one binding, not a bucket policy, and a
// test that claimed otherwise would be lying about what tests can prove.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const {
    OPERATIONAL_PREFIXES, CaptureWriteRefused, assertOperationalKey, operationalBucket,
  } = await import('../backend/operational-storage.js');
  const { updateBotIndex, BOT_INDEX_KEY } = await import('../backend/index.js');

  // -- a binding that counts calls and can be made to fail -------------------
  function fakeBinding({ returnValue = { etag: 'e1' }, throwError = null } = {}) {
    const calls = { get: 0, put: 0, delete: 0, head: 0 };
    const puts = [];
    return {
      calls, puts,
      async get(key) { calls.get += 1; return null; },
      async head(key) { calls.head += 1; return null; },
      async put(key, value, options) {
        calls.put += 1;
        puts.push({ key, value, options });
        if (throwError) throw throwError;
        return returnValue;
      },
      async delete(key) { calls.delete += 1; },
    };
  }

  function collectLogs() {
    const lines = [];
    return { lines, sink: line => lines.push(JSON.parse(line)) };
  }

  // -- 1. the keys this Worker legitimately writes ---------------------------
  const EXEC_KEY = 'paper-trades/requests/12345678-1234-4234-8234-123456789abc.json';
  for (const key of [BOT_INDEX_KEY, EXEC_KEY]) {
    assert.equal(assertOperationalKey(key), key, `${key} must be an accepted operational key`);
  }
  assert.deepEqual([...OPERATIONAL_PREFIXES], ['system/', 'paper-trades/'],
    'the operational allowlist is exactly the two key shapes the Worker owns');

  // -- 2. every capture prefix and the root manifest are refused -------------
  const CAPTURE_KEYS = [
    'manifest.json',
    'intraday/latest.json',
    'intraday/spy/latest.json',
    'intraday/prices.json',
    'intraday/health.json',
    'intraday/20261111/snapshot_120000000000.csv',
    'intraday/20261111/snapshot_120000000000.provenance.json',
    'intraday/spy/20261111/first.csv',
    'raw/qqq_chain_20261111.csv',
    'raw/weekly/20261113/qqq_chain_20261111_093000000000.csv',
    'prices/qqq_intraday_20261111.csv',
    'derived/OIranges.csv',
    'baselines/qqq_rvol_buckets.json',
    'baselines/spy_rvol_buckets.json',
    'macro/eia_steo.json',
    'settlement/qqq/2026-11-11.json',
    'oa203/scanner/2026-11-11/summary.json',
    'moo144/tradier/2026-11-11/manifest.json',
    'squeeze-scanner/v1/scheduled/latest.json',
  ];
  for (const key of CAPTURE_KEYS) {
    assert.throws(() => assertOperationalKey(key), CaptureWriteRefused, `${key} must be refused`);
  }

  // -- 3. malformed keys, including traversal behind an allowed prefix ------
  const MALFORMED = [
    '', '/system/bot-membership-v1.json', 'system/../intraday/latest.json',
    'paper-trades/../../intraday/latest.json', 'paper-trades//requests/x.json',
    'system/./bot-membership-v1.json', 'system\\bot-membership-v1.json',
    'paper-trades/\u0000.json', null, undefined, 42, {},
    'system/' + 'a'.repeat(1100),
  ];
  for (const key of MALFORMED) {
    assert.throws(() => assertOperationalKey(key), CaptureWriteRefused,
      `malformed key ${JSON.stringify(key)} must be refused`);
  }
  // The traversal case above is the important one: it satisfies the prefix
  // test, so only the segment rule stops it reaching capture.
  assert.throws(() => assertOperationalKey('system/../intraday/latest.json'), /segment/);

  // -- 4. an accepted write reaches the binding unchanged -------------------
  {
    const binding = fakeBinding();
    const { lines, sink } = collectLogs();
    const store = operationalBucket({ PAPER_TRADES: binding }, 'bot_index', sink);
    const options = { onlyIf: { etagMatches: 'abc' }, httpMetadata: { contentType: 'application/json' } };
    const returned = await store.put(BOT_INDEX_KEY, '{"a":1}', options);
    assert.equal(returned.etag, 'e1', 'the binding return value is passed through');
    assert.equal(binding.calls.put, 1);
    assert.deepEqual(binding.puts[0].options, options, 'conditional options are forwarded untouched');
    assert.equal(binding.puts[0].value, '{"a":1}');
    assert.deepEqual(lines, [{ event: 'r2_write', binding: 'PAPER_TRADES', outcome: 'written',
      caller: 'bot_index', key: BOT_INDEX_KEY }]);
  }

  // -- 5. a capture write never reaches the binding and is logged -----------
  {
    const binding = fakeBinding();
    const { lines, sink } = collectLogs();
    const store = operationalBucket({ PAPER_TRADES: binding }, 'paper_trade', sink);
    await assert.rejects(() => store.put('intraday/latest.json', 'x'), CaptureWriteRefused);
    await assert.rejects(() => store.delete('manifest.json'), CaptureWriteRefused);
    assert.equal(binding.calls.put, 0, 'a refused write must not touch the bucket');
    assert.equal(binding.calls.delete, 0);
    assert.equal(lines.length, 2);
    assert.equal(lines[0].outcome, 'refused');
    assert.equal(lines[0].caller, 'paper_trade');
    assert.equal(lines[0].key, 'intraday/latest.json');
    assert.match(lines[0].reason, /may only write/);
  }

  // -- 6. a conditional conflict is logged and its null propagates ----------
  {
    const binding = fakeBinding({ returnValue: null });
    const { lines, sink } = collectLogs();
    const store = operationalBucket({ PAPER_TRADES: binding }, 'paper_trade', sink);
    assert.equal(await store.put(EXEC_KEY, 'x', { onlyIf: { etagMatches: 'stale' } }), null,
      'a lost conditional write must stay falsy so the caller retries');
    assert.equal(lines[0].outcome, 'not_written');
  }

  // -- 7. binding errors propagate unchanged, so the 429 retry still works --
  {
    const throttled = Object.assign(new Error('slow down'), { status: 429, code: 10058 });
    const binding = fakeBinding({ throwError: throttled });
    const { lines, sink } = collectLogs();
    const store = operationalBucket({ PAPER_TRADES: binding }, 'bot_index', sink);
    await assert.rejects(() => store.put(BOT_INDEX_KEY, 'x'), error => {
      assert.equal(error, throttled, 'the original error object is rethrown, not wrapped');
      assert.equal(error.status, 429);
      assert.equal(error.code, 10058);
      return true;
    });
    assert.equal(lines[0].outcome, 'failed');
    assert.equal(lines[0].error, 'Error');
  }

  // -- 8. the bound call sites route through the boundary -------------------
  // A future edit that reaches for `env.PAPER_TRADES.put`/`.delete` again would
  // reintroduce exactly the hole this repair closes, so pin the source shape.
  {
    const backendDir = path.join(__dirname, '../backend');
    // Comments may discuss the direct call; only executable text counts.
    const withoutComments = source => source
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
    for (const file of fs.readdirSync(backendDir).filter(f => f.endsWith('.js'))) {
      const source = withoutComments(fs.readFileSync(path.join(backendDir, file), 'utf8'));
      assert.ok(!/PAPER_TRADES\s*\.\s*put\s*\(/.test(source),
        `backend/${file} must not write through env.PAPER_TRADES directly`);
      assert.ok(!/PAPER_TRADES\s*\.\s*delete\s*\(/.test(source),
        `backend/${file} must not delete through env.PAPER_TRADES directly`);
    }
    const bots = fs.readFileSync(path.join(backendDir, 'bots.js'), 'utf8');
    const trades = fs.readFileSync(path.join(backendDir, 'paper-trade.js'), 'utf8');
    assert.ok(bots.includes('operationalBucket(env, \'bot_index\')'), 'the bot index write is bounded');
    assert.ok(trades.includes('operationalBucket(env, \'paper_trade\')'), 'execution writes are bounded');
  }

  // -- 9. and the boundary is code, not bucket policy -----------------------
  // Recorded as an assertion, not a caveat in prose: the raw binding still
  // accepts a capture key. Direct binding use and provider-side permissions
  // remain outside what this test can establish.
  {
    const binding = fakeBinding();
    await binding.put('intraday/latest.json', 'unreachable through the boundary');
    assert.equal(binding.calls.put, 1,
      'the raw binding bypasses the boundary -- documented, not fixed, here');
  }

  // -- 10. updateBotIndex still drives the guarded path end to end ----------
  {
    const binding = fakeBinding({ returnValue: { etag: 'e9' } });
    binding.get = async () => null;                 // absent index -> create path
    const { lines, sink } = collectLogs();
    const env = { PAPER_TRADES: binding };
    const index = await updateBotIndex(env, ['momentum_bot']);
    assert.deepEqual(index.members, ['momentum_bot']);
    assert.equal(binding.calls.put, 1);
    assert.equal(binding.puts[0].key, BOT_INDEX_KEY);
    assert.deepEqual(binding.puts[0].options.onlyIf, { etagDoesNotMatch: '*' },
      'the create-only precondition is preserved through the boundary');
  }

  // -- 11. a missing binding keeps the endpoint's own 503 -------------------
  {
    assert.throws(() => operationalBucket({}, 'paper_trade'), CaptureWriteRefused,
      'a Worker with no bucket binding must fail at construction, not at first write');

    const { default: worker } = await import('../backend/index.js');
    const { withD1 } = await import('./helpers/test_env.mjs');
    const { createStorage } = await import('../backend/storage.js');
    const { derivePasswordHash, randomSaltBase64 } = await import('../backend/index.js');
    const unbound = withD1({});                       // no PAPER_TRADES
    const storage = createStorage(unbound);
    const salt = randomSaltBase64();
    await storage.users.insert('unbound_bot', {
      username: 'unbound_bot', salt, hash: await derivePasswordHash('fixture-password', salt, 1000),
      iterations: 1000, version: 0, balance_cash: 5, trades: [], is_bot: true,
    });
    await storage.sessions.create('unbound_bot', 'unbound_bot', 3600);
    const response = await worker.fetch(new Request('https://fixture.invalid/api/paper-trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: 'session=unbound_bot' },
      body: JSON.stringify({
        execution_request_id: '12345678-1234-4234-8234-123456789abc',
        sym: 'AAPL', instrument_type: 'share', side: 'buy', qty: 1,
      }),
    }), unbound);
    assert.equal(response.status, 503, 'a missing binding is the endpoint\'s usual 503, not a 500');
    assert.match((await response.json()).error, /Execution state unavailable/);
  }

  console.log('PASS capture-prefix R2 writes are refused through the operational interface');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
