'use strict';
const test = require('node:test'); const assert = require('node:assert/strict');
const http = require('node:http'); const { EventEmitter } = require('node:events'); const { randomBytes } = require('node:crypto');
const { Authenticator, ISSUER, SCOPE, AUTH_LIMITS } = require('./auth');
const { createHttpServer, HTTP_LIMITS } = require('./http'); const { config, redactedLogger, installTermination } = require('./production');
const { Consumer } = require('./consumer'); const { Source, LIMITS } = require('./source'); const { fixtures, NOW } = require('./fixtures');
const { env, testKeys, testAuth, request, challenge, RESOURCE, OWNER, CLIENT } = require('./test-auth');
const keys = testKeys();
const ping = { jsonrpc: '2.0', id: 1, method: 'ping' };
async function listener(fn, options = {}) {
  const { settings, auth } = testAuth(keys); await auth.refresh();
  const files = fixtures(), consumer = new Consumer(new Source({ read: async key => files[key] }), { now: () => NOW });
  const server = createHttpServer(consumer, { policy: settings, auth, ...options });
  await new Promise(resolve => server.listen(0, '0.0.0.0', resolve));
  try { await fn(server, consumer, auth); } finally { await server.shutdown(); }
}

test('standard Bearer casing/spacing and media casing work without accepting false or disabled media', async () => listener(async server => {
  const token = keys.token();
  for (const authorization of ['bearer ' + token, 'BEARER   ' + token]) {
    assert.equal((await request(server, ping, { extra: { Authorization: authorization, Accept: 'Application/JSON; Q=0.5, Text/Event-Stream' } })).status, 200);
  }
  for (const accept of ['notapplication/json, text/event-stream', 'application/json;q=0, text/event-stream', 'application/json;q=2, text/event-stream', 'application/json;q=0.0001, text/event-stream', 'application/json;q=1;q=0, text/event-stream', 'application/json, text/event-stream;q=0']) {
    assert.equal((await request(server, ping, { token, extra: { Accept: accept } })).status, 406);
  }
}));

