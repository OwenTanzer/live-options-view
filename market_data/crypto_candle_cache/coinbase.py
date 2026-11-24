"""Coinbase Exchange public candles (no key, no account).

Endpoint: ``GET https://api.exchange.coinbase.com/products/<SYM>-USD/candles``
with ``granularity`` in seconds (60, 300, 900, 3600, 21600, 86400)
and optional ``start``/``end`` ISO-8601 timestamps. A response is a
list of ``[epoch_seconds, low, high, open, close, volume]`` — note
the order: low/high come before open/close — newest bar first, at
most **300 bars** per request. A window holding more than 300 bars
is refused with HTTP 400, not truncated (caught live 2026-11-24:
a 1-minute request over 24 hours failed outright), so
``fetch_history`` pages in capped 300-bar windows from the start.

Verified live 2026-11-24 ~00:24 UTC: ``BTC-USD`` hourly bars
returned (latest bar 2026-11-24T00:00:00Z, close 61204.79), and the
``USDS-USD`` and ``USD1-USD`` tickers both resolve, confirming the
two Coinbase-labelled additions to the 50-coin universe. Deep
paging was verified by the parallel Crypto Backend job the same
night: BTC daily bars walk back to 2015-07-20 (4,145 bars) via
repeated ``start``/``end`` windows, so Coinbase — not Tiingo — is
the depth source for Coinbase-labelled coins.

Paging mirrors the stock backfill's Tiingo walk: each page sets
``end`` to the earliest bar start of the previous page and pages
merge deduped by timestamp. A page that adds no new bars ends the
walk (guards the case where the history is an exact multiple of
300). Coinbase bars carry ``volume`` on every interval, unlike
Tiingo intraday bars — so a crypto bar always carries ``volume``,
and a derived bar's volume is the sum of its base bars.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.exchange.coinbase.com"
PAGE_CAP = 300

GRANULARITY_SECONDS = {"1m": 60, "1h": 3600, "1d": 86400}
BASE_INTERVALS = ("1m", "1h", "1d")

USER_AGENT = "live-options-view/crypto-candle-cache (public candles, no key)"


class CoinbaseError(RuntimeError):
    """A Coinbase request failed (HTTP status or malformed payload)."""


class CoinbaseRateLimited(CoinbaseError):
    """HTTP 429: back off; the checkpoint keeps whatever was fetched."""


def _format_time(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_candle_row(row):
    """Parse one ``[epoch, low, high, open, close, volume]`` row.

    Returns a bar ``{"t", "open", "high", "low", "close", "volume"}``
    with ``t`` the bar start in ISO-8601 UTC. The One Clock fields
    (``observed_at``, ``origin``) are attached by the caller, which
    knows the fetch time — never invented here.
    """
    epoch, low, high, open_, close, volume = row
    start = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return {
        "t": _format_time(start),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


class CoinbaseClient:
    """Minimal Coinbase candles client (stdlib only, no key).

    ``session`` is an injectable object with a ``get(url)`` returning
    an object with ``status_code`` and ``json()`` (tests pass a fake);
    the default uses :mod:`urllib.request` with a polite delay
    between requests.
    """

    def __init__(self, session=None, delay_seconds=0.2):
        self.session = session
        self.delay_seconds = delay_seconds
        self.requests = 0

    def _get(self, url):
        self.requests += 1
        if self.session is not None:
            response = self.session.get(url)
            if response.status_code == 429:
                raise CoinbaseRateLimited("Coinbase HTTP 429")
            if response.status_code != 200:
                raise CoinbaseError(f"Coinbase HTTP {response.status_code}: {url}")
            return response.json()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise CoinbaseRateLimited("Coinbase HTTP 429") from exc
            raise CoinbaseError(f"Coinbase HTTP {exc.code}: {url}") from exc

    def has_product(self, product):
        """Does Coinbase Exchange list ``product`` (e.g. ``"USDD-USD"``)?

        One ticker read: HTTP 200 means the pair answers. Any
        failure (404/400 for a missing product, or a transport
        error) returns False — the caller then prices the coin from
        CoinGecko instead, which still publishes bars rather than
        dropping the coin. Only entrants are probed (see
        :func:`collector.resolve_universe`).
        """
        try:
            self._get(f"{BASE_URL}/products/{product}/ticker")
        except CoinbaseError:
            return False
        return True

    def fetch_window(self, product, interval, start, end):
        """Fetch one window (at most 300 bars), oldest-first.

        ``start``/``end`` are datetimes; ``end`` is exclusive in
        effect (Coinbase returns bars with start < end). Returns
        ``(bars, requests)`` with bars sorted ascending by ``t``.
        """
        seconds = GRANULARITY_SECONDS[interval]
        params = {
            "granularity": str(seconds),
            "start": _format_time(start),
            "end": _format_time(end),
        }
        url = (f"{BASE_URL}/products/{product}/candles?"
               + urllib.parse.urlencode(params))
        payload = self._get(url)
        if not isinstance(payload, list):
            raise CoinbaseError(f"unexpected Coinbase payload for {product} {interval}")
        bars = [parse_candle_row(row) for row in payload]
        bars.sort(key=lambda b: b["t"])
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return bars, 1

    def fetch_history(self, product, interval, start, end):
        """Walk backward from ``end`` to ``start``, 300 bars a page.

        The page window is capped at 300 bars (``PAGE_CAP`` × the
        granularity): Coinbase answers a wider window with HTTP 400
        instead of truncating it — a 1-minute request over 24 hours
        (1,440 bars) fails outright (caught live 2026-11-24), so the
        walk advances in capped windows from the start. Returns
        ``(bars, requests)`` with bars deduped by ``t`` and sorted
        ascending.
        """
        seconds = GRANULARITY_SECONDS[interval]
        max_span = timedelta(seconds=seconds * PAGE_CAP)
        seen = {}
        requests = 0
        window_end = end
        while True:
            window_start = max(start, window_end - max_span)
            bars, made = self.fetch_window(
                product, interval, window_start, window_end)
            requests += made
            new = 0
            for bar in bars:
                if bar["t"] not in seen:
                    seen[bar["t"]] = bar
                    new += 1
            if not bars or new == 0:
                break
            earliest = min(bar["t"] for bar in bars)
            earliest_dt = datetime.fromisoformat(
                earliest.replace("Z", "+00:00"))
            if earliest_dt <= start:
                break
            # Consecutive pages can overlap on the boundary day;
            # dedupe by timestamp makes the merge exact.
            window_end = earliest_dt
            if requests > 10000:  # pragma: no cover - runaway guard
                raise CoinbaseError("Coinbase paging walk exceeded 10000 requests")
        ordered = [seen[key] for key in sorted(seen)
                   if key >= _format_time(start)]
        return ordered, requests
