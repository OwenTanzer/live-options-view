// backend/storage.js -- the single source of truth for accounts and sessions.
//
// Issue #208: Workers KV was the wrong store for this data. KV has no
// conditional write, no transaction, no uniqueness constraint and a daily
// *write* quota that exhausted in production; `withUserRecord` papered over the
// first two with a read-modify-write retry loop and a confirming re-read that
// admits in its own comment it is not a guarantee. Account and session state
// now live in D1 (SQLite, transactional, UNIQUE-constrained), and the runtime
// makes zero KV calls.
//
// Design rules enforced here, not just promised in a doc:
//
// * `users.insert` is a real `INSERT ... ON CONFLICT DO NOTHING`, so two
//   simultaneous registrations cannot both create the same username.
// * `users.mutate` is compare-and-swap on a `version` column: one UPDATE whose
//   WHERE clause carries the version that was read, with `changes` as proof.
//   No confirming re-read, no "best effort" comment.
// * Sessions are stored as SHA-256 of the token. The cookie keeps the raw
//   token (opaque to the client either way), the database never holds a bearer
//   token, and a leaked database dump cannot be replayed as cookies.
// * There is no KV fallback in the request path. If `DB` is not bound,
//   `createStorage` throws rather than silently degrading to KV; the KV
//   adapters here exist only for the bounded migration and its reverse.
//
// Records keep their original JSON shape byte-for-byte (salt, hash, iterations,
// balance_cash, trades, bot fields), which is what makes the migration a copy
// rather than a translation.

import { userKey } from './common.js';

const TOKEN_HASH_ALGORITHM = 'SHA-256';

/** Lowercase account identity, matching the legacy `user:<name>` key. */
export function accountId(username) {
  return String(username).toLowerCase();
}

