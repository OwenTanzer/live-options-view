# OA-203: daily top-500 liquid options-chain intraday-return scanner

Linear [OA-203](https://linear.app/objecta/issue/OA-203) · GitHub #111 · Top priority for Big Banana.

Goal of this first build: observe where the largest intraday option returns
actually happen and keep the evidence. It records and ranks. It does not
trade or predict.

Code: `scripts/oa203_returns.py` (return definitions, pure) and
`scripts/oa203_return_scanner.py` (universe, sampling, archive, build,
backfill, readout). Tests: `tests/test_oa203_return_scanner.py`.

## Decisions taken as defaults (open to review)

| Question | Default in this build | Alternative |
|---|---|---|
| How the 500 chains are chosen | Previous session's OCC cleared option volume per underlying | A Tradier-only sweep of a candidate list (costs ~30 min of request budget, and only sees nearest-expiry volume) |
| Sampling | REST polling, one sweep of all 500 chains every ~5 minutes, plus after-close 1-minute trade bars for the top contracts | A second streaming connection (Tradier allows one per account, and MOO-169 owns it) or a higher rate limit, only with Tradier's confirmation |
| Request budget | Own cap 100/min, pause when the token-wide `X-Ratelimit-Available` falls to 10 | Change `OA203_MAX_RPM` / `OA203_RATE_RESERVE` |
| Where it lives | New scripts in live-options-view, own Railway cron service, own R2 prefix | — |

## Universe (reproducible, information available before the open)

1. Previous NYSE session `D-1` from the exchange calendar.
2. Download OCC's public volume query for `D-1` (all underlyings, by account
   type and put/call). Each cleared contract is reported once per side, so
   chain volume = sum of all rows / 2 (checked: SPY on 2026-09-24 = 14.28M).
   The file must contain rows for `D-1`; otherwise selection fails loudly.
3. Rank underlyings by that volume. Walk the ranking and, for each name, ask
   Tradier for its expirations (`includeAllRoots`). The chain is the nearest
   expiration on or after the trade date (same-day included). Keep the first
   500 that have one; names without one are recorded with a reason
   (`no_listed_expirations`, `no_unexpired_expiration`, `provider_error`)
   and the next rank is used. Any remaining shortfall is recorded.
4. `universe.json` stores the rule and version, OCC report date and SHA-256,
   every selected and skipped name with its OCC volume and rank, and the
   config. The raw OCC file is archived beside it.

Chain liquidity (OCC volume, used for selection) stays separate from
contract liquidity (each contract's own volume, OI and spread, reported on
every leaderboard row and never used to drop a row).

## Sampling

- One sweep samples all 500 chains: blocks of 50 underlyings, one batched
  underlying quote request per block (spot for strike-vs-spot), then the
  block's chains on 3 worker threads.
- Every contract row is archived: symbol, root, type, strike, expiry, bid,
  ask, bid/ask timestamps and sizes, last, trade time, volume, OI, spot and
  its source/time, sample time and sweep number.
- Each sweep appends a manifest entry with every chain's status (`ok`,
  `empty`, `error` + message) and whether the spot quote was missing.
- The loop runs from the open to the calendar's close (early closes
  included). A sweep still running at the close stops at a block boundary
  and is marked `truncated`.

### Provider coverage, cadence, limits, storage (DoD 4)

