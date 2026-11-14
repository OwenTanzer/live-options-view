// backend/settlement.js (B4) -- settlement: the pure pricing/validation
// functions (this slice), plus the settlement machinery and the /api/settle
// route (slice e). Moved verbatim from worker.js; the entry re-exports the
// public names. computeBookFromTrades lives in backend/common.js, not here:
// the bot roster, paper trading, and settlement all need it (see the B4 run
// log for the cycle it avoids).

import {
  ISO_DATE, R2_ORIGIN,
  checkOrigin, clearedSessionCookieHeader, computeBookFromTrades,
  jsonResponse, readJsonBody, requireSession,
  withUserRecord,
} from './common.js';
import { createStorage } from './storage.js';
import { botUsernames } from './bots.js';

const SETTLEMENT_ID_PREFIX = 'settle';
const UNDERLYINGS = new Set(['QQQ', 'SPY', 'IBIT']);
export function settlementMark(b, marks) {
  const underlying = b.underlying ?? 'QQQ'; // pre-migration books were QQQ only
  const value = marks[`${underlying}:${b.exp}`] ?? (underlying === 'QQQ' ? marks[b.exp] : null);
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

// Longs settle worthless -- this book has no exercise capacity, so an ITM
// long must be closed before expiration to realize value. Shorts don't get
// that same free pass: a real counterparty will exercise an ITM option
// against a short seller regardless of what this account can cover, so
// shorts settle at intrinsic value using the caller-supplied spot for that
// expiration.
export function settlementPriceServer(b, spotMarks) {
  if (b.pos > 0) return 0;
  const spot = settlementMark(b, spotMarks);
  if (spot == null) return 0;
  return b.type === 'call' ? Math.max(spot - b.strike, 0) : Math.max(b.strike - spot, 0);
}

export function validateSettleRequest(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';
  if (typeof body.as_of !== 'string' || !ISO_DATE.test(body.as_of)) return 'as_of must be an ISO date string';
  if (!body.spot_marks || typeof body.spot_marks !== 'object' || Array.isArray(body.spot_marks)) {
    return 'spot_marks must be an object';
  }
  for (const [exp, spot] of Object.entries(body.spot_marks)) {
    const [root, date] = exp.includes(':') ? exp.split(':') : ['QQQ', exp];
    if (!UNDERLYINGS.has(root) || !ISO_DATE.test(date) || exp.split(':').length > 2) return 'spot_marks keys must be ISO dates or UNDERLYING:ISO-date';
    if (typeof spot !== 'number' || !Number.isFinite(spot) || spot < 0) return 'spot_marks values must be non-negative numbers';
  }
  return null;
}

// ---------------------------------------------------------------------------
// Settlement: server-side port of docs/index.html's settleExpired()/
// settlementPrice(), now that trades are server-authoritative in KV rather
// than a client-local array. The client still supplies spot_marks (the
// underlying prices it's observed) since this Worker doesn't track
// underlying spot itself -- those values are used only to price settlement,
// never trusted for anything else.
// ---------------------------------------------------------------------------
// Core settlement mutation, shared by the session-authenticated /api/settle
// endpoint and the unattended cron sweep (see settleAllBots below) -- a bot
// account with no human ever opening its dashboard must settle the same way
// a logged-in user's does, just without a session to key off of.
async function settleAccount(env, username, as_of, spot_marks) {
  return withUserRecord(env, username, (record) => {
    if (record.account_closed) return { result: { settled: [], balance_cash: record.balance_cash,
      account_closed: true, liquidated: false } };
    const book = computeBookFromTrades(record.trades);
    const expired = Object.entries(book).filter(([, b]) =>
      b.instrument_type === 'option' && b.pos !== 0 && b.exp && b.exp < as_of);
    const alreadySettled = new Set(record.trades.map(t => t.execution_request_id));
    const settleable = expired.filter(([sym, b]) => !alreadySettled.has(`${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`));
    // A short position's settlement price depends on that expiration's spot
    // mark; without one, settlementPriceServer would silently price it at 0
    // ("expired worthless") regardless of whether it was actually ITM. Rather
    // than let a missing mark forgive a real obligation, leave those shorts
    // pending -- settlement is idempotent, so a later call with a complete
    // mark set (the next cron sweep, or a dashboard load for that date)
    // picks them back up. Longs are unaffected: they always settle at 0
    // regardless of spot (see settlementPriceServer).
    const pending = settleable.filter(([, b]) => b.pos > 0 || settlementMark(b, spot_marks) != null);

    if (!pending.length) {
      return { result: { settled: [], balance_cash: record.balance_cash, liquidated: false } };
    }

    let balance_cash = record.balance_cash;
    const newTrades = [...record.trades];
    const settled = [];

    for (const [sym, b] of pending) {
      const price = settlementPriceServer(b, spot_marks);
      const side = b.pos > 0 ? 'sell' : 'buy';
      const cashDelta = side === 'sell' ? price * Math.abs(b.pos) * 100 : -(price * Math.abs(b.pos) * 100);
      // An ITM short whose payout exceeds what's left in the account is an
      // obligation this book can't meet -- rather than clamp or partially
      // apply it, the whole account is liquidated (see callers below).
      if (balance_cash + cashDelta < 0) {
        return { error: 'insolvent' };
      }
      balance_cash += cashDelta;
      const trade = Object.freeze({
        execution_id: `${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`,
        execution_request_id: `${SETTLEMENT_ID_PREFIX}:${sym}:${b.exp}`,
        ts: new Date().toISOString(),
        sym, strike: b.strike, type: b.type, exp: b.exp,
        underlying: b.underlying,
        instrument_type: 'option', multiplier: 100,
        side, qty: Math.abs(b.pos), price,
        note: price > 0 ? 'exercised against (assigned)' : 'expired worthless',
        username,
      });
      newTrades.push(trade);
      settled.push(trade);
    }

    return {
      record: { ...record, balance_cash, trades: newTrades },
      result: { settled, balance_cash, liquidated: false },
    };
  });
}
// Drops a liquidated account's user record, roster index entry, and (if any)
// live session. The cron sweep hits this same path as the interactive
// endpoint, just with `token` always null -- a bot's own registration does
// start a session like any other account's, but the cron sweep never has
// that token in hand, so SESSIONS.delete on a token that was never passed
// is a harmless no-op rather than an attempt to clear a specific session.
async function liquidateAccount(env, username, token) {
  const store = createStorage(env);
  const record = await store.users.get(username);
  if (record && record.is_bot) {
    const outcome = await withUserRecord(env, username, (record) => ({
      record: { ...record, account_closed: true, closed_at: record.closed_at ?? new Date().toISOString(),
        closure_reason: record.closure_reason ?? 'insolvent_settlement' }, result: { closed: true },
    }));
    if (outcome.error) throw new Error('Bot closure could not be persisted');
    return; // Retain identity and history; never auto-provision a wiped bot.
  }
  await store.users.delete(username);
  // Drop the roster index entry too, or a liquidated bot leaves a pointer to
  // an account that no longer exists. handleBots tolerates the dangling case,
  // but leaving one behind would slowly turn the roster into a graveyard.
  await store.botMarkers.delete(username);
  if (token) await store.sessions.delete(token);
}
// Exclusive upper bound for expired contracts. Before 16:15 ET it is today's
// date, so today's contracts remain live. At/after 16:15 ET it advances by one
// calendar day, making same-day expirations eligible for settlement once the
// market has closed and the final chain archive has had time to land in R2.
//
// Cron Triggers use UTC and have no DST awareness, so both the date and cutoff
// are derived in New York time. The returned date remains compatible with the
// existing `b.exp < asOf` predicate used by both settlement paths.
function settlementAsOfNY(now = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(now).filter(part => part.type !== 'literal').map(part => [part.type, part.value]),
  );
  const dateStr = `${parts.year}-${parts.month}-${parts.day}`;
  if ((Number(parts.hour) * 60) + Number(parts.minute) < (16 * 60) + 15) return dateStr;

  const next = new Date(`${dateStr}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString().slice(0, 10);
}
// Same daily chain archive the historical panel reads (see fetchChainCsv in
// docs/index.html) -- fetched directly from R2 rather than through the
// worker's own routes since this runs server-side, not from a browser.
// Only the UnderlyingPrice of the expiration's own 0DTE chain is needed, so
// this stops at the first matching row instead of parsing the whole CSV.
// The publisher only writes a mark from a timed 16:00-16:15 ET post-close
// observation (see market_data/live_market/settlement_marks.py). The
// consumer re-checks that window in New York time, so a manually written R2
// object -- or a publisher bug -- with a midday observation and an
// otherwise-correct schema cannot become an expiration's mark. The ET date
// (not the UTC date prefix) is what must match the expiration.
function observedAtInPostCloseWindow(observedAt, dateStr) {
  const ms = Date.parse(observedAt);
  if (!Number.isFinite(ms)) return false;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(ms)).filter(part => part.type !== 'literal').map(part => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}` === dateStr &&
    Number(parts.hour) === 16 && Number(parts.minute) <= 15;
}
export async function fetchSpotMarkForDate(dateStr, underlying = 'QQQ') {
  if (!UNDERLYINGS.has(underlying)) return null;
  if (underlying !== 'QQQ') {
    const response = await fetch(`${R2_ORIGIN}/settlement/${underlying.toLowerCase()}/${dateStr}.json`, { cf: { cacheTtl: 0 } });
    if (!response.ok) return null;
    const mark = await response.json();
    if (mark.symbol !== underlying || mark.expiration !== dateStr || mark.date !== dateStr ||
        mark.price_basis !== 'first_observed_post_close' || mark.source !== 'dxlink' ||
        !observedAtInPostCloseWindow(mark.observed_at, dateStr)) return null;
    return typeof mark.spot === 'number' && Number.isFinite(mark.spot) && mark.spot > 0 ? mark.spot : null;
  }
  const key = dateStr.replace(/-/g, '');
  for (const path of [`raw/qqq_chain_${key}.csv`, `raw/opex/qqq_chain_${key}.csv`]) {
    const response = await fetch(`${R2_ORIGIN}/${path}`, { cf: { cacheTtl: 0 } });
    if (!response.ok) continue;
    const text = await response.text();
    const lines = text.split('\n');
    if (lines.length < 2) continue;
    const header = lines[0].split(',');
    const priceIdx = header.indexOf('UnderlyingPrice');
    const expIdx = header.indexOf('Expiration');
    if (priceIdx === -1) continue;
    for (const line of lines.slice(1)) {
      if (!line.trim()) continue;
      const cols = line.split(',');
      if (expIdx !== -1 && cols[expIdx] !== dateStr) continue;
      const price = Number(cols[priceIdx]);
      if (Number.isFinite(price) && price >= 0) return price;
    }
  }
  return null;
}
// Cron entry point (see the `scheduled` handler above). Settles every bot
// account's expired positions against end-of-day spot marks pulled from the
// archived chain data, independent of whether anyone's dashboard is open.
//
// Returns a counts-only summary of what the sweep did (E2: the scheduled
// handler logs it as the run's outcome). The summary never carries usernames,
// balances, trades or prices -- only counts and the as-of date -- and building
// it does not change what is read, written or settled.
export async function settleAllBots(env, now = new Date()) {
  const asOf = settlementAsOfNY(now);
  const usernames = await botUsernames(env);
  const summary = {
    as_of: asOf,
    accounts_indexed: usernames.length,
    accounts_ineligible: 0,          // not a bot, or already closed
    accounts_with_expired: 0,
    expirations_needed: 0,
    marks_resolved: 0,
    marks_missing: 0,                // source answered without a usable mark
    mark_errors: 0,                  // source threw; obligations stay pending
    accounts_settled: 0,
    positions_settled: 0,
    accounts_unchanged: 0,           // nothing settleable yet (e.g. short awaiting a mark)
    accounts_liquidated: 0,
    accounts_retry: 0,               // not_found / conflict: left for the next run
    no_op_reason: null,
  };

  const pendingByUsername = new Map();
  const neededExpirations = new Set();

  for (const username of usernames) {
    const record = await createStorage(env).users.get(username);
    if (!record) throw new Error('Indexed settlement account unavailable');
    if (!record.is_bot || record.account_closed) { summary.accounts_ineligible++; continue; }
    const book = computeBookFromTrades(record.trades);
    const expired = Object.entries(book).filter(([, b]) =>
      b.instrument_type === 'option' && b.pos !== 0 && b.exp && b.exp < asOf);
    if (!expired.length) continue;
    pendingByUsername.set(username, true);
    for (const [, b] of expired) neededExpirations.add(`${b.underlying}:${b.exp}`);
  }
  summary.accounts_with_expired = pendingByUsername.size;
  summary.expirations_needed = neededExpirations.size;

  if (!pendingByUsername.size) {
    summary.no_op_reason = usernames.length ? 'no_expired_positions' : 'no_bot_accounts';
    return summary;
  }

  const spotMarks = {};
  for (const identity of neededExpirations) {
    const [underlying, exp] = identity.split(':');
    try {
      const spot = await fetchSpotMarkForDate(exp, underlying);
      if (spot != null) { spotMarks[identity] = spot; summary.marks_resolved++; }
      else summary.marks_missing++;
    } catch { summary.mark_errors++; /* A failed source leaves only its own obligations pending. */ }
  }

  for (const username of pendingByUsername.keys()) {
    const outcome = await settleAccount(env, username, asOf, spotMarks);
    if (outcome.error === 'insolvent') {
      await liquidateAccount(env, username, null);
      summary.accounts_liquidated++;
    } else if (outcome.error) {
      summary.accounts_retry++;
    } else if (outcome.result?.settled?.length) {
      summary.accounts_settled++;
      summary.positions_settled += outcome.result.settled.length;
    } else {
      summary.accounts_unchanged++;
    }
    // Any other error (not_found, conflict) is left for the next cron run --
    // settlement is idempotent (see the alreadySettled check in
    // settleAccount), so a transient miss here just gets retried.
  }
  if (!summary.accounts_settled && !summary.accounts_liquidated) {
    summary.no_op_reason = summary.accounts_retry ? 'retry_pending' : 'nothing_settleable';
  }
  return summary;
}
export async function handleSettle(request, env) {
  const session = await requireSession(request, env);
  if (!session) return new Response('Forbidden', { status: 401 });

  const originError = checkOrigin(request);
  if (originError) return originError;

  const bodyResult = await readJsonBody(request);
  if (bodyResult.error) return bodyResult.error;

  const validationError = validateSettleRequest(bodyResult.body);
  if (validationError) return new Response(validationError, { status: 400 });

  const { as_of, spot_marks } = bodyResult.body;

  // New underlyings use the same durable marks as cron. Never let a browser
  // visit time (or a nearest-expiry snapshot) become the expiration's mark.
  const marks = Object.fromEntries(Object.entries(spot_marks).filter(([key]) =>
    ISO_DATE.test(key) || key.startsWith('QQQ:')));
  const book = computeBookFromTrades(session.record.trades);
  for (const b of Object.values(book)) {
    if (b.instrument_type !== 'option' || b.pos >= 0 || !b.exp || b.exp >= as_of || b.underlying === 'QQQ') continue;
    const key = `${b.underlying}:${b.exp}`;
    if (Object.hasOwn(marks, key)) continue;
    try {
      const spot = await fetchSpotMarkForDate(b.exp, b.underlying);
      if (spot != null) marks[key] = spot;
    } catch { /* Leave this underlying pending, independently of other marks. */ }
  }

  const kvOutcome = await settleAccount(env, session.username, as_of, marks);

  if (kvOutcome.error === 'insolvent') {
    await liquidateAccount(env, session.username, session.token);
    if (session.record.is_bot) {
      return jsonResponse({ settled: [], account_closed: true,
        closure_reason: 'insolvent_settlement', balance_cash: session.record.balance_cash }, 200);
    }
    return jsonResponse(
      { error: 'account_liquidated', reason: 'A settlement obligation exceeded the account balance' },
      410,
      { 'Set-Cookie': clearedSessionCookieHeader(request) },
    );
  }
  if (kvOutcome.error) {
    return jsonResponse({ error: 'Settlement could not be recorded, try again' }, 503);
  }

  return jsonResponse(kvOutcome.result, 200);
}
