'use strict';
// Local test keys and subjects only. Not copied into the production image.
const { generateKeyPairSync, sign, createHash } = require('node:crypto');
const http = require('node:http');
const { Authenticator, ISSUER, SCOPE } = require('./auth');
const { config } = require('./production');
const RESOURCE = 'https://market.example/mcp', OWNER = 'auth0|fixture-owner', CLIENT = 'fixture-client';
const env = () => ({ PORT: '8765', MCP_RESOURCE_URL: RESOURCE, MCP_ALLOWED_HOSTS: 'market.example',
  MCP_ALLOWED_ORIGINS: 'https://client.example', MCP_HEALTH_HOSTS: 'market.example,healthcheck.railway.app', MCP_AUTH_OWNER_SUB: OWNER, MCP_AUTH_CLIENT_IDS: CLIENT });
function testKeys(kid = 'fixture-key') {
  const pair = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = { ...pair.publicKey.export({ format: 'jwk' }), kid, use: 'sig', alg: 'RS256' };
  function token(claims = {}, header = {}) {
    const now = Math.floor(Date.now() / 1000);
    const encode = v => Buffer.from(JSON.stringify(v)).toString('base64url');
    const data = encode({ alg: 'RS256', typ: 'JWT', kid, ...header }) + '.' + encode({ iss: ISSUER, aud: RESOURCE, sub: OWNER,
      azp: CLIENT, scope: SCOPE, iat: now, exp: now + 600, gty: 'authorization_code', ...claims });
    return data + '.' + sign('RSA-SHA256', Buffer.from(data), pair.privateKey).toString('base64url');
  }
  return { jwk, token };
}
function testAuth(keys = testKeys(), extra = {}) {
  return { settings: config(env()), auth: new Authenticator({ audience: RESOURCE, owner: OWNER, clients: [CLIENT], readJwks: async () => ({ keys: [keys.jwk] }), ...extra }), keys };
}
function request(server, message, { token, method = 'POST', path = '/mcp', host = 'market.example', extra = {}, raw } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path, method,
      headers: { Host: host, Accept: 'application/json, text/event-stream', 'Content-Type': 'application/json',
        'MCP-Protocol-Version': '2025-11-25', ...(token ? { Authorization: 'Bearer ' + token } : {}), ...extra } }, res => {
      const chunks = []; res.on('data', c => chunks.push(c)); res.on('end', () => { const text = Buffer.concat(chunks).toString('utf8'); let body; try { body = JSON.parse(text); } catch {} resolve({ status: res.statusCode, headers: res.headers, text, body }); });
    }); req.on('error', reject); req.end(raw !== undefined ? raw : message ? JSON.stringify(message) : undefined);
  });
}
const challenge = verifier => createHash('sha256').update(verifier).digest('base64url');
module.exports = { env, testKeys, testAuth, request, challenge, RESOURCE, OWNER, CLIENT };
