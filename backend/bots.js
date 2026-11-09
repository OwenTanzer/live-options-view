// backend/bots.js (B4) -- bot roster: the membership index (R2 + legacy KV
// markers), the operator reconcile/metadata routes, the public /api/bots
// roster, and netPositions. Moved verbatim from worker.js; the entry
// re-exports the public names. syncBotMembership is exported for auth
// (register/login retry it) -- it is not one of the Worker's public exports.

import {
  BOT_USERNAME_RE, MAX_KV_WRITE_ATTEMPTS, STARTING_BALANCE, STRATEGY_ID_RE,
  botKey, computeBookFromTrades, jsonResponse, readJsonBody, sleep, userKey,
  withUserRecord,
} from './common.js';

export const BOT_INDEX_KEY = 'system/bot-membership-v1.json';
const MAX_BOT_INDEX_MEMBERS = 500;
function validateBotIndex(value) {
  if (!value || value.schema_version !== 1 || typeof value.ready !== 'boolean' ||
      !Array.isArray(value.members) || value.members.length > MAX_BOT_INDEX_MEMBERS ||
      value.members.some(n => typeof n !== 'string' || !BOT_USERNAME_RE.test(n) || n !== n.toLowerCase()) ||
      new Set(value.members).size !== value.members.length) {
    throw new Error('Invalid bot membership index');
  }
  return value;
}
async function loadBotIndex(env) {
  const object = await env.PAPER_TRADES.get(BOT_INDEX_KEY);
  return object ? { value: validateBotIndex(await object.json()), etag: object.etag } : null;
}
// Conditional writes protect against lost membership when separate Workers
// register bots concurrently. An absent index is explicitly NOT migration-ready.
export async function updateBotIndex(env, additions, { ready = false, sleeper = sleep } = {}) {
  const names = additions.map(n => n.toLowerCase());
  if (names.some(n => !BOT_USERNAME_RE.test(n))) throw new Error('Invalid bot membership');
  for (let attempt = 0; attempt < MAX_KV_WRITE_ATTEMPTS; attempt++) {
    const current = await loadBotIndex(env);
    const members = [...new Set([...(current?.value.members || []), ...names])].sort();
    const next = validateBotIndex({ schema_version: 1, ready: ready || current?.value.ready || false, members });
    if (current && JSON.stringify(current.value) === JSON.stringify(next)) return next;
    try {
      const stored = await env.PAPER_TRADES.put(BOT_INDEX_KEY, JSON.stringify(next), {
        onlyIf: current ? { etagMatches: current.etag } : { etagDoesNotMatch: '*' },
        httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' },
      });
      if (stored) return next;
    } catch (error) {
      const rateLimited = Number(error?.status) === 429 ||
        [429, 10058].includes(Number(error?.code)) ||
        /\b(?:429|10058|TooManyRequests)\b/i.test(`${error?.name} ${error?.message}`);
      if (!rateLimited || attempt === MAX_KV_WRITE_ATTEMPTS - 1) throw error;
    }
    // Both a conditional conflict and R2's same-object write throttle require
    // a fresh read/merge. Wait beyond the one-second write window first.
    if (attempt < MAX_KV_WRITE_ATTEMPTS - 1) await sleeper(1100);
  }
  throw new Error('Bot membership update conflicted; retry');
}
// Only migration/reconciliation and the explicit staged rollout mode enumerate
// KV. Never fall back to listing on an ordinary index read failure.
async function listMigrationKeys(env, prefix) {
  const names = [];
  let cursor;
  for (let page = 0; page < 5; page++) {
    const result = await env.USERS.list({ prefix, limit: 100, ...(cursor ? { cursor } : {}) });
    names.push(...result.keys.map(k => k.name));
    if (names.length > MAX_BOT_INDEX_MEMBERS) throw new Error('Bot migration scan limit exceeded');
    if (result.list_complete === true) return names;
    if (!result.cursor || result.cursor === cursor) throw new Error('Incomplete bot migration scan');
    cursor = result.cursor;
  }
  throw new Error('Bot migration scan limit exceeded');
}
export async function botUsernames(env) {
  if (env.BOT_INDEX_READS === 'legacy') {
    return (await listMigrationKeys(env, 'bot:')).map(k => k.slice(4));
  }
  if (env.BOT_INDEX_READS && env.BOT_INDEX_READS !== 'r2') throw new Error('Invalid bot index mode');
  const current = await loadBotIndex(env);
  if (!current?.value.ready) throw new Error('Bot membership migration is not ready');
  return current.value.members;
}
export async function repairLegacyBotMarker(env, username, sleeper = sleep) {
  const key = botKey(username);
  for (let attempt = 0; attempt < 3; attempt++) {
    const raw = await env.USERS.get(key);
    let marker;
    try { marker = raw ? JSON.parse(raw) : null; } catch { marker = null; }
    if (typeof marker?.username === 'string' &&
        marker.username.toLowerCase() === username.toLowerCase()) return;
    try {
      await env.USERS.put(key, JSON.stringify({ username }));
      return;
    } catch (error) {
      // A competing repair or an eventually consistent stale read can still
      // cause a same-key PUT. Retry only rate limits, outside the one-second
      // write window, and re-read so a completed competing repair is a no-op.
      const rateLimited = error?.status === 429 || /\b429\b/.test(String(error?.message));
      if (!rateLimited || attempt === 2) throw error;
      await sleeper(1100);
    }
  }
}
export async function syncBotMembership(env, username) {
  // Keep the legacy marker during rollout/rollback. The account already exists;
  // login and metadata sync retry this sequence after any interrupted write.
  await repairLegacyBotMarker(env, username);
  await updateBotIndex(env, [username]);
}
export async function handleBotIndexReconcile(request, env) {
  if (!env.BOT_REGISTRATION_KEY || request.headers.get('X-Bot-Registration-Key') !== env.BOT_REGISTRATION_KEY) {
    return jsonResponse({ error: 'Invalid bot registration key' }, 403);
  }
  try {
    // Scan account records, not just bot: markers, to repair interrupted legacy
    // registrations. A bounded, fully paginated scan must finish before publish.
    const keys = await listMigrationKeys(env, 'user:');
    const members = [];
    for (const key of keys) {
      const raw = await env.USERS.get(key);
      if (!raw) throw new Error('Migration account unavailable; retry');
      const record = JSON.parse(raw);
      if (record.is_bot === true) {
        if (typeof record.username !== 'string' || userKey(record.username) !== key) {
          throw new Error('Migration account identity mismatch');
        }
        members.push(record.username);
      }
    }
    // Union with the newest version: a registration during scanning must survive.
    // Closed bots retain identity/history; reconciliation never prunes on KV misses.
    const index = await updateBotIndex(env, members, { ready: true });
    console.log(JSON.stringify({ event: 'bot_index_reconciled', scanned: keys.length, members: index.members.length }));
    return jsonResponse({ ready: index.ready, scanned: keys.length, members: index.members.length }, 200);
  } catch (error) {
    console.error('bot_index_reconciliation_failed');
    return jsonResponse({ error: 'Bot index reconciliation failed; retry or inspect storage' }, 503);
  }
}
// Re-syncs a bot's operator-owned metadata (strategy_id, alias) after
// registration.
//
// The authoritative strategy assignment lives in the Crassus runtime's
// accounts.json, not here -- capturing it once at registration meant that
// moving an existing bot to a new strategy left /api/bots attributing all its
// future performance to the old one, with no way to correct it short of
// abandoning the username. The runner calls this at startup so the roster
// tracks the config that is actually running.
//
// Authenticated with the same operator key as bot registration: this edits the
// public roster's attribution, so it must not be reachable by a logged-in bot
// session, let alone anonymously.
// Public strategy summary for the Automated card. Only the fields the runner
// intentionally publishes are retained; nested objects, credentials and
// arbitrary metadata are rejected rather than copied into KV.
const STRATEGY_STATE_KEYS = new Set([
  'strategy', 'symbol', 'window', 'signal_date', 'close', 'sma',
  'prior_close', 'prior_sma', 'signal', 'target', 'position', 'position_qty',
  'equity', 'ask', 'pending', 'reason', 'adjustment_basis', 'execution_convention',
]);
function sanitizeStrategyState(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (!STRATEGY_STATE_KEYS.has(key)) return null;
    if (item === null || typeof item === 'boolean') { out[key] = item; continue; }
    if (typeof item === 'number' && Number.isFinite(item)) { out[key] = item; continue; }
    if (typeof item === 'string' && item.length <= 500) { out[key] = item; continue; }
    return null;
  }
  return Object.keys(out).length ? out : null;
}

