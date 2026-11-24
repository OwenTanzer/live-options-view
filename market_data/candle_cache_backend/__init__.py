"""Candle-cache backend: local cache reader/writer for the R2 candle layout.

Reads and writes the layout published by
``market_data/single_stock_historical_backfill/`` (PR #393, unmerged at
build time): ``nasdaq/candles/<TICKER>/<interval>.json`` plus one
``snapshot_provenance.v1`` sidecar per ticker. Every cached bar is
stamped with the One Clock observation envelope (see ``envelope.py``)
and selected as-of a chosen time with the mirrored selector (see
``selector.py``). R2 publication is opt-in only and takes its
configuration from the environment; nothing here publishes, schedules
or deploys anything.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
