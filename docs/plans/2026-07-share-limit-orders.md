# Share limit order extension plan

Depends on: PR 1 option limits and PR 2 listed-share market trading

## Goal

Enable marketable limit buys and sells for listed shares only after the option-limit contract and share-market path have each been implemented and verified independently.

## Design

1. Reuse the generic client order normalizer and Worker execution-price guard introduced for options.
2. Reuse the fresh, side-specific share quote validation and 1x accounting introduced for market shares.
3. Generalize the share validator so quantity/position checks compose with either market or limit fields.
4. Add the same Market/Limit selector and cent-denominated limit input to the share ticket.
5. Preserve the no-short guard: a share sell limit may reduce or close a long position but may not exceed it.
6. Keep limit behavior consistent across instruments: fill at the fresh ask/bid when marketable, including price improvement; otherwise reject without persisting a working order.

## Verification

- Unit-test accepted share buy/sell limits, invalid increments, and over-selling protection.
- Compose a fresh share quote with the generic limit-price guard for both marketable and rejected cases.
- Run the full collector and web suites plus syntax checks.
- Verify the share ticket switches between Market and Limit, reveals the price input, and keeps unavailable-quote actions disabled.

## Deferred

Resting orders, cancellation, stop-loss, and bracket/OCO behavior still require a persistent order lifecycle and quote-trigger service.
