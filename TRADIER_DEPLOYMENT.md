# OA-169 Tradier worker deployment

Deployed 2026-09-18 after market close. Operational acceptance remains open.

- Railway project: live-market-monitor (`c27d4273-6b16-4921-bd61-0c4f27a8c8ae`).
- Production environment: `9008da9e-6888-4a58-a33b-e3eef5cc01f5`.
- Dedicated service: moo169-tradier-collector (`dc765a1b-0532-4bf3-88b9-f59415fef5a6`).
- Source: OwenTanzer/live-options-view, master. Collector from merged PR #93; launcher commit `8cbc0de1843ce1d8c1ed38b86de1d64877639e6a`.
- Start: `python -u tradier_cron_launcher.py`.
- Schedule: `15 12 * * 1-5` UTC (05:15 Pacific daylight time). Collector waits for exchange open and gates holidays/early closes.
- Single replica; 1 GiB volume `65c090e4-6d65-446b-86b0-d2fd11ba365a` mounted at `/data`; MOO144_SPOOL_DIR=/data (collector adds moo144-collector-spool/date).
- 8 strikes, 180-second checkpoints, 5 consecutive stream reconnects, 300-second lease TTL, 536870912-byte spool cap.
- TRADIER_TOKEN and four R2 variables reference moo144-tradier-probe variables; no secrets in source.
- API-managed settings. New services cannot opt into deprecated railway.toml configuration; root railway.toml continues to belong to the existing service. No project-wide migration was performed.
- Effective Railway cron restart policy reads NEVER despite ON_FAILURE update success. The launcher retries nonzero child exits at most ten times, waiting at least lease TTL + 10 seconds (310 seconds default). Clean exit, confirmed takeover, and operator termination do not retry. Exhaustion remains visible as a failed process; partial sessions remain partial. Container-level crashes still depend on platform scheduling/operator intervention; child-process retry is not proof of platform-level restart acceptance.
- Watch paths: /tradier_cron_launcher.py, /scripts/moo144_tradier_collector.py, /scripts/moo144_tradier_probe.py, /requirements.txt, /Dockerfile.

## Verification evidence

Launcher subprocess tests passed for fail-twice-then-success, retry exhaustion, and SIGTERM propagation without retry. These do not replace real infrastructure fault recovery checks.

After-close smoke deployment `e7ba6879-4759-41c1-8fd7-f1a4b3f66023`, commit 8cbc0de: SUCCESS. Runtime logs 2026-09-18T20:45:11Z show volume mount and container start; at 20:45:13Z the collector emitted session_already_closed, date 2026-09-18. This verifies imports/startup/calendar exit, not live Tradier/R2 authentication or event capture.

Cron was restored; final scheduled deployment requested as `ab27a53d-c513-4cf8-bee7-c3a28590914b`.

## Acceptance schedule (Pacific)

- Monday September 21, 13:15: first candidate full-session audit. Check on-time capture, coverage, counts/hashes, uploads, spool drain, health and complete manifest. Friday supplies no accepted full session.
- Tuesday September 22, 07:30: verify subsequent automatic startup with new date/universe; review isolated restart/upload-failure recovery evidence. Never inject faults into production capture for this check.
- OA-180's three accepted sessions remain a separate gate. Do not infer acceptance from elapsed days or successful deployment.

## Rollback

Disable this dedicated service's cron and stop its active execution if necessary. Preserve its volume and archived evidence. Revert only its source/start configuration to an explicitly reviewed revision. Do not substitute the old one-shot probe or alter the dashboard/Crassus services. Never label a rollback-interrupted session complete.

## Pre-open readiness fix (prepared September 22; live verification pending)

September 21–22 archives remain usable for explicitly bounded preliminary analysis;
their roughly 2–4 second opening gaps are not the permanent production standard.

The daily worker now waits until exchange open minus 60 seconds (09:29 Eastern
on normal sessions), then acquires ownership, selects/persists the day's universe,
reconciles storage, and connects the streaming subscription. Selection uses a
positive, noncrossed QQQ bid/ask midpoint with both quote timestamps no older than
120 seconds; it fails rather than silently using yesterday's last price. Selection
time/reference source are persisted. The one-shot probe remains open-only.

