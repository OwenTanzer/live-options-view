// Offline exercise of the ordinary live rendering path. No provider or archive writes.
function simulateOrderFlow(step, now) {
  const spot = 638 - (30 - Math.abs(30 - step % 60));
  const day = new Date(now).toISOString().slice(0, 10);
  const expiry = day.replaceAll('-', '').slice(2);
  const opening = {}, contracts = {}, rows = [];
  for (let strike = 520; strike <= 712; strike++) {
    for (const type of ['C', 'P']) {
      const symbol = 'QQQ' + expiry + type + String(strike * 1000).padStart(8, '0');
      opening[symbol] = now - 60000;
      const size = Math.max(1, 32 - Math.abs(spot - strike));
      contracts[symbol] = {B: size, S: Math.ceil(size / 2), U: 1, prints: 1};
      rows.push({OptionSymbol: symbol, UnderlyingPrice: spot, OpenInterest: 250, Gamma: 0.01});
    }
  }
  const prints = Array.from({length: 24}, (_, i) => {
    const strike = spot + (i % 7) - 3, type = i % 2 ? 'C' : 'P';
    return {t: now - (23 - i) * 2000, strike, type, symbol: 'QQQ' + expiry + type + String(strike * 1000).padStart(8, '0'),
      side: i % 2 ? 'buy' : 'sell', size: i % 4 + 1, price: 1.25};
  });
  return {feed: {rows: prints, lastReceived: now, session: {day, first: now - 60000, last: now, prints: 386, opening, contracts, gaps: []}},
    chain: {timestamp: new Date(now).toISOString(), underlying_price: spot, rows}};
}
