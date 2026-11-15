# OA-203 options return scanner

One weekday job: scan the day's most-traded underlyings for short-dated option
returns, archive every sweep, and rank the contracts into a leaderboard. The
deployed command is:

    python -u -m market_data.options_scanner run

The code lives in this package. Railway service `oa203-banana-scanner-AWsT`
uses that command and the [shared Python image](../../deployment/python/README.md),
with repository-root context, restart policy `NEVER` and cron `45 12 * * 1-5`
UTC. Its empty watch list covers all source paths. Build/start/cron settings
are service-managed; activation and readiness are separate runtime settings.

Weekly publication validates every contract path from the gzip stream. Weekly detail readers scan the same member to its end before returning the selected row and checking the pointer digest. The producer reserves space for the published members and pointer within the 64 MiB total query budget. The weekly scan enforces the remaining budget and a 64 KiB line limit.

## Where a job lives

| module | responsibility |
| --- | --- |
| `runtime.py` | the event log, wall clock, stop flag and run settings |
| `occ.py` | OCC daily contract volume â†’ the day's underlyings |
| `tradier.py` | the market-data client: retries, rate limits, per-sweep budget |
| `archive.py` | the day directory: journal, manifest, row iteration |
| `sweep.py` | universe selection and one sweep across it |
| `outputs.py` | leaderboard rows and the CSV/JSON artifacts |
| `active.py` | the intraday active snapshot and its artifact budget |
| `backfill.py` | post-close trade-bar backfill for the top contracts |
| `assessment.py` | was the session good enough to trust? |
| `session.py` | one trading day: build, sweep, finalize, recover prior days |
| `readout.py` | offline inspection of an archived day |
| `cli.py` | `run`, `universe`, `smoke`, `build`, `snapshot`, `inspect`, `readout` |
| `returns.py` | return and leaderboard metrics (pure functions) |

## The path a session takes

`run` (`cli.py`) â†’ `run_day` (`session.py`) â†’ `build_universe_for` (OCC volume,
then `sweep.select_universe`) â†’ repeated `sweep.run_sweep` writing
`sweeps/sweep_<n>.jsonl.gz` and one `sweeps/manifest.jsonl` line per sweep â†’
`active.build_active_snapshot` on its cadence â†’ after the close, `finalize`
(`session.py`) builds `build_contract_rows` (`outputs.py`), writes the
leaderboard, uploads through `DayArchive` and records the assessment.

## Operator commands

    python -m market_data.options_scanner universe --size 8     # select only, print
    python -m market_data.options_scanner build --dir <day>     # rebuild from local archive
    python -m market_data.options_scanner inspect --dir <day> --symbol QQQ260918C00600000
    python -m market_data.options_scanner readout --dir <day>

## The stop flag

`runtime.STOP` is the one owner. Modules read it through the module
(`runtime.STOP`) rather than importing a copy, so setting it â€” from a signal
handler or a test â€” actually stops the scan.

## Component tools

Run helpers as modules from the repository root: `python -m market_data.options_scanner.windows_replay --help`. Window math, output formatting, readiness checks and active-snapshot resource limits live beside the scanner. Readiness commands can access providers and storage; they are not routine navigation or test steps.