test('newline-heavy gzip detail fails explicitly under the 256 MB production heap and leaves queries usable', () => {
  const { spawnSync } = require('node:child_process');
  const result = spawnSync(process.execPath, ['--max-old-space-size=256', '-e', String.raw`
    const assert = require('node:assert/strict');
    const { gzipSync } = require('node:zlib');
    const { Consumer } = require('./consumer'); const { Source, LIMITS } = require('./source');
    const { fixtures, SESSION, NOW } = require('./fixtures');
    (async () => {
      const files = fixtures(), key = Object.keys(files).find(k => k.endsWith('.jsonl.gz'));
      assert.ok(key); files[key] = gzipSync('\n'.repeat(LIMITS.expanded_artifact_bytes - 1));
      const consumer = new Consumer(new Source({ read: async k => files[k] }), { now: () => NOW });
      const ranking = await consumer.call('return_rankings', { session: SESSION, limit: 1 });
      const detail = await consumer.call('result_detail', { reference: ranking.rows[0].detail_reference, limit: 1 });
      assert.equal(detail.status, 'partial'); assert.equal(detail.errors[0].code, 'excessive_response');
      assert.match(detail.errors[0].message, /physical-line/); assert.deepEqual(detail.quote_path, []);
      assert.equal((await consumer.call('market_context', { limit: 1 })).status, 'available');
      console.log(JSON.stringify({ status: detail.status, error: detail.errors[0].code, recovered: true }));
    })().catch(e => { console.error(e); process.exitCode = 1; });
  `], { cwd: __dirname, encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024 });
  assert.ifError(result.error); assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { status: 'partial', error: 'excessive_response', recovered: true });
});
test('production configuration fails closed and has explicit all-interface bind, resource and allowlists', () => {
  assert.equal(config(env()).bind, '0.0.0.0'); assert.equal(config(env()).port, 8765);
  assert.equal(config({ ...env(), MCP_ALLOWED_ORIGINS: '' }).origins.length, 0);
  for (const name of ['PORT', 'MCP_RESOURCE_URL', 'MCP_ALLOWED_HOSTS', 'MCP_ALLOWED_ORIGINS', 'MCP_AUTH_OWNER_SUB', 'MCP_AUTH_CLIENT_IDS']) { const e = env(); delete e[name]; assert.throws(() => config(e)); }
  for (const change of [{ PORT: '0' }, { PORT: '1.5' }, { MCP_RESOURCE_URL: 'http://market.example/mcp' }, { MCP_RESOURCE_URL: 'https://market.example/mcp?token=secret' }, { MCP_ALLOWED_HOSTS: '*' }, { MCP_ALLOWED_ORIGINS: '*' }, { MCP_AUTH_CLIENT_IDS: 'bad\nclient' }, { MCP_AUTH_OWNER_SUB: 'fixture-client@clients' }]) assert.throws(() => config({ ...env(), ...change }));
});
test('signed token gate rejects absent, expired, wrong audience/owner/client/scope/issuer and machine grants', async () => listener(async server => {
  let r = await request(server, ping); assert.equal(r.status, 401); assert.match(r.headers['www-authenticate'], /resource_metadata="https:\/\/market.example\/\.well-known\/oauth-protected-resource\/mcp"/);
  for (const claims of [{ exp: 1 }, { aud: 'https://discord.example/mcp' }, { aud: [RESOURCE, 'https://discord.example/mcp'] }, { iss: 'https://evil.example/' }, { iat: Math.floor(Date.now() / 1000) + 100 }, { exp: Math.floor(Date.now() / 1000) + 7200 }]) assert.equal((await request(server, ping, { token: keys.token(claims) })).status, 401);
  for (const claims of [{ sub: 'auth0|another-owner' }, { azp: 'unapproved-client' }, { gty: 'client-credentials' }, { gty: 'password' }]) assert.equal((await request(server, ping, { token: keys.token(claims) })).status, 403);
  r = await request(server, ping, { token: keys.token({ scope: 'discord:read' }) }); assert.equal(r.status, 403); assert.match(r.headers['www-authenticate'], /insufficient_scope/);
  assert.equal((await request(server, ping, { token: keys.token() })).status, 200);
  assert.equal((await request(server, ping, { token: keys.token({ aud: [RESOURCE, ISSUER + 'userinfo'] }) })).status, 200);
}));
test('JWT rejects signature/algorithm confusion, JOSE key URLs, malformed and oversized tokens', async () => {
  const { auth } = testAuth(keys); await auth.refresh(); const other = testKeys();
  for (const token of [keys.token({}, { alg: 'none' }), keys.token({}, { alg: 'HS256' }), keys.token({}, { jku: 'http://127.0.0.1/private' }), keys.token({}, { crit: ['custom'] }), keys.token({}, { typ: 'id-token' }), other.token(), 'a.b.c', 'x'.repeat(AUTH_LIMITS.tokenBytes)]) await assert.rejects(auth.authenticate('Bearer ' + token), e => e.status === 401);
});
test('JWKS cache is bounded, single-flight, cooldown-limited and fails closed on expiry/outage', async () => {
  let now = Date.now(), reads = 0, fail = false;
  const auth = new Authenticator({ audience: RESOURCE, owner: OWNER, clients: [CLIENT], now: () => now,
    readJwks: async () => { reads++; await new Promise(r => setTimeout(r, 5)); if (fail) throw new Error('secret error'); return { keys: [keys.jwk] }; } });
  await Promise.all([auth.refresh(), auth.refresh(), auth.refresh(), auth.refresh()]); assert.equal(reads, 1);
  await Promise.all(Array.from({ length: 4 }, () => auth.authenticate('Bearer ' + keys.token()))); assert.equal(reads, 1);
  await assert.rejects(auth.authenticate('Bearer ' + keys.token({}, { kid: 'unknown' })), e => e.status === 401); assert.equal(reads, 1);
  now += AUTH_LIMITS.cacheMs; fail = true; await assert.rejects(auth.authenticate('Bearer ' + keys.token()), e => e.status === 503); assert.equal(auth.isReady(), false);
  await assert.rejects(auth.authenticate('Bearer ' + keys.token()), e => e.status === 503); assert.equal(reads, 2);
  for (const value of [{ keys: [] }, { keys: Array(17).fill(keys.jwk) }, { keys: [{ ...keys.jwk, d: 'private' }] }, { keys: [keys.jwk, keys.jwk] }]) { const bad = testAuth(keys, { readJwks: async () => value }).auth; await assert.rejects(bad.refresh(), e => e.status === 503); }
});
test('token expiring during awaited JWKS refresh is rejected at authorization completion', async () => {
  let now = Date.now(); const issued = Math.floor(now / 1000);
  const auth = testAuth(keys, { now: () => now, readJwks: async () => { now += 2000; return { keys: [keys.jwk] }; } }).auth;
  await assert.rejects(auth.authenticate('Bearer ' + keys.token({ iat: issued, exp: issued + 1 })), e => e.status === 401 && e.code === 'invalid_token');
});
test('health and resource discovery are minimal; exact Host/Origin and CORS protect market routes', async () => listener(async server => {
  let r = await request(server, null, { method: 'GET', path: '/healthz', host: 'healthcheck.railway.app' }); assert.equal(r.status, 200); assert.deepEqual(r.body, { status: 'ok' });
  r = await request(server, null, { method: 'GET', path: '/.well-known/oauth-protected-resource/mcp' }); assert.equal(r.status, 200); assert.equal(r.body.resource, RESOURCE); assert.deepEqual(r.body.authorization_servers, [ISSUER]); assert.deepEqual(r.body.scopes_supported, [SCOPE]);
  assert.equal((await request(server, ping, { token: keys.token(), host: 'evil.example', extra: { 'X-Forwarded-Host': 'market.example' } })).status, 403);
  assert.equal((await request(server, ping, { token: keys.token(), host: 'healthcheck.railway.app' })).status, 403);
  assert.equal((await request(server, ping, { token: keys.token(), extra: { Origin: 'https://evil.example' } })).status, 403);
  assert.equal((await request(server, ping, { token: keys.token(), extra: { Origin: 'null' } })).status, 403);
  r = await request(server, null, { method: 'OPTIONS', extra: { Origin: 'https://client.example' } }); assert.equal(r.status, 204); assert.equal(r.headers['access-control-allow-origin'], 'https://client.example');
  r = await request(server, ping, { token: keys.token(), extra: { Origin: 'https://client.example' } }); assert.equal(r.status, 200); assert.equal(r.headers.vary, 'Origin');
}));
test('local authorization-code + S256 PKCE fixture issues a token for a real authenticated MCP flow', async () => listener(async server => {
  // Test-only issuer fixture: no requests or grants on the live Auth0 tenant.
  const codes = new Map(); const issuer = http.createServer(async (req, res) => {
    const u = new URL(req.url, 'http://127.0.0.1'); let status = 200, result;
    if (u.pathname === '/authorize' && u.searchParams.get('response_type') === 'code' && u.searchParams.get('code_challenge_method') === 'S256' && u.searchParams.get('resource') === RESOURCE && u.searchParams.get('audience') === RESOURCE && u.searchParams.get('scope') === SCOPE && u.searchParams.get('client_id') === CLIENT && u.searchParams.get('redirect_uri') === 'https://client.example/callback') {
      const code = randomBytes(16).toString('hex'); codes.set(code, u.searchParams.get('code_challenge')); result = { code, state: u.searchParams.get('state') };
    } else if (u.pathname === '/token' && req.method === 'POST') {
      const parts = []; for await (const c of req) parts.push(c); const p = new URLSearchParams(Buffer.concat(parts).toString()); const expected = codes.get(p.get('code'));
      if (!expected || p.get('grant_type') !== 'authorization_code' || p.get('client_id') !== CLIENT || p.get('redirect_uri') !== 'https://client.example/callback' || p.get('resource') !== RESOURCE || p.get('audience') !== RESOURCE || challenge(p.get('code_verifier') || '') !== expected) { status = 400; result = { error: 'invalid_grant' }; }
      else { codes.delete(p.get('code')); result = { access_token: keys.token(), token_type: 'Bearer', expires_in: 600 }; }
    } else { status = 400; result = { error: 'invalid_request' }; }
    res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(result));
  }); await new Promise(r => issuer.listen(0, '127.0.0.1', r));
  try {
    const base = 'http://127.0.0.1:' + issuer.address().port, verifier = randomBytes(32).toString('base64url'), state = randomBytes(16).toString('hex');
    const query = new URLSearchParams({ response_type: 'code', code_challenge_method: 'S256', code_challenge: challenge(verifier), client_id: CLIENT, redirect_uri: 'https://client.example/callback', resource: RESOURCE, audience: RESOURCE, scope: SCOPE, state });
    const authorization = await (await fetch(base + '/authorize?' + query)).json(); assert.equal(authorization.state, state);
    const exchange = v => fetch(base + '/token', { method: 'POST', body: new URLSearchParams({ grant_type: 'authorization_code', code: authorization.code, code_verifier: v, client_id: CLIENT, redirect_uri: 'https://client.example/callback', resource: RESOURCE, audience: RESOURCE }) });
    assert.equal((await exchange('wrong-verifier')).status, 400); const grant = await (await exchange(verifier)).json(); assert.equal((await exchange(verifier)).status, 400);
    const call = async message => { const r = await request(server, message, { token: grant.access_token }); assert.equal(r.status, message.id ? 200 : 202); return r.body; };
    let r = await call({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'pkce-fixture', version: '1' } } }); assert.equal(r.result.protocolVersion, '2025-11-25');
    await call({ jsonrpc: '2.0', method: 'notifications/initialized' }); r = await call({ jsonrpc: '2.0', id: 2, method: 'tools/list' }); assert.equal(r.result.tools.length, 5);
    const tool = async (name, args) => (await call({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name, arguments: args } })).result.structuredContent;
    assert.equal((await tool('discover_sources', { session: '2026-10-02' })).capabilities.length, 3);
    for (const [name, args] of [['market_context', { limit: 1 }], ['squeeze_results', { limit: 1 }], ['return_rankings', { session: '2026-10-02', view: 'clean', limit: 1 }]]) { const reading = await tool(name, args); const detail = await tool('result_detail', { reference: reading.rows[0].detail_reference, limit: 1 }); assert.equal(detail.status, 'available'); assert.deepEqual(detail.actual, reading.actual); }
  } finally { await new Promise(r => issuer.close(r)); }
}));
test('request bounds, four active slots, body expiry and recovery hold with authentication', async () => listener(async (server, consumer, auth) => {
  const token = keys.token(), pending = [];
  // Hold authenticated requests before body timers start; host scheduling must
  // not decide whether all four slots are observed simultaneously.
  const originalAuthenticate = auth.authenticate.bind(auth);
  let entered = 0, signalReady, release;
  const ready = new Promise(resolve => signalReady = resolve);
  const gate = new Promise(resolve => release = resolve);
  auth.authenticate = async header => {
    const result = await originalAuthenticate(header);
    if (++entered === 4) signalReady();
    await gate; return result;
  };
  for (let i = 0; i < 4; i++) { const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path: '/mcp', method: 'POST', headers: { Host: 'market.example', Authorization: 'Bearer ' + token, Accept: 'application/json, text/event-stream', 'Content-Type': 'application/json', 'MCP-Protocol-Version': '2025-11-25', 'Content-Length': '100' } }); const result = new Promise(resolve => { req.on('response', res => { res.resume(); res.on('end', () => resolve(res.statusCode)); }); req.on('error', () => resolve('closed')); }); req.write('{'); pending.push(result); }
  let readinessDeadline;
  try {
    await Promise.race([ready, new Promise((resolve, reject) => { readinessDeadline = setTimeout(() => reject(new Error('Four requests did not enter authentication')), 5000); })]);
    assert.equal(server.requestCounts().active, 4);
    assert.equal((await request(server, ping, { token })).status, 429);
  } finally { clearTimeout(readinessDeadline); auth.authenticate = originalAuthenticate; release(); }
  const results = await Promise.all(pending); assert.ok(results.every(r => r === 408 || r === 'closed')); assert.equal((await request(server, ping, { token })).status, 200);
  assert.equal((await request(server, ping, { token, raw: 'x'.repeat(LIMITS.input_bytes + 1) })).status, 413);
  assert.equal((await request(server, ping, { token, extra: { 'Content-Type': 'application/json-malformed' } })).status, 415);
}, { bodyMs: 100 }));
test('pilot MCP request budgets bound rate and authenticated daily work, with explicit recovery', async () => {
  let now = Date.parse('2026-10-05T12:00:00Z');
  await listener(async server => {
    const token = keys.token(); let completed = 0;
    while (completed < HTTP_LIMITS.perDay) {
      const count = Math.min(HTTP_LIMITS.perMinute, HTTP_LIMITS.perDay - completed);
      for (let i = 0; i < count; i++) assert.equal((await request(server, ping, { token })).status, 200);
      completed += count;
      if (count === HTTP_LIMITS.perMinute) { const r = await request(server, ping, { token }); assert.equal(r.status, 429); assert.ok(r.headers['retry-after']); }
      now += 60000;
    }
    const r = await request(server, ping, { token }); assert.equal(r.status, 429); assert.match(r.body.error, /UTC day/);
    now += 86400000; assert.equal((await request(server, ping, { token })).status, 200);
    assert.equal((await request(server, null, { method: 'GET', path: '/healthz' })).status, 200);
  }, { now: () => now });
});
test('graceful termination drains a request, clears ephemeral references and does not log sensitive values', async () => {
  const logs = [], logger = redactedLogger(s => logs.push(s)), signals = new EventEmitter();
  await listener(async (server, consumer) => {
    consumer.reference({ secret: 'fixture-retained' }); const exits = []; installTermination(server, { signals, exit: code => exits.push(code) });
    const original = consumer.call.bind(consumer); let entered, release;
    const started = new Promise(r => entered = r), gate = new Promise(r => release = r);
    consumer.call = async (...args) => { entered(); await gate; return original(...args); };
    const pending = request(server, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'market_context', arguments: { limit: 1 } } }, { token: keys.token() });
    await started; signals.emit('SIGTERM'); assert.deepEqual(exits, []); release(); assert.equal((await pending).status, 200);
    await server.shutdown(); assert.equal(consumer.references.size, 0); await new Promise(r => setImmediate(r)); assert.deepEqual(exits, [0]);
  }, { logger });
  logger({ event: 'request', status: 401, authorization: 'Bearer secret', url: '?secret=secret', owner: OWNER, exception: 'secret', duration_ms: 1 });
  for (let i = 0; i < 100; i++) logger({ event: 'request', status: 401 }); assert.ok(logs.length <= 60); assert.ok(!logs.join('').includes('secret')); assert.ok(!logs.join('').includes(OWNER));
});
test('retained references enforce both count and serialized byte budgets, expiration and clearing', () => {
  let now = NOW; const c = new Consumer(new Source({ read: async () => '' }), { now: () => now });
  for (let i = 0; i < 64; i++) c.reference({ i }); assert.equal(c.references.size, 64);
  const first = c.references.keys().next().value; c.reference({ next: true }); assert.ok(!c.references.has(first));
  for (let i = 0; i < 64; i++) c.reference({ payload: 'x'.repeat(512 * 1024), i }); assert.ok(c.retainedBytes <= LIMITS.reference_bytes); assert.ok(c.references.size < 64);
  now += LIMITS.reference_ttl_ms + 1; c.pruneReferences(); assert.equal(c.references.size, 0); assert.equal(c.retainedBytes, 0);
});
test('injected fixture evidence is explicitly synthetic and never claims a public fetch', async () => {
  const files = fixtures(), c = new Consumer(new Source({ read: async key => files[key] }));
  const value = await c.call('market_context', { limit: 1 }); assert.equal(value.source_mode, 'injected_test_artifacts');
  assert.ok(value.sources.every(s => s.link === null)); assert.ok(Date.now() - Date.parse(value.retrieval_time) < 5000);
  assert.equal(value.freshness.status, 'stale');
});

test('failed-authentication flood cannot consume the owner allowance in the same minute', async () => {
  let now = Date.parse('2026-10-05T12:00:00Z');
  await listener(async server => {
    const ownerToken = keys.token(), foreignToken = keys.token({ sub: 'auth0|another-owner' });
    const attempts = [{}, { token: 'not-a-valid-token' }, { token: foreignToken }];
    for (let i = 0; i < 125; i++) {
      const r = await request(server, ping, attempts[i % attempts.length]);
      assert.equal(r.status, i < 120 ? (i % 3 === 2 ? 403 : 401) : 429);
      if (i >= 120) assert.equal(r.headers['retry-after'], '60');
    }
    // A fresh valid owner token must pass even after the failed-attempt cap.
    assert.equal((await request(server, ping, { token: ownerToken })).status, 200);
    for (let i = 1; i < HTTP_LIMITS.perMinute; i++) {
      assert.equal((await request(server, ping, { token: ownerToken })).status, 200);
    }
    assert.equal((await request(server, ping, { token: ownerToken })).status, 429);
    now += 60000;
    assert.equal((await request(server, ping)).status, 401);
    assert.equal((await request(server, ping, { token: ownerToken })).status, 200);
  }, { now: () => now });
});
