# tastytrade brokerage route for `oi_skew_qqq`

Status: **built, switched off, waiting for owner approval.** Merging this
changes nothing at runtime. The catalog entry is `"active": false`, and even
when it is active the route refuses to trade until the account owner sets
the environment variables below.

## What it does

`crassus/tastytrade_broker.py` lets one crassus account send its orders to a
tastytrade brokerage account instead of the Worker's `/api/paper-trade`. The
strategy, the decision ledger, the in-flight intent file, the runner and the
mandatory EOD flatten are the same code every paper bot uses. Only the
session (`TastySession`) and the executor (`TastyExecutionClient`) are
swapped, and only for an account whose catalog entry says
`"broker": "tastytrade"`.

- **Account state:** `me()` reads `/accounts/{n}/balances` and
  `/accounts/{n}/positions`. Each held QQQ option becomes one synthetic trade,
  so `Book`, the strategy and `maybe_flatten` see the broker's real positions.
- **Orders:** each order is single-leg, Day, and a limit at the ask (buys) or
  the bid (sells), priced from a fresh `/api/live-quotes` quote. A close with
  no executable quote goes out as a market order, because getting flat
  matters more than a few cents. Every order is dry-run first
  (`/orders/dry-run`), and a dry-run rejection stops it.
- **Identity:** `external-identifier` is the crassus `execution_request_id`.
- **Fills:** the route polls up to 20 s for a fill, then cancels. A cancelled
  order is recorded `rejected`, and the next cycle can decide again.

## Real-money differences from the paper executor

| Situation | Paper (Worker) | tastytrade route |
|---|---|---|
| Timeout / 5xx on submit | reconcile, may retry (Worker dedupes ids) | look up by `external-identifier`; **never resubmitted** |
| Crash with intent on disk | replay if the trade is absent | **never replayed**: absent after 2 min means recorded `rejected` |
| Order not filled | n/a (instant fill) | cancelled after 20 s; a failed cancel stays `ambiguous` |
| Sell with no position | allowed (paper short) | **refused**: no naked shorts |

## Risk guard (outside the strategy's control)

Opening orders must pass every check. Closing orders are always allowed,
since they can only reduce exposure.

| Limit | Default | Hard ceiling (code change to raise) |
|---|---|---|
| Contracts per order | 1 | 1 |
| Open contracts held | 1 | 1 |
| Premium per contract | 3.00 ($300) | 5.00 ($500) |
| Daily loss vs start-of-day net liq | $250 | $500 |
| Opens per day | 4 | 6 |

Other checks on opening orders:

- Only `oi_skew_qqq` may open positions, and only on QQQ.
- `eod_flatten` and the strategy may always close.
- The daily loss cap compares net liquidating value with the first value seen
  that ET session. Day state is kept in `state/<alias>.tasty_day.json`, so a
  restart does not reset it. If net liq is unavailable, opening is refused.
- Account params (`params.tastytrade_limits`) can tighten these limits but
  never loosen them.

**Kill switches:**

- `CRASSUS_TASTY_HALT=1`, or a `state/TASTY_HALT` file, blocks new positions
  but still lets the bot close.
- The existing `state/STOP` file stops the runner entirely.

## Activation (account owner only)

Credentials never go in source, commits, issues, logs or screenshots. This
grant is **separate** from the collector's read-only grant in
`site/tastytrade-oauth.md`. That grant and its `read`-only guard are untouched.

1. **Sandbox first.**
   1. Create a sandbox user and OAuth app with the tastytrade sandbox account
      tool, with scopes `read trade`.
   2. On the crassus Railway service, set:
      - `CRASSUS_TASTY_MODE=sandbox`
      - `CRASSUS_TASTY_ACCOUNT_NUMBER`
      - `CRASSUS_TASTY_CLIENT_SECRET`
      - `CRASSUS_TASTY_REFRESH_TOKEN`
   3. Activate the entry through the ignored `accounts.json` override:
      `{"username": "tasty_oiskew", "active": true}`.
   4. Run with `--dry-run` for one session, then without it. Confirm in the
      ledger that orders, fills, cancels and the EOD flatten behave as
      described above.
2. **Verify against the sandbox** the items marked unverified below.
3. **Go live.**
   1. Create a production OAuth app or grant with `read trade` scopes for the
      real account.
   2. Replace the three secrets.
   3. Set `CRASSUS_TASTY_MODE=live` and
      `CRASSUS_TASTY_LIVE_APPROVAL=live:<account number>`.

   The approval must name the same account number, or the route refuses to
   start.

If any variable is missing or wrong, only this bot is disabled, and a
`runner_error` is recorded in the ledger. Every paper bot keeps running.

## Not yet verified against a real tastytrade endpoint

The hermetic suite (`scripts/verify_tastytrade_broker.py`) proves the
control flow against a fake API. These broker details come from tastytrade's
docs and need one sandbox session to confirm:

- **OAuth host:** the token exchange uses `{base}/oauth/token` on the sandbox
  host.
- **`external-identifier`:** it is accepted on order submission, echoed in
  `GET /orders`, and accepts a 36-character UUID.
- **Field names:** order status names (`Live`, `Filled`, `Cancelled`, …),
  plus the field names in balances (`net-liquidating-value`, `cash-balance`)
  and positions (`quantity-direction`, `average-open-price`).
- **Symbols:** the padding rule (root padded to 6 characters, e.g.
  `QQQ   261109C00500000`).
- **Cancel:** `DELETE /accounts/{n}/orders/{id}` cancels an order.

## Evidence caveat

In the 40-day synthetic backtest (2026-08-14), `oi_skew_qqq` never traded,
and it has no live paper P&L history that supports real-money use. This
route makes real trading *possible*. It does not show that the strategy has
an edge. The sandbox step and a paper track record should come before step 3.
