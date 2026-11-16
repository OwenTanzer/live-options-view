# Options Replay (OA-237 S1)

Land in a random historical QQQ session with the date hidden, buy a 0DTE
option at the recorded ask, watch it, sell it at the recorded bid, and see the
net profit after fees. Then reveal the day.

## Run it

```sh
pip install boto3                      # the only dependency (already used by synthetic_days)
export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...   # read-only use
python research/options_replay/qualify.py   # once: builds the day pool (~2-3 min)
python research/options_replay/server.py    # then open http://127.0.0.1:8737/
```

Alternatively, `OPTIONS_REPLAY_ENV_FILE=path/to/.env` loads `R2_*` from a dotenv file.
`--fee` sets the cost per contract per side (default $0.65). `--seed` fixes the
day/start choice for testing. `--day YYYY-MM-DD --start HH:MM` pins one session
and landing time. That is for operator reproduction only, because the operator then knows the date. The first load of a day downloads its session
snapshots (about 12 s). The server prefetches the next random day in the
background.

## Where the data comes from

`intraday/{YYYYMMDD}/snapshot_{HHMMSSffffff}.csv` in the collector bucket. These are
the live collector's ~60-70 s 0DTE chain snapshots, timestamped in ET wall-clock time.
Each row has the recorded `Bid`/`Ask` and that snapshot's `UnderlyingPrice`.
The `QQQ` column is not used, because it can disagree with `UnderlyingPrice`
(2026-11-13 10:30: 688.25 vs 675.19). The public Historical archive
(`raw/qqq_chain_*`) holds only one end-of-day chain per date, so it cannot drive
a replay clock.

Access is read-only: the code calls only `list_objects_v2`/`get_object`. All
caches, day packs, the day pool and session journals live under
`research/options_replay/.cache/`, which is gitignored and disposable. Nothing
touches the Worker, `/api/paper-trade`, accounts, KV, D1, the bots or the
collectors.

## Rules

| Rule | Implementation |
|---|---|
| Day pool | NYSE sessions, with a quote at most 150 s old for ≥ 90 % of the regular session (from the listing). Of 25 sampled snapshots, ≥ 90 % must pass the parity consistency check (`qualify.py`). A day is checked again snapshot-by-snapshot at load; it is refused if quotes would be live for < 85 % of the session or dark for > 15 min. |
| Spot/quote consistency | Each snapshot's `UnderlyingPrice` must agree with its own quotes by put-call parity: `K + Cmid − Pmid` at the pair nearest the money, within $0.15 + half the two spreads. Consistent snapshots have a median residual of $0.02. This is a consistency check on recorded values. The archive has no per-quote timestamps, so it does not prove that spot and quotes were observed at the same instant. |
| Hidden date | Until reveal the page receives no TradeDate, Expiration, OptionSymbol, weekday or source key. Contracts are labelled `QQQ 684 call, 0DTE`. Time of day and time to the close are shown. |
| No future data | The clock and ledger are server-side. The view is built only from snapshots at or before the clock, and the clock only moves forward. The rest of the day appears only after reveal. |
| Fills | Buy at the recorded ask and sell at the recorded bid of the latest snapshot at or before the clock. Each fill reports the quote time and age. The fee per contract per side is shown, carried into the basis, and included in net P&L and in the "if sold now" mark. |
| Disabled, never modeled | The board is disabled when there is no snapshot (`no_snapshot`), the snapshot is > 150 s old (`gap`), the feed has been frozen for > 150 s (`frozen`: board and spot unchanged), or the snapshot fails the consistency check (`parity`, `no_pair`, `no_underlying`). A banner states the reason in plain language, with the numbers for parity, and the time it began. The chart shades past disabled stretches; an ongoing stretch is cut off at the clock, so its length is never revealed. A side is disabled when its price is missing, zero (no bid to sell into) or crossed. |
| Expiry | A position still open at the close expires. It is settled at intrinsic value against the last parity-consistent recorded underlying and labelled as an expiry settlement, not a fill. |
| Isolation | The server binds 127.0.0.1, refuses cross-origin POSTs, and keeps paper orders in memory plus a local journal. Long only, with up to 20 contracts per order and $10,000 of paper cash. |

