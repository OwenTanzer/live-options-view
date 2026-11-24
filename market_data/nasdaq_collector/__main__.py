"""Run the NASDAQ-100 collector with ``python -m market_data.nasdaq_collector``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