/** SHA-256 of a session token, hex encoded. The DB never stores the token. */
export async function hashSessionToken(token) {
  const digest = await crypto.subtle.digest(
    TOKEN_HASH_ALGORITHM, new TextEncoder().encode(String(token)),
  );
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

export class StorageUnavailable extends Error {}

// ---------------------------------------------------------------------------
// D1 backend
// ---------------------------------------------------------------------------

function d1Users(db) {
  return {
    async get(username) {
      const row = await db.prepare('SELECT record FROM users WHERE username = ?')
        .bind(accountId(username)).first();
      return row ? JSON.parse(row.record) : null;
    },

    // Registration. The UNIQUE primary key is the concurrency control: exactly
    // one of N simultaneous inserts reports 'created'.
    async insert(username, record) {
      const id = accountId(username);
      const result = await db.prepare(
        'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(username) DO NOTHING',
      ).bind(id, JSON.stringify(record), Number(record.version) || 0,
             new Date().toISOString()).run();
      return result.meta.changes === 1 ? 'created' : 'exists';
    },

    // Migration-only upsert that never overwrites a row that exists: a cutover
    // must not clobber a write that landed after the copy started.
    async insertIfAbsent(username, record) {
      const id = accountId(username);
      const result = await db.prepare(
        'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(username) DO NOTHING',
      ).bind(id, JSON.stringify(record), Number(record.version) || 0,
             new Date().toISOString()).run();
      return result.meta.changes === 1 ? 'created' : 'exists';
    },

    // Unconditional administrative write (tests, migration tooling). Request
    // handling must use insert/mutate so uniqueness and compare-and-swap are
    // not bypassed.
    async put(username, record) {
      const id = accountId(username);
      await db.prepare(
        'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(username) DO UPDATE SET record = excluded.record, ' +
        'version = excluded.version, updated_at = excluded.updated_at',
      ).bind(id, JSON.stringify(record), Number(record.version) || 0,
             new Date().toISOString()).run();
    },

    // Apply `mutate(record)` atomically. Same outcome contract as the old
    // withUserRecord: {error} rejects without a write, {result} without a
    // record is a no-op, {record, result} writes a bumped version.
    async mutate(username, mutate) {
      const id = accountId(username);
      for (let attempt = 0; attempt < 5; attempt++) {
        const row = await db.prepare('SELECT record, version FROM users WHERE username = ?')
          .bind(id).first();
        if (!row) return { error: 'not_found' };
        const record = JSON.parse(row.record);
        const outcome = mutate(record);
        if (outcome.error) return outcome;
        if (!outcome.record) return outcome;

        const next = { ...outcome.record, version: Number(row.version) + 1 };
        const write = await db.prepare(
          'UPDATE users SET record = ?, version = ?, updated_at = ? ' +
          'WHERE username = ? AND version = ?',
        ).bind(JSON.stringify(next), next.version, new Date().toISOString(),
               id, Number(row.version)).run();
        if (write.meta.changes === 1) return { record: next, result: outcome.result };
        // Someone else wrote first: re-read and re-apply, bounded.
      }
      return { error: 'conflict' };
    },

    async delete(username) {
      await db.prepare('DELETE FROM users WHERE username = ?').bind(accountId(username)).run();
    },

    // Complete scan for reconciliation. Paginated by primary key so it does not
    // depend on an ordering guarantee SQLite does not make.
    async listUsernames() {
      const names = [];
      let after = '';
      for (;;) {
        const page = await db.prepare(
          'SELECT username FROM users WHERE username > ? ORDER BY username LIMIT 200',
        ).bind(after).all();
        const rows = page.results ?? [];
        for (const row of rows) names.push(row.username);
        if (rows.length < 200) return names;
        after = rows[rows.length - 1].username;
      }
    },

    async count() {
      const row = await db.prepare('SELECT COUNT(*) AS n FROM users').first();
      return Number(row?.n ?? 0);
    },
  };
}

function d1Sessions(db) {
  return {
    async create(token, username, ttlSeconds) {
      const hash = await hashSessionToken(token);
      const created = new Date();
      await db.prepare(
        'INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(token_hash) DO UPDATE SET username = excluded.username, ' +
        'created_at = excluded.created_at, expires_at = excluded.expires_at',
      ).bind(hash, accountId(username), created.toISOString(),
             nowSeconds() + Number(ttlSeconds)).run();
      return hash;
    },

    async get(token) {
      const hash = await hashSessionToken(token);
      const row = await db.prepare(
        'SELECT username, created_at, expires_at FROM sessions WHERE token_hash = ?',
      ).bind(hash).first();
      if (!row) return null;
      if (Number(row.expires_at) <= nowSeconds()) {
        await db.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(hash).run();
        return null;
      }
      return { username: row.username, createdAt: row.created_at };
    },

    async delete(token) {
      const hash = await hashSessionToken(token);
      await db.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(hash).run();
    },

    async deleteForUser(username) {
      await db.prepare('DELETE FROM sessions WHERE username = ?').bind(accountId(username)).run();
    },

    // Migration: insert a session whose raw token is known only to KV's key.
    async insertHashedIfAbsent(tokenHash, username, expiresAt) {
      const result = await db.prepare(
        'INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(token_hash) DO NOTHING',
      ).bind(tokenHash, accountId(username), new Date().toISOString(),
             Number(expiresAt)).run();
      return result.meta.changes === 1 ? 'created' : 'exists';
    },

    async count() {
      const row = await db.prepare('SELECT COUNT(*) AS n FROM sessions').first();
      return Number(row?.n ?? 0);
    },
  };
}

function d1BotMarkers(db) {
  return {
    async has(username) {
      const row = await db.prepare('SELECT 1 AS present FROM bot_markers WHERE username = ?')
        .bind(accountId(username)).first();
      return Boolean(row);
    },
    async put(username, value) {
      await db.prepare(
        'INSERT INTO bot_markers (username, value, created_at) VALUES (?, ?, ?) ' +
        'ON CONFLICT(username) DO UPDATE SET value = excluded.value',
      ).bind(accountId(username), JSON.stringify(value ?? {}), new Date().toISOString()).run();
    },
    async delete(username) {
      await db.prepare('DELETE FROM bot_markers WHERE username = ?').bind(accountId(username)).run();
    },
    async listUsernames() {
      const page = await db.prepare('SELECT username FROM bot_markers ORDER BY username').all();
      return (page.results ?? []).map(row => row.username);
    },
  };
}

function d1Migration(db) {
  return {
    async checkpoint(name) {
      const row = await db.prepare('SELECT value FROM migration_state WHERE name = ?')
        .bind(name).first();
      return row ? JSON.parse(row.value) : null;
    },
    async saveCheckpoint(name, value) {
      await db.prepare(
        'INSERT INTO migration_state (name, value) VALUES (?, ?) ' +
        'ON CONFLICT(name) DO UPDATE SET value = excluded.value',
      ).bind(name, JSON.stringify(value)).run();
    },
  };
}

// ---------------------------------------------------------------------------
// Legacy KV adapters -- migration and reverse-migration only, never requests
// ---------------------------------------------------------------------------

/** Read-only view of the legacy namespaces, used as the migration source. */
export function createLegacyKvSource(env) {
  if (!env.USERS || !env.SESSIONS) {
    throw new StorageUnavailable('legacy KV namespaces are not bound');
  }
  // Workers KV pages by an opaque cursor. The migration needs to resume inside
  // a page as well, so the contract here exposes the page token and lets the
  // caller carry a per-page offset; `null` nextToken means the prefix is done.
  //
  // The migration walks a page one record at a time, so a naive adapter issues
  // one KV `list` per record -- on the Free plan list requests are a separate,
  // small daily allowance (1,000/day), and a 275-session copy would spend it in
  // minutes. This adapter therefore memoises *page key lists* for its own
  // lifetime. It is created inside each `handleMigration` call, so the cache is
  // request-scoped: a new request gets a new adapter and a fresh listing. Only
  // key names are cached (never values), keys include the binding, prefix and
  // page token, and a failed list is evicted rather than remembered.
  const pageCache = new Map();

  function page(binding, namespace, prefix, pageToken) {
    const cacheKey = `${binding}\u0000${prefix}\u0000${pageToken ?? ''}`;
    const cached = pageCache.get(cacheKey);
    if (cached) return cached;

    const pending = (async () => {
      const result = await namespace.list({
        prefix, limit: 100, ...(pageToken ? { cursor: pageToken } : {}),
      });
      return {
        keys: (result.keys ?? []).map(k => k.name),
        nextToken: result.list_complete === true ? null : (result.cursor ?? null),
      };
    })();
    pageCache.set(cacheKey, pending);
    // A rejection must not pin the failure for the rest of the request: drop it
    // so the next call re-lists (and so a retry can succeed).
    pending.catch(() => pageCache.delete(cacheKey));
    return pending;
  }
  return {
    listUsers: ({ pageToken } = {}) => page('USERS', env.USERS, 'user:', pageToken),
    listSessions: ({ pageToken } = {}) => page('SESSIONS', env.SESSIONS, 'sess:', pageToken),
    listBotMarkers: ({ pageToken } = {}) => page('USERS', env.USERS, 'bot:', pageToken),
    async getUser(username) {
      return env.USERS.get(userKey(username));
    },
    async getRaw(key) {
      return env.USERS.get(key);
    },
    async getSessionRaw(token) {
      return env.SESSIONS.get(`sess:${token}`);
    },
    async putUser(username, record) {
      await env.USERS.put(userKey(username), JSON.stringify(record));
    },
    async putRaw(key, value) {
      await env.USERS.put(key, value);
    },
  };
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

/**
 * Returns the storage contract for request handling.
 *
 * Deliberately has no KV fallback. A Worker deployed without `DB` fails loudly
 * on its first request rather than quietly writing to the store whose quota
 * took the site down; that failure is visible in logs and cannot hide.
 */
export function createStorage(env) {
  if (!env.DB) {
    throw new StorageUnavailable(
      'D1 binding DB is required for accounts and sessions (issue #208); ' +
      'KV fallback is disabled by design',
    );
  }
  return {
    backend: 'd1',
    users: d1Users(env.DB),
    sessions: d1Sessions(env.DB),
    botMarkers: d1BotMarkers(env.DB),
    migration: d1Migration(env.DB),
  };
}
