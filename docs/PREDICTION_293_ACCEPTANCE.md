# Prediction markets #293 — engineering return

Prepared November 21, 2026 in the isolated `steve/prediction-markets-293`
checkout based on `019cc0704a7f4019d770dc0c4ae83eff7d5bff32`.

Implemented: versioned binary rules and Kalshi fixed-point book decoding;
private, digest-addressed batches with manifest replay, duplicate detection,
gap/resnapshot and archive backpressure; timestamped One Clock observations
without trades; equivalence-gated call-spread intervals; authenticated market,
archive and portfolio reads; existing reserved/idempotent D1 paper execution
with long YES/NO, size/freshness checks, fees and exits; official 24/7 settlement,
void refunds and immutable corrections; searchable mobile table, rules, paper
ticket and reload-safe pending-request retry. Existing portfolio links to event
marks/history and shows held event positions. No production binding is added.

## Verified locally

- `node tests/prediction.test.mjs`: 12 tests, including buy/resolve, buy/exit,
  NO, partial exit/loss, fees, void, correction, duplicate request, concurrent
  closes, insufficient funds/size, stale/missing/crossed/gapped/future books,
  delayed/unofficial resolution, lost finalization recovery, weekend human
  settlement, entry rollback with exits, anonymous route denial, fixture
  isolation, equivalent interval/replay, look-ahead and basis/expiry mismatch,
  archive outage/retry/restart, rollover and retained rule versions.
- `node tests/prediction_ui.test.mjs`: DOM workflow passed; safe text rendering,
  table search, rule details, ticket sends no client price, network-loss retry
  after reload reuses the same request ID. This is not rendered-browser QA.
- Passed: `worker_logic.test.js`, `paper_trading.test.js`,
  `multi_underlying.test.js`, `storage_d1.test.mjs`, `account_closure.test.js`,
  `settlement_cron_outcome.test.mjs`, `api_cron_schedule.test.mjs`,
  `no_runtime_kv.test.mjs`, `capture_write_boundary.test.js`,
  `r2_proxy_bounded.test.js`, `preview_isolation.test.mjs`,
  `preview_storage_boundary.test.mjs`. `git diff --check` passed.

## Checks still required on the review revision

`python tests/test_changed_paths.py`: 76 passed; two temporary-directory tests
failed with Windows PermissionError, including when TEMP was inside the writable
checkout. `preview_transport` and `deploy_sequencing` cannot spawn Git (EPERM).
Asset packaging/rollback suites require an output outside the source worktree;
the writable temporary location inside this checkout is rejected by that guard.
Do not weaken these gates. Run them in normal CI. Rendered preview/browser QA is
unverified: Playwright is not installed locally. The DOM fixture does not
establish phone layout or browser integration.

The local Git worktree points to metadata outside its writable sandbox. Fetch
and staging fail with Permission denied; CLI GitHub authentication/network also
fails. Connected GitHub can read the exact base, but a `master` lookup returned
an older historical revision, so current remote-master freshness is uncertain.
Remote tree creation was denied: MCP tool call requires approval, but approval
policy is never. Local delivery uses separate Git metadata under the writable
checkout and a transfer bundle; the original shared Git metadata remains
untouched. Reconcile master and the simultaneous Bitcoin option instrument change during
review; this worktree does not read or edit that other worktree. Preserve both
discriminators and rerun their respective suites before integration.

## Production acceptance is separate and pending

No merge, deployment, operational restart, production observation, venue order,
credentialed feed or KV operation was performed. The independent collector core
and fixture launcher are implemented; a licensed production transport/R2 host
must still be supplied and certified. No current standard Kalshi agreement
rights are assumed. The first supported fee convention is fixed per contract;
actual venue fee conventions require a matching implementation before activation.
Configure bucket privacy, lifecycle retention and single-writer ownership, then
record a permitted actual event, sized timestamped quote, simulated fill, reload
and official resolution. No minimum sample-size/performance gate applies.

At scale, replace the complete D1 account sweep with a bounded due queue and
rotate/archive generations before retained manifests exceed the license's
retention window. Unknown rule revisions leave settlement pending, rather than
silently substituting terms; reconcile the official revision explicitly.

Next owner: dispatcher/reviewer. Obtain final-revision CI and rendered desktop/
phone fixture checks, reconcile the current master/Bitcoin branch, then review
the producer/API/site lanes independently. Keep all production entry/feed gates
closed pending rights and licensed acceptance.

—Steve 🐦
