// Posterior predictive for the minute-end midpoint under a constant unknown
// drift and Brownian price noise. Time is measured in minutes and prices in $.
// The prior on the one-minute drift is Normal(0, sigma^2); expected squared
// one-minute noise is sigma^2. A quote is an observation, never an OHLC bar.
function normalCdf(z) {
  // Abramowitz and Stegun 26.2.17, max absolute error < 8e-8.
  const x = Math.abs(z);
  const t = 1 / (1 + 0.2316419 * x);
  const poly = t * (0.319381530 + t * (-0.356563782 + t *
    (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  const tail = Math.exp(-x * x / 2) * poly / Math.sqrt(2 * Math.PI);
  return z < 0 ? tail : 1 - tail;
}

export function regimeNowcast(rows, gaps, { minute, cutoff, low, high, knownAt, sigma }) {
  if (![minute, cutoff, low, high, knownAt, sigma].every(Number.isFinite)
      || low <= 0 || high <= low || sigma <= 0) {
    return { reason: 'Enter a positive band and one-minute price volatility, and when those inputs were known.' };
  }
  if (knownAt > cutoff || new Date(knownAt).toISOString().slice(0,10) !==
      new Date(minute).toISOString().slice(0,10)) {
    return { reason: 'Use settings known by the cursor on this QQQ trading date.' };
  }
  if (cutoff < minute || cutoff >= minute + 60000) {
    return { reason: 'Choose a cursor inside the minute.' };
  }
  if (gaps.some(([start, end]) => start <= cutoff && end >= minute)) {
    return { reason: 'Incomplete tape coverage through this cursor.' };
  }
  let first, last;
  for (const row of rows) {
    if (row.kind !== 'quote' || ![row.at, row.bidAt, row.askAt, row.bid, row.ask].every(Number.isFinite)
        || row.at < minute || row.at > cutoff || row.bid <= 0 || row.ask < row.bid) continue;
    const at = Math.max(row.bidAt, row.askAt);
    const oldest = Math.min(row.bidAt, row.askAt);
    if (oldest < minute || at > row.at || row.at - oldest > 5000) continue;
    const quote = { at, oldest, received: row.at, price: (row.bid + row.ask) / 2 };
    if (!first || at < first.at || (at === first.at && row.at < first.received)) first = quote;
    if (!last || at > last.at || (at === last.at && row.at >= last.received)) last = quote;
  }
  if (!last || cutoff - last.oldest > 5000) return { reason: 'Awaiting a fresh in-minute two-sided QQQ quote.' };
  const observed = (last.at - first.at) / 60000;
  const remaining = (minute + 60000 - last.at) / 60000;
  const drift = observed > 0 ? (last.price - first.price) / (1 + observed) : 0;
  const mean = last.price + remaining * drift;
  const sd = sigma * Math.sqrt(remaining + remaining * remaining / (1 + observed));
  const below = normalCdf((low - mean) / sd);
  const above = normalCdf((mean - high) / sd);
  return { below, inside: Math.max(0, 1 - below - above), above };
}
