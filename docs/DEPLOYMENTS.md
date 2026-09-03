# Deployment Contract

This document defines how services associated with `OwenTanzer/live-options-view`
are released, started, verified, and recovered. It is a living operational
contract, not an incident report.

Last reconciled against GitHub and the Railway `live-market-monitor` production
environment: **2026-09-03**.

## Sources of truth

Deployment truth is split across three layers:

1. This document defines the intended policy and invariants.
2. Versioned files such as `railway.toml`, `Dockerfile`, workflows, and service
   code define repository-controlled behavior.
3. Railway contains the active source branch, watch paths, check-suite gate,
   schedule, restart policy, scaling, networking, and environment variables.

The active Railway configuration determines what actually runs. A mismatch with
this document or the repository is configuration drift and must be reconciled;
it is not a reason to silently redefine the intended policy.

Never store credential values in this file, commits, pull-request discussion, or
logs. Variable names are safe to document. Values belong in Railway.

## Service registry

| Service | Source | Mode | Start command | Restart policy | Intended deployment behavior |
|---|---|---|---|---|---|
| `live-options-view` | Repository `master`, repository root | Continuous, session-aware collector | `python collector.py` | `NEVER` | Deploy after relevant collector changes pass CI |
| `crassus-runner` | Repository `master`, root directory `crassus` | Continuous five-minute bot runner | `python -m crassus.runner --interval 300` | `ALWAYS` | Deploy after changes under `crassus/` pass CI |
| `moo144-tradier-probe` | Temporary MOO-144 worker | Scheduled one-shot capture | `python -u scripts/moo144_tradier_probe.py` | `NEVER` | Build from reviewed code; execute only in its explicit run window |
| `r2-paper-trades-recovery` | Railway Bun function image | Temporary recovery utility | Railway-managed embedded command | Service-specific | Manual incident utility; do not treat as a normal repository deployment |

Temporary services must have an owner, an issue, an exit condition, and a
decision to remove or formalize them. If `r2-paper-trades-recovery` is retained,
its embedded program must be moved into version control before it becomes part
of normal operations.

## Current drift requiring correction

As of the reconciliation date, `live-options-view` and `crassus-runner` have
narrow, service-specific watch paths. Railway still reports `checkSuites`
disabled for both services: two accepted and committed attempts to enable
"Wait for CI" were silently discarded by Railway. The MOO-144 probe also points
to the merged feature branch rather than `master` until its scheduled capture
is complete.

Before the watch paths were corrected, the documentation-independent MOO-144
merge rebuilt both continuous services even though neither runtime changed.
Both deployments succeeded, but that coupling was wasteful and increased the
blast radius of every merge.

The remaining target state is:

- Railway persists "Wait for CI" for both continuous repository-backed
  production services.
- The MOO-144 worker uses reviewed code from `master` if it remains active
  after its scheduled capture.
- A documentation-only or unrelated-service change deploys no runtime service.

## Automatic deployment policy

Automatic deployment means automatic promotion of relevant, reviewed code. It
must not mean indiscriminate execution of every service after every merge.

### Required watch scopes

| Service | Files that may trigger deployment |
|---|---|
| `live-options-view` | `/collector.py`, `/market_signals.py`, `/crude_calibration.py`, `/requirements.txt`, `/Dockerfile`, `/railway.toml` |
| `crassus-runner` | `/crassus/**` |
| `moo144-tradier-probe` | `scripts/moo144_tradier_probe.py`, `requirements.txt`, `Dockerfile` |

Tests, documentation, analysis, and unrelated workflows must not redeploy a
production runtime. A change to shared dependencies may legitimately deploy
more than one service and should say so in the pull request.

### Check gate

For every repository-backed service:

1. The relevant GitHub workflow must run on the proposed change.
2. The workflow must pass for the exact commit being deployed.
3. Railway must wait for successful check suites before deploying.
4. A manual override requires an explicit incident reason and immediate
   post-deployment verification.

Branch protection and Railway's check-suite setting are enforcement mechanisms,
not substitutes for this rule.

### Continuous versus scheduled execution

Continuous services may start when a successful deployment becomes active.

Scheduled and one-shot services require a separation between code promotion and
execution. A deployment created outside the intended run window must either
cleanly no-op or be held until the controlled pre-run window. Inside the actual
scheduled window, invalid date, closed market, missing credentials, missing
storage, or failed preflight must still fail hard and visibly.

The current MOO-144 probe deliberately fails closed when the market is closed.
That is correct capture safety, but it also means arbitrary automatic deploys
produce false-red deployments until off-schedule startup is made a clean no-op
or deployment is restricted to the pre-run window.

## `live-options-view` collector

### Runtime contract

The collector authenticates with tastytrade OAuth, connects to DXLink, maintains
market state, and writes public and archival artifacts to Cloudflare R2.

Its session lifecycle is Eastern Time:

- Before 06:00, wait for that day's premarket start.
- From 06:00 through 16:14, start or resume the current session.
- At or after 16:15, wait for the next eligible session rather than exiting into
  a restart loop.
- A mid-session deployment creates a real observation gap. The first subsequent
  snapshot is resumed current state, not reconstructed continuity.

The core credential and storage contract currently includes:

- `TASTY_OAUTH_CLIENT_SECRET`
- `TASTY_OAUTH_REFRESH_TOKEN`
- `TASTY_OAUTH_SCOPES`
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `LIVE_QUOTE_KEY`
- optional `EIA_API_KEY`

