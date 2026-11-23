// Integer ten-thousandths of USD. Pure functions shared with collector/replay.
export function fixed(value, scale) {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!m) throw Error('Invalid decimal');
  const digits = Math.log10(scale), fraction = m[2] ?? '';
  if (fraction.slice(digits).replace(/0/g, '')) throw Error('Unsupported increment');
  const result = Number(m[1]) * scale + Number(fraction.slice(0, digits).padEnd(digits, '0'));
  if (!Number.isSafeInteger(result)) throw Error('Unsafe decimal');
  return result;
}
export function kalshiSnapshot(payload) {
  const book = payload.orderbook_fp;
  if (!book) throw Error('Expected documented fixed-point orderbook');
  const levels = rows => {
    if (!Array.isArray(rows) || rows.length > 100) throw Error('Invalid book depth');
    // Only integer orders are supported; fractional displayed liquidity is rounded DOWN.
    return rows.map(([price, size]) => [fixed(price, 10000), Math.floor(fixed(size, 100) / 100)])
      .filter(x => x[1] > 0).sort((a, b) => b[0] - a[0]);
  };
  return { yes: levels(book.yes_dollars), no: levels(book.no_dollars) };
}
// July 7, 2026 official direct-member schedule: ceil to a centicent, not a cent.
// Integer contract cost is already integral in centicents. Marketable taker only.
export function kalshiFee(price, qty, fee) {
  if (!['quadratic', 'quadratic_with_maker_fees'].includes(fee?.type) ||
      !Number.isSafeInteger(price) || price < 0 || price > 10000 ||
      !Number.isSafeInteger(qty) || qty < 1 || qty > 10000) throw Error('Unsupported fee inputs');
  const multiplier = fixed(fee.multiplier, 10000);
  if (multiplier > 1000000) throw Error('Unsupported fee multiplier');
  const n = 7n * BigInt(multiplier) * BigInt(qty) * BigInt(price) * BigInt(10000 - price);
  const d = 100n * 10000n * 10000n;
  return Number((n + d - 1n) / d);
}
export const WEATHER_SERIES = Object.freeze(['KXHIGHNY', 'KXHIGHCHI', 'KXHIGHLAX', 'KXHIGHMIA']);
export const SERIES_SUBJECT = Object.freeze({
  KXHIGHNY: 'WEATHER', KXHIGHCHI: 'WEATHER', KXHIGHLAX: 'WEATHER', KXHIGHMIA: 'WEATHER',
  KXELONMARS: 'SPACE', KXMILLENNIUMNEXT: 'MATHEMATICS', KXPERFORMBONDSONG: 'MUSIC',
  KXGTA7RELEASEDATE: 'GAMES', KXERUPTSUPER: 'GEOLOGY', KXGDPYEAR: 'ECONOMICS',
  KXCO2LEVEL: 'CLIMATE', KXFDATYPE1DIABETES: 'HEALTH',
});
export const isKalshiBinary = r => ['official_weather_binary', 'official_event_binary'].includes(r.rule_type);
export function validateKalshiRules(r) {
  if (r.provider !== 'kalshi' || r.environment !== 'production' || !isKalshiBinary(r) ||
      !Object.hasOwn(SERIES_SUBJECT, r.series) || r.currency !== 'USD' || r.payout_units !== 10000 ||
      r.size_increment !== 1 || r.fee_convention !== 'kalshi_taker_2026_07_07' ||
      r.void_policy !== 'official_only' || r.underlying !== SERIES_SUBJECT[r.series] ||
      !Array.isArray(r.price_ranges) || !r.price_ranges.length) throw Error('Unsupported binary contract');
  for (const k of ['published_at', 'cutoff', 'window_end']) if (!Number.isFinite(Date.parse(r[k]))) throw Error('Missing rule time');
  for (const p of r.price_ranges) if (![p.start, p.end, p.step].every(Number.isSafeInteger) ||
      p.start < 0 || p.end > 10000 || p.end <= p.start || p.step < 1) throw Error('Unsupported tick schedule');
  kalshiFee(5000, 1, r.fee);
  return r;
}
export function validTick(r, price) {
  return isKalshiBinary(r)
    ? r.price_ranges.some(p => price >= p.start && price <= p.end && (price - p.start) % p.step === 0)
    : price % r.tick_units === 0;
}
