# OA-133: Reddit safe-failure repair and acceptance evidence

Issue: https://linear.app/objecta/issue/OA-133/repair-reddit-sentiment-ingestion-and-bound-fallback-latency

## Scope and baseline — October 4, 2026

Fresh, clean clone of `OwenTanzer/live-options-view`, default branch `master`,
base `fbbc3456da8025b6b1021f3d7282b2d6cae570e3`. Dedicated branch
`fix/oa-133-reddit-failure-window`. No AGENTS.md, CLAUDE.md or `.agents/skills`
files are present in this checkout. Read DESIGN.md, crassus/README.md, the
Crassus CI workflow, ingestion/strategy/runner/supervisor and related fixtures.
OA-133 description and its sole August 13 routing comment were read; related
Reddit PRs/commits were inspected before editing. PR #72 (optional local-model
scoring) is closed without merge and is not resurrected here. Existing resource-lifetime
repair `c97786c` is retained, not rebuilt. No deployment, runner start/restart,
account action, order, credential provisioning or source probe was performed.
OA-74 strategy validation / Žižek work remains out of scope.

## Reproduction before production-code edits

The existing suites passed: ingestion **73 checks**, strategy **47 checks**.
New hermetic assertions failed on the unmodified base:

* Six successive account reads of one failing reader: **6 acquisitions,
  120 simulated seconds**, expected one acquisition / 20 seconds.
* HTTP 403 reached the browser factory instead of terminal `source_denied`.
* A changed listing child (`{"data":{"children":[{}]}}`) escaped as a raw
  `KeyError('data')`, rather than an explicit parsing failure.

Root cause: the strategy already has a module-level shared reader; its cache
was populated only after successful aggregation. Exceptions therefore caused
per-account re-acquisition. There was no concurrent single-flight guard.
Navigation and selector waits each had their own 20-second allowance; launch,
DOM reads, scrolls and teardown were not covered by a whole-operation deadline.
The declared eight-scroll limit was not actually used by the loop. Prior crash
recovery allowed another launch within the same failed acquisition.

## Independently dated deployment observation

Read-only Railway SSH at **2026-10-04 22:20:13 UTC (15:20:13 PDT)**:

| Identity | Observed value |
|---|---|
| Service / environment | `crassus-runner` / `production` |
| Deployment | `5398353d-bf1f-4735-9c03-8acdfc65be78` |
| Advertised source commit | `2412771e3154984ec3d7ee4f926d3907a3fdc395` |
| Working directory | `/app` |
| REDDIT_USER_AGENT | Absent (presence only; no secret values read out) |

The deployed `crassus/sentiment.py`, `crassus/runner.py` and tracked account
catalog match these files on current master byte for byte:

| File | SHA-256 |
|---|---|
| sentiment.py | `b18b92a2140d28cd32a2fb549af7013c1b222f8cfc009c3f943069b061ed9ff4` |
| runner.py | `698628625e737da16fcbfe7842a88c8e35b79fd5f88ce33aad88ed1a98d58d6e` |
| accounts.example.json | `68213c0b51580c111dce8a7a45e7fbd37f97ba6d4ecf4c38aed2db7a4b5a0d57` |

Bounded existing-log inspection at **22:22:54 UTC** read bytes
`[96001072, 129555504)` (32 MiB) of
`/data/crassus/logs/decisions-93c554fca847.jsonl`, discarding the first partial
line. It yielded 21,366 records spanning **October 2 01:52:10 UTC through
October 4 22:20:27 UTC**. Of 7,368 Reddit-family records (including closed-market
abstentions), **294** explicitly reported Reddit unavailable, all containing
HTTP 403 and the 20,000 ms browser timeout. This is a bounded operational
sample, not an availability-rate estimate. The last nine unavailable records
were October 2 16:27:29–16:30:44 UTC, across Luigi, Jesus, Doris and their Phelps
and fixed-window twins. Adjacent base-account records were about 21 seconds
apart; ledger `latency_ms` is null here and is **not** acquisition duration.

