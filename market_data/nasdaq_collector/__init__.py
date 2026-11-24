"""NASDAQ-100 snapshot collector.

Publishes ``nasdaq/latest.json`` plus a ``snapshot_provenance.v1`` sidecar
(``nasdaq/latest.provenance.json``) to R2, hourly, from public sources only:
Nasdaq's official quote API for prices, Yahoo Finance for candles and
Google News RSS for per-ticker headlines. No account or auth.

Run from the repository root::

    python -m market_data.nasdaq_collector collect --out nasdaq-out
    python -m market_data.nasdaq_collector collect --publish   # opt-in R2 write
    python -m market_data.nasdaq_collector universe

See ``README.md`` in this package and ``docs/specs/NASDAQ_SNAPSHOT.md``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.0"
