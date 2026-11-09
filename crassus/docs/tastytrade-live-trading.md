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
  `/accounts/{n}/positions`, then keeps only the contracts **this bot's own
  fills** produced (see Ownership). Those become synthetic trades, so `Book`,
  the strategy and `maybe_flatten` see the bot's real positions and nothing
  else.
- **Orders:** each order is single-leg, Day, and a limit at the ask (buys) or
  the bid (sells), priced from a fresh, well-formed `/api/live-quotes` quote
  (a crossed or non-positive quote is not a price). Every order is dry-run
  first (`/orders/dry-run`), and a dry-run rejection stops it. Closing with
  no usable quote follows the owner's `eod_unquoted_exit` policy (below).
- **Identity:** `external-identifier` is the crassus `execution_request_id`.
- **Fills:** the route polls up to 20 s for a fill, then cancels. An order
  the broker reports ended unfilled is recorded `rejected`, and the next
  cycle can decide again. A partial fill is recorded `filled` with a note,
  and ownership reflects exactly the filled quantity.

## Ownership (the account may hold other things)

The brokerage account is not assumed to be dedicated to the bot.
`state/<alias>.tasty_owned.json` records each order the bot sends (before
sending it) and credits only that order's fills, idempotently by
`execution_request_id`. So:

- Manual trades, hedges and other underlyings are reported in every ledger
  record as `external_positions`, but are never in the `Book`. The strategy
  and the EOD flatten cannot close, size against or sell them.
- If the broker holds less than the bot believes it owns (expired, assigned
  or closed by hand), ownership shrinks to match. It never grows from a
  position the bot did not fill.
- Ownership survives restarts. A missing file means "owns nothing". An
  unreadable or malformed file stops this bot's cycle, with the reason in
  the ledger, rather than guessing.
- An order still unresolved is re-checked against the broker's order history
  each cycle for two days, so a fill that becomes visible late is still
  attributed (`late_fills` in the ledger).

## Real-money differences from the paper executor

| Situation | Paper (Worker) | tastytrade route |
|---|---|---|
| Timeout / 5xx on submit | reconcile, may retry (Worker dedupes ids) | look up by `external-identifier` across **every page** of order history; **never resubmitted** |
| Crash before the order was sent | replay if the trade is absent | **never replayed**: recorded `rejected` (the intent shows it was never sent) |
| Crash or timeout after sending | replay if the trade is absent | **never replayed**; stays `ambiguous` until found, or until absent from 3 complete history scans over at least 15 minutes, and is still watched for a late fill after that |
| Unknown or missing order status, or a terminal status with no fill data | n/a | stays `ambiguous`; the intent is kept |
| Order not filled | n/a (instant fill) | cancelled after 20 s; a failed cancel stays `ambiguous`; a fill that wins the cancel race is recorded `filled` |
| Sell with no position | allowed (paper short) | **refused**: no naked shorts, and never against a position the bot did not open |

## Risk guard (outside the strategy's control)

Opening orders must pass every check. Closing orders are always allowed,
since they can only reduce exposure.

The **default** is what the catalog entry runs with. The **hard ceiling** is
the most account params can set; going beyond it is a code change. Owen
chooses the values to run with; these are proposals.

| Limit | Units | Default | Hard ceiling |
|---|---|---|---|
| Contracts per order | contracts | 1 | 1 |
| Open contracts held by the bot | contracts | 1 | 1 |
| Premium per contract | option price | 3.00 ($300) | 5.00 ($500) |
| Daily **entry stop** | USD of account net liq | $250 | $500 |
| Opens per day | orders | 4 | 6 |

**What the entry stop does and does not do.** It compares account net
liquidating value with the first value seen that ET session. Once the account
is down by the stop amount, no new position may open. It does **not** stop
out a position already held, and it does not cap the day's loss at $250.
Because the bot only holds long options, a held position can lose at most its
premium. The bot's own worst case for one session is therefore the entry stop
plus one full position at the premium cap, before fees and before gap or
assignment risk:

| | Entry stop | + one position at the premium cap | = worst-case session loss |
|---|---|---|---|
| Defaults | $250 | $300 | **$550** |
| Hard ceilings | $500 | $500 | **$1,000** |

(`tastytrade_broker.worst_case_daily_loss`.) The stop uses whole-account net
liq, so losses in manual holdings also stop the bot's new entries. That is
deliberately conservative.

Other checks on opening orders:

- Only `oi_skew_qqq` may open positions, and only on QQQ.
- `eod_flatten` and the strategy may always close the bot's own positions.
- Each opening order reserves its slot in the daily count **before** it is
  sent, keyed by request id. A timeout, unknown status or crash keeps the
  slot reserved; only a broker-reported unfilled end releases it. A
  reconciled or recovered fill counts once, including across restarts. Day
  state is kept in `state/<alias>.tasty_day.json`. If net liq is
  unavailable, opening is refused.
- `params.tastytrade_limits` can tighten these limits but never loosen them.
  Unknown keys, non-numbers, fractional counts and non-positive values are
  configuration errors.

## Closing without a usable quote (owner choice)

In the EOD flatten window the bot normally closes with a limit at the bid.
If there is no fresh, well-formed quote, `params.eod_unquoted_exit` decides:

- `"retry"` (catalog default): send nothing and retry every cycle, with the
  reason in the ledger. If no quote ever arrives, **the position is carried
  into expiration**, and a 0DTE in-the-money call could then be exercised.
- `"market"`: inside the final `eod_unquoted_exit_minutes` (default 5), the
  flatten sends a market sell-to-close for the first held contract. Only
  `eod_flatten` may do this; a strategy close without a quote is always
  refused.

Which one to run is a risk decision for the account owner.

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
- **Cancel:** `DELETE /accounts/{n}/orders/{id}` cancels an order, and what
  it returns when the order filled first.
- **Pagination:** `GET /orders` accepts `per-page` / `page-offset` and returns
  `pagination.total-pages`. Without that field, a full page is treated as
  incomplete, and the order stays `ambiguous`.
- **Fill fields:** a cancelled or partially filled order reports
  `legs[].remaining-quantity` or `legs[].fills[].quantity`. Without them, a
  terminal order stays `ambiguous`. This fails closed: the bot stops until
  the order is resolved.
- **Visibility delay:** how soon an accepted order appears in `GET /orders`.

## Evidence

These are source claims awaiting Chej's independent reconstruction from the
full paper ledger. They are not verified net returns or live performance.

**Live paper record.** `crassus_oiskew` on `/api/bots`, 2026-11-09:

- Cash is $10,792 against a $10,000 start, with one open call held at cost
  ($295). That is **+$1,087 (+10.9%) over 149 trades** since 2026-08-12.
- It ranks first of the 31 roster bots. The next best is +$507.
- The per-day FIFO ledger through 2026-09-03 showed −$544 realized, so the
  gain came from September onward.

**Backtest.** In the 40-day synthetic backtest (2026-08-14) the strategy
never fired. That says more about the synthetic days' OI dynamics than about
the strategy.

**Limitations.**

- This is paper P&L. Fills happen at the Worker's quote, which tastytrade
  will not match exactly.
- The sample is one strategy on roughly three months of data.
- Real money also caps the bot at 1 contract with the loss and premium
  limits above.

Running the sandbox step first is still the plan, mainly to confirm the API
details listed above.
