# Listed-share market trading plan

Issue: #8

## Goal

Add paper-market orders for the equity names already shown in the price strip, with share-native accounting and presentation that remains separate from option contracts.

## Design

1. Mark the existing listed equities as the only tradeable share symbols in this PR. Indexes, futures, yields, crypto, and arbitrary symbols remain read-only.
2. Add an instrument_type field to paper-trade requests and records. Legacy requests default to option.
3. Dispatch execution quote validation by instrument: options retain exact-contract metadata checks, while shares require a fresh two-sided equity quote.
4. Use multiplier 100 for options and 1 for shares in cash, cost-basis, realized P/L, unrealized P/L, and market-value calculations.
5. Render separate Options and Shares position tables. Share rows show symbol, quantity, entry, live mark, market value, and unrealized P/L.
6. Keep share orders market-only in this PR so share execution can be verified independently before the shared limit path is enabled in PR 3.

## Verification

- Unit-test listed-symbol validation and rejection of unsupported/non-equity symbols.
- Unit-test fresh, stale, missing, and invalid share execution quotes.
- Unit-test share partial exits, realized P/L, and the 1x multiplier.
- Run all web suites and JavaScript syntax checks.
- Verify listed equity price tiles expose a market-order ticket while non-equity tiles remain read-only.

## Deferred

Arbitrary ticker lookup is deferred because the collector has a fixed subscription set. Supporting Other stock safely requires explicit quote lookup and a bounded subscription lifecycle. Share limit orders are intentionally deferred to PR 3.
