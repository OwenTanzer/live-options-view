# OA-237 options trainer: S4 handoff (two conditions: Replay and Simulation)

**Status: ready for review, not closed.**

| Stage | Status |
|---|---|
| S1-S3b | accepted |
| S3c | open: the frozen combined model waits on its forward held-out comparison (first 10 gated collector sessions per symbol from 2026-11-16) |
| Quest | open |

Everything is local and isolated from active trading, collectors, the Worker, KV and D1. It reads R2 only.

## Source

Branch `claude/oa237-options-replay` on `OwenTanzer/live-options-view`. The handoff was checked at the
revision that adds this file; the commit hash is quoted in the handoff message. Code lives under
`research/options_replay/` and the tests are `tests/test_options_replay.py`.

## Setup

```sh
python -m pip install boto3 numpy          # numpy is used by research/synthetic_days (Simulation path)
export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...   # read-only use
# or: export OPTIONS_REPLAY_ENV_FILE=path/to/.env   (R2_* lines)
python research/options_replay/qualify.py              # once: Replay day pool (~2-3 min)
python research/options_replay/server.py               # http://127.0.0.1:8737/
```

The first Simulation start builds and freezes its calibration inputs, which takes about 4 minutes; later
starts are offline. A fresh build fetches only the pinned objects in `sim_inputs_pin.json` and must reproduce
the frozen inputs byte for byte or stop. **While forward data is reserved, use the preserved frozen cache
(`.cache/sim_inputs/`) or the original pre-freeze objects instead of a fresh build; see Known limits.** Caches, day packs, the trainer store and session journals live under
`research/options_replay/.cache/` (gitignored).

## The two conditions

| | Replay | Simulation |
|---|---|---|
| Day | random qualified historical QQQ session; date, weekday and source keys hidden until reveal | generated session from a recorded seed; labelled SIMULATION everywhere and never shown with a date |
| Quotes | recorded collector bid/ask (`intraday/{YYYYMMDD}/snapshot_*.csv`); missing, stale (>150 s), frozen or parity-inconsistent quotes are disabled, never modelled | **sim-v2** (default): the S3c combined model, provisional. **sim-v1** (S3a donor pricing) stays regenerable for existing seeds |
| Fills | buy at ask, sell at bid, $0.65/contract/side | same |
| Pacing | bar = recorded snapshot; Step, Play 1x/5x/15x/60x, Pause, Reset; 500 ms server-enforced dwell | same |
| Capture/recovery | automatic SQLite trainer store (fills with decision-time state + SHA-256, append-only notes, immutable voids for undo, idempotent request ids, stale-clock refusal, resume after restart) | same; resume regenerates from seed + version and refuses changed inputs |

## Reproduce (each command uses a fresh store path)

```sh
python research/options_replay/handoff_demo.py --store research/options_replay/.cache/handoff.sqlite3
python research/options_replay/s2c_cases.py --day 2026-11-02 --start 14:44 --store research/options_replay/.cache/s2c.sqlite3
python research/options_replay/records.py list|sessions|requests --store <store>
python -m unittest tests.test_options_replay
```

`handoff_demo.py` covers both conditions:

1. Server process A (operator flags `--day 2026-11-02 --start 14:44`) makes a Replay trade.
2. Server process B makes a sim-v2 seed-20261116 trade.
3. Both are killed, and the records are read straight from the store. The Simulation quotes are
   regenerated independently.
4. Process C resumes and reveals both sessions and checks every fill against its raw archived row or
   its regeneration.

Prices and nets are deterministic. Record IDs are new on each run.

| Condition | Trade | Net | Example run IDs (fresh store, 2026-11-15) |
|---|---|---|---|
| Replay, 2026-11-02 (revealed) | buy 1× 684C @ 1.96 ask 14:44:00 (quote 1.94/1.96); sell @ 1.74 bid 15:14:00 (1.74/1.76); fees $1.30 | **−$23.30** | session tdQ-__Ar-2mp, trade tr_6ce8e2acc194, fills fl_4f7f1345247d / fl_d3c4da8892ff |
| Simulation sim-v2, seed 20261116 | buy 1× 433C @ 2.52 ask 09:46:00 (2.49/2.52); sell @ 2.43 bid 10:00:00 (2.43/2.46); fees $1.30 | **−$10.30** | session iqHtHfSxEbVL, trade tr_55cdca20a5d4, fills fl_2cafdece4612 / fl_d4c0a7055560 |

Both were recovered from the store with no server, resumed in a new process, and revealed. Both reconcile,
and every fill matches its source.

## Provenance

- **Replay data:** R2 `qqq-options-chain-data`, `intraday/{YYYYMMDD}/snapshot_*.csv` (collector), read-only.
  The day pool and its rules are in `.cache/qualified_days.json`: coverage at the 150 s quote-age rule, and parity
  consistency checked on 25 samples, re-checked at load.
