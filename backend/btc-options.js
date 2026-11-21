// Private BTC monthly paper product. No broker orders or public archive fallback.
import { computeBookFromTrades, jsonResponse, requireSession, withUserRecord } from './common.js';
import { createStorage } from './storage.js';

export const BTC_KEY = /^BTCOPT:[0-9a-f]{40}$/;
export const BTC_FEE = 2.50; // Explicit simulation convention, not a broker fee quote.
export function btcAllowed(env, username) {
  return String(env.BTC_OPTIONS_USERS || '').split(',').map(s => s.trim()).includes(username);
}
export function btcSession(now = Date.now()) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago',
    weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(now).map(x => [x.type, x.value]));
  const minute = +p.hour * 60 + +p.minute;
  return (['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].includes(p.weekday) && minute >= 960 && minute < 962) ||
    (p.weekday === 'Sat' && minute >= 120 && minute < 240) ? 'maintenance' : 'open';
}
export function validBtcContract(c, key = c?.contract_key) {
  return c && BTC_KEY.test(key) && c.contract_key === key && c.instrument_type === 'futures_option' &&
    c.product === '/BTC' && c.series === 'monthly' && c.exchange === 'CME' && c.multiplier === 5 &&
    ['call', 'put'].includes(c.type) && Number.isFinite(c.strike) && c.strike > 0 &&
    typeof c.provider_symbol === 'string' && c.provider_symbol.length <= 128 &&
    typeof c.streamer_symbol === 'string' && c.streamer_symbol.endsWith(':XCME') &&
    /^\/BTC[A-Z][0-9]{1,4}$/.test(c.future_symbol) && c.future_expiry === c.exp &&
    /^\d{4}-\d{2}-\d{2}$/.test(c.exp) && Number.isFinite(Date.parse(c.expires_at)) &&
    Number.isFinite(Date.parse(c.stops_trading_at));
}
export function exactBtcQuote(payload, intent, now = Date.now()) {
  const row = payload?.quotes?.find(q => q.symbol === intent.sym);
  const c = row?.contract;
  const fail = error => ({ error, status: 409 });
  if (!validBtcContract(c, intent.sym)) return fail('Exact BTC monthly identity unavailable');
  if (payload.session !== 'open' || btcSession(now) !== 'open') return fail('BTC session closed');
  if (now >= Date.parse(c.stops_trading_at)) return fail('Contract expired');
  if (intent.side === 'buy' && !payload.entry_keys?.includes(intent.sym)) return fail('Contract is not in the active monthly series');
  const side = intent.side === 'buy' ? 'ask' : 'bid';
  const ts = Date.parse(row[side + '_ts']);
  const receipt = Date.parse(row[side + '_received_ts']);
  if (![ts, receipt].every(t => Number.isFinite(t) && now - t >= -1000 && now - t <= 15000)) return fail('Executable side is stale or untimed');
  if (!(Number.isFinite(row[side]) && row[side] > 0 && Number.isFinite(row[side + '_size']) && row[side + '_size'] >= 1)) return fail('Executable side has no price or size');
  if (Number.isFinite(row.bid) && Number.isFinite(row.ask) && row.bid > row.ask) return fail('Crossed book');
  if (row.available !== true) return fail('Quote unavailable after disconnect or maintenance');
  return { bid: row.bid, ask: row.ask, strike: c.strike, type: c.type, exp: c.exp, multiplier: 5,
    contract: structuredClone(c), quoteTs: row[side + '_ts'], bidTs: row.bid_ts, askTs: row.ask_ts,
    source_quote: structuredClone(row), fees: BTC_FEE, fee_model: 'btc-paper-flat-2.50-per-side-v1' };
}
export function heldBtc(record) {
  return Object.values(computeBookFromTrades(record.trades || [])).filter(b => b.instrument_type === 'futures_option' && b.pos > 0).map(b => b.contract);
}
export async function fetchBtc(env, record, symbols = []) {
  if (!env.LIVE_QUOTE_ORIGIN || !env.LIVE_QUOTE_KEY) throw new Error('Quote service unavailable');
  const url = new URL('/btc-options', env.LIVE_QUOTE_ORIGIN);
  url.searchParams.set('symbols', symbols.join(','));
  url.searchParams.set('held', JSON.stringify(heldBtc(record).map(c => c.provider_symbol)));
  return fetch(url, { headers: { 'X-Live-Quote-Key': env.LIVE_QUOTE_KEY },
    redirect: 'error', signal: AbortSignal.timeout(10000), cf: { cacheTtl: 0, cacheEverything: false } });
}
export async function handleBtcOptions(request, env) {
  const session = await requireSession(request, env);
  if (!session) return jsonResponse({ error: 'Sign in required' }, 401);
  // Holdings are not a market-data entitlement. Entries may be disabled
  // independently while an entitled owner continues to manage positions.
  if (!btcAllowed(env, session.username)) return jsonResponse({ error: 'BTC options access not enabled' }, 403);
  const symbols = (new URL(request.url).searchParams.get('symbols') || '').split(',').filter(Boolean);
  if (symbols.length > 64 || symbols.some(s => !BTC_KEY.test(s))) return jsonResponse({ error: 'Invalid contract keys' }, 400);
  try {
    const response = await fetchBtc(env, session.record, symbols);
    if (!response.ok) return jsonResponse({ error: 'BTC source unavailable', provider_status: response.status }, 503);
    const data = await response.json();
    const entries = env.BTC_OPTIONS_ENTRIES === '1';
    return jsonResponse({ ...data, entries_enabled: entries, fee_per_side: BTC_FEE,
      fee_model: 'Simulated $2.50 per contract per side; $0 expiration fee' }, 200);
  } catch { return jsonResponse({ error: 'BTC source unavailable' }, 503); }
}

// Licensed final marks must be provisioned separately in this exact private
// namespace. No daily close, browser-supplied mark or live spot can substitute.
export async function btcFinalMark(env, c, now = Date.now()) {
  if (!validBtcContract(c) || now < Date.parse(c.expires_at)) return null;
  const obj = await env.PAPER_TRADES?.get(`paper-trades/btc-final/${c.contract_key.slice(7)}/${c.exp}.json`);
  if (!obj) return null;
  const m = await obj.json();
  return m.contract_key === c.contract_key && m.future_symbol === c.future_symbol && m.exp === c.exp &&
    m.kind === 'official_final' && m.source === 'CME' && typeof m.source_ref === 'string' && m.source_ref.length > 0 &&
    Number.isFinite(m.value) && m.value >= 0 && Date.parse(m.published_at) >= Date.parse(c.expires_at) &&
    Date.parse(m.published_at) <= now ? m : null;
}
export async function settleBtcAccount(env, username, now = Date.now()) {
  const record = await createStorage(env).users.get(username);
  if (!record) return { error: 'not_found' };
  const marks = new Map();
  for (const c of heldBtc(record)) {
    try { const m = await btcFinalMark(env, c, now); if (m) marks.set(c.contract_key, m); } catch { /* retry later */ }
  }
  return withUserRecord(env, username, current => {
    const trades = [...current.trades], settled = [], pending = [];
    let balance_cash = current.balance_cash;
    for (const [sym, b] of Object.entries(computeBookFromTrades(trades))) {
      if (b.instrument_type !== 'futures_option' || b.pos <= 0 || now < Date.parse(b.contract?.expires_at)) continue;
      const id = `settle:${sym}:${b.exp}`;
      if (trades.some(t => t.execution_request_id === id)) continue;
      const mark = marks.get(sym);
      if (!mark) { pending.push(sym); continue; }
      const price = Math.max(b.type === 'call' ? mark.value - b.strike : b.strike - mark.value, 0);
      const cash_delta = price * b.pos * 5;
      const trade = { execution_id: id, execution_request_id: id, sym, underlying: '/BTC',
        instrument_type: 'futures_option', contract: b.contract, multiplier: 5, strike: b.strike,
        type: b.type, exp: b.exp, side: 'sell', qty: b.pos, price, fees: 0, cash_delta,
        fee_model: 'btc-paper-flat-2.50-per-side-v1', settlement_mark: mark, status: 'filled',
        ts: new Date(now).toISOString(), username, note: 'Simulated monthly exercise and immediate futures cash settlement' };
      trades.push(trade); settled.push(trade); balance_cash += cash_delta;
    }
    return { ...(settled.length ? { record: { ...current, trades, balance_cash } } : {}),
      result: { settled, pending, balance_cash } };
  });
}
