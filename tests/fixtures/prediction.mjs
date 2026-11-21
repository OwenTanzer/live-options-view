const iso = n => new Date(n).toISOString();
export function fixture(at = Date.now()) {
  return { rules: { provider: 'kalshi', environment: 'fixtures', series: 'BTC-THRESHOLD', event: 'Synthetic BTC above 100000', market: 'BTC-100K', version: '1', index: 'synthetic-BTC-USD', text: 'Synthetic index strictly above $100,000 at the stated instant. $1 per winning contract.', source: 'synthetic://rules/1', fee_convention: 'fixed_per_contract', fee_units: 0, payout_units: 10000, tick_units: 100, size_increment: 1, currency: 'USD', underlying: 'BTC', threshold: 100000, inclusive: false, rule_type: 'above_threshold', void_policy: 'refund_entry_cost', published_at: iso(at - 1000), cutoff: iso(at + 60000), window_start: iso(at + 60000), window_end: iso(at + 60000) },
    status: 'open', book: { sequence: 1, synchronized: true, gap: false, yes: [[4000, 3]], no: [[5500, 3]], exchange_ts: iso(at), receive_ts: iso(at) } };
}