- **Rate limit:** Tradier market data is 120 requests/minute per token
  (https://docs.tradier.com/docs/rate-limiting). A sweep is ~510 requests
  (500 chains + 10 quote batches), so at 100/min one sweep takes about
  5.1 minutes, about 76 sweeps per 6.5-hour session. The client also reads
  `X-Ratelimit-Available/Expiry`, which count the whole token, so it backs
  off automatically when other services share the token.
- **Cadence consequence:** a move that starts and ends between two samples
  of the same chain is invisible to the quote path. The trade-bar backfill
  recovers such moves for the top contracts. A faster cadence needs a
  smaller universe, a higher limit, or streaming.
- **Selection cost:** ~520 expiration requests (about 5 minutes), started
  30 minutes before the open.
- **Backfill cost:** one `/markets/timesales` request per contract; default
  top 200 by all-rank plus top 200 by clean-rank (~2–4 minutes after close).
- **Storage:** measured at ~31 bytes per contract row gzip-compressed. At an
  estimated 50k contracts per sweep that is ~1.5 MB per sweep and ~120 MB
  per day, plus small JSON/CSV outputs. R2 prefix `oa203/scanner/<date>/`,
  local spool `OA203_SPOOL_DIR` (default `/data/oa203`, needs a volume).
- **Coverage limits:** index options (SPX, VIX, XSP, NDX, RUT) are included.
  AM-settled roots listed on the same date as PM-settled weeklies (e.g. SPX
  on monthly expiry) appear with no live quotes and show as low coverage.
  OCC symbols that Tradier does not recognise are skipped with a reason.

## Return definitions (`oa203_returns.py`)

All returns are chronological: the exit is strictly later than the entry.
An unordered daily high/low ratio is never used.

| Basis | Entry / exit | Label |
|---|---|---|
| `mid` | (bid+ask)/2 from two-sided, uncrossed quotes | Descriptive; headline ranking |
| `exec` | Buy at ask, sell at bid | Comparison; not a guaranteed fill |
| `trade_1min` | 1-minute trade bars: first bar open / bar highs; a low only pairs with highs of strictly later bars | After-close backfill, top contracts only |

For each basis: `first_to_max` (first valid regular-session observation to
the highest later one: the headline metric), `open_to_close`, and
`trough_to_peak` (best gain from any observation to any later one). Each
comes with entry/exit prices, times, absolute change (per share and per
contract), entry spread and quote ages.

Flags are reported beside the return and never remove a row. The `clean`
view excludes flagged rows: `tiny_entry` (< $0.05), `entry_stale`/
`exit_stale` (older quote side unchanged > 30 min), `exit_isolated_spike`/
`entry_isolated_dip` (> 3× both valid neighbours), `low_coverage` (valid
samples < 50% of successful chain fetches), `crossed_quotes_seen`.
Thresholds are versioned (`oa203-returns-v1`) and written into every summary.

## Outputs per day

- `universe.json`, `occ_volume_<D-1>.csv.gz`
- `sweeps/sweep_NNNN.jsonl.gz`, `sweeps/manifest.jsonl`
- `contracts.csv.gz`: every sampled contract, winners and non-winners,
  with `rank`, `rank_in_type`, `clean_rank`, `clean_rank_in_type`
- `leaderboard.csv`: rows in the top 200 overall or top 200 clean
- `backfill_timesales.jsonl.gz`: raw trade bars for the top contracts
- `summary.json`: `complete`/`partial` with reasons, counts, request stats

`build`, `inspect` and `readout` run the same code offline on a downloaded
day directory. `inspect --symbol` prints a contract's timestamped path and
the exact return calculation (DoD 2).

## Failed or incomplete collection (DoD 3)

A session is `partial`, with reasons, if any of: universe shortfall, no
in-session sweeps, chain success rate < 98%, a gap between sweeps longer
than twice the typical sweep plus a minute, stopping early before the close,
upload failures, backfill not run or with errors, or any 429 responses.
Same-day restarts reload `universe.json` and continue the sweep numbering.

## Deployment (not done; needs approval)

A new Railway cron service in the live-market-monitor project, separate
from `moo169-tradier-collector`:

- Start: `python -u scripts/oa203_return_scanner.py run`
- Schedule: `45 12 * * 1-5` UTC (08:45 EDT / 07:45 EST; the process waits
  for selection time and the open)
- Variables: `TRADIER_TOKEN` and the four R2 variables (referenced, as the
  collector does), `OA203_SPOOL_DIR=/data/oa203`
- A volume mounted at `/data` (≥ 2 GiB leaves room for several days if
  uploads stall)
- Single replica. It opens no streaming connection.

## Readout (DoD 5)

After the first complete session:
`python scripts/oa203_return_scanner.py readout --dir <day>` tabulates the
top 100 clean contracts by underlying, call/put, strike vs spot, days to
expiration, and entry/exit hour. Pass several `--dir` flags to see what
recurs across days.

## Validation so far

- Unit tests: return math (ordering, same-bar trade bars, flags), OCC
  parsing, universe fill and shortfall, rate limiter (header back-off, 429,
  local cap), and a sample → archive → build → backfill → assess round trip.
- Live read-only smoke on 2026-09-25 during the session: OCC ranking plus
  20-name selection, then 2 sweeps of SPY/TSLA/IBIT/NVDA (872 contracts),
  202 trade-bar backfills, no 429s.
