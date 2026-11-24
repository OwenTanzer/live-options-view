"""Hourly crypto price collector: public Coinbase/CoinGecko snapshot.

Run ``python -m market_data.crypto_collector`` from the repository
root; [__main__.py](__main__.py) delegates to [cli.py](cli.py).
Railway service configuration (new service; build/start/watch
settings are service-managed, like the live collector) uses the
[shared Python image](../../deployment/python/README.md), no cron
in the Railway sense -- the ``loop`` command repeats hourly itself
because crypto trades 24/7 -- and restart policy ``NEVER``.

No credentials: the collector reads only public endpoints. It never
trades, never stores favourites or alerts, and never touches the
private BTC quote lane (``market_data/live_market/btc_options.py``).
"""

from .coins import COINS

__all__ = ["COINS"]
