"""Module entry point: ``python -m market_data.crypto_candle_cache``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
