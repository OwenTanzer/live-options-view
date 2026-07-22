# Crassus / Golden Goose

A thin Python bot runtime and client library for automated strategies against
the [Options View](https://options.moopertonic.net) paper-trading layer.
Implements `crassus_golden_goose_guidance.v1` (Linear **MOO-24**).

Both names refer to the same system and are interchangeable.

> Build the smallest organism that can repeatedly touch reality and leave an
> intelligible fossil record.

The loop is: **observe → decide → validate → submit → reconcile → record → repeat.**

Fake-capital loss, absurd positions and account liquidation are all permitted
experimental outcomes. The one unacceptable failure is losing the causal
record of what the bot saw, decided, and did.

---

## Status

| Phase | | |
|---|---|---|
| **P0** deployment smoke test | 🔴 **Blocked** | Account endpoints are absent from the deployed Worker — see below |
| **P1** single-account closed loop | 🟢 Implemented, verified against a mock | 66/66 invariant checks pass |
| **P2** multi-account supervisor | 🟡 Runtime supports it; unexercised against a real server | |
| **P3** strategy ecology | ⚪ Not started | Only the smoke strategy exists |
| **P4** evaluation & hardening | ⚪ Not started | |

### P0 blocker — the deployed Worker predates the auth commit

The code is on `master`; it does not appear to be what is serving
`options.moopertonic.net`.

| Probe | Deployed | `master` (`2d8476d`) |
|---|---|---|
| `POST /api/register`, `/login`, `/logout`, `GET /api/me`, `POST /api/settle` | **404** (falls through to `env.ASSETS`) | routes exist |
| `GET /api/live-quotes` | 200, and 400 on missing `symbols` | matches |
| `POST /api/paper-trade`, no session | **403** `Forbidden` (`text/plain`, 9 bytes) | **401** — `requireSession` runs *before* `checkOrigin` |
| `POST /api/paper-trade`, allowlisted `Origin` | **403** | passes — `checkOrigin` only rejects a *present, non-allowlisted* origin |

The deployed `paper-trade` rejects an allowlisted origin and never reaches a
session check, so it cannot be running `master`'s handler. Together with the
404s this points at a build from before `9e6eac5` ("Add login-based paper
trading balances", merged as PR #15 / `2d8476d`).

**The fix looks like a redeploy of `master`, plus confirming the `USERS` and
`SESSIONS` KV namespaces are bound** — not a code change. P0's exit criterion
(one one-contract fill visible in `/api/me`) is unreachable until then.

The same P0 script passes 7/7 against `scripts/mock_worker.py`, which
implements the documented contract — so the failure is the deployment, not the
test.

Note this also means **`POST /api/paper-trade` currently rejects every client**,
browser included — worth checking whether paper trading works in the UI today.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# P0 -- test the real deployment
.venv/bin/python scripts/p0_smoke.py

# Prove the integrity invariants (spins up the mock Worker itself)
.venv/bin/python scripts/verify_invariants.py

# Run the loop
cp accounts.example.json accounts.json      # gitignored; then set passwords
.venv/bin/python -m crassus.runner --once --dry-run
.venv/bin/python -m crassus.runner --interval 300
```

Kill switch: `Ctrl-C`, `SIGTERM`, or `touch state/STOP`.

---

## Layout

| | |
|---|---|
| `crassus/clock.py` | ET-aware time and market phase |
| `crassus/config.py` | Accounts and credentials (never from source control) |
| `crassus/audit.py` | The decision ledger — append-only JSONL |
| `crassus/market.py` | R2 snapshot reader + on-demand live quotes |
| `crassus/client.py` | Sessions, execution, reconciliation — **owns every integrity invariant** |
| `crassus/strategy.py` | The strategy contract |
| `crassus/strategies/` | Strategy implementations |
| `crassus/runner.py` | The loop |
| `scripts/p0_smoke.py` | P0 deployment smoke test |
| `scripts/mock_worker.py` | Local stand-in for the Worker, with fault injection |
| `scripts/verify_invariants.py` | 66 checks across 15 scenarios, run hermetically against a local mock (`crassus/scripts/fixtures/snapshot.json` stands in for the R2 snapshot; no network access to production is required) |

## Integrity invariants

Enforced in `client.py`, each verified by a scenario in `verify_invariants.py`:

1. **`execution_request_id` is written to disk and fsynced before the request is sent.** A timeout must not erase the identity needed to determine whether the trade happened.
2. **One outstanding mutation per account.** The Worker's KV writes are best-effort, not compare-and-swap.
3. **After a timeout or 5xx, `/api/me` is queried before any retry.** The trade may already be there.
4. **`balance_cash` is cash movement, never P&L.** Open exposure is absent from it entirely.
5. **`no_trade` and failures are logged, not just fills.** A ledger of only executions is survivorship-filtered.
6. **429 and `Retry-After` are honored globally.** The binding limit is per-IP, so all accounts share one budget.
7. **HTTP 410 is terminal and recorded as a margin call.** The Worker deletes the user outright. ⚠️ This invariant is verified against the *documented* contract (the mock), not the real deployed Worker's actual settlement behavior — see "Known gaps" below.

## Server constraints worth knowing

- **No `account_id`** — an account *is* a login. Six bots = six registrations, six cookie jars.
- **The `reason` field is silently dropped.** Strategy reasoning lives only in the local ledger.
- **No close endpoint, no positions endpoint, no server P&L.** Closing is an opposite-side trade; positions are derived average-cost from the `/api/me` trade list.
- **Execution quotes must be ≤15s old.** Outside market hours the collector's quotes age out, so trades will 409 — the runner declines rather than spending rate-limit budget on a certain rejection.
- **Insolvent settlement deletes the account.** Not a negative balance — permanent erasure.
- Bot accounts **do not pre-exist**; the runner registers them on first use.

## Account ↔ strategy mapping (target, once P3 lands)

| Account | Strategy | |
|---|---|---|
| Ankit | `aggressive_calls_reload` | P3 |
| Bob | `patient_calls_dip` | P3 |
| Doktor Freuding | `aggressive_puts_reload` | P3 |
| Luigi | `patient_puts_dip` | P3 |
| Jesus | `cheap_atm_calls` | P3 |
| Doris | `cheap_atm_puts` | P3 |

Only `smoke_atm_roundtrip` is implemented today, so `accounts.example.json`
runs every account on it -- copying the file as-is is meant to work, not
just illustrate the eventual mapping. Swap in a P3 strategy_id for an
account only once that strategy is actually registered; the runner
validates every configured `strategy_id` against the registry at startup
and refuses to start (rather than crashing mid-run on the first
unregistered one it happens to reach) if one doesn't exist yet.

Strategy-level rules — max 3 positions, 2:50pm flatten, the 4-of-5 green-day
rule, daily loss limits — are **configuration, not platform invariants**, and
are deliberately not baked into the runtime.

## Known gaps

- Trade-record field names in `Book._normalize` and `ExecutionClient._trade_matches` are **unverified** — no real `/api/me` payload has ever been observable. Both normalize defensively across plausible spellings; tighten them once P0 passes.
- `clock.session_phase` does not know about exchange holidays or early closes.
- Liquidation auto-reprovisioning (register a replacement account after a 410) is **detected and recorded but not yet implemented** — it lands with P2.
- **HTTP 410 liquidation detection is unverified against the real Worker, and that's a real gap, not a hedge.** `scripts/mock_worker.py` fabricates a 410 from `/api/me` and `/api/paper-trade` on demand, matching the documented contract. But `handleSettle` in `worker.js` deletes both the user and session record outright on insolvency (see `worker.js`, around the `insolvent` branch); the *next* request against that same session resolves through `requireSession()` returning `null`, and `/api/me` / `/api/paper-trade` report a generic 401 there, never a 410. The client's `AccountLiquidated` detector is exercised only against the mock's contract-accurate behavior -- it has never seen the real Worker do this, because the real Worker doesn't yet preserve enough state to do it. Deferred until either (a) `worker.js` writes a liquidation tombstone instead of deleting the record, so a resolved session can report 410 explicitly, or (b) the client additionally treats a 401 on a session it previously authenticated as ambiguous (worth reconciling) rather than assuming it's a benign logout. Do not read the passing `scenario_liquidation` check as evidence this works in production.