export async function handleBotMetadata(request, env) {
  const key = request.headers.get('X-Bot-Registration-Key');
  if (!env.BOT_REGISTRATION_KEY || key !== env.BOT_REGISTRATION_KEY) {
    return jsonResponse({ error: 'Invalid bot registration key' }, 403);
  }
  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;
  const {
    username, strategy_id, alias, paper_margin_limit, paper_financing_rate,
    strategy_state,
  } = bodyResult.body || {};

  if (typeof username !== 'string' || !BOT_USERNAME_RE.test(username)) {
    return jsonResponse({ error: 'username must be a valid username' }, 400);
  }
  if (strategy_id !== undefined && strategy_id !== null &&
      (typeof strategy_id !== 'string' || !STRATEGY_ID_RE.test(strategy_id))) {
    return jsonResponse({ error: 'strategy_id must be 1-40 chars: lowercase letters, numbers, underscore' }, 400);
  }
  if (alias !== undefined && (typeof alias !== 'string' || alias.length > 40)) {
    return jsonResponse({ error: 'Alias must be a string of at most 40 characters' }, 400);
  }
  if (paper_margin_limit !== undefined &&
      (typeof paper_margin_limit !== 'number' || !Number.isFinite(paper_margin_limit) ||
       paper_margin_limit < 1 || paper_margin_limit > 4)) {
    return jsonResponse({ error: 'paper_margin_limit must be a number from 1 to 4' }, 400);
  }
  if (paper_financing_rate !== undefined &&
      (typeof paper_financing_rate !== 'number' || !Number.isFinite(paper_financing_rate) ||
       paper_financing_rate < 0 || paper_financing_rate > 1)) {
    return jsonResponse({ error: 'paper_financing_rate must be a number from 0 to 1' }, 400);
  }
  const cleanState = strategy_state === undefined ? undefined : sanitizeStrategyState(strategy_state);
  if (strategy_state !== undefined && cleanState === null) {
    return jsonResponse({ error: 'strategy_state must be a small flat object' }, 400);
  }

  const outcome = await withUserRecord(env, username, (record) => {
    if (!record.is_bot) return { error: 'not_a_bot' };
    const next = { ...record };
    if (strategy_id !== undefined) next.strategy_id = strategy_id;
    if (alias !== undefined) next.alias = alias;
    if (paper_margin_limit !== undefined) next.paper_margin_limit = paper_margin_limit;
    if (paper_financing_rate !== undefined) next.paper_financing_rate = paper_financing_rate;
    if (cleanState !== undefined) next.strategy_state = cleanState;
    const result = {
      strategy_id: next.strategy_id, alias: next.alias,
      paper_margin_limit: next.paper_margin_limit ?? 1,
      paper_financing_rate: next.paper_financing_rate ?? 0,
    };
    if (next.strategy_id === record.strategy_id && next.alias === record.alias &&
        next.paper_margin_limit === record.paper_margin_limit &&
        next.paper_financing_rate === record.paper_financing_rate &&
        JSON.stringify(next.strategy_state) === JSON.stringify(record.strategy_state)) {
      return { result };
    }
    return { record: next, result };
  });

  if (outcome.error === 'not_found') return jsonResponse({ error: 'No such account' }, 404);
  // A human account is never editable through the operator key -- that would be
  // a path to publishing a real user's balance by flipping them into the roster.
  if (outcome.error === 'not_a_bot') return jsonResponse({ error: 'Not a bot account' }, 409);
  if (outcome.error) return jsonResponse({ error: 'Metadata could not be updated, try again' }, 503);

  // Repair both membership representations after an interrupted registration.
  // Authenticated bot login also retries this operation.
  try { await syncBotMembership(env, username); }
  catch { return jsonResponse({ error: 'Bot membership update failed; retry metadata sync' }, 503); }

  return jsonResponse({ username, ...outcome.result }, 200);
}
// Public read-only roster of the automated (Crassus) accounts, for the
// Automated tab's side-by-side comparison. Deliberately unauthenticated: these
// are paper-money bots whose whole purpose is to be observed. Two invariants
// hold it safe to expose:
//
//   1. Only indexed accounts whose record is still marked is_bot appear.
//      Membership alone can never publish a human account.
//   2. Only the whitelisted fields below are returned. `salt`, `hash`,
//      `iterations` and session state never leave this function -- a
//      spread-the-record-and-delete-secrets approach would leak any field a
//      later commit adds, so the projection is explicit.
//
// Positions are netted server-side but left unmarked; the client marks them
// against the same live quotes the paper panel already polls, so the roster
// and the single-account view can never disagree about the mark.
export async function handleBots(request, env) {
  try {
    const usernames = await botUsernames(env);

    const bots = [];
    for (const name of usernames) {
      const raw = await env.USERS.get(`user:${name}`);
      if (!raw) throw new Error('Indexed bot account unavailable');
      const record = JSON.parse(raw);
      if (!record.is_bot) continue;    // index and record disagree -- trust the record
      const trades = Array.isArray(record.trades) ? record.trades : [];
      bots.push({
        username: record.username,
        alias: record.alias || record.username,
        strategy_id: record.strategy_id ?? null,
        balance_cash: record.balance_cash,
        starting_balance: record.starting_balance ?? STARTING_BALANCE,
        account_closed: record.account_closed === true,
        closed_at: record.closed_at ?? null,
        closure_reason: record.closure_reason ?? null,
        trade_count: trades.length,
        first_trade_ts: trades.length ? trades[0].ts : null,
        last_trade_ts: trades.length ? trades[trades.length - 1].ts : null,
        positions: netPositions(trades),
        strategy_state: record.strategy_state ?? null,
        paper_margin_limit: record.paper_margin_limit ?? 1,
        paper_financing_rate: record.paper_financing_rate ?? 0,
        created_at: record.createdAt ?? null,
      });
    }

    bots.sort((a, b) => a.alias.localeCompare(b.alias));
    return jsonResponse({ bots, as_of: new Date().toISOString() }, 200);
  } catch {
    console.error('bot_roster_read_failed');
    return jsonResponse({ error: 'Bot roster temporarily unavailable' }, 503);
  }
}
// Projects the account's position book into the roster's wire shape.
//
// This delegates to computeBookFromTrades rather than accumulating its own cost
// basis. An earlier version summed fills on the surviving side, which silently
// diverged from the real book: it never reset on a flat round trip, so a
// contract closed and reopened at a new price reported a blend of both eras.
// The smoke strategy closes and reopens the same ATM contract every cycle, so
// that was wrong within minutes of the first run. computeBookFromTrades already
// handles reductions, flat resets and side flips, and is the same function
// /api/settle books against -- sharing it is what keeps the roster and
// settlement from disagreeing about what a bot holds.
export function netPositions(trades) {
  const book = computeBookFromTrades(trades);
  return Object.entries(book)
    .filter(([, b]) => b.pos !== 0)
    .map(([sym, b]) => ({
      sym,
      instrument_type: b.instrument_type,
      multiplier: b.multiplier,
      strike: b.strike,
      type: b.type,
      exp: b.exp,
      qty: b.pos,
      avg_price: Number(b.avg.toFixed(4)),
    }));
}
