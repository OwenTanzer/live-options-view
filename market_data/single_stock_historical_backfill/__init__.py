"""Single-stock historical backfill collector.

Backfills, then maintains, per-stock historical candles for the 101
NASDAQ-100 listings from Tiingo, and publishes them to R2 as
``nasdaq/candles/<TICKER>/<interval>.json`` — one file per interval
(1m, 3m, 5m, 9m, 15m, 30m, 1h, 2h, 4h, 1d, 1w), plus one
``snapshot_provenance.v1`` sidecar per ticker
(``nasdaq/candles/<TICKER>/provenance.json``).

Only three base datasets are ever fetched — the three Tiingo actually
returns — and every other interval is derived from them, never fetched:

- ``1m``: 1-minute bars, three newest pages of 10,000 bars each
  (~75 trading days; Tiingo caps a response at 10,000 bars and returns
  the *newest* bars in the window, so paging walks backward with
  ``endDate`` — see ``tiingo.py``).
- ``1h``: hourly bars back to 2017 (~15,478 bars, 2 pages).
- ``1d``: daily bars from 2000 (one request; AAPL returned 6,764 bars
  on 2026-11-23).

Tiingo intraday bars carry **no volume** — the ``volume`` key is absent
from every intraday response, so no volume field is stored on any
intraday bar or derived interval. Daily bars do carry volume, and the
weekly bars derived from them sum it. The NDX index is NOT backfilled
here: Tiingo does not carry NDX (404), and the site's index chart stays
on Yahoo Finance ``^NDX`` as in PR #391.

Rate limit (Tiingo free Starter): 50 requests/hour, 1,000/day,
500 symbols/month. A full backfill is 6 requests per listing
(3 + 2 + 1) = 606 requests for 101 listings, so the first fill takes
~12 hours paced; ``pacing.py`` keeps a checkpoint so a run can pause
and resume, and ``update`` afterwards fetches only new bars.

Run from the repository root::

    python -m market_data.single_stock_historical_backfill universe
    python -m market_data.single_stock_historical_backfill backfill --out backfill-out
    python -m market_data.single_stock_historical_backfill update --out backfill-out
    python -m market_data.single_stock_historical_backfill status --out backfill-out

Publishing is opt-in (``backfill --publish`` / ``update --publish``).
The Tiingo token comes from the environment only
(``TIINGO_API_TOKEN``); it is never stored in the repository.

See ``README.md`` in this package and
``docs/specs/SINGLE_STOCK_HISTORICAL_BACKFILL.md``.

Directory-name note: the work was scoped as
``market_data/single-stock-historical-backfill/``, but a hyphenated
directory is not a valid Python package name and every collector in
this repository is an underscore package imported as
``python -m market_data.<collector>`` (``nasdaq_collector``,
``crypto_collector``). The package is therefore
``single_stock_historical_backfill``; the R2 paths it publishes do
not carry the collector name, so nothing downstream sees the difference.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
