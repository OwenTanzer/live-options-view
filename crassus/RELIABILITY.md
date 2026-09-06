# Runner reliability — issue #73

The August 25 incident showed a Playwright Node heap failure followed by no
completed cycles while Railway stayed green. The exact heap-retaining object
from that historical process has **not** been identified: there is no heap dump
or faithful historical replay. Do not close #73 on this change alone.

## Changes and boundaries

`python -m crassus.runner --interval 300` now runs a small Linux supervisor
and exactly one worker. No service-command change is needed. The worker alone
owns the ledger, pending executions and archive thread. `--once` remains a direct
single pass. Direct Python `Runner` users are not automatically supervised.

Only completed account passes refresh the watchdog through a bounded private
pipe. Start events, archive activity and process existence cannot refresh it.
After more than two intervals without completion (600 seconds at the default),
the supervisor emits ERROR `runner_unhealthy`, terminates the worker and exits
75 so Railway's existing ALWAYS policy restarts it. Startup has the same deadline.
This applies around the clock because the current runner also completes cycles
on weekends and outside market hours. A future market-hours-only scheduler must
update this contract before changing that cadence.

Every 30 seconds `runner_health` reports last cycle start/completion, started
sequence, completed count, duration, last fatal exception class when available,
worker resident memory, aggregate descendant resident memory and descendant count.
A liveness success does not certify successful scraping, sufficient balances,
data freshness or successful orders. Before the first completion, timestamp and
count fields explicitly show no completed pass. `runner_unhealthy` is an
alertable log event and forces an actual failed process; this PR does not configure
an external notification destination or HTTP health endpoint.

The supervisor is a Linux child subreaper: a detached Chromium process is adopted
if Python or its Node driver dies. Shutdown sends TERM to the worker group, allows
five seconds for graceful cleanup, then kills remaining descendants and reaps
adopted children. During normal operation it also reaps exited, adopted Chromium
helpers on each supervisor poll; the real browser regression exposed these
zero-memory zombie processes after otherwise successful cleanup. Process identities include kernel start times to reduce PID
reuse risk. A development sandbox exposing `/proc` from another PID namespace
reports memory unavailable and cannot validate detached-child cleanup; the exact
production-image CI job exercises this on a normal Linux process namespace.
Unexpected worker exits, including exit 0, and malformed/closed heartbeat pipes
are unhealthy. Deployment termination is forwarded and bounded as well.

Playwright is used in one production ingestion path: `sentiment.py`'s Reddit
fallback. `trump_sentiment.py` uses requests/XML, with no browser lifecycle.
Previously the Reddit reader retained the browser/context/Node stack across all
cycles. A fresh aggregation now owns that stack, reuses it across its subreddit
pages, and closes context, browser and driver in a finally block before returning.
Cache hits still avoid scraping. Partial launches and interrupted cleanup attempt
all allocated teardown steps. A transient launch failure suppresses repeated
launch attempts within that read, but the next fresh read can recover. The existing
JSON-first behavior, 429 backoff and one within-fetch crash retry remain intact.
This removes indefinite cross-cycle browser retention; it is a defensive lifecycle
bound, not proof of the historical leak's precise cause. Browser launches are now
more frequent when fallback is needed; measure cycle duration after release.

## Evidence and gates

- `python scripts/verify_reliability.py`: hermetic cleanup/cancellation, launch
  recovery, duplicate heartbeat rejection, missing-cycle timeout, malformed pipe,
  fatal/zero worker exit and real detached-process cleanup. Existing integrity,
  runner recovery, strategy and archive suites remain required.
- `python scripts/soak_reliability.py --cycles 30 --output /tmp/reliability-soak.json`:
  use the production Docker image on Linux. This kills a real Playwright Node
  driver during a synchronous scraping session and checks nonzero supervisor
  exit, no surviving descendants, then 30 fresh successful browser aggregations.
  All pages are fulfilled with fixture HTML; CI also disables container networking.
  Each aggregation checks 24 posts, zero remaining descendants after cleanup,
  active descendant memory below 1 GiB, and resting Python memory growth no more
  than 64 MiB between early/late windows after five warm-up reads. Raw measurements
  are uploaded as the `crassus-reliability-soak` CI artifact. This short synthetic
  regression cannot establish behavior under every real Reddit response or replace
  a full-session production soak.

Before release, review the actual PR head and passing CI, including the real
browser job. Do not release over a failed/missing soak. Preserve the already
verified `/data/crassus` volume, pending state and private archive configuration.
No migration, ledger rewrite or automatic restoration is part of this change.
The supervisor never retries trades: restart uses the existing durable pending
intent and original request ID, with ledger append before finalization. If killed
during an unresolved request, its pending envelope must survive for that recovery.

After a separately approved release: confirm the deployed commit, supervisor
startup, advancing completed count, periodic memory samples and continued archive
uploads. Exercise injected failure in an isolated non-trading deployment with a
volume and ALWAYS policy; confirm automatic restart and advancing real runner
cycles. Inspect a full trading session's resource trend and investigate the
historical retaining object if reproducible. Issue #73 remains open until its
remaining root-cause and production acceptance evidence is recorded.
