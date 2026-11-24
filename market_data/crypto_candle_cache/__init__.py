"""Crypto candle cache — R2-backed historical candles for the top-50 crypto.

The package was scoped as ``market_data/crypto-candle-cache/``,
but a hyphenated directory is not a valid Python package name, and
every collector in this repository is an underscore package
imported as ``python -m market_data.<collector>``
(``crypto_collector``, ``nasdaq_collector``,
``single_stock_historical_backfill``). The package is therefore
``crypto_candle_cache``. The R2 paths it publishes
(``crypto/candles/<COIN>/<interval>.json``) do not carry the
collector name, so nothing downstream sees the difference.

Entry points: ``python -m market_data.crypto_candle_cache <command>``
(see :mod:`cli`). The contract lives in
``docs/specs/CRYPTO_CANDLE_CACHE.md``.
"""

from __future__ import annotations

__all__ = ["SOURCE", "SESSION"]

#: One Clock adapter id for cached crypto candles.
SOURCE = "crypto_candle_cache"
#: One Clock session for crypto: continuous, 24/7 — no equity session.
SESSION = "continuous"
