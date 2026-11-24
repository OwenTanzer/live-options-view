"""CoinGecko public OHLC — the daily base for CoinGecko-labelled coins.

Endpoint: ``GET https://api.coingecko.com/api/v3/coins/<id>/ohlc``
with ``vs_currency=usd`` and ``days`` (1/7/14/30/90/180/365/max).
No key. A response is a list of ``[epoch_ms, open, high, low,
close]`` — no volume, ever.

Granularity is server-chosen by ``days`` and was verified live
2026-11-24: ``days=7`` for bitcoin returns bars 4 hours apart, so
only the long windows map cleanly onto a calendar base — ``days=365``
returns daily bars (the parallel Crypto Backend job's TRX read:
366 daily bars, 2025-11-25 → 2026-11-24). This module therefore
offers **only the 1d base** (``days=365``). CoinGecko-labelled coins
have no 1m/1h base here, and no intraday interval derived from one:
their 3m/5m/9m/15m/30m/1h/2h/4h files are absent, and the
provenance sidecar records them as ``coverage: "absent"`` with the
reason ``"CoinGecko free OHLC has no 1m/1h series"``. A missing
interval is a labelled gap, never a bar copied from another source
or an interpolated price. The 1w file is derived from the 1d base.

CoinGecko's public API is rate-limited; the client spaces requests
and backs off on HTTP 429. ``days=max`` (full history) is not used:
it is the most expensive call and its depth varies by coin, so the
documented window is one year and older history for these coins is
a Tiingo question (see :mod:`tiingo`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.coingecko.com/api/v3/coins"
DAYS = 365  # the only window this module requests: daily bars

USER_AGENT = "live-options-view/crypto-candle-cache (CoinGecko public OHLC, no key)"


class CoinGeckoError(RuntimeError):
    """A CoinGecko request failed (HTTP status or malformed payload)."""


class CoinGeckoRateLimited(CoinGeckoError):
    """HTTP 429: back off; the checkpoint keeps whatever was fetched."""


def parse_ohlc_row(row):
    """Parse one ``[epoch_ms, open, high, low, close]`` row.

    Returns a daily bar with ``t`` midnight UTC. No ``volume`` key:
    CoinGecko OHLC carries none.
    """
    epoch_ms, open_, high, low, close = row
    start = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
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

    def fetch_daily(self, coingecko_id, days=DAYS):
        """Fetch the 1d base for one CoinGecko id.

        Returns ``(bars, requests)`` with bars sorted ascending by
        ``t`` and deduped by ``t``.
        """
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
        bars = [seen[key] for key in sorted(seen)]
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return bars, 1
