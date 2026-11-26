// Bounded allowlist for public replay. The containing snapshot has already
// passed instrument, identity, publication and availability checks.
import {sessionAt} from '../site/shared/one-clock-session.js';

const keys = ['5m','9m','15m','30m','60m','session'];
const statuses = new Set(['ok','no_data','warming_up','stale','stale_anchor']);
export function momentumMarket(market, {instrument, session, eventAt}) {
  const windows = market?.momentum_windows;
  if (instrument !== 'QQQ' || market?.symbol !== instrument || windows?.session !== session) return {};
  const spotAt = Date.parse(windows.observed_at);
  if (!Number.isFinite(spotAt) || spotAt !== Date.parse(market.spot_ts) ||
      spotAt > eventAt || sessionAt(spotAt) !== session) return {};
  const readings = {};
  for (const key of keys) {
    const raw = windows.readings?.[key];
    let reading = {status:'no_data', return_pct:null, direction:null, anchor_observed_at:null};
    if (raw && statuses.has(raw.status)) {
      if (raw.status !== 'ok') reading.status = raw.status;
      else {
        const anchor = Date.parse(raw.anchor_observed_at);
        if (Number.isFinite(anchor) && anchor <= spotAt && sessionAt(anchor) === session &&
            Number.isFinite(raw.return_pct) && ['up','down','flat'].includes(raw.direction)) {
          reading = {status:'ok', return_pct:raw.return_pct, direction:raw.direction,
                     anchor_observed_at:new Date(anchor).toISOString()};
        }
      }
    }
    readings[key] = reading;
  }
  const iso = new Date(spotAt).toISOString();
  return {underlying_market: {symbol:instrument, spot_ts:iso,
    freshness:market.freshness === 'live' ? 'live' : 'stale',
    momentum_windows:{observed_at:iso, session, readings}}};
}