The subscription is consumed continuously through the open. Pre-open provider
events are counted separately as discarded warmup and excluded from regular-session
event counts and statistics. The latest timestamped warmup quote per subscribed
symbol is retained as opening context. When it supplies a regular trade's quote age,
that trade carries `preceding_quote_source: preopen` and the original quote payload
(including its receipt timestamp) in `preceding_quote_context`. A newer regular
quote replaces it; older out-of-order quotes cannot overwrite newer context.

After the opening boundary, timesales are classified using provider `date` and
`session`, separately from receipt time. Timestamps must be inside the calendar's
regular interval (open inclusive, close exclusive); a supplied nonempty session
label must be `normal`. Missing labels are allowed when provider time is valid.
Excluded trades are archived as `excluded_timesale` diagnostic records with a
reason and original payload, and counted separately from regular timesales.
Missing/invalid provider timestamps make the session partial. The summary's
`excluded_timesales` reports counts by reason. Diagnostic records remain included
in total archive record counts, while quote context does not add extra records.

The reader processes complete lines without waiting
for the default 512-byte requests buffer. Contract scope, same-day reuse, date
attribution, holiday/early-close calendar, archive prefixes, and launcher remain intact.

`stream_ready` logs valid provider traffic on each connection; readiness from a
connection that dropped before the open cannot certify its replacement. Summaries
include `stream_connected_at`, `opening_stream_ready_at`, and
`preopen_events_discarded`. Completion requires proven stream readiness by the
opening boundary. There is no five-second lateness exemption. HTTP success alone
does not prove readiness. Reconnects remain conservatively partial, including any
pre-open reconnects; this patch does not relax existing outage acceptance.

Before merge/deploy: run collector/probe tests and inspect only these source changes.
Merge touches watched paths and may automatically deploy the dedicated service.
Keep the existing cron, variables, volume, and single replica. After deployment,
verify the next live session logs `stream_ready` before 09:30 on the connection
that survives the boundary, correct fresh universe, first event/provider times,
and final reconciled archive/health. Unit tests cannot certify provider-side
opening delivery; readiness is operational evidence, not exchange-feed completeness.

Provider references: https://docs.tradier.com/docs/clock,
https://docs.tradier.com/docs/quotes, and
https://docs.tradier.com/reference/http-streaming. The streaming session is created
immediately before connection, within the documented five-minute session-ID lifetime.

## IBIT collection (proposed; not deployed)

