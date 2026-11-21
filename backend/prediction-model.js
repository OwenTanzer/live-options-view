// Versioned, venue-neutral binary evidence. Monetary units are 1/10000 USD.
import { validateKalshiRules, validTick, kalshiFee } from './kalshi-model.js';
export const UNIT = 10000;
export function marketKey(r) {
  return [r.provider, r.environment, r.series, r.market].map(encodeURIComponent).join(':');
}
export function positionKey(r, outcome) { return `event:${marketKey(r)}:${outcome}`; }
export function validateRules(r) {
  for (const k of ['provider', 'environment', 'series', 'event', 'market', 'version', 'index', 'text', 'source', 'fee_convention']) {
    if (typeof r?.[k] !== 'string' || !r[k] || r[k].length > 10000) throw Error(`Invalid rule ${k}`);
  }
  if (['official_weather_binary', 'official_event_binary'].includes(r.rule_type)) return validateKalshiRules(r);
  if (!['fixtures', 'demo', 'production'].includes(r.environment) || r.rule_type !== 'above_threshold' ||
      r.underlying !== 'BTC' || r.currency !== 'USD' || r.payout_units !== UNIT ||
      !Number.isFinite(r.threshold) || r.threshold <= 0 || typeof r.inclusive !== 'boolean' ||
      !Number.isSafeInteger(r.tick_units) || r.tick_units < 1 || UNIT % r.tick_units ||
      r.size_increment !== 1 || !Number.isSafeInteger(r.fee_units) || r.fee_units < 0 || r.fee_units > UNIT ||
      r.fee_convention !== 'fixed_per_contract' || r.void_policy !== 'refund_entry_cost') throw Error('Unsupported binary rules');
  for (const k of ['published_at', 'cutoff', 'window_start', 'window_end']) if (!Number.isFinite(Date.parse(r[k]))) throw Error(`Invalid ${k}`);
  if (Date.parse(r.window_start) > Date.parse(r.window_end) || Date.parse(r.cutoff) > Date.parse(r.window_end)) throw Error('Invalid rule window');
  return r;
}
export function quoteEvent(m, outcome, side, qty, now) {
  const fail = error => ({ error, status: 409 });
  try { validateRules(m?.rules); } catch (e) { return fail(e.message); }
  if (!['YES', 'NO'].includes(outcome) || !['buy', 'sell'].includes(side)) return fail('Invalid outcome or side');
  const b = m.book, r = m.rules;
  if (!b?.synchronized || b.gap) return fail('Book is unsynchronized');
  if (m.status !== 'open' || now >= Date.parse(r.cutoff)) return fail('Market is closed or halted');
  const rest = ['official_weather_binary', 'official_event_binary'].includes(r.rule_type) && b.clock_basis === 'provider_http_date';
  const quoteTime = rest ? b.provider_response_ts : b.exchange_ts;
  if (rest && (b.exchange_ts !== null || !Number.isFinite(Date.parse(b.request_ts)) ||
      Date.parse(b.receive_ts) - Date.parse(b.request_ts) > 5000 ||
      Date.parse(b.receive_ts) < Date.parse(b.request_ts))) return fail('Invalid REST snapshot clock');
  for (const t of [quoteTime, b.receive_ts, r.published_at]) {
    const ms = Date.parse(t);
    if (!Number.isFinite(ms) || ms > now) return fail('Future or invalid evidence');
  }
  if (now - Date.parse(quoteTime) > (rest ? 10000 : 15000) || now - Date.parse(b.receive_ts) > (rest ? 10000 : 15000)) return fail('Book is stale');
  const yes = b.yes?.[0], no = b.no?.[0];
  if (!yes || !no) return fail('Missing book side');
  for (const level of [yes, no]) if (!Number.isSafeInteger(level[0]) || level[0] < 0 || level[0] > UNIT || !validTick(r, level[0]) || !Number.isSafeInteger(level[1]) || level[1] <= 0) return fail('Invalid book level');
  if (yes[0] + no[0] > UNIT) return fail('Crossed book');
  const own = outcome === 'YES' ? yes : no, opposite = outcome === 'YES' ? no : yes;
  const size = side === 'buy' ? opposite[1] : own[1];
  if (!Number.isSafeInteger(qty) || qty < 1 || qty > size) return fail('Insufficient displayed size');
  const price = side === 'buy' ? UNIT - opposite[0] : own[0];
  if (!validTick(r, price)) return fail('Unsupported executable tick');
  return { bid: own[0] / UNIT, ask: (UNIT - opposite[0]) / UNIT,
    bid_size: own[1], ask_size: opposite[1],
    price_units: side === 'buy' ? UNIT - opposite[0] : own[0], size,
    fee_units: rest ? kalshiFee(price, qty, r.fee) : r.fee_units * qty, multiplier: 1, underlying: r.underlying,
    strike: r.threshold, type: outcome, exp: null, quoteTs: quoteTime,
    bidTs: quoteTime, askTs: quoteTime,
    prediction: { rules: r, outcome, sequence: b.sequence, receive_ts: b.receive_ts, exchange_ts: b.exchange_ts,
      ...(rest ? { clock_basis: b.clock_basis, provider_response_ts: b.provider_response_ts, request_ts: b.request_ts } : {}) } };
}

