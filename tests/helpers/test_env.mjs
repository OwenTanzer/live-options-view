// tests/helpers/test_env.mjs -- give a Worker test env a real SQLite database.
//
// Handlers take their storage from `createStorage(env)`, so a test env needs a
// `DB`. This wraps a legacy-shaped env (USERS/SESSIONS/PAPER_TRADES fakes) with
// a fresh in-memory SQLite database carrying the real schema, and seeds accounts
// and sessions into it. The KV fakes stay in the env only for tests that
// exercise the legacy-shaped fixtures; the code under test never reads them.

import { FakeD1 } from './d1_shim.mjs';
import { createStorage } from '../../backend/storage.js';
import { SESSION_TTL_SECONDS } from '../../backend/common.js';

export function withD1(env = {}) {
  const DB = new FakeD1();
  DB.db.exec(FakeD1.schemaSql());
  return { ...env, DB };
}

/** Seed account records (the legacy JSON shape) into the env's database. */
export async function seedAccounts(env, records) {
  const storage = createStorage(env);
  for (const entry of records) {
    const record = typeof entry === 'string' ? JSON.parse(entry) : entry;
    await storage.users.insert(record.username, { version: 0, ...record });
  }
  return storage;
}

/** Create a live session and return the raw token to put in a cookie. */
export async function seedSession(env, token, username) {
  await createStorage(env).sessions.create(token, username, SESSION_TTL_SECONDS);
  return token;
}

export { FakeD1 };
