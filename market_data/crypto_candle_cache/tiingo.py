"""Tiingo crypto prices — the deep-history backstop (token-authenticated).

Endpoint: ``GET https://api.tiingo.com/tiingo/crypto/prices`` with
``tickers`` (lowercase, e.g. ``btcusd``), ``startDate``/``endDate``
(``YYYY-MM-DD``) and ``resampleFreq`` (``1min``/``1hour``/``1day``),
token as the ``token`` query param from the environment only
(``TIINGO_API_TOKEN`` — never a literal in this tree).

Route status, stated honestly: the route itself was confirmed to
exist on 2026-11-24 (an unauthenticated request returns
``{"detail": "Please supply a token"}``, not a 404), but **no
authenticated Tiingo crypto call has been made from this
repository** — the parallel Crypto Backend job has made 0 Tiingo
requests so far, and the shared free Starter budget (50/hour,
1,000/day, shared with the stock backfill job) is not spent here
to prove a shape the job will prove once. The bar shape below is
therefore Tiingo's documented crypto-prices shape, and the parser
is defensive: a missing ``volume`` is stored as no volume key, not
null, and an unknown field is ignored. The first authenticated
call must be recorded in the spec's verification section before
any Tiingo-sourced bar is treated as established.

Tiingo crypto history starts December 2017 (per the Crypto Backend
job's design). Its role in this cache is a backstop, not a primary
source: Coinbase-labelled coins fetch from Coinbase first (whose
paging reaches 2015 for BTC daily, verified by the backend job),
and Tiingo is tried only when a Coinbase base fetch fails or
returns fewer bars than the requested window. CoinGecko-labelled
coins use CoinGecko for the daily base (see :mod:`coingecko`) and
Tiingo for intraday depth only where the Tiingo ticker exists.
Which source actually supplied each base is recorded per interval
file in the provenance sidecar — a bar is never silently
relabelled from one provider to another.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.tiingo.com/tiingo/crypto/prices"

RESAMPLE_FREQ = {"1m": "1min", "1h": "1hour", "1d": "1day"}
BASE_INTERVALS = ("1m", "1h", "1d")

USER_AGENT = "live-options-view/crypto-candle-cache (Tiingo crypto, token-authenticated)"


class TiingoError(RuntimeError):
    """A Tiingo request failed (HTTP status or malformed payload)."""


class TiingoNotFound(TiingoError):
    """HTTP 404: Tiingo carries no such crypto ticker."""


class TiingoRateLimited(TiingoError):
    """HTTP 429: the shared Starter budget is exhausted; abort cleanly."""


def token_from_env(env=None):
    """Read the token from the environment only; None when absent."""
    env = os.environ if env is None else env
    return env.get("TIINGO_API_TOKEN") or None


def ticker_for(symbol):
    """Tiingo crypto ticker for a coin symbol (lowercase + usd)."""
    return symbol.lower() + "usd"


def parse_crypto_bar(row):
    """Parse one Tiingo crypto bar.

    Documented shape: ``{"date", "open", "high", "low", "close",
    "volume", ...}`` with ``date`` ISO-8601. ``volume`` is optional
    in the parser: Tiingo crypto bars may omit it, and an omitted
    volume is stored as no ``volume`` key — never null, never zero.
    """
    text = str(row["date"]).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    bar = {
        "t": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }
    if row.get("volume") is not None:
        bar["volume"] = float(row["volume"])
    return bar


class TiingoCryptoClient:
    """Minimal Tiingo crypto client (stdlib only, token from env).

    ``session`` is injectable like :class:`coinbase.CoinbaseClient`'s.
    The client makes no request without a token.
    """

    def __init__(self, token=None, session=None, delay_seconds=0.2):
        self.token = token if token is not None else token_from_env()
        self.session = session
        self.delay_seconds = delay_seconds
        self.requests = 0

    def fetch_bars(self, symbol, interval, start_date, end_date):
        """Fetch one interval for one coin over a date window.

        ``start_date``/``end_date`` are ``YYYY-MM-DD`` strings.
        Returns ``(bars, requests)`` with bars sorted ascending by
        ``t``. Raises :class:`TiingoError` without a token, and
        :class:`TiingoNotFound` when Tiingo carries no such ticker.
        """
        if not self.token:
            raise TiingoError("TIINGO_API_TOKEN is not set; Tiingo backstop unavailable")
        params = {
            "tickers": ticker_for(symbol),
            "startDate": start_date,
            "endDate": end_date,
            "resampleFreq": RESAMPLE_FREQ[interval],
            "token": self.token,
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        self.requests += 1
        if self.session is not None:
            response = self.session.get(url)
            if response.status_code == 404:
                raise TiingoNotFound(f"Tiingo has no ticker {ticker_for(symbol)}")
            if response.status_code == 429:
                raise TiingoRateLimited("Tiingo HTTP 429")
            if response.status_code != 200:
                raise TiingoError(f"Tiingo HTTP {response.status_code}")
            payload = response.json()
        else:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise TiingoNotFound(
                        f"Tiingo has no ticker {ticker_for(symbol)}") from exc
                if exc.code == 429:
                    raise TiingoRateLimited("Tiingo HTTP 429") from exc
                raise TiingoError(f"Tiingo HTTP {exc.code}") from exc
        if not isinstance(payload, list):
            raise TiingoError(f"unexpected Tiingo payload for {symbol} {interval}")
        bars = [parse_crypto_bar(row) for row in payload]
        bars.sort(key=lambda b: b["t"])
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return bars, 1