// Both receive and exchange times must be at/before the One Clock cursor.
// The call spread estimates a digital over an interval, not a point probability.
export function compareEvent(m, options, spot, cursor) {
  const result = { schema: 'one-clock.prediction.v1', cursor: new Date(cursor).toISOString(),
    market: marketKey(m.rules), rule_version: m.rules.version, evidence: { event: m, options, spot },
    label: 'Option-implied risk-neutral estimate over a strike interval', reasons: [], eligible: false };
  const available = x => x && ['exchange_ts', 'receive_ts'].every(k => Number.isFinite(Date.parse(x[k])) && Date.parse(x[k]) <= cursor);
  if (!available(m.book) || !available(options) || !available(spot) || Date.parse(m.rules.published_at) > cursor) result.reasons.push('Evidence unavailable at cursor');
  const r = m.rules;
  if (r.rule_type !== 'above_threshold') result.reasons.push('No comparable options settlement basis for this event');
  if (spot?.underlying !== r.underlying || spot?.index !== r.index || spot?.currency !== r.currency || !(spot?.price > 0)) result.reasons.push('Mismatched spot context');
  for (const k of ['underlying', 'index', 'window_start', 'window_end', 'inclusive', 'currency']) if (options?.[k] !== r[k]) result.reasons.push(`Mismatched ${k}`);
  if (options?.settlement_basis !== 'spot' || options?.basis !== 0 || options?.expiry !== r.window_end) result.reasons.push('Mismatched expiry or futures/spot basis');
  if (options?.quote_units !== 'USD' || !Number.isFinite(options?.discount_factor) || options.discount_factor <= 0) result.reasons.push('Unknown quote units or discount factor');
  const a = options?.lower, b = options?.upper;
  if (!a || !b || !(a.strike <= r.threshold && r.threshold <= b.strike && b.strike > a.strike) ||
      [a, b].some(c => !Number.isFinite(c.bid) || !Number.isFinite(c.ask) || c.bid < 0 || c.ask < c.bid || !(c.size > 0))) result.reasons.push('Invalid bracketing calls');
  const q = quoteEvent(m, 'YES', 'buy', 1, cursor);
  if (q.error) result.reasons.push(q.error);
  if (available(options) && cursor - Date.parse(options.exchange_ts) > 15000) result.reasons.push('Stale options');
  if (available(spot) && cursor - Date.parse(spot.exchange_ts) > 15000) result.reasons.push('Stale spot');
  if (result.reasons.length) return result;
  const denominator = (b.strike - a.strike) * options.discount_factor;
  const interval = [Math.max(0, (a.bid - b.ask) / denominator), Math.min(1, (a.ask - b.bid) / denominator)];
  if (interval[0] > interval[1]) { result.reasons.push('Inconsistent call spread'); return result; }
  return { ...result, eligible: true, event_interval: [q.bid, q.ask], option_interval: interval,
    gap_interval: [q.bid - interval[1], q.ask - interval[0]],
    timing_uncertainty_ms: Math.abs(Date.parse(m.book.exchange_ts) - Date.parse(options.exchange_ts)),
    size: Math.min(q.size, a.size, b.size), caveat: 'Costs, risk premia and common inputs can erase disagreement.' };
}
