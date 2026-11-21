# Tradier archive collector: operation and recovery

This service archives time-and-sales and supplies the QQQ order-flow plot at /order-flow.html. The Live chain has a separate source. After each accepted event enters the archive spool, a bounded display projection retains up to 2,048 recent option prints. A background writer publishes changed snapshots at most every three seconds to order-flow/qqq/v1/recent.json. Failures leave archive intake running. Last print time is provider event time. The display can omit older prints during bursts.
See [archive consumers and compatibility](ARCHIVE_ONLY.md). The stable entry is
[launcher.py](launcher.py), which launches the
[compatibility script](__main__.py) over this package.

## Accepted service configuration

These settings were checked in the November 8, 2026 release. They describe that
accepted configuration, not a continuously refreshed health report.

| Setting | Value |
|---|---|
| Railway project / environment | `live-market-monitor` / `production` |
| Service | `moo169-tradier-collector` |
| Start | `python -u -m market_data.tradier.launcher` |
| Cron | `15 12 * * 1-5` UTC; 04:15 Pacific standard time / 05:15 daylight time |
| Build | Repository-root context; `deployment/python/Dockerfile`; service-managed build/start settings |
| Runtime | One replica, persistent spool mounted at `/data` |
| Restart | Railway cron policy `NEVER`; launcher supplies bounded child-process recovery |
| Source watches | `market_data/tradier/**`, `market_data/diagnostics.py`, `market_data/tradier/__main__.py`, `market_data/tradier/probe.py`, `deployment/python/**` |

The November 8 release readback recorded deployment
`3a4875c9-b677-4c51-a86b-b547c71d18c5`, SUCCESS, `buildOnly=true`, source
`646e61e4c3c0df71006519eaded1d8c53c8d55a7`. Building a cron image does not prove
market-session capture. Refresh service state before a future release; do not
manually run the producer just to check documentation. The exchange calendar in
[config.py](config.py) governs holidays and early closes independently of UTC cron.

## Capture, ownership and recovery

Read [run.py](run.py) for the sequence: settings → pre-open preparation → lease →
universe and lane admission → capture → reconciliation/finalization.

- [universe.py](universe.py) persists the opening selection. New same-day QQQ universes also store the day's listed standard call/put pairs. The primary QQQ lane checks fresh two-sided QQQ quotes every five seconds when quotes arrive and adds the nearest configured number of strikes on the existing WebSocket. Opening and previously added symbols remain subscribed. Other lanes retain their selections. Sessions created before the pair catalog was available continue with their saved fixed subscription.
- Each added contract has an archived subscription marker. Its first observed quote or trade starts its reported coverage. Reconnect and same-day rebuild subscribe to the opening set and previously observed additions. An unconfirmed attempt remains in the archive and is skipped after reconnect. A provider error records unobserved legs of the latest request in the session’s rejected map and prevents retrying those legs for the day. Earlier observations retain their original timestamps. The live table shows partial-day flow and withholds opening-based totals for these strikes. The final stream summary includes the per-contract first observations (null while pending).
- [lease.py](lease.py) uses a renewable fenced lease plus a deadline watchdog.
  Confirmed takeover differs from absent or uncertain ownership; a competing
  owner must not be fought by an automatic restart.
- [capture.py](capture.py) distinguishes pre-open warmup, regular-session events
  and excluded events using provider timestamps/session labels. Successful HTTP
  connection alone does not prove readiness across the opening boundary.
- [spool.py](spool.py) bounds local disk use; [recovery.py](recovery.py) verifies
  content before recognizing uploaded segments or draining prior-date leftovers.
  Preserve unreconciled local data and never relabel a partial session complete.
- The launcher retries nonzero child exits at most ten times, waiting beyond the
  lease lifetime (310 seconds by default). Clean exit and operator termination do
  not retry. This does not establish recovery from a container-level crash.

Archives retain `moo144/tradier/<date>/`: universe, lease, health, per-owner run,
summary and manifest records, and compressed event segments. Completion needs
on-time readiness, capture coverage and successful reconciliation; a deployment
receipt cannot substitute for the final session evidence.

## Settings and optional underlyings

[load_settings](run.py) is the authoritative parser for `TRADIER_TOKEN` and
`MOO144_*`; storage credentials remain in service secrets. Defaults include eight
strikes, 180-second checkpoints, five reconnects, a 300-second lease lifetime and
512 MiB primary spool cap. `MOO144_SPOOL_DIR` must point to persistent storage.

`MOO144_UNDERLYINGS` defaults to QQQ. Optional lanes share the same stream and lease;
never create a second stream with the same token merely to add IBIT. Admission,
cutoff and stream routing live in [lanes.py](lanes.py). Optional lanes require an
explicit `MOO144_OPTIONAL_SPOOL_BYTES` allocation; combined caps are bounded against
volume capacity. `IBIT:nearest` and `IBIT:same_day` select different expiration
policies. Source support does not establish live enablement: check actual service
variables and acceptance evidence before changing scope.

## Release and operational checks

Use the existing service's configured source and cron. After publication, verify
exact source revision, SUCCESS/build-only metadata, start, cron, next run, watches,
volume and activation settings. Next-session checks separately establish opening
readiness, persisted universe, event timestamps, coverage, spool drain and the
final reconciled manifest. Use isolated fixtures for fault recovery tests.

For a necessary rollback, stop only this service's capture/schedule under the
applicable operational authority, preserve its volume and evidence, and restore
an explicitly reviewed source/start configuration. Do not substitute the probe or
change unrelated services. An interrupted session remains partial.

Historical rollout receipts and the detailed original IBIT proposal are available
in [documentation history](../../docs/HISTORY.md). Past acceptance appointments
are not current pending tasks.
