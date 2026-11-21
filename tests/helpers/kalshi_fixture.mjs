// Synthetic provider protocol examples. Never imported by the production Worker.
import { readFileSync } from 'node:fs';
import { withD1 } from './test_env.mjs';
import { CREDIT_ORIGIN, SITE, OWNER_ID } from '../../backend/kalshi-access.js';
import { KALSHI_API } from '../../backend/kalshi-source.js';
export const COOKIE = '__Host-cds-session=synthetic_session_0123456789';
export const TICKER = 'KXHIGHNY-26NOV21-T60';
export function fixture() {
  const at = Date.now(), iso = t => new Date(t).toISOString();
  const raw = { ticker: TICKER, event_ticker: 'KXHIGHNY-26NOV21', market_type: 'binary',
    title: 'SYNTHETIC TEST: New York daily high', yes_sub_title: '60° or above',
    status: 'active', created_time: iso(at - 86400000), close_time: iso(at + 3600000),
    latest_expiration_time: iso(at + 86400000), notional_value_dollars: '1.0000',
    rules_primary: 'SYNTHETIC FIXTURE: official daily maximum temperature at the named station.', rules_secondary: '',
    settlement_bounds_type: 'default', price_ranges: [{ start: '0.0000', end: '1.0000', step: '0.0100' }] };
  const series = { ticker: 'KXHIGHNY', fee_type: 'quadratic', fee_multiplier: 1,
    contract_terms_url: 'https://kalshi.com/synthetic-fixture-terms', contract_url: 'https://kalshi.com/synthetic-fixture',
    settlement_sources: [{ name: 'SYNTHETIC weather station', url: 'https://example.invalid' }] };
  const event = { event_ticker: raw.event_ticker, series_ticker: series.ticker, category: 'Climate and Weather', markets: [raw] };
  return { raw, series, event, fees: { event_fee_changes: [], cursor: '' },
    book: { orderbook_fp: { yes_dollars: [['0.4000', '3.00']], no_dollars: [['0.5500', '3.00']] } },
    identity: OWNER_ID, requests: [], dateOffset: 0, age: null };
}
export function envFixture() {
  const env = withD1({ KALSHI_PERSONAL_ENABLED: '1', KALSHI_ENTRIES: '1',
    PAPER_TRADES: new Proxy({}, { get() { throw Error('Kalshi must not touch R2'); } }) });
  env.DB.db.exec(readFileSync(new URL('../../backend/migrations/0006_kalshi_private.sql', import.meta.url), 'utf8'));
  return env;
}
export function network(f) {
  return async (input, options = {}) => {
    const u = String(input); f.requests.push({ url: u, options });
    if (u === CREDIT_ORIGIN + '/api/private/identity') return Response.json({ github_id: f.identity });
    if (!u.startsWith(KALSHI_API + '/')) throw Error('Unexpected network destination');
    if ((options.method || 'GET') !== 'GET' || options.headers?.Cookie || options.headers?.Authorization) throw Error('Unsafe provider request');
    const path = u.slice(KALSHI_API.length); let data;
    if (path.startsWith('/markets/orderbooks?')) data = {orderbooks: new URL(u).searchParams.get('tickers').split(',').map(ticker => ({ticker, ...f.book}))};
    else if (path.includes('/orderbook')) data = f.book;
    else if (path.startsWith('/markets/')) data = { market: f.raw };
    else if (path.startsWith('/events/fee_changes')) data = f.fees;
    else if (path.startsWith('/events?')) data = { events: path.includes('series_ticker=KXHIGHNY') ? [f.event] : [], cursor: '' };
    else if (path.startsWith('/events/')) data = { event: f.event };
    else if (path.startsWith('/series/fee_changes')) data = { series_fee_change_arr: [] };
    else if (path.startsWith('/series/')) data = { series: f.series };
    else throw Error('Unexpected source route');
    return Response.json(data, { headers: { Date: new Date(Date.now() + f.dateOffset).toUTCString(), ...(f.age ? { Age: f.age } : {}) } });
  };
}
export function request(path, body, cookie = COOKIE, origin = SITE) {
  return new Request(origin + '/api/kalshi/' + path, { headers: { Cookie: cookie, Origin: origin,
    ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { method: 'POST', body: JSON.stringify(body) } : {}) });
}