Six recent Railway operational log lines additionally included a weekend
cycle beginning October 4 22:20:03.985 UTC and completing at 22:20:27.081 UTC:
23.096 seconds, 26 processed / four skipped, zero browser descendants in nearby
health records. Weekend progress is not Reddit health: the strategy skips
retrieval outside the open session. No trading observation window was run.

These observations are separate from the frozen September 8–30 cohort and its
[performance audit](https://app.notion.com/p/3eb3c519147b8171b23acb96a052e935)
and [source evidence](https://app.notion.com/p/3eb3c519147b81e5b2b2ebb13cd1e14d).
Neither frozen page nor raw historical records were modified.

## Behavioral change

* Serialize retrieval on the existing reader. Cache failure text (not an
  exception traceback) for the existing 300-second interval, extended for a
  longer Reddit cooldown. Automatic retry is permitted after expiry; the next
  scheduled caller performs it. `force` cannot circumvent a failed window.
* Discard expired success before refreshing. No stale-success fallback. HTTP
  freshness evidence (`Age` / `Date`) older than the retrieval window is rejected.
* Treat 401/403 denial, 429 backoff, malformed/challenge payloads and stale
  responses as terminal. No browser fallback for those outcomes. Remove the
  prior webdriver masking / impersonated browser identity. Ordinary fallback
  remains only for transport/server failures, with no failed-read relaunch.
* Move only source acquisition into a disposable read-only process, using the
  existing Linux supervisor's process-identity helpers. The child receives only
  source parameters and a narrow environment allowlist (including existing
  proxy/certificate configuration), not account/broker/
  archive credentials. Scoring stays in the original process and is unchanged.
* Use **20 seconds total** for HTTP, browser launch, navigation, extraction and
  graceful cleanup, then a **one-second bounded child-reap wait**, plus normal
  local process/scheduling overhead. This reuses the former single navigation
  allowance rather than introducing a new strategy timer. Against the existing
  300-second cadence, the nominal failure budget is 7% including reap, paid
  once per shared window instead of per affected account. Explicit HTTP denial
  uses **zero** browser attempts. Ordinary failed fallback gets one attempt.
* Kill only the acquisition's owned group and identity-checked descendants on
  timeout/cancellation. Existing runner supervision reaps adopted grandchildren;
  it is neither restarted nor modified. Linux process-tree visibility is
  required; unsupported execution environments fail safely before source work.
* Validate listing children, title/body types and pagination cursors; enforce
  the existing scroll cap. Diagnostics separate denial, parsing failure,
  HTTP/browser/cleanup timeout, cancellation and stale response evidence.
  Existing per-account sample-count reasons still identify insufficient samples.

No strategy file, account/model configuration, VADER aggregation, Phelps timing,
entry/exit rule, threshold, account selection, runner scheduling or unrelated
source was changed. Flat unavailable accounts retain `no_trade`; held positions
retain the existing close-on-loss-of-support behavior and wrapper gates.
Different reader configurations do not share caches. This is process-local
sharing, not coordination across independent runner processes.

## Verification

Run from `crassus/`:

```sh
python scripts/verify_reddit_ingestion.py
python scripts/verify_reddit_sentiment.py
python scripts/verify_reddit_failure_window.py
python scripts/verify_reliability.py
```

The new suite covers sequential/concurrent sharing, expiry/recovery,
failed-refresh stale-cache rejection, denial, malformed children and headers,
old HTTP timestamps, empty/insufficient samples, distinct reader parameters,
per-account thresholds, unchanged held-position exits, cancellation and an
actual Runner.run_cycle fixture with nine Reddit callers plus an unrelated
account. The runner fixture measures one 20-second simulated cost and reaches
the unrelated account. Real subprocess HTTP/browser/cleanup hangs terminate
in approximately **0.153 seconds** with a 0.15-second test budget.

All 20 script suites in `.github/workflows/crassus-ci.yml` are applicable.
The initial aggregate pass found one old reliability assertion expecting
`force=True` to retry a failed launch immediately; it was updated to test
recovery at the explicit failure-window expiry, and rerun successfully.
There were no initial failures in the two baseline focused suites. Linux
process-tree tests skip locally because the container exposes a different PID
namespace; this is an environment limitation, not a passed cleanup assertion.
Docker is not installed locally. CI must execute the existing image build and
soak plus the new real-Chromium acquisition test with `--network none`.
That test checks ordinary DOM success, a cleanup hang at the actual 20-second
budget, and zero residual descendants. No real Reddit request is involved.
There is no additional configured Crassus lint/type-check command; compile and
`git diff --check` supplement the documented suites. Final commit CI status
must be inspected separately from these local results.

The first PR CI run passed all invariant scripts (including both previously
skipped process-tree checks), Web CI, Docker build and the 30-cycle Chromium
soak. Its new acquisition fixture initially failed in the success case: an
empty synthetic `shreddit-post` had no visible dimensions, so Playwright's
visibility wait reached the 20-second deadline. The fixture now includes a
visible body, matching the existing DOM contract; the production selector and
timeout are unchanged. The corrected fixture must pass on the final commit.

## Remaining decisions and acceptance gates

**Code/CI acceptance:** all applicable suites and Docker checks must pass on
the exact reviewed commit. The process-tree test must actually run on Linux;
a local skip is not enough. Review the narrower 20-second whole-acquisition
budget and removal of access-denial fallback before approving a deployment.
This draft neither closes OA-133 nor requests an automatic deployment.

**Source access:** no parser correction can restore an upstream 403. The exact
pending decision is which Reddit-permitted access mechanism is approved for
this deployment, and whether it requires credentials/terms or a new provider.
No credentials, provider substitution, persistent grant or network change is
implemented. A configured honest User-Agent is not evidence of authorization
or a fix for denial.

**Source freshness:** the existing strategy defines a retrieval-cache interval
but no maximum age for individual Reddit posts (and the browser parser drops
post timestamps). Defining that cutoff/minimum *fresh* sample set would change
which texts enter VADER. The owner must specify that policy before it can be
implemented and before sustained healthy-feed acceptance. This patch detects
stale HTTP response evidence and cache expiry, but does **not** claim to detect
old posts delivered inside a fresh response. Existing insufficient-sample
handling is preserved; its sample count cannot yet be called a fresh-post count.

**Deployment verification after separate approval:**

1. Verify deployed commit/file hashes and reviewed configuration identity;
   retain account catalog, model/scorer, thresholds, cadence and wrapper settings.
2. Verify process-tree visibility and successful network-disabled image fixture
   checks; confirm no browser resource remains after acquisition failure.
3. In a separate read-only, source-only diagnostic with **no runner/executor**,
   observe one normal acquisition and repeated concurrent/sequential consumers
   in its cache window. Record request/fallback counts, redacted status, source
   timestamps, sample count, total latency and cleanup. Do not respond to denial
   by bypassing it or changing credentials/access.
4. Observe another acquisition after expiry (earliest just over 300 seconds),
   verifying updated source timestamps. Recovery needs an observed failure then
   success after expiry; if no natural transition occurs, hermetic recovery
   coverage remains distinct from live recovery evidence.
5. Only after the source-access and post-age decisions above, assess enough
   fresh samples under the unchanged account minimums. A small sample remains
   explicit `no_trade`. Two acquisitions across an expiry boundary are the
   smallest useful post-deployment smoke observation, **not sustained health**.
6. If an independently authorized existing runner is operating, inspect already
   produced cycle-completion logs for bounded Reddit latency, unaffected other
   strategies and no restart; do not start one merely to gather evidence.

Sustained healthy-feed observation and OA-74 performance validation remain
separate. Healthy ingestion improves measurement conditions; it does not show a
Phelps performance edge or authorize real trading.
