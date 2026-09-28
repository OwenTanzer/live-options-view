# September 28 capture reliability package

## Changes

* Count disconnect and resume records through the normal statistics path in
  every stream lane. This fixes the two-record discrepancy in the September 28
  QQQ and IBIT summaries. Historical archives are not rewritten.
* Record stream failure phase (session creation, connection, or read), bounded
  exception causes, retryability, attempt, silence duration, last receipt and
  last trade provider timestamp. Log scheduled retry delays and actual recovery.
  URLs, tokens and session IDs are redacted from these new error diagnostics.
* Record latest/max trade delivery lag and counts above five seconds in health
  and summaries. Emit at most one lag warning per minute per lane. This is the
  difference between provider trade time and collector receipt time, not quote
  age; corrections, delayed trades, and clock skew can affect it.
* Persist the observed spool high-water mark in health and final summaries. It
  samples queued plus open-segment bytes before each event write, per process
  attempt. It is not a historical cross-restart maximum or an OS disk metric.
* Smooth Banana dispatch at the configured rate (default 100 requests/minute),
  retain a rolling-minute cap, and release the limiter lock while sleeping.
  Retry attempts consume this same budget. Reject configurations above 120
  minus the configured reserve. Provider Allowed headers can lower pacing.
* Track remaining provider budget conservatively across concurrent and
  out-of-order responses. Subtract outstanding requests, never replenish the
  same window from a higher stale observation, and pause at the reserve.
* Honor numeric and HTTP-date Retry-After on 429, provider reset time, and a
  conservative 60-second fallback when Retry-After is missing or invalid.
* Stop new live-sampling request dispatch when the session closes, including
  workers waiting on the limiter. Requests already in flight can finish later;
  existing ranking filters still exclude after-close observations. Backfill
  remains permitted after close.
* Log HTTP status/transport causes, attempt, elapsed time and retry delay.
  Archive per-sweep request-stat deltas and start intervals; log upload time,
  effective rate and lifetime maximum request duration. Worker wait/request
  seconds are sums across workers, not wall-clock sweep durations.

The universe remains 500 chains. At the default 100 requests/minute, 500 chain
requests plus 10 quote requests require roughly 306 seconds of request budget,
before slow responses, errors, shared-token pauses, and uploads. Faster polling
requires a different provider-budget or sampling design.

## Limits and evidence

The September 28 stream outage followed rising delivery lag and approximately
ten seconds of silence. A read timeout is plausible, but the old logs did not
retain enough exception detail to prove the upstream/network cause. This patch
does not claim to eliminate provider disconnections or recover missing trades.
The single shared stream, lease fencing, timeout, and partial-session labeling
remain in place.

Tradier documents a token-wide market-data limit of 120/minute in production
and 60/minute in sandbox, with remaining-budget/reset headers:
https://docs.tradier.com/docs/rate-limiting
Streaming interruptions are expected operational conditions:
https://docs.tradier.com/reference/streaming

This process cannot reserve budget atomically against independent consumers of
the same token. Default pacing leaves headroom and responds conservatively to
headers; heavy concurrent consumers may still produce a 429. A shared limiter
or separate token is a deeper change if the new logs demonstrate that need.

## Rollout and acceptance

Deploy both services from the reviewed revision before a subsequent session.
Keep the existing volume mounts, cron schedules, and single-stream ownership.
Keep OA203_MAX_RPM=100 and OA203_RATE_RESERVE=10; these are the existing defaults,
so no environment changes are required. Roll back by redeploying the prior revision;
the added manifest/summary fields are additive.

After a full session:

1. Reconcile physical per-part records with summary counts, including gap
   markers, for both stream lanes. Verify uploaded hashes as in the audit.
2. Inspect each stream_failure together with retry/resume logs and lag warnings.
   Repeated read timeouts preceded by rising lag warrant provider/network
   investigation; rising spool peaks/upload failures warrant storage review.
3. Check Banana rate_limited, transport_errors, server_errors, worker wait time,
   actual sweep start intervals and upload duration. Investigate repeated 429s
   with all token consumers before increasing its cap.
4. Confirm sampling dispatched no new requests after close. Late in-flight
   responses remain possible and must remain excluded from ranking.

The uploads.json remote-evidence limitation from the audit remains a separate
archive-provenance item; this package does not close that Todoist audit gate.
