# NASDAQ-100 snapshot collector

Hourly snapshots of the 101 NASDAQ-100 listings plus the NDX index, from
public sources only — no account, no auth. Publishes `nasdaq/latest.json`
and its `snapshot_provenance.v1` sidecar `nasdaq/latest.provenance.json`
to R2 (opt-in `--publish`).

Run from the repository root:

```sh
python -m market_data.nasdaq_collector universe
python -m market_data.nasdaq_collector collect --out nasdaq-out
python -m market_data.nasdaq_collector collect --publish   # opt-in R2 write
```

## Sources

| Data | Provider | Notes |
|---|---|---|
| Prices, day range | Nasdaq official quote API (`api.nasdaq.com/api/quote/<SYM>/info`), browser-style User-Agent | One request per listing plus NDX; day range from `keyStats.dayrange`, falling back to the latest Yahoo daily bar. Each quote carries a last-trade timestamp (`primaryData.lastTradeTimestamp`); freshness is measured from it when present — date-only stamps (as published for NDX) fall back to collector receipt. Market cap is not in the quote response; it comes from one `list-type/nasdaq100` request per run (same official source as the universe) |
| Candles | Yahoo Finance chart API (`query1.finance.yahoo.com/v8/finance/chart/`) | Real 1-minute bars; the non-standard 3m/9m/2h/4h intervals are aggregated from them on America/New_York time, bucketed from the 09:30 ET open (see `candles.py`) |
| News | Google News RSS per ticker (`news.google.com/rss/search`) | Daily per ticker; re-fetched only when stored headlines are more than 12h old |

## Universe

The 101 listings (100 companies) are embedded in `symbols.py`, taken from
Nasdaq's official `list-type/nasdaq100` API on 2026-11-23
(`totalrecords: 101`). Alphabet Inc. appears twice — GOOGL (Class A) and
GOOG (Class C) — which is why there are 101 listings for 100 companies.
Re-verify against the same endpoint after each annual reconstitution
(December) or announced change; `symbols.validate()` fails closed on a
wrong count or a duplicate.

## Snapshot shape

`nasdaq/latest.json` (`schema_version: 1`, `dataset: "nasdaq_snapshot"`):

- `index`: NDX level, previous close, change, `percent_change` labelled
  **regular-session move vs previous close**, day range, market status,
  breadth (advancers/decliners/unchanged) and top 5 gainers/losers.
- `stocks[]`: per listing — symbol, name, price, change,
  `percent_change` labelled **Nasdaq latest reported move (after-hours
  move after the close)**, market cap, rank by market cap, 24h high/low
  (day range), volume, 1h/7d change where available, news headlines
  (title, source, link, published_at), `status` and `price_basis`.
- `coverage`: listings / available / source-unavailable / with-news /
  with-candles counts. `session`: the session model (below).

The two percentage bases are labelled differently on purpose: a stock's
`percent_change` and the index's `percent_change` are not the same
measurement, and the snapshot never lets a reader confuse them.

## Session model

US equity sessions, weekdays 09:30–16:00 America/New_York.
`market_status` is `open` only inside those hours on a non-holiday
weekday; otherwise `closed` with a reason (`weekend`, `holiday_2026`,
`outside_hours`). The embedded holiday calendar covers **2026 only**
(NYSE holidays); for other years holidays are not asserted and the status
falls back to the weekday/hours rule, labelled
`outside_hours_holiday_calendar_unverified`. A stock whose source is
unavailable is reported with `status: "source_unavailable"` and null price
fields — a price is never substituted, carried forward, or invented.

## Provenance

Every snapshot gets a `snapshot_provenance.v1` sidecar recording
`constructed_at`, `archive_uploaded_at`, `latest_published_at`,
`publication.status` (`published`, or `not_published` with a reason),
the source label, the as-of time, and per-stock `price_basis`,
`provider_observed_at` (Nasdaq's last-trade timestamp when published),
`freshness` and `status`. A missing sidecar means
unknown provenance. See `docs/specs/NASDAQ_SNAPSHOT.md` for the full
contract, including the CI wiring a maintainer must add.

## R2 publication

Opt-in only (`--publish`). Environment (unconfirmed against live service
config — these names are the collector's expectation, not a verified live
deployment):

- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `NASDAQ_R2_BUCKET` — bucket holding the `nasdaq/` prefix

The snapshot is uploaded first, then the sidecar; a sidecar failure is
raised, not swallowed.

## Tests

```sh
python -m pytest tests/test_nasdaq_collector.py
```

Offline: universe count, snapshot schema, provenance shape, candle
aggregation (including 9m bucketing from 1-minute bars), the two `%`
labels, news parsing/refresh and the session model. No test touches a
provider or R2.
