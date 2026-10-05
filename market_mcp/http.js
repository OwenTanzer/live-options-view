#!/usr/bin/env node
'use strict';
const http = require('node:http');
const { Source, LIMITS } = require('./source');
const { Consumer } = require('./consumer');
const { Protocol, VERSION } = require('./server');
const { AuthError, SCOPE, AUTH_LIMITS } = require('./auth');
const HTTP_LIMITS = Object.freeze({ active: 4, sockets: 32, bodyMs: 5000, requestMs: 25000, drainMs: 25000, responseBytes: 768 * 1024, perMinute: 120, perDay: 500 });
function accepts(value, type) {
  if (typeof value !== 'string') return false;
  return value.split(',').some(part => {
    const [media, ...parameters] = part.trim().split(';');
    if (media.trim().toLowerCase() !== type) return false;
    const qualities = parameters.map(p => p.trim()).filter(p => /^q\s*=/i.test(p));
    if (!qualities.length) return true;
    if (qualities.length !== 1) return false;
    const match = /^q\s*=\s*(0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/i.exec(qualities[0]);
    return !!match && Number(match[1]) > 0;
  });
}
function createHttpServer(consumer, { policy = null, auth = null, logger = () => {}, bodyMs = HTTP_LIMITS.bodyMs, drainMs = HTTP_LIMITS.drainMs, now = () => Date.now() } = {}) {
  if (!!policy !== !!auth) throw new Error('Production policy and authenticator must be supplied together.');
  let active = 0, draining = false, stopping, minute = -1, minuteCount = 0, day = -1, dayCount = 0;
  const server = http.createServer({ maxHeaderSize: LIMITS.input_bytes, connectionsCheckingInterval: 1000 }, async (req, res) => {
    const started = Date.now();
    res.on('finish', () => logger({ event: 'request', status: res.statusCode, duration_ms: Date.now() - started }));
    const reply = (status, value) => {
      if (res.destroyed || res.writableEnded) return;
      if (!req.complete) res.setHeader('Connection', 'close');
      let body = value === undefined ? '' : JSON.stringify(value);
      if (Buffer.byteLength(body) > HTTP_LIMITS.responseBytes) { status = 500; body = JSON.stringify({ error: 'Response limit exceeded; narrow the query.' }); }
      res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(body);
    };
    const host = req.headers.host, origin = req.headers.origin;
    const allowedHosts = policy ? (req.url === '/healthz' ? policy.healthHosts : policy.hosts) : [`127.0.0.1:${server.address()?.port}`];
    if (typeof host !== 'string' || !allowedHosts.includes(host)) { reply(403, { error: 'Host is not permitted.' }); return; }
    if (origin !== undefined) {
      const origins = policy ? policy.origins : [`http://${host}`];
      if (!origins.includes(origin)) { reply(403, { error: 'Origin is not permitted.' }); return; }
      res.setHeader('Access-Control-Allow-Origin', origin); res.setHeader('Vary', 'Origin');
      res.setHeader('Access-Control-Expose-Headers', 'WWW-Authenticate,MCP-Protocol-Version');
    }
    if (draining) { reply(503, { error: 'Service is draining; repeat the query after reconnecting.' }); return; }
    if (req.url === '/healthz' && req.method === 'GET') {
      reply(!auth || auth.isReady() ? 200 : 503, { status: !auth || auth.isReady() ? 'ok' : 'unavailable' }); return;
    }
    if (policy && ['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'].includes(req.url) && req.method === 'GET') { reply(200, policy.metadata); return; }
    if (req.url !== '/mcp') { reply(404, { error: 'Unknown route.' }); return; }
    if (req.method === 'OPTIONS' && origin !== undefined) {
      res.setHeader('Access-Control-Allow-Methods', 'POST');
      res.setHeader('Access-Control-Allow-Headers', 'Authorization,Content-Type,Accept,MCP-Protocol-Version'); reply(204); return;
    }
    if (policy) {
      const window = Math.floor(now() / 60000); if (window !== minute) { minute = window; minuteCount = 0; }
      if (++minuteCount > HTTP_LIMITS.perMinute) { res.setHeader('Retry-After', String(60 - Math.floor(now() / 1000) % 60)); reply(429, { error: 'Pilot MCP request rate exceeded.' }); return; }
    }
    if (active >= HTTP_LIMITS.active) { res.setHeader('Retry-After', '1'); reply(429, { error: 'At most four concurrent requests.' }); return; }
    active++; let timer, deadline;
    try {
      deadline = setTimeout(() => { reply(504, { error: 'Request deadline exceeded.' }); req.destroy(); }, HTTP_LIMITS.requestMs);
      if (auth) try { await auth.authenticate(req.headers.authorization); }
      catch (e) {
        const failure = e instanceof AuthError ? e : new AuthError(503, 'authorization_unavailable');
        if (failure.status === 401 || failure.status === 403) res.setHeader('WWW-Authenticate', `Bearer resource_metadata="${policy.metadataUrl}", scope="${SCOPE}"${failure.code === 'missing_token' ? '' : `, error="${failure.code === 'insufficient_scope' ? 'insufficient_scope' : 'invalid_token'}"`}`);
        if (failure.status === 503) res.setHeader('Retry-After', '30');
        reply(failure.status, { error: failure.code }); return;
      }
      if (policy) {
        const window = Math.floor(now() / 86400000); if (window !== day) { day = window; dayCount = 0; }
        if (++dayCount > HTTP_LIMITS.perDay) { res.setHeader('Retry-After', String(86400 - Math.floor(now() / 1000) % 86400)); reply(429, { error: 'Pilot authenticated request allowance reached for this UTC day.' }); return; }
      }
      if (req.method !== 'POST') { res.setHeader('Allow', 'POST'); reply(405, { error: 'POST JSON responses only; no SSE or session deletion.' }); return; }
      if (!accepts(req.headers.accept, 'application/json') || !accepts(req.headers.accept, 'text/event-stream')) { reply(406, { error: 'Accept application/json and text/event-stream.' }); return; }
      if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')) { reply(415, { error: 'Use application/json.' }); return; }
      const version = req.headers['mcp-protocol-version'];
      if (version !== undefined && version !== VERSION) { reply(400, { error: 'Unsupported MCP protocol version.' }); return; }
      if (req.headers['content-length'] && (!/^\d+$/.test(req.headers['content-length']) || Number(req.headers['content-length']) > LIMITS.input_bytes)) { reply(413, { error: 'Request exceeds 16 KiB.' }); return; }
      let size = 0; const chunks = [];
      timer = setTimeout(() => { reply(408, { error: 'Request body deadline exceeded.' }); req.destroy(); }, bodyMs);
      for await (const chunk of req) { size += chunk.length; if (size > LIMITS.input_bytes) { reply(413, { error: 'Request exceeds 16 KiB.' }); return; } chunks.push(chunk); }
      clearTimeout(timer); let message;
      try { message = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { reply(400, { jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error.' } }); return; }
      if (message?.method !== 'initialize' && version !== VERSION) { reply(400, { error: 'Send the negotiated MCP-Protocol-Version: ' + VERSION }); return; }
      const protocol = new Protocol(consumer);
      if (message?.method !== 'initialize') { protocol.initialized = true; protocol.ready = true; }
      const result = await protocol.handle(message);
      if (result === null) { reply(202); return; }
      res.setHeader('MCP-Protocol-Version', VERSION); reply(result.error ? 400 : 200, result);
    } catch { reply(400, { error: 'Request could not be read.' }); }
    finally { clearTimeout(timer); clearTimeout(deadline); active--; }
  });
  server.maxConnections = HTTP_LIMITS.sockets; server.maxRequestsPerSocket = 100;
  server.headersTimeout = 5000; server.requestTimeout = 5000; server.keepAliveTimeout = 5000;
  const maintenance = setInterval(() => {
    consumer.pruneReferences();
    if (auth && (auth.loadedAt === null || auth.now() - auth.loadedAt > AUTH_LIMITS.cacheMs - 60000)) auth.refresh().catch(() => {});
  }, 30000); maintenance.unref();
  server.on('close', () => clearInterval(maintenance));
  server.shutdown = () => {
    if (stopping) return stopping;
    draining = true; clearInterval(maintenance); logger({ event: 'shutdown' });
    stopping = new Promise(resolve => {
      const timer = setTimeout(() => { server.closeAllConnections(); }, drainMs);
      server.close(() => { clearTimeout(timer); consumer.clearReferences(); resolve(); });
      server.closeIdleConnections();
    }); return stopping;
  };
  server.requestCounts = () => ({ active });
  return server;
}
if (require.main === module) {
  const argv = process.argv.slice(2); let source;
  if (argv.length === 1 && argv[0] === '--public') source = new Source({ publicAccess: true });
  else if (argv.length === 2 && argv[0] === '--fixtures') source = new Source({ directory: argv[1] });
  else { process.stderr.write('Usage: node market_mcp/http.js --fixtures <artifact-root> | --public\n'); process.exit(2); }
  createHttpServer(new Consumer(source)).listen(8765, '127.0.0.1', () => process.stderr.write('Local MCP HTTP: http://127.0.0.1:8765/mcp\n'));
}
module.exports = { createHttpServer, HTTP_LIMITS };