Legacy tastytrade login variables may remain in Railway during migration, but
their presence does not prove that the active OAuth path is healthy.

### Health invariants

A fresh file timestamp alone does not prove that the market feed is live. The
operator must be able to distinguish:

- process liveness;
- DXLink connection, authorization, and channel state;
- freshness of the last feed event;
- successful R2 uploads;
- expected snapshot cadence;
- clean start, recovery after crash, and recovery after a longer gap;
- browser freshness and calibration failures.

`intraday/health.json` is the primary lifecycle artifact. Its `run_id`, startup
classification, feed timestamps, reconnect state, upload counters, cadence, and
symbol coverage must advance consistently with `intraday/prices.json` and
`intraday/latest.json`.

Archive object keys must remain unique across rapid snapshots and redeploys.
Never restore minute-granular keys that allow a recovery run to overwrite the
record preceding it.

### Verification after deployment

1. Confirm the Railway deployment reports success and the expected commit.
2. Confirm a new `run_id` appears in `intraday/health.json`.
3. Confirm DXLink is connected and authorized and feed events are fresh.
4. Confirm upload-success counters advance without a growing failure count.
5. Confirm `prices.json` advances and is not marked stale.
6. Confirm the next expected `latest.json` and uniquely keyed archive snapshot
   appear in R2.
7. Confirm the browser reads the same fresh artifacts and displays calibration
   failures rather than silently inventing valid heatmap thresholds.

## `crassus-runner`

`crassus-runner` is independently deployable even though it shares the
repository. Its root directory and Dockerfile contain its runtime, including the
browser dependencies needed by browser-backed strategies.

Its configuration contract includes `BOT_REGISTRATION_KEY`,
`CRASSUS_ACCOUNTS_FILE`, and the required per-bot `CRASSUS_PW_*` variables.
Document variable names only.

After deployment:

1. Confirm the exact commit and successful Railway status.
2. Confirm the runner remains alive under the `ALWAYS` restart policy.
3. Confirm normal five-minute cycles continue.
4. Confirm bots register and no credential, browser-launch, or repeated restart
   error appears.

Changes outside `crassus/**` must not redeploy this service unless a deliberately
shared dependency is introduced and documented.

## MOO-144 Tradier probe

The probe is a read-only capability experiment, not a production collector. It
selects a narrow near-the-money QQQ 0DTE universe, captures Tradier quote and
time-and-sale events, and writes verified gzip NDJSON segments, a summary, and a
manifest under an isolated R2 prefix.

Required variables are:

- `TRADIER_TOKEN`
- `MOO144_RUN_DATE`
- `MOO144_DURATION_SECONDS`
- `MOO144_STRIKE_COUNT`
- optional bounded reconnect and checkpoint settings
- the four R2 variables listed above

The worker must enforce:

- explicit run-date equality;
- an open Tradier market clock;
- sufficient time before market close;
- a valid QQQ 0DTE expiration;
- read-only market endpoints;
- verified R2 preflight before capture;
- an atomic per-date claim preventing duplicate launches;
- bounded reconnect behavior;
- nonzero time-and-sale evidence before reporting success.

A manually triggered off-hours deployment that fails the market-open guard is
evidence that the guard worked, not evidence of a successful capture. Completion
requires `probe_complete`, a verified manifest, event parts, summary statistics,
and nonzero time-and-sale records in R2.

The date-specific cron expression and `MOO144_RUN_DATE` belong in Railway and
the MOO-144 run record. They must not be copied here as permanent configuration.

## Recovery and rollback

For a continuous service:

1. Identify the last known-good commit and the first failing deployment.
2. Prefer a reviewed revert or Railway rollback to an unreviewed forward patch.
3. Verify the restored commit and repeat the service-specific health checks.
4. Record any observation gap. A successful restart does not retroactively make
   the missing interval continuous.

For a one-shot service, first inspect its durable claim and output prefix. Never
roll back or redeploy blindly: a second launch may duplicate or corrupt the
experiment, and the atomic claim may correctly refuse it.

Do not force-push deployment branches or delete evidence during incident
response. Preserve logs, run identifiers, manifests, and the exact commit SHA.

## Incident-derived invariants

These rules were learned from actual failures and must survive refactoring:

- A running container is not proof of a live market-data feed.
- Fresh uploads can contain stale in-memory state; event freshness must be
  measured independently.
- A new process identity during a session marks a recovery boundary and a real
  data gap.
- Normal post-session behavior must not create restart loops or credential
  hammering.
- Automatic redeployment must be narrow enough that an unrelated merge cannot
  restart independent services.
- One-shot deployment and one-shot execution are separate events.
- Raw evidence and manifests must be immutable and independently verifiable.

## Maintenance rule

Any pull request that adds, removes, renames, or materially changes a deployed
service must update this document in the same change. Any Railway UI mutation
that changes deployment semantics must be followed by a reconciliation commit.

Historical incidents belong in Git, Linear, or a dated incident record rather
than being appended indefinitely to this contract.

The superseded June 23, 2026 pre-deployment audit remains available in Git
history at commit [`57c9d2d`](https://github.com/OwenTanzer/live-options-view/blob/57c9d2df4e7f1832ed54539268ecffb08e6d36ef/docs/PRE_DEPLOYMENT_REVIEW.md).
