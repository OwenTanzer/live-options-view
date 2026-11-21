import { configured } from './kalshi-access.js';
import { budget } from './kalshi-store.js';
import { readLimited } from './common.js';
import { hashSessionToken } from './storage.js';
import { fixed, kalshiSnapshot, WEATHER_SERIES, SERIES_SUBJECT } from './kalshi-model.js';
import { validateRules, quoteEvent } from './prediction-model.js';

export const KALSHI_API = 'https://external-api.kalshi.com/trade-api/v2';
const ticker = value => typeof value === 'string' && /^[A-Z0-9][A-Z0-9.-]{0,100}$/.test(value);
export async function publicRead(env, path) {
  if (!configured(env)) throw Error('Personal research is not enabled');
  if (!/^\/(markets|events|series)(\/|\?)/.test(path) || path.includes('..') || path.includes('#')) throw Error('Unsupported source path');
  await budget(env, 'provider-day', 2000, 86400000);
  await budget(env, 'provider-minute', 60, 60000);
  const start = Date.now();
  const response = await fetch(KALSHI_API + path, { method: 'GET', cache: 'no-store', redirect: 'error',
    headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' }, signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw Error('Kalshi public source unavailable');
  const data = JSON.parse(await readLimited(response, 1000000));
  const end = Date.now(), date = Date.parse(response.headers.get('Date'));
  if (!Number.isFinite(date) || date > end || end - date > 10000 || end - start > 5000 ||
      Number(response.headers.get('Age') || 0) > 0) throw Error('Source response is stale or unclocked');
  return { data, request_ts: new Date(start).toISOString(), receive_ts: new Date(end).toISOString(),
    provider_response_ts: new Date(date).toISOString(), path };
}

export function effectiveFee(series, changes, market, now, seriesChanges = { series_fee_change_arr: [] }) {
  if (!Array.isArray(changes?.event_fee_changes) || changes.cursor) throw Error('Incomplete event fee evidence');
  let type = series.fee_type, multiplier = series.fee_multiplier;
  const history = changes.event_fee_changes;
  if (!Array.isArray(seriesChanges.series_fee_change_arr) || seriesChanges.series_fee_change_arr.some(x =>
      x.series_ticker !== series.ticker || !Number.isFinite(Date.parse(x.scheduled_ts)))) throw Error('Invalid series fee schedule');
  if (history.some(x => x.event_ticker !== market.event_ticker || x.series_ticker !== series.ticker ||
      !Number.isFinite(Date.parse(x.scheduled_ts)))) throw Error('Invalid fee history');
  const latest = history.filter(x => Date.parse(x.scheduled_ts) <= now).sort((a,b) => Date.parse(b.scheduled_ts) - Date.parse(a.scheduled_ts))[0];
  if (latest) { type = latest.fee_type_override ?? type; multiplier = latest.fee_multiplier_override ?? multiplier; }
  if (market.fee_waiver_expiration_time && Date.parse(market.fee_waiver_expiration_time) > now) multiplier = 0;
  if (multiplier === null || multiplier === undefined) throw Error('Unknown fee multiplier');
  return { type, multiplier: String(multiplier), role: 'taker', rounding: 'ceil_0.0001_USD',
    schedule: 'https://kalshi.com/docs/kalshi-fee-schedule.pdf', effective: '2026-07-07',
    next_change: [...history, ...seriesChanges.series_fee_change_arr,
      ...(market.fee_waiver_expiration_time ? [{ scheduled_ts: market.fee_waiver_expiration_time }] : [])]
      .filter(x => Date.parse(x.scheduled_ts) > now).sort((a,b) => Date.parse(a.scheduled_ts) - Date.parse(b.scheduled_ts))[0]?.scheduled_ts ?? null };
}
export async function normalizeMarket(raw, event, series, fees, bookReceipt, seriesChanges) {
  if (!ticker(raw?.ticker) || raw.event_ticker !== event.event_ticker || event.series_ticker !== series.ticker ||
      !Object.hasOwn(SERIES_SUBJECT, series.ticker) || raw.market_type !== 'binary' || raw.mve_collection_ticker ||
      raw.is_provisional === true || fixed(raw.notional_value_dollars, 10000) !== 10000 ||
      raw.settlement_bounds_type !== 'default' || !raw.rules_primary || !series.contract_terms_url ||
      !series.settlement_sources?.length) throw Error('Unsupported contract; no paper execution');
  const terms = { provider: 'kalshi', environment: 'production', series: series.ticker, event: event.event_ticker,
    market: raw.ticker, title: raw.title, category: event.category, rule_type: WEATHER_SERIES.includes(series.ticker) ? 'official_weather_binary' : 'official_event_binary',
    underlying: SERIES_SUBJECT[series.ticker], index: series.settlement_sources.map(x => x.name).join('; '),
    settlement_sources: series.settlement_sources, text: raw.rules_primary + '\n' + (raw.rules_secondary || ''),
    source: series.contract_terms_url, contract_url: series.contract_url,
    published_at: raw.created_time, cutoff: raw.close_time, window_end: raw.latest_expiration_time,
    currency: 'USD', payout_units: 10000, size_increment: 1, void_policy: 'official_only',
    fee_convention: 'kalshi_taker_2026_07_07', fee: effectiveFee(series, fees, raw, Date.parse(bookReceipt.receive_ts), seriesChanges),
    price_ranges: (raw.price_ranges || []).map(p => ({ start: fixed(p.start, 10000), end: fixed(p.end, 10000), step: fixed(p.step, 10000) })),
    early_close_condition: raw.early_close_condition || '', can_close_early: raw.can_close_early === true,
  };
  const rules = { ...terms, version: await hashSessionToken(JSON.stringify(terms)) };
  validateRules(rules);
  const b = { ...kalshiSnapshot(bookReceipt.data), synchronized: true, gap: false,
    // The REST response has no exchange quote timestamp or sequence. Do not invent either.
    exchange_ts: null, sequence: null, clock_basis: 'provider_http_date',
    request_ts: bookReceipt.request_ts, receive_ts: bookReceipt.receive_ts, provider_response_ts: bookReceipt.provider_response_ts };
  return { rules, status: raw.status === 'active' ? 'open' : raw.status, book: b };
}
export async function fetchMarket(env, market) {
  if (!ticker(market) || !Object.keys(SERIES_SUBJECT).some(s => market.startsWith(s + '-'))) throw Error('Unsupported market series');
  const mr = await publicRead(env, `/markets/${market}`), raw = mr.data.market;
  if (raw?.ticker !== market || !ticker(raw.event_ticker)) throw Error('Market identity mismatch');
  const er = await publicRead(env, `/events/${raw.event_ticker}`), event = er.data.event;
  if (event?.event_ticker !== raw.event_ticker || !Object.hasOwn(SERIES_SUBJECT, event.series_ticker)) throw Error('Event identity mismatch');
  const [sr, fr, br, sc] = await Promise.all([
    publicRead(env, `/series/${event.series_ticker}`),
    publicRead(env, `/events/fee_changes?event_ticker=${encodeURIComponent(raw.event_ticker)}&limit=100`),
    publicRead(env, `/markets/${market}/orderbook?depth=10`),
    publicRead(env, `/series/fee_changes?series_ticker=${encodeURIComponent(event.series_ticker)}`),
  ]);
  const normalized = await normalizeMarket(raw, event, sr.data.series, fr.data, br, sc.data);
  return { schema: 'kalshi.private.evidence.v1', source: 'public_kalshi_rest', environment: 'production',
    market: normalized, receipts: [mr, er, sr, fr, br, sc] };
}
export async function discover(env) {
  // Sample distinct subjects before adding city temperature series; keep the catalog bounded.
  const result = [];
  for (const series of [...Object.keys(SERIES_SUBJECT).filter(s => !WEATHER_SERIES.includes(s)), ...WEATHER_SERIES]) {
    const r = await publicRead(env, `/events?series_ticker=${series}&status=open&with_nested_markets=true&limit=2`);
    for (const e of (r.data.events || []).slice(0, 2)) {
      if (e.series_ticker !== series) continue;
      for (const m of (e.markets || []).slice(0, WEATHER_SERIES.includes(series) ? 3 : 6)) {
        if (m.market_type !== 'binary' || !ticker(m.ticker) || m.status !== 'active') continue;
        // Discovery never promotes last_price or list bid/ask into an executable quote.
        result.push({ ticker: m.ticker, event: e.event_ticker, series, title: m.title,
          outcome: m.yes_sub_title, category: e.category, cutoff: m.close_time,
          volume_24h: m.volume_24h_fp, open_interest: m.open_interest_fp, observed_at: r.receive_ts });
      }
    }
  }
  return { markets: result, collected_at: new Date().toISOString(), scope: 'Selected series span space, mathematics, music, games, geology, economics, climate, health and city temperatures. Inspect a contract for its current executable sides.' };
}
export function displayMarket(evidence, now = Date.now()) {
  const m = evidence.market, yes = quoteEvent(m, 'YES', 'buy', 1, now), no = quoteEvent(m, 'NO', 'buy', 1, now);
  return { rules: m.rules, status: m.status, quotes: yes.error || no.error ? null : { YES: yes, NO: no },
    reason: yes.error || no.error || null, received_at: m.book.receive_ts,
    provider_response_ts: m.book.provider_response_ts, exchange_ts: null,
    clock_basis: 'Provider HTTP response time; exchange quote timestamp unavailable',
    comparison: 'No matched options settlement basis for this event; numerical comparison unavailable.' };
}
