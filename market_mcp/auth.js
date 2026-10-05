'use strict';
// Resource-server verification only. No OAuth codes, refresh tokens, client
// secrets, user tokens or grants are persisted or exchanged here.
const https = require('node:https');
const { createPublicKey, verify } = require('node:crypto');
const ISSUER = 'https://dev-oraxxi11mrzuff2h.us.auth0.com/';
const JWKS_URL = ISSUER + '.well-known/jwks.json';
const SCOPE = 'market:read';
const AUTH_LIMITS = Object.freeze({ tokenBytes: 8192, jwksBytes: 65536, keys: 16,
  jwksTimeoutMs: 3000, cacheMs: 300000, refreshCooldownMs: 30000, maxTokenSeconds: 3600 });
class AuthError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}
function readPublicJwks() {
  return new Promise((resolve, reject) => {
    let bytes = 0, settled = false; const chunks = [];
    const fail = () => { if (!settled) { settled = true; reject(new AuthError(503, 'authorization_unavailable')); } };
    const req = https.get(JWKS_URL, { headers: { Accept: 'application/json' } }, res => {
      if (res.statusCode !== 200) { fail(); res.destroy(); return; }
      res.on('data', chunk => { bytes += chunk.length; if (bytes > AUTH_LIMITS.jwksBytes) { fail(); res.destroy(); } else chunks.push(chunk); });
      res.on('error', fail);
      res.on('end', () => { if (!settled) { settled = true; try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new AuthError(503, 'authorization_unavailable')); } } });
    });
    const timer = setTimeout(() => { fail(); req.destroy(); }, AUTH_LIMITS.jwksTimeoutMs);
    req.on('error', fail); req.on('close', () => clearTimeout(timer));
  });
}
function jsonSegment(segment) {
  if (!/^[A-Za-z0-9_-]+$/.test(segment)) throw new AuthError(401, 'invalid_token');
  try { const value = JSON.parse(Buffer.from(segment, 'base64url').toString('utf8')); if (!value || Array.isArray(value) || typeof value !== 'object') throw 0; return value; }
  catch { throw new AuthError(401, 'invalid_token'); }
}
function validTime(p, now) {
  return Number.isSafeInteger(p.exp) && Number.isSafeInteger(p.iat) && p.exp > now && p.iat <= now + 30 && p.exp > p.iat && p.exp - p.iat <= AUTH_LIMITS.maxTokenSeconds && (p.nbf === undefined || (Number.isSafeInteger(p.nbf) && p.nbf <= now + 30));
}
class Authenticator {
  constructor({ audience, owner, clients, readJwks = readPublicJwks, now = () => Date.now() }) {
    if (!audience || !owner || !Array.isArray(clients) || !clients.length) throw new Error('Explicit market resource, owner subject and client allowlist are required.');
    this.audience = audience; this.owner = owner; this.clients = new Set(clients);
    this.readJwks = readJwks; this.now = now; this.keys = new Map(); this.loadedAt = null; this.lastAttempt = null; this.pending = null;
  }
  async refresh() {
    if (this.pending) return this.pending;
    if (this.lastAttempt !== null && this.now() - this.lastAttempt < AUTH_LIMITS.refreshCooldownMs) return;
    this.lastAttempt = this.now();
    this.pending = (async () => {
      try {
        const value = await this.readJwks();
        if (Buffer.byteLength(JSON.stringify(value)) > AUTH_LIMITS.jwksBytes || !Array.isArray(value?.keys) || !value.keys.length || value.keys.length > AUTH_LIMITS.keys) throw 0;
        const keys = new Map();
        for (const jwk of value.keys) {
          if (typeof jwk.kid !== 'string' || !jwk.kid || jwk.kid.length > 128 || keys.has(jwk.kid) || jwk.kty !== 'RSA' || (jwk.alg && jwk.alg !== 'RS256') || (jwk.use && jwk.use !== 'sig') || (jwk.key_ops && (!Array.isArray(jwk.key_ops) || !jwk.key_ops.includes('verify'))) || jwk.d) throw 0;
          const key = createPublicKey({ key: { kty: 'RSA', n: jwk.n, e: jwk.e }, format: 'jwk' });
          if (key.asymmetricKeyDetails.modulusLength < 2048 || key.asymmetricKeyDetails.modulusLength > 4096) throw 0;
          keys.set(jwk.kid, key);
        }
        this.keys = keys; this.loadedAt = this.now();
      } catch { throw new AuthError(503, 'authorization_unavailable'); }
    })();
    try { await this.pending; } finally { this.pending = null; }
  }
  async authenticate(header) {
    if (header === undefined) throw new AuthError(401, 'missing_token');
    if (typeof header !== 'string' || Buffer.byteLength(header) > AUTH_LIMITS.tokenBytes) throw new AuthError(401, 'invalid_token');
    const match = /^Bearer +([A-Za-z0-9_.-]+)$/i.exec(header);
    if (!match) throw new AuthError(401, 'invalid_token');
    const token = match[1], pieces = token.split('.');
    if (pieces.length !== 3 || !pieces[2] || !/^[A-Za-z0-9_-]+$/.test(pieces[2])) throw new AuthError(401, 'invalid_token');
    const h = jsonSegment(pieces[0]), p = jsonSegment(pieces[1]);
    if (h.alg !== 'RS256' || !['JWT', 'at+jwt'].includes(h.typ) || typeof h.kid !== 'string' || h.kid.length > 128 || h.crit || h.jku || h.jwk || h.x5u || h.b64 !== undefined) throw new AuthError(401, 'invalid_token');
    const now = Math.floor(this.now() / 1000), aud = typeof p.aud === 'string' ? [p.aud] : p.aud;
    if (p.iss !== ISSUER || !Array.isArray(aud) || !aud.every(a => a === this.audience || a === ISSUER + 'userinfo') || !aud.includes(this.audience) || !validTime(p, now)) throw new AuthError(401, 'invalid_token');
    if (this.loadedAt === null || this.now() - this.loadedAt >= AUTH_LIMITS.cacheMs || !this.keys.has(h.kid)) await this.refresh();
    if (this.loadedAt === null || this.now() - this.loadedAt >= AUTH_LIMITS.cacheMs) throw new AuthError(503, 'authorization_unavailable');
    const key = this.keys.get(h.kid);
    if (!key || !verify('RSA-SHA256', Buffer.from(pieces[0] + '.' + pieces[1]), key, Buffer.from(pieces[2], 'base64url'))) throw new AuthError(401, 'invalid_token');
    // Network key refresh may consume the token's remaining lifetime.
    if (!validTime(p, Math.floor(this.now() / 1000))) throw new AuthError(401, 'invalid_token');
    if (p.sub !== this.owner || !this.clients.has(p.azp) || (p.gty !== undefined && !['authorization_code', 'refresh_token'].includes(p.gty))) throw new AuthError(403, 'not_authorized');
    if (typeof p.scope !== 'string' || !p.scope.split(/\s+/).includes(SCOPE)) throw new AuthError(403, 'insufficient_scope');
    return { subject: p.sub }; // Identity is not logged or passed to market producers.
  }
  isReady() { return this.loadedAt !== null && this.now() - this.loadedAt < AUTH_LIMITS.cacheMs; }
}
module.exports = { Authenticator, AuthError, ISSUER, JWKS_URL, SCOPE, AUTH_LIMITS, readPublicJwks };
