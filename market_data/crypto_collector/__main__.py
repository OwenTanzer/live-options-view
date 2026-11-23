"""Run the crypto collector: python -m market_data.crypto_collector."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
