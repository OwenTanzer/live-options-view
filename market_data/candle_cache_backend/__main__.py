"""Module entry point: ``python -m market_data.candle_cache_backend``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
