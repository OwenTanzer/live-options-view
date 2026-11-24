"""Run the single-stock historical backfill collector with
``python -m market_data.single_stock_historical_backfill``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
