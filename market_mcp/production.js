#!/usr/bin/env node
'use strict';
const { Source } = require('./source');
const { Consumer } = require('./consumer');
const { createHttpServer } = require('./http');
const { Authenticator, ISSUER, SCOPE } = require('./auth');
function config(env) {
  const required = name => { const value = env[name]; if (typeof value !== 'string' || !value || value.length > 2048 || /[\s\r\n]/.test(value)) throw new Error('Missing or invalid ' + name); return value; };
  const port = required('PORT'); if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) throw new Error('Invalid PORT');
  let resource; try { resource = new URL(required('MCP_RESOURCE_URL')); } catch { throw new Error('Invalid MCP_RESOURCE_URL'); }
  if (resource.protocol !== 'https:' || resource.username || resource.password || resource.search || resource.hash || resource.pathname !== '/mcp' || resource.port || !/^[a-z0-9.-]+\.[a-z]{2,}$/.test(resource.hostname)) throw new Error('MCP_RESOURCE_URL must be an explicit HTTPS market endpoint ending /mcp');
  const list = name => required(name).split(',');
  const hosts = list('MCP_ALLOWED_HOSTS');
  if (!hosts.includes(resource.host) || hosts.some(h => !/^[a-z0-9.-]+\.[a-z]{2,}$/.test(h))) throw new Error('Invalid MCP_ALLOWED_HOSTS');
  if (typeof env.MCP_ALLOWED_ORIGINS !== 'string') throw new Error('Explicit MCP_ALLOWED_ORIGINS is required (empty denies all browser origins)');
  const origins = env.MCP_ALLOWED_ORIGINS === '' ? [] : list('MCP_ALLOWED_ORIGINS');
  if (origins.some(s => { try { const u = new URL(s); return u.protocol !== 'https:' || u.origin !== s; } catch { return true; } })) throw new Error('Invalid MCP_ALLOWED_ORIGINS');
  const healthHosts = env.MCP_HEALTH_HOSTS ? list('MCP_HEALTH_HOSTS') : hosts;
  if (healthHosts.some(h => !/^[a-z0-9.-]+\.[a-z]{2,}$/.test(h))) throw new Error('Invalid MCP_HEALTH_HOSTS');
  const clients = list('MCP_AUTH_CLIENT_IDS'); if (clients.some(c => !/^[A-Za-z0-9_-]{1,128}$/.test(c))) throw new Error('Invalid MCP_AUTH_CLIENT_IDS');
  const owner = required('MCP_AUTH_OWNER_SUB'); if (!/^[A-Za-z0-9_-]+\|[^|]+$/.test(owner)) throw new Error('Invalid MCP_AUTH_OWNER_SUB');
  return { port: Number(port), bind: '0.0.0.0', resource: resource.href, origin: resource.origin, hosts, healthHosts, origins, clients, owner,
    metadataUrl: resource.origin + '/.well-known/oauth-protected-resource/mcp',
    metadata: { resource: resource.href, authorization_servers: [ISSUER], scopes_supported: [SCOPE], bearer_methods_supported: ['header'] } };
}
function redactedLogger(write = line => process.stderr.write(line + '\n'), now = () => Date.now()) {
  let start = now(), count = 0, suppressed = 0;
  return event => {
    if (now() - start >= 60000) { if (suppressed) write(JSON.stringify({ event: 'logs_suppressed', count: suppressed })); start = now(); count = 0; suppressed = 0; }
    if (++count > 60) { suppressed++; return; }
    // Strict construction: request values, headers, claims, bodies, URLs and
    // exception messages are never accepted as log fields.
    const value = { event: ['request', 'listening', 'shutdown', 'startup_failed'].includes(event.event) ? event.event : 'internal' };
    if (Number.isInteger(event.status)) value.status = event.status;
    if (Number.isFinite(event.duration_ms)) value.duration_ms = Math.max(0, Math.round(event.duration_ms));
    if (Number.isInteger(event.port)) value.port = event.port;
    write(JSON.stringify(value));
  };
}
function installTermination(server, { signals = process, exit = code => process.exit(code) } = {}) {
  let stopping = false;
  const stop = () => { if (!stopping) { stopping = true; server.shutdown().then(() => exit(0), () => exit(1)); } };
  signals.once('SIGTERM', stop); signals.once('SIGINT', stop);
  return stop;
}
async function start(env = process.env) {
  const settings = config(env), logger = redactedLogger();
  const auth = new Authenticator({ audience: settings.resource, owner: settings.owner, clients: settings.clients });
  await auth.refresh(); // Fail closed before readiness if public key discovery fails.
  const server = createHttpServer(new Consumer(new Source({ publicAccess: true })), { policy: settings, auth, logger });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(settings.port, settings.bind, resolve); });
  installTermination(server); logger({ event: 'listening', port: settings.port }); return server;
}
if (require.main === module) start().catch(() => { redactedLogger()({ event: 'startup_failed' }); process.exitCode = 1; });
module.exports = { config, redactedLogger, installTermination, start };