## Controls and pacing (S2a)

- A **bar** is one recorded snapshot, about 60-70 s apart. **Step 1 bar** (→) moves exactly to the next bar.
- **Play / Pause** (space) runs at 1x real time by default; 5x, 15x and 60x are optional. Playback never
  crosses more than one new bar per move. The server refuses any clock move within 500 ms of the
  previous one (HTTP 429), so every bar stays on screen for at least 500 ms at any speed. **+5 min**
  walks the same path one bar at a time.
- **Reset** (two clicks) returns to the landing time with a fresh ledger. Earlier attempts are kept,
  including how far they had seen, and are reconciled at reveal.
- **Contract strip**: ±4/±6/±10 strikes, anchored so rows don't jump under the cursor. It re-centers
  only when spot leaves the middle half (or on **Center on spot**, `c`). Each side shows bid, ask and
  spread. A held contract stays pinned in the strip with an inline **Sell**.
- **Reveal** discloses the date and shows a reconciliation table. For each fill it lists the decision
  clock, the source snapshot key, the recorded bid/ask, the same contract's row re-read from the raw
  archived CSV (with a match check), premium and fee. It ends with gross − fees = net, checked against
  the ledger.

## Saved trades and notes (S2b)

The trainer store is a local SQLite file, `research/options_replay/.cache/trainer.sqlite3`
(`--store` to move it). It is gitignored and isolated from live trading, paper accounts, the
Worker, R2, KV and D1.

- **Automatic, no Save/Export.** Each order is written in one transaction (WAL,
  `synchronous=FULL`) *before* the in-memory ledger changes. If the write fails, the order is
  refused and nothing changes. The page marks a fill "saved ✓ fl_…" only from the commit
  acknowledgment.
- **Trade identity.** A trade (`tr_…`) is one round trip on one contract. It opens on the buy
  that takes the contract from flat to long and closes when the quantity returns to 0 (or at
  expiry). Fills (`fl_…`) carry a per-trade `seq`.
- **Decision-time state.** Each fill stores the exact masked view the player had when placing
  the order, once, with its SHA-256. SQLite triggers make fills immutable (no UPDATE or
  DELETE).
- **Notes.** Notes are optional, one per trade (`nt_…`), and append-only (`rev` 1, 2, …;
  identical text adds nothing). The notes window is closed by default and autosaves while you
  type; buying and selling work with it closed. Editing a note never touches fills or decision
  state, and the hash is re-verified on every read.
- **Masking.** Until a session is revealed, retrieval through the page, `/api/records` and the
  CLI shows no date, weekday or source key. A session that was never revealed before the
  server restarted stays masked. Resuming it is reconnect territory (S2c).

Retrieval:

```sh
python research/options_replay/records.py list                     # no server needed
python research/options_replay/records.py show tr_… [--state]
curl "http://127.0.0.1:8737/api/records?state=1"                   # all trades
curl "http://127.0.0.1:8737/api/trade?id=tr_…"
```

The page's **Saved records** panel reads the store on load, so a fresh browser sees every trade.

## Interrupted saves and resume (S2c)

- **Request ids.** Every order, undo, reset and note revision carries a client `request_id`.
  The id is committed in the same transaction as its effect (`request` table). A retry
  with the same id and the identical body returns the original acknowledgment
  (`replayed: true`) instead of applying again. This covers a lost response, a double
  click, five simultaneous submissions, and a retry after reconnect. The same id with a
  different body is refused.
