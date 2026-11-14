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
- Ownership never grows from a position the bot did not fill, and it only
  shrinks on evidence:
  - **expired**: the contract's expiry date has passed;
  - **manual close**: the order history shows a closing fill on that
    symbol, not sent by the bot, after the bot's own fill, covering the gap.
- If the broker shows fewer contracts than the bot owns and neither applies
  (the positions endpoint lagging a fill, a contract briefly missing from a
  snapshot), the bot **keeps** the contract as its exposure and records an
  `ownership_discrepancies` entry in every ledger record. While one is open:
  - new opens are blocked;
  - a close is not sent for more than the broker currently shows, so it can
    never consume a manual contract of the same series;
  - it clears by itself when the position reappears, or resolves as expired
    (a 0DTE contract the next session).
- Ownership survives restarts. A missing file means "owns nothing". An
  unreadable or malformed file stops this bot's cycle, with the reason in
  the ledger, rather than guessing.
- An order still unresolved is re-checked against the broker's order history
  each cycle for two days, so a fill that becomes visible late is still
  attributed (`late_fills` in the ledger).

## State is bound to the broker account

All of the route's state (ownership, the risk day and the in-flight intent)
lives in `state/tastytrade/<alias>.<mode>.<hash>/`, where the hash is a
SHA-256 of the mode and account number (the number itself is not stored).
The directory's `identity.json`, and a tag inside each state file, must match
the configured mode and account, or the bot refuses to trade.

- Moving from sandbox to live, or to another account, starts in a fresh
  directory that owns nothing. The old directory is left untouched for audit.
  Nothing migrates automatically.
- Unscoped state from an earlier build (`state/<alias>.tasty_owned.json` or
  `.tasty_day.json`) stops the bot until an operator moves it into the right
  scoped directory or archives it.
- The kill-switch file stays at `state/TASTY_HALT`.

## Real-money differences from the paper executor

| Situation | Paper (Worker) | tastytrade route |
|---|---|---|
| Timeout / 5xx on submit | reconcile, may retry (Worker dedupes ids) | look up by `external-identifier` across **every page** of order history; **never resubmitted** |
| Crash before the order was sent | replay if the trade is absent | **never replayed**: recorded `rejected` (the intent shows it was never sent) |
| Crash or timeout after sending | replay if the trade is absent | **never replayed**; stays `ambiguous` until found, or until absent from at least 3 complete history scans whose first and last are at least 15 minutes apart. Even then its contracts stay counted as exposure for the rest of the session and it is watched for a late fill |
| Order history that is malformed, empty-bodied or missing pagination | n/a | not evidence of anything: the lookup fails and the order stays `ambiguous` |
| Recovery after a long outage | n/a | the history search starts the day before the in-flight order was created, not just yesterday |
| Unknown or missing order status, or a terminal status with no fill data | n/a | stays `ambiguous`; the intent is kept |
| Order not filled | n/a (instant fill) | cancelled after 20 s; a failed cancel stays `ambiguous`; a fill that wins the cancel race is recorded `filled` |
| Sell with no position | allowed (paper short) | **refused**: no naked shorts, and never against a position the bot did not open |

## Risk guard (outside the strategy's control)

Opening orders must pass every check. Closing orders are always allowed,
since they can only reduce exposure.

The **default** is what the catalog entry runs with. The **hard ceiling** is
the most account params can set. A param may set a limit anywhere above zero
up to its ceiling, including looser than the default; a value above the
ceiling is clamped to it, and going beyond a ceiling is a code change. Owen
chooses the values to run with; these are proposals.

| Limit | Units | Default | Hard ceiling |
|---|---|---|---|
| Contracts per order | contracts | 1 | 1 |
| Open contracts held by the bot | contracts | 1 | 1 |
| Premium per contract | option price | 3.00 ($300) | 5.00 ($500) |
| Daily **entry stop** | USD of loss on the chosen basis | $250 | $500 |
| Opens per day | orders | 4 | 6 |

**What the entry stop measures (`entry_stop_basis`, an owner choice).**

| Basis | Loss measured | Masked by | Bounds the bot's own loss? |
|---|---|---|---|
| `bot` | the bot's own fills this ET session: cost of today's buys plus positions carried in at cost, minus today's sale proceeds | nothing outside the bot | yes |
| `account` | whole-account net liq against the first finite value seen this session | manual gains, deposits; a late first reading misses earlier losses | **no** |
| `both` (default) | either one stops new entries | — | yes |

The bot-attributed loss errs high: a held position is valued at zero, a buy
with no reported fill price counts at the premium cap, and a sale with no
reported price adds nothing.

**What it does and does not do.** Once the loss reaches the stop, no new
position may open. It does **not** stop out a position already held.

With `bot` or `both`, a new open is allowed only while the bot's own loss is
below the stop, and that open adds at most one order at the premium cap. The
bot's own loss for one session is therefore bounded by the entry stop plus
one order at the premium cap:

| | Entry stop | + one order at the premium cap | = bound on the bot's session loss |
|---|---|---|---|
| Defaults | $250 | $300 | **$550** |
| Hard ceilings | $500 | $500 | **$1,000** |

(`tastytrade_broker.worst_case_daily_loss`.) The bound excludes fees and
commissions. It also excludes exposure after expiry, exercise or
assignment: a long option can become a stock position whose loss is not
limited to the premium. A position carried past the close (see the unquoted
exit below) carries that risk separately.

With `account` alone there is **no** bound on the bot's own loss. Four $300
bot losses offset by $300 manual gains leave net liq flat, so every open
passes. Account-level losses also stop the bot under `both`, which is the
conservative default; whether manual losses should halt the bot is Owen's
call.

Other checks on opening orders:

- Only `oi_skew_qqq` may open positions, and only on QQQ.
- `eod_flatten` and the strategy may always close the bot's own positions.
- Each opening order reserves its slot in the daily count **before** it is
  sent, keyed by request id. A timeout, unknown status or crash keeps the
  slot reserved; only a broker-reported unfilled end releases it. A
  reconciled or recovered fill counts once, including across restarts.
- An opening order that is still unresolved also counts its contracts
  against `max_open_contracts` for the rest of its session. That holds even
  after it was recorded "not placed", because a Day order could still fill
  until the close.
- If net liq is unavailable or not a finite number (NaN, infinity), opening is
  refused, and such a value never becomes the session baseline.
- An open `ownership_discrepancies` entry blocks opening (see Ownership).
- `params.tastytrade_limits` sets values up to the ceilings. Unknown keys,
  non-numbers, fractional counts and non-positive values are configuration
  errors.

## Closing without a usable quote (owner choice)

In the EOD flatten window the bot normally closes with a limit at the bid.
"Usable quote" means one test, shared by the flatten and the executor
(`market.executable_price`): fresh, numeric and finite, bid ≥ 0, ask > 0,
not crossed, and a positive price on the closing side. A crossed or zero-bid
quote counts as no quote. If there is no usable quote,
`params.eod_unquoted_exit` decides:

- `"retry"` (catalog default): send nothing and retry every cycle, with the
  reason in the ledger. If no quote ever arrives, **the position is carried
  into expiration**, and a 0DTE in-the-money call could then be exercised.
- `"market"`: inside the final `eod_unquoted_exit_minutes` (default 5), the
  flatten sends a market sell-to-close for the first held contract. Only
  `eod_flatten` may do this; a strategy close without a quote is always
  refused.
  - The executor re-checks the window itself, from the exchange calendar,
    immediately before sending. That covers holidays and early closes such
    as 13:00, and a quote that deteriorates between the flatten's decision
    and the order.
  - Outside the window, after the close, or on a non-trading day, it sends
    nothing and retries.

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
- **Positions lag:** how far `/positions` can trail a fill. Until it catches
  up, the bot holds the contract as a discrepancy and opens nothing.
- **Order timestamps and history window:** whether `terminal-at` /
  `received-at` are present (manual-close evidence needs one), and whether
  `start-date` filters on the order's creation date.
- **Fill prices:** `legs[].fills[].fill-price`. Without it the bot-loss stop
  falls back to the limit price, or to the premium cap for a buy.

## Evidence

These are source claims awaiting Chej's independent reconstruction from the
full paper ledger. They are not verified net returns or live performance.

**Live paper record.** From the read-only export sent for review
(Cloudflare KV record and the runner's decision ledger, pulled 2026-11-10;
file hashes are in the export):

- 150 fills (75 round trips) from 2026-08-12 to 2026-11-09, all closed.
  Cash is $12,085 against a $10,000 start, and replaying every fill
  reproduces it exactly. That is **+$2,085 realized (+20.9%)**, before fees,
  with no deposits, withdrawals or resets.
- The earlier "+$1,087 over 149 trades" counted fills and valued one open
  call at cost. That call closed at the 2026-11-09 flatten for +$998.
- 18 wins and 57 losses. The 4 largest wins total +$4,138; the other 71
  trades total −$2,053. The closed-trade max drawdown is −$1,163.
- By month of close: Aug −$656, Sep +$631, Oct +$1,227, Nov +$883.
- Decision records exist from 2026-08-31 onward; 2026-08-12 to 08-31 was lost
  in a runner restart. The bot was logged out of the paper server for most
  of Nov 4–6.

**Backtest.** In the 40-day synthetic backtest (2026-08-14) the strategy
never fired. That says more about the synthetic days' OI dynamics than about
the strategy.

**Limitations.**

- This is paper P&L. Fills happen at the Worker's quote, which tastytrade
  will not match exactly.
- The sample is one strategy on roughly three months of data, and the result
  depends on a few large winners.
- It was picked as the best of 31 bots, which overstates how promising it
  looks.
- Real money also caps the bot at 1 contract with the loss and premium
  limits above.

Running the sandbox step first is still the plan, mainly to confirm the API
details listed above.
