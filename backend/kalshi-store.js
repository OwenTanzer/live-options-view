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
export async function budget(env, scope, limit, windowMs, now = Date.now(), amount = 1) {
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > limit) throw Error('Invalid budget reservation');
  const slot = Math.floor(now / windowMs);
  const row = await env.DB.prepare(`INSERT INTO kalshi_budget (account_id, scope, slot, used) VALUES (?, ?, ?, ?)
    ON CONFLICT(account_id, scope) DO UPDATE SET slot = excluded.slot,
      used = CASE WHEN kalshi_budget.slot = excluded.slot THEN kalshi_budget.used + excluded.used ELSE excluded.used END
    WHERE kalshi_budget.slot != excluded.slot OR kalshi_budget.used + excluded.used <= ? RETURNING used`)
    .bind(ACCOUNT, scope, slot, amount, limit).first();
  if (!row) throw Error('Private research request budget reached; try later');
}
export async function archive(env, market, payload, scope = 'archive', id = crypto.randomUUID()) {
  const text = JSON.stringify(payload), digest = await hashSessionToken(text);
  const received = new Date().toISOString();
  if (text.length > 100000) throw Error('Evidence exceeds storage bound');
  // Live display records share the existing 2,000/day interactive source budget.
  // Scheduled records are bounded by one accepted tick per minute.
  if (scope !== 'minute') await budget(env, scope, scope === 'display' ? 2000 : 400, 86400000);
  const inserted = await env.DB.prepare('INSERT INTO kalshi_evidence (account_id, id, received_at, market, digest, payload) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id,id) DO NOTHING')
    .bind(ACCOUNT, id, received, market, digest, text).run();
  if (inserted.meta?.changes === 0) {
    const prior = await env.DB.prepare('SELECT digest,received_at FROM kalshi_evidence WHERE account_id=? AND id=?')
      .bind(ACCOUNT,id).first();
    if (scope !== 'minute' || !prior) throw Error('Archive identity conflict');
    return { id, ...prior };
  }
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
  // A fill's replay remains available as long as its ledger entry references it.
  const cutoff = new Date(Date.now() - 7 * 86400000).toISOString();
  await env.DB.prepare(`DELETE FROM kalshi_evidence WHERE account_id = ? AND received_at < ?
    AND id NOT IN (
      SELECT json_extract(trade.value, '$.provenance.archive.id')
      FROM users, json_each(users.record, '$.trades') AS trade
      WHERE users.username = ? AND json_extract(trade.value, '$.provenance.archive.id') IS NOT NULL
    )`).bind(ACCOUNT, cutoff, ACCOUNT).run();
}

// The receipt is stamped when archived. It supports a retained capture view;
// storage commit/public website availability may be later.
export async function archiveAt(env, cutoff, id, before, beforeId) {
  const iso = new Date(cutoff).toISOString();
  if (id) {
    const row = await env.DB.prepare(
      'SELECT digest,payload FROM kalshi_evidence WHERE account_id=? AND id=? AND received_at<=?'
    ).bind(ACCOUNT, id, iso).first();
    if (!row) return null;
    if (await hashSessionToken(row.payload) !== row.digest) throw Error('Archive integrity failure');
    return {evidence:JSON.parse(row.payload)};
  }
  const args = [ACCOUNT, iso];
  let boundary = '';
  if (before) {
    boundary = ' AND (received_at,id)<(?,?)';
    args.push(before, beforeId);
  }
  const {results} = await env.DB.prepare(
    'SELECT id,received_at,market,digest FROM kalshi_evidence WHERE account_id=? AND received_at<=?'+
    boundary+' ORDER BY received_at DESC,id DESC LIMIT 101'
  ).bind(...args).all();
  const archives = results.slice(0,100);
  const last = archives.at(-1);
  return {archives,next:results.length>100?{before:last.received_at,before_id:last.id}:null};
}
