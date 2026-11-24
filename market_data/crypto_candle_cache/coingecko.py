"""CoinGecko public OHLC. A 30-day request returns four-hour bars labelled
by their close. Assemble a UTC daily candle only from all six constituents.
See https://docs.coingecko.com/reference/coins-id-ohlc .
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .resample import _aggregate

BASE_URL = "https://api.coingecko.com/api/v3/coins"
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
DAYS = 30  # Public OHLC returns four-hour bodies at this horizon.

USER_AGENT = "live-options-view/crypto-candle-cache (CoinGecko public OHLC, no key)"


class CoinGeckoError(RuntimeError):
    """A CoinGecko request failed (HTTP status or malformed payload)."""


class CoinGeckoRateLimited(CoinGeckoError):
    """HTTP 429: back off; the checkpoint keeps whatever was fetched."""


def parse_ohlc_row(row):
    """Parse one ``[epoch_ms, open, high, low, close]`` row.

    Returns a four-hour bar with its UTC opening time. No ``volume`` key:
    CoinGecko OHLC carries none.
    """
    epoch_ms, open_, high, low, close = row
    start = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc) - timedelta(hours=4)
    return {
        "t": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


class CoinGeckoClient:
    """Minimal CoinGecko OHLC client (stdlib only, no key).

    ``session`` is injectable like :class:`coinbase.CoinbaseClient`'s.
    """

    def __init__(self, session=None, delay_seconds=2.0):
        self.session = session
        self.delay_seconds = delay_seconds
        self.requests = 0

    def fetch_top_markets(self, per_page=100):
        """Fetch the CoinGecko top-markets list (no id filter).

        Returns the raw row list, ordered by market cap — the list
        :func:`coins.parse_top_markets` turns into the live top 50.
        Raises :class:`CoinGeckoError` on a non-200 response or a
        non-list payload; the caller falls back to the embedded
        universe rather than publishing a bad one.
        """
        params = {"vs_currency": "usd", "order": "market_cap_desc",
                  "per_page": str(per_page), "page": "1",
                  "price_change_percentage": "1h,24h,7d"}
        url = (f"{MARKETS_URL}?" + urllib.parse.urlencode(params))
        self.requests += 1
        if self.session is not None:
            response = self.session.get(url)
            if response.status_code == 429:
                raise CoinGeckoRateLimited("CoinGecko HTTP 429")
            if response.status_code != 200:
                raise CoinGeckoError(f"CoinGecko HTTP {response.status_code}")
            payload = response.json()
        else:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise CoinGeckoRateLimited("CoinGecko HTTP 429") from exc
                raise CoinGeckoError(f"CoinGecko HTTP {exc.code}") from exc
        if not isinstance(payload, list):
            raise CoinGeckoError("unexpected CoinGecko top-markets payload")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return payload

    def fetch_daily(self, coingecko_id, days=DAYS):
        """Fetch the 1d base for one CoinGecko id.

        Returns ``(bars, requests)`` with bars sorted ascending by
        ``t`` and deduped by ``t``.
        """
        if days != DAYS:
            raise ValueError("daily aggregation requires the 30-day four-hour OHLC response")
        params = {"vs_currency": "usd", "days": str(days)}
        url = (f"{BASE_URL}/{urllib.parse.quote(coingecko_id)}/ohlc?"
               + urllib.parse.urlencode(params))
        self.requests += 1
        if self.session is not None:
            response = self.session.get(url)
            if response.status_code == 429:
                raise CoinGeckoRateLimited("CoinGecko HTTP 429")
            if response.status_code != 200:
                raise CoinGeckoError(f"CoinGecko HTTP {response.status_code}")
            payload = response.json()
        else:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise CoinGeckoRateLimited("CoinGecko HTTP 429") from exc
                raise CoinGeckoError(f"CoinGecko HTTP {exc.code}") from exc
        if not isinstance(payload, list):
            raise CoinGeckoError(f"unexpected CoinGecko payload for {coingecko_id}")
        seen = {}
        for row in payload:
            bar = parse_ohlc_row(row)
            seen[bar["t"]] = bar
        bars = []
        for bar in _aggregate(seen.values(), 240, 1440):
            if not bar.get("partial"):
                del bar["bars"]
                bars.append(bar)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return bars, 1
