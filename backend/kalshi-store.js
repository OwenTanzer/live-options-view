import { ACCOUNT } from './kalshi-access.js';
import { hashSessionToken } from './storage.js';

export async function readState(env, key) {
  const r = await env.DB.prepare('SELECT value FROM kalshi_state WHERE account_id = ? AND key = ?').bind(ACCOUNT, key).first();
  return r ? JSON.parse(r.value) : null;
}
export async function writeState(env, key, value) {
  await env.DB.prepare('INSERT INTO kalshi_state (account_id, key, value) VALUES (?, ?, ?) ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value')
    .bind(ACCOUNT, key, JSON.stringify(value)).run();
}
// Atomic counters survive concurrent tabs, Worker restarts and cron overlap.
export async function budget(env, scope, limit, windowMs, now = Date.now()) {
  const slot = Math.floor(now / windowMs);
  const row = await env.DB.prepare(`INSERT INTO kalshi_budget (account_id, scope, slot, used) VALUES (?, ?, ?, 1)
    ON CONFLICT(account_id, scope) DO UPDATE SET slot = excluded.slot,
      used = CASE WHEN kalshi_budget.slot = excluded.slot THEN kalshi_budget.used + 1 ELSE 1 END
    WHERE kalshi_budget.slot != excluded.slot OR kalshi_budget.used < ? RETURNING used`)
    .bind(ACCOUNT, scope, slot, limit).first();
  if (!row) throw Error('Private research request budget reached; try later');
}
export async function archive(env, market, payload) {
  const text = JSON.stringify(payload), digest = await hashSessionToken(text);
  const id = crypto.randomUUID(), received = new Date().toISOString();
  if (text.length > 100000) throw Error('Evidence exceeds storage bound');
  await budget(env, 'archive', 400, 86400000);
  await env.DB.prepare('INSERT INTO kalshi_evidence (account_id, id, received_at, market, digest, payload) VALUES (?, ?, ?, ?, ?, ?)')
    .bind(ACCOUNT, id, received, market, digest, text).run();
  return { id, digest, received_at: received };
}
export async function getArchive(env, id) {
  if (!/^[a-f0-9-]{36}$/.test(id || '')) throw Error('Invalid archive identity');
  const r = await env.DB.prepare('SELECT digest, payload FROM kalshi_evidence WHERE account_id = ? AND id = ?').bind(ACCOUNT, id).first();
  if (!r || await hashSessionToken(r.payload) !== r.digest) throw Error('Archive unavailable or corrupt');
  return JSON.parse(r.payload);
}
export async function archiveIndex(env) {
  return (await env.DB.prepare('SELECT id, received_at, market, digest FROM kalshi_evidence WHERE account_id = ? ORDER BY received_at DESC LIMIT 100').bind(ACCOUNT).all()).results;
}
export async function pruneArchive(env) {
  // Only expendable observations: immutable fill evidence is embedded in the D1 ledger.
  await env.DB.prepare('DELETE FROM kalshi_evidence WHERE account_id = ? AND received_at < ?')
    .bind(ACCOUNT, new Date(Date.now() - 7 * 86400000).toISOString()).run();
}
