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
day/start choice for testing. The first load of a day downloads its session
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
| Day pool | NYSE sessions, with a quote at most 150 s old for ≥ 90 % of the regular session (from the listing). Of 25 sampled snapshots, ≥ 90 % must pass the parity sync check (`qualify.py`). A day is checked again snapshot-by-snapshot at load; it is refused if quotes would be live for < 85 % of the session or dark for > 15 min. |
| Spot/option sync | Each snapshot's `UnderlyingPrice` must match its own quotes by put-call parity: `K + Cmid − Pmid` at the pair nearest the money, within $0.15 + half the two spreads. Synchronized snapshots have a median residual of $0.02. |
| Hidden date | Until reveal the page receives no TradeDate, Expiration, OptionSymbol, weekday or source key. Contracts are labelled `QQQ 684 call, 0DTE`. Time of day and time to the close are shown. |
| No future data | The clock and ledger are server-side. The view is built only from snapshots at or before the clock, and the clock only moves forward. The rest of the day appears only after reveal. |
| Fills | Buy at the recorded ask and sell at the recorded bid of the latest snapshot at or before the clock. Each fill reports the quote time and age. The fee per contract per side is shown, carried into the basis, and included in net P&L and in the "if sold now" mark. |
| Disabled, never modeled | The board is disabled when there is no snapshot, the snapshot is > 150 s old, the feed has been frozen (board and spot unchanged) for > 150 s, or the snapshot fails the sync check. A side is disabled when its price is missing, zero (no bid to sell into) or crossed. |
| Expiry | A position still open at the close expires. It is settled at intrinsic value against the last synchronized recorded underlying and labelled as an expiry settlement, not a fill. |
| Isolation | The server binds 127.0.0.1, refuses cross-origin POSTs, and keeps paper orders in memory plus a local journal. Long only, with up to 20 contracts per order and $10,000 of paper cash. |

## Tests

```sh
python -m unittest tests.test_options_replay -v
```

The tests use synthetic packs and need no network or credentials.