Tradier permits one simultaneous market-data stream per account
(https://docs.tradier.com/docs/streaming-data, Limits). IBIT is therefore
collected by the **same** worker and stream as QQQ, never by a second service
or a second process sharing `TRADIER_TOKEN`. Do not deploy another Tradier
stream collector with this token without explicit Tradier confirmation that
the access arrangement permits concurrent streams.

`MOO144_UNDERLYINGS` lists the underlyings sharing the stream as comma-separated
`SYMBOL[:policy]` (1-4 entries; the first is the primary). It defaults to `QQQ`, so the deployed service
is unchanged until the variable is set. Policies:

- `same_day` (default): requires a 0DTE expiration.
- `nearest`: first listed expiration on/after the trade date. IBIT lists only
  Monday/Wednesday/Friday expirations (since February 2026), so
  `IBIT:nearest` gives 0DTE on Mon/Wed/Fri and 1DTE on Tue/Thu. The universe
  records `expiration`, `expiration_policy` and `days_to_expiration`.

How the shared stream works:

- One stream lease per day at the original key `moo144/tradier/<date>/lease.json`,
  whatever underlyings are configured, so every collector build and
  configuration contends for it: at most one stream owner, fenced and renewed
  exactly as before.
- The first-listed underlying is the **primary** (QQQ). It keeps the
  single-underlying startup path unchanged: its universe selection, stale-date
  recovery, reconciliation and preflight happen on the main thread, and a
  failure there is fatal/restart-recoverable exactly as today.
- Other underlyings are **optional** (IBIT). Their preparation (load persisted
  universe or select one, reconcile local spool) starts concurrently with the
  primary's, in daemon threads that are read-only toward the archive. The main
  thread waits for them only until the **cutoff: open - 20 s**, leaving time to
  persist/preflight admitted lanes, create the stream session, connect and
  prove readiness before 09:30. If the primary is itself only ready after the
  cutoff (late start/restart), optional lanes get at most 5 s more.
- At the cutoff every optional lane is frozen. Ready lanes join the one initial
  subscription; the main thread then persists a freshly selected universe and
  writes their preflight. Lanes that failed or were not ready are excluded for
  the session (`lane_unavailable: preparation_failed: ...` /
  `missed_preparation_cutoff`) and get a partial summary at the end. A late
  result is discarded (`optional_lane_result_discarded` log): it cannot change
  the subscription or write anything, and nothing waits for its thread.
- Optional lanes recover prior-date spool leftovers after the close, off the
  pre-open path, and only while this process still owns the stream lease.
- A router writes each event to its underlying's lane: separate archive
  (`moo144/tradier/<date>/` for QQQ, unchanged; `moo144/tradier-ibit/<date>/`
  for IBIT), spool (`moo144-collector-spool[-ibit]/`), uploader, stats,
  reconciliation, health, summary and manifest. Stream-level records (gaps,
  heartbeats, malformed payloads) are copied to every lane; stream-level
  partial reasons apply to every lane. An unavailable optional lane is not
  restart-recoverable, since a restart would interrupt the primary.

## IBIT spool allocation (enablement gate)

The primary keeps `MOO144_MAX_SPOOL_BYTES` (512 MiB default) unchanged. Optional
lanes do not share it: they require an explicit `MOO144_OPTIONAL_SPOOL_BYTES`
cap each. If it is unset, or the primary cap plus all optional caps exceed 80%
of the spool volume, optional lanes are excluded for the session
(`optional_spool_allocation_unset` / `spool_allocation_exceeds_volume`) and QQQ
runs alone. Exhaustion of any lane's cap still stops the shared capture.

Observed evidence (2026-09-23, Railway `DISK_USAGE_GB` for
moo169-tradier-collector, 72 h at 15-min samples covering the Sep 21-23
sessions): peak 0.035 GB, average 0.020 GB. That is about 7% of QQQ's 512 MiB
cap. Railway reports this per service, so confirm it matches the `/data` volume
(1 GiB) in the volume view before relying on it.

Proposed allocation, pending that confirmation: `MOO144_OPTIONAL_SPOOL_BYTES=134217728`
(128 MiB). Caps then total 640 MiB, within the 80% (819 MiB) guard, leaving at
least 384 MiB for recovery files and filesystem overhead. IBIT's event rate is
unmeasured; check its peak backlog on the first sessions and adjust, or grow the
volume, before treating the allocation as verified.

## IBIT rollout plan

1. Merge in a window that does not interrupt live capture (after the close):
   merging touches watched paths and redeploys moo169-tradier-collector. With
   `MOO144_UNDERLYINGS` unset it runs QQQ alone, as today.
2. Keep IBIT disabled until QQQ's OA-169/OA-180 acceptance requirements are
   satisfied and the volume/capacity check above is done.
3. Then, on moo169-tradier-collector only, set
   `MOO144_UNDERLYINGS=QQQ,IBIT:nearest` and `MOO144_OPTIONAL_SPOOL_BYTES`. No new
   service, token, cron or volume, and no second stream.
4. Verify IBIT's own full-session archive/coverage audit plus a subsequent
   automatic startup. Unit tests do not satisfy these live gates.

Configuration decisions to confirm at enablement (not code defects):

- `IBIT:nearest` (0DTE Mon/Wed/Fri, 1DTE Tue/Thu; `expiration_policy` and
  `days_to_expiration` recorded in every universe) vs `IBIT` (`same_day`),
  which collects only on expiration days and records the lane as unavailable
  on Tue/Thu.
- `MOO144_STRIKE_COUNT` (8) applies per underlying. Check the actual price and
  strike band 8 IBIT strikes cover on the first session.

Rollback: unset `MOO144_UNDERLYINGS` (or set it to `QQQ`). QQQ archives are
unaffected either way.
