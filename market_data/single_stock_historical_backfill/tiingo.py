"""Tiingo client for the single-stock historical backfill.

Read-only. The token comes from the environment only
(``TIINGO_API_TOKEN``); it is never stored in the repository, logged,
or written to any file. The token is sent in the Authorization header.

Endpoints used (``https://api.tiingo.com``):

- intraday: ``/iex/{ticker}/prices`` with ``startDate``,
  ``endDate`` and ``resampleFreq`` (``1min`` / ``1hour``).
- daily: ``/tiingo/daily/{ticker}/prices`` with ``startDate``,
  ``endDate``.

Verified live 2026-11-23 (see ``docs/specs/SINGLE_STOCK_HISTORICAL_BACKFILL.md``):

- A response holds at most **10,000 bars**. When the requested window
  holds more, Tiingo returns the **newest** 10,000 bars — a
  ``startDate``-only request for 1-minute bars from 2026-08-10 returned
  bars starting 2026-10-19, not 2026-08-10. Paging therefore walks
  **backward**: each page sets ``endDate`` to the first (earliest) bar
  date of the previous page, and bars are merged/deduped by timestamp.
  The ``endDate`` day is returned in full, so consecutive pages overlap
  on that day; dedupe by timestamp is what makes the merge exact.
- Omitting ``startDate`` does **not** mean "from the beginning": a
  1-minute request with no ``startDate`` returned only the most recent
  session day (390 bars for AAPL on 2026-11-23). The 1-minute walk
  therefore always sends an explicit ``startDate`` (today minus 120
  calendar days, ~85 trading days — comfortably beyond the three-page
  reach). A placeholder such as ``0000-01-01`` is rejected with
  HTTP 400, so no placeholder is ever sent.
- 1-minute bars: 390 per trading day (09:30-16:00 ET, regular session
  only), labelled by minute start in UTC. Three pages reach back
  ~75 trading days.
- Hourly bars: wall-clock-hour buckets labelled by hour start in UTC
  (~6 per trading day); 2017-01-01 to 2026-11-23 is 15,478 bars in
  2 pages (10,000 + 5,478).
- Daily bars: one request from 2000-01-01 returned 6,764 bars for
  AAPL (first 2000-01-03, last 2026-11-23, close 349.08). Daily bars
  carry ``volume`` and split-adjusted fields; intraday bars do not.
- Intraday bars have keys ``date``/``open``/``high``/``low``/``close``
  only — **no ``volume`` key at all**. Parsed intraday bars therefore
  never carry a volume field; see ``resample.py`` for the derived
  intervals, which inherit that.
- One observed anomaly: the first (oldest) hourly bar of a capped page
  can be a flat stub (O=H=L=C) on a market holiday — 2020-07-03
  (observed Independence Day, market closed) appeared as a single flat
  bar at 16:00Z with O=H=L=C=364.36, the previous close. Bars are
  stored exactly as returned, never dropped or corrected; the stub is
  one bar among 15,478 and resampling carries it as-is.
- Tiingo does **not** carry NDX (404). The index is out of scope here;
  the site's NDX chart stays on Yahoo Finance ``^NDX``.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import math
import requests

BASE_URL = "https://api.tiingo.com"
TOKEN_ENV_VAR = "TIINGO_API_TOKEN"
USER_AGENT = ("live-options-view single-stock-historical-backfill/0.1.0 "
              "(Tiingo free Starter)")

# Tiingo caps a single response at 10,000 bars (verified live 2026-11-23).
PAGE_CAP = 10_000

# Days back the 1-minute walk starts from: 120 calendar days is ~85
# trading days, comfortably beyond the three-page (~75 trading day)
# reach, so the walk is bounded by max_pages, not by the start date —
# while still stopping at the start date if max_pages is ever raised.
ONE_MINUTE_DAYS_BACK = 120

# The three bases Tiingo actually returns. ``earliest`` is the oldest
# date a backfill walks to (None for ``1m`` = today minus
# ONE_MINUTE_DAYS_BACK, computed at run time). ``max_pages`` is None
# where the walk is bounded by ``earliest`` instead. Daily bars are
# one request in practice (6,764 for AAPL from 2000), but the pager
# still guards on PAGE_CAP.
BASE_SPECS = {
    "1m": {"kind": "intraday", "resample": "1min", "earliest": None,
           "max_pages": 3},
    "1h": {"kind": "intraday", "resample": "1hour",
           "earliest": "2017-01-01", "max_pages": None},
    "1d": {"kind": "daily", "resample": None, "earliest": "2000-01-01",
           "max_pages": None},
}

BASES = tuple(BASE_SPECS)


class TiingoError(RuntimeError):
    """A Tiingo request failed (transport, HTTP or malformed payload)."""


class TiingoRateLimited(TiingoError):
    """HTTP 429: the Tiingo rate limit was hit.

    ``retry_after`` is the server's Retry-After in seconds when it sent
    one, else None. The caller (``collector.py``) sleeps and retries
    once, then aborts the run with the checkpoint intact.
    """

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class TiingoNotFound(TiingoError):
    """HTTP 404: Tiingo has no such ticker (e.g. NDX)."""


def read_token(env=None):
    """Read the Tiingo token from the environment only.

    Raises RuntimeError when ``TIINGO_API_TOKEN`` is absent or empty.
    The value is never logged or written anywhere by this module.
    """
    env = os.environ if env is None else env
    token = env.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"missing Tiingo token: set {TOKEN_ENV_VAR} in the environment "
            "(the token is never stored in the repository)")
    return token


def _normalise_time(value):
    """Normalise a Tiingo ``date`` string to ISO-8601 UTC, second precision.

    Tiingo returns e.g. ``"2026-11-23T20:59:00.000Z"`` (intraday) and
    ``"2026-11-23T00:00:00.000Z"`` (daily). Both become
    ``"2026-11-23T20:59:00Z"`` / ``"2026-11-23T00:00:00Z"``.
    """
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_intraday_bar(raw):
    """Require four measured finite prices. Incomplete bars cannot form candles."""
    try:
        values = {key: float(raw[key]) for key in ("open", "high", "low", "close")}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError
        return {"t": _normalise_time(raw["date"]), **values}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TiingoError("Incomplete or invalid Tiingo candle") from None


def parse_daily_bar(raw):
    bar = parse_intraday_bar(raw)
    try:
        if raw.get("volume") is not None:
            bar["volume"] = int(raw["volume"])
        if raw.get("adjClose") is not None:
            bar["adj_close"] = float(raw["adjClose"])
        if raw.get("adjVolume") is not None:
            bar["adj_volume"] = int(raw["adjVolume"])
        return bar
    except (KeyError, TypeError, ValueError) as exc:
        raise TiingoError("Invalid daily candle fields") from None


class TiingoClient:
    """Minimal read-only Tiingo client (``requests`` session).

    ``session`` may be injected (tests pass a fake); otherwise a
    ``requests.Session`` with the collector User-Agent is built.
    ``limiter`` (a ``pacing.RateLimiter``) is acquired once per HTTP
    request, inside ``_get`` — pacing is per request, not per fetch,
    because one fetch can page into several requests. ``today`` (a
    ``datetime.date``) fixes "today" for the 1-minute walk's start
    date; it defaults to the current UTC date. ``requests_made``
    counts every HTTP request this client performs, so provenance can
    record the true cost of a run.
    """

    def __init__(self, token=None, session=None, limiter=None, today=None):
        self.token = token if token is not None else read_token()
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
        self.session = session
        self.limiter = limiter
        self.today = today
        self.requests_made = 0

    def _get(self, path, params):
        """One authenticated GET.

        The rate limiter is acquired once per HTTP request, before the
        request is made. Redirects are refused: a redirect on an
        authenticated request would leak the token to another host.
        """
        if self.limiter is not None:
            self.limiter.acquire()
        params = dict(params)
        headers = {"Authorization": "Token " + self.token}
        url = BASE_URL + path
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=30,
                                    allow_redirects=False)
        except requests.RequestException as exc:
            raise TiingoError(f"Tiingo transport failed ({type(exc).__name__})") from None
        self.requests_made += 1
        if resp.status_code in (301, 302, 303, 307, 308):
            raise TiingoError(f"Tiingo redirected ({resp.status_code}); refused")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise TiingoRateLimited(
                "Tiingo rate limit hit (HTTP 429)",
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)
        if resp.status_code == 404:
            raise TiingoNotFound(f"Tiingo has no data for this request: {path}")
        if resp.status_code != 200:
            raise TiingoError(f"Tiingo HTTP {resp.status_code} for {path}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise TiingoError(f"Tiingo returned non-JSON for {path}") from exc
        if not isinstance(payload, list):
            raise TiingoError(f"Tiingo returned a non-list payload for {path}")
        return payload

    def _fetch_page(self, ticker, base, start_date, end_date=None):
        """Fetch one page (at most PAGE_CAP bars) for ``base``.

        ``start_date``/``end_date`` are ``YYYY-MM-DD`` strings;
        ``start_date`` is always sent (see the ``startDate`` note in
        the module docstring). Returns parsed bars in ascending time
        order.
        """
        spec = BASE_SPECS[base]
        if spec["kind"] == "intraday":
            params = {"resampleFreq": spec["resample"]}
            path = f"/iex/{ticker}/prices"
        else:
            params = {}
            path = f"/tiingo/daily/{ticker}/prices"
        if start_date is not None:
            params["startDate"] = start_date
        if end_date is not None:
            params["endDate"] = end_date
        parse = parse_intraday_bar if spec["kind"] == "intraday" else parse_daily_bar
        return [parse(raw) for raw in self._get(path, params)]

    def fetch_base(self, ticker, base, max_pages=None):
        """Backfill one base dataset for ``ticker``, paging backward.

        Walks from the newest bars toward ``earliest`` (or
        ``max_pages`` pages for ``1m``), merging pages deduped by
        timestamp. ``max_pages`` overrides the spec's page cap when
        given (the CLI exposes it for ``1m`` smoke runs). Returns
        ``(bars, pages)`` with bars sorted ascending by ``t``.
        ``pages`` is the number of HTTP requests used.
        """
        spec = BASE_SPECS[base]
        earliest = spec["earliest"]
        if earliest is None:
            # 1m: today minus ONE_MINUTE_DAYS_BACK. Never omit startDate
            # (Tiingo then returns only the most recent session day) and
            # never send a placeholder (0000-01-01 is HTTP 400).
            today = self.today or datetime.now(timezone.utc).date()
            if isinstance(today, datetime):
                today = today.date()
            earliest = (today - timedelta(days=ONE_MINUTE_DAYS_BACK)).isoformat()
        if max_pages is None:
            max_pages = spec["max_pages"]
        merged = {}
        pages = 0
        end_date = None
        while True:
            page = self._fetch_page(ticker, base, earliest, end_date)
            pages += 1
            new = 0
            for bar in page:
                if bar["t"] not in merged:
                    merged[bar["t"]] = bar
                    new += 1
            if len(page) < PAGE_CAP:
                break  # reached the start of the window (or the data ran out)
            if new == 0:
                break  # a full page added nothing new: nothing older exists
            first_t = min(merged)
            if earliest is not None and first_t[:10] <= earliest:
                break
            if max_pages is not None and pages >= max_pages:
                break
            # Walk backward: the next page ends on the earliest day seen
            # so far. That day is returned in full, so it overlaps the
            # page just merged — dedupe by timestamp makes it exact.
            end_date = first_t[:10]
        bars = [merged[t] for t in sorted(merged)]
        return bars, pages

    def fetch_newer(self, ticker, base, after_t):
        """Fetch bars for ``base`` from the newest stored timestamp, inclusive.

        ``after_t`` is an ISO-8601 UTC timestamp (the newest stored bar).
        Used by incremental ``update`` runs; returns ``(bars, pages)``
        with at most one page in practice (a day's worth of new bars is
        far below PAGE_CAP), but pages defensively like ``fetch_base``.
        """
        spec = BASE_SPECS[base]
        start_date = after_t[:10]
        merged = {}
        pages = 0
        end_date = None
        while True:
            page = self._fetch_page(ticker, base, start_date, end_date)
            pages += 1
            new = 0
            for bar in page:
                if bar["t"] >= after_t and bar["t"] not in merged:
                    merged[bar["t"]] = bar
                    new += 1
            if len(page) < PAGE_CAP:
                break
            if new == 0:
                break  # a full page added nothing new: the window is exhausted
            end_date = min(bar["t"] for bar in page)[:10]
        bars = [merged[t] for t in sorted(merged)]
        return bars, pages
