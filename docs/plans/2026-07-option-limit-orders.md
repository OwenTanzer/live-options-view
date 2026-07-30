# Option limit orders plan

Issue: #6

## Goal

Add explicit market and limit order choices to the existing option ticket without changing the legacy market-order contract used by automated clients.

## Design

1. Keep `POST /api/paper-trade` as the single execution authority.
2. Add optional `order_type` and `limit_price` fields. Requests that omit `order_type` remain market orders for backward compatibility.
3. Treat this first limit implementation as marketable-limit execution: a buy fills at the fresh ask only when the ask is at or below the limit; a sell fills at the fresh bid only when the bid is at or above the limit. Non-marketable orders are rejected and are not persisted as working orders.
4. Record `order_id`, `order_type`, `limit_price`, and `status` on successful fills so the model can grow into working, stop, and bracket orders later.
5. Validate the same constraints in the browser and Worker, while keeping the Worker authoritative.

## Verification

- Unit-test market compatibility, limit-price validation, idempotency matching, and buy/sell price protection.
- Unit-test client-side quantity and limit validation.
- Run all web JavaScript suites and syntax checks.
- Manually verify ticket state: market/limit toggle, pending state, rejection recovery, fill status, and order ID display.

## Deferred

Persistent working orders, cancellation, stop-loss, and one-cancels-other brackets need an order lifecycle and quote-trigger service. The fields introduced here leave room for those states without implying that an unmarketable order is resting.
