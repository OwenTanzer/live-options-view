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
event counts and statistics. The reader processes complete lines without waiting
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
