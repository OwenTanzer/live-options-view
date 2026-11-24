// Deterministic provider responses for the public Deribit market-data methods.
export const origin = 'https://usdc-fixture.invalid';
export const instrument = 'BTC_USDC-27NOV36-90000-C';
export function usdcFixture(input, book = {}) {
  const url = new URL(input);
  if (url.origin !== origin) throw new Error('Unexpected USDC fixture origin');
  const name = url.searchParams.get('instrument_name') || instrument;
  const spec = { instrument_name: name, kind: 'option', is_active: true,
    quote_currency: 'USDC', settlement_currency: 'USDC', contract_size: 1,
    min_trade_amount: 0.01, strike: 90000, option_type: name.endsWith('-C') ? 'call' : 'put',
    expiration_timestamp: Date.parse('2036-11-27T08:00:00Z') };
  const bid = book.bid ?? 99, ask = book.ask ?? 100;
  const methods = {
    get_instruments: [spec], get_instrument: spec,
    get_book_summary_by_currency: [{ instrument_name: name, creation_timestamp: Date.now(),
      bid_price: bid, ask_price: ask, mark_price: (bid + ask) / 2, underlying_price: 90000 }],
    get_order_book: { instrument_name: name, state: 'open', timestamp: book.timestamp ?? Date.now(),
      index_price: 90000, best_bid_price: bid, best_ask_price: ask, best_bid_amount: 0.02, best_ask_amount: 0.01 }
  };
  const method = url.pathname.split('/').pop();
  if (!(method in methods)) throw new Error('Unexpected USDC fixture method ' + method);
  return Response.json({result: methods[method]});
}