- **Stale requests.** Writes and clock moves carry `expected_clock`. If the replay clock
  has moved since, the request is refused (409 `stale`) and the current view is returned
  so the page resyncs. A note revision carries `base_rev`; if a newer revision exists, it
  is refused as a conflict and nothing is overwritten. The editor keeps the text and
  offers to save it as the next revision.
- **Failed writes** roll back completely (503 `not_saved`) and nothing is applied. The page
  shows *saving…*, *saved ✓*, *no response: unknown whether saved* (retrying with the
  same id) or *not saved* (with Retry). It shows *saved* only from a storage
  acknowledgment.
- **Undo** voids only the latest fill, and only while the clock is still at its decision
  time (no undo with hindsight). It writes an immutable `fill_void` record; the fill and
  its decision state stay. The ledger is rebuilt from stored fills through the same code
  path as live fills.
- **Resume.** The session clock, attempt and resets are persisted. After a restart, or
  from another browser, an unrevealed session appears under *Unfinished sessions*.
  **Resume** rebuilds it from storage, still masked, so it can be finished and revealed.
  Any request for an unknown in-memory session resumes it transparently.
- **Fault injection** (testing only, off by default): with `--allow-fault-injection`, the
  header `X-Replay-Fault: fail_write | drop_response` fails a write before commit, or
  commits it and drops the response.

Reproduce every case with two real server processes and an independent read of the store:

```sh
python research/options_replay/s2c_cases.py --day 2026-11-02 --start 14:44     --store research/options_replay/.cache/s2c_cases.sqlite3     # fresh path each run
python research/options_replay/records.py list --store research/options_replay/.cache/s2c_cases.sqlite3
python research/options_replay/records.py requests --store research/options_replay/.cache/s2c_cases.sqlite3
```

## Simulation mode (S3a)

Choose **Simulation** (optionally with a seed) instead of **Replay**. The page shows a
persistent *SIMULATION: a generated session, not a historical day* banner with the seed,
generator version and inputs digest. Pacing, saving, recovery and resume behave exactly
as in Replay; simulated sessions are stored as `SIM:<generator>:<seed>:<inputs>` and are
never shown with a date.

- **Mechanism** (`simulate.py`, reusing `research/synthetic_days`): moving block bootstrap
  of real QQQ 5-minute returns (30-minute blocks from the same time-of-day position),
  upsampled to 60 s with a range-scaled Brownian bridge. Option quotes are intrinsic plus a
  donor end-of-day 0DTE chain's extrinsic value at the nearest moneyness, scaled by a
  time-of-day × moneyness decay grid and a spread ramp measured from collector intraday
  snapshots.
- **Calibration source** (read-only R2): `history/QQQ/5/2023-2026` (830 sessions), 80 evenly
  spaced `history/QQQ/options_0dte` donor days, and 15 intraday days × 30 snapshots. Every
  object used is listed with its SHA-256 in `.cache/sim_inputs/inputs_manifest.json`, and the
  inputs are frozen in `frozen_inputs.pkl` (hash-checked).
- **Reproducibility:** seed + `GENERATOR_VERSION` (`sim-v1`) + inputs digest → the identical
  path and quotes (`pack_sha256`). Resume regenerates from the seed and refuses if the
  inputs or version changed. At reveal, every fill is checked against an independent
  regeneration of its quote.
- **Provisional pricing.** A comparison over 20 seeds and the cached recorded days
  (`python research/options_replay/simulate.py --compare-recorded`) puts generated ATM
  straddles at about 0.36-0.56× recorded levels with ~4-5× wider spreads. Calls and puts
  are priced independently, so parity is not enforced, and one donor chain sets the
  whole day's smile. Empirical decay calibration is S3b; pricing/spread checks are S3c.

```sh
python research/options_replay/simulate.py --seed 20261115     # identity of one generated day
python research/options_replay/simulate.py --compare-recorded   # provisional-pricing gap
```

## Tests

```sh
python -m unittest tests.test_options_replay -v
```

The tests use synthetic packs and need no network or credentials.