- **Simulation path inputs:** R2 `history/QQQ/5`, `history/QQQ/options_0dte` (80 days) and intraday snapshots
  (15 × 30). The manifest is `.cache/sim_inputs/inputs_manifest.json`, digest `13e18c6aea01fdb9…`, frozen and
  hash-checked.
- **sim-v2 pricing:** `pricing.py` + `calibration/s3c_params.json` (sha256 `2f680858…`), fitted on development days
  only (QQQ 2026-06-24..11-13, SPY 2026-10-08..11-13).
- **Protocols and history, all committed before the corresponding fitting:**
  - S3b: `calibration/PROTOCOL.md` (804dc79); results `calibration/RESULTS.md`, `attempts.jsonl`.
  - S3c: `calibration/S3C_PROTOCOL.md` (714cade), with Amendment 1 (9a29ad9) citing the expiration conventions
    and declaring the dev2 corrections. Frozen model f99b361; results `calibration/S3C_RESULTS.md`,
    `s3c_attempts.jsonl`, `s3c_manifest.json`.

## Known limits

- **Pricing is not validated.** S3b missed every cell. S3c's frozen model has development results only and
  carried misses: absolute level about $9-12, 30-minute hold profit $4-7, opening decay, and the QQQ endpoint.
  The s3c-dev2 development candidate is mixed and is not promoted. Held-out evidence depends on forward
  collector sessions; it is a coverage dependency, not a date.
- **Next-session expiry is unsupported.** No non-0DTE expiry exists in the intraday archives.
- **A fresh Simulation build is not literally no-read.** `sim_inputs_pin.json` limits a fresh build to pinned
  objects and checks the frozen bytes, so later bars never reach the model inputs. But `history/QQQ/5/{year}.json`
  is one growing file per year: `five_min_bars` downloads and JSON-decodes the whole current-year file
  before cutting it at the pinned last bar. While forward sessions are reserved for S3c, receive Simulation
  from the preserved frozen cache (`.cache/sim_inputs/frozen_inputs.pkl` + `inputs_manifest.json`, digest
  `13e18c6aea01fdb9…`) or the original pre-freeze objects, not from a fresh rebuild against the live archive.
- **Held-out dataset builds use finished sessions only.** `calibration/dataset.py` keeps a day's file once
  built, so it is run for the reserved comparison only after `calibration/coverage_s3c.py` reports 10 finished
  sessions for both symbols. `evaluate_s3c.py --set held_out` then decodes exactly the first ten per symbol.
- **Expiration conventions:** 16:15 ET last trade (SEC Rel. 34-105153), OCC Rule 805 exercise-by-exception at
  $0.01 against the 16:00 close (reference price secondary-sourced), and the FINRA 2360 17:30 ET decision
  deadline. The post-close variance term is a modelling assumption. A Simulation session ends at 16:00, while
  Replay data runs to about 16:14 but the Replay clock ends at 16:00.
- **Replay coverage:** parity-inconsistent or stale snapshots disable trading (about 1-9 % of a session, more on
  fast days). There are 97 qualified QQQ days, 2026-06-24..2026-11-13.
- **Fills** are top-of-book bid/ask with no size, queue or liquidity model. Fees are a flat $0.65/contract/side.
- **Single-user local server:** 127.0.0.1, same-origin check, no authentication, not hosted. Fault injection is
  test-only (`--allow-fault-injection`).
- `--day/--start` are operator reproduction flags; whoever uses them knows the date.

## Shipped phone trainer

The private trainer serves QQQ at the root and SPY at `/spy/`. Each symbol has an isolated practice store. The dashboard links to the trainer hostname. First-time browsers open directly and get a private practice history via a secure cookie. Previously invited browsers retain access to their existing records. The trainer reuses the dashboard's market-order validator and sends accepted orders to its own clock and ledger. It never submits those orders to a live account.

For the SPY service, set `OPTIONS_TRAINER_SYMBOL=SPY`, run `prepare_spy.py` with the existing read-only data access, then start `server.py --port 8744`. Its default cache is `.cache/spy`; retain its donor file alongside the store. QQQ uses port 8737 and its original frozen inputs. The gateway expects both servers on loopback and reads `release-invite` and `release-cookie` from its private working directory. Both symbols offer same-day expiration. Next-session expirations have no verified synchronized intraday archive here.

Replay volume sums observed volume increments across same-day contracts in each recorded snapshot. It is partial-chain option activity. Simulation resamples volume from the same donor block as the generated path and shows it when the five-minute interval ends. QQQ donors supply shares; SPY donors supply option contracts. Each chart names the unit. Simulated dollar feedback is provisional.

Reveal requires a flat position or expiry settlement. Review shows fills, costs, matched quantities, estimated price contributions and residual. Legacy sessions revealed with positions still open remain unscored.
