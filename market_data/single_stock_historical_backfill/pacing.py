"""Pacing and checkpoint state for the single-stock historical backfill.

Tiingo free Starter limits: **50 requests/hour, 1,000/day, 500 unique
symbols/month**. A full backfill is 606 requests (6 per listing), so the
first fill needs at least 606 / 50 = 12.12 hours of paced requests; the
daily cap (606 < 1,000) does not bind a single backfill, but it does
bind a second full backfill on the same day — which is why completed
symbols are checkpointed and never re-fetched.

Two pieces of state, both in one JSON file (default
``<out>/.backfill-checkpoint.json``):

- ``requests``: ISO-8601 UTC timestamps of every Tiingo request this
  collector has made, pruned to the last 25 hours on save. The
  rate limiter counts how many fall inside the last hour / last day
  and sleeps until the oldest one ages out of the hour window.
- ``symbols``: per-ticker, per-base progress — ``status``
  (``complete`` / ``source_unavailable`` / ``failed``), ``pages``,
  ``bars``, ``first_bar``, ``last_bar``, ``requests`` and
  ``completed_at``. A run resumes by skipping completed symbols, so a
  killed or rate-limited run picks up where it stopped.

The checkpoint is written atomically (temporary file + rename) after
every symbol+base, so a kill mid-run loses at most one page. It is
local state only: it is never published to R2.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

HOURLY_LIMIT = 50
DAILY_LIMIT = 1000
CHECKPOINT_VERSION = 1

UTC = timezone.utc


def _parse_time(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_time(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RateLimiter:
    """Enforce the Tiingo free-Starter request budgets.

    ``now`` and ``sleep`` may be injected (tests pass a fake clock);
    otherwise the real clock and ``time.sleep`` are used. ``acquire()``
    blocks until a request is allowed, then records it; call it once
    per HTTP request, immediately before the request is made.
    """

    def __init__(self, hourly_limit=HOURLY_LIMIT, daily_limit=DAILY_LIMIT,
                 requests=None, now=None, sleep=None):
        self.hourly_limit = hourly_limit
        self.daily_limit = daily_limit
        self.requests = [(_parse_time(t) if isinstance(t, str) else t)
                         for t in (requests or [])]
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep

    def _prune(self, now):
        cutoff = now - timedelta(hours=25)
        self.requests = [t for t in self.requests if t > cutoff]

    def counts(self, now=None):
        """``(used_in_last_hour, used_in_last_day)`` at ``now``."""
        now = now or self._now()
        hour_start = now - timedelta(hours=1)
        day_start = now - timedelta(days=1)
        hour_used = sum(1 for t in self.requests if t > hour_start)
        day_used = sum(1 for t in self.requests if t > day_start)
        return hour_used, day_used

    def wait_seconds(self, now=None):
        """Seconds until the next request is allowed (0 when allowed now).

        The hourly budget binds first in practice: when the last hour
        is full, the wait is until the oldest request in the window
        ages out, plus one second. The daily budget binds only after
        1,000 requests in a day; its wait is computed the same way.
        """
        now = now or self._now()
        hour_used, day_used = self.counts(now)
        wait = 0.0
        if hour_used >= self.hourly_limit:
            window = sorted(t for t in self.requests
                            if t > now - timedelta(hours=1))
            oldest = window[0]
            wait = max(wait, (oldest + timedelta(hours=1) - now).total_seconds() + 1)
        if day_used >= self.daily_limit:
            window = sorted(t for t in self.requests
                            if t > now - timedelta(days=1))
            oldest = window[0]
            wait = max(wait, (oldest + timedelta(days=1) - now).total_seconds() + 1)
        return wait

    def acquire(self):
        """Block until a request is allowed, then record it.

        Returns the seconds waited. Raises RuntimeError if the wait
        would exceed 25 hours (a misconfigured limit, not a real
        budget: the hourly wait is at most ~72 seconds per request
        once the bucket is full, and the daily wait at most ~86 seconds
        per request at the daily cap — both are bounded sleeps, not
        deadlocks).
        """
        now = self._now()
        self._prune(now)
        wait = self.wait_seconds(now)
        if wait > 25 * 3600:
            raise RuntimeError(f"rate-limit wait of {wait:.0f}s exceeds 25h; refusing")
        if wait > 0:
            self._sleep(wait)
        moment = self._now()
        self.requests.append(moment)
        self._prune(moment)
        return wait

    def budget_report(self, now=None):
        """JSON-friendly budget snapshot for ``status``."""
        now = now or self._now()
        hour_used, day_used = self.counts(now)
        return {
            "hourly_limit": self.hourly_limit,
            "hour_used": hour_used,
            "hour_remaining": max(0, self.hourly_limit - hour_used),
            "daily_limit": self.daily_limit,
            "day_used": day_used,
            "day_remaining": max(0, self.daily_limit - day_used),
            "next_request_in_seconds": round(self.wait_seconds(now), 1),
            "as_of": _format_time(now),
        }

    def serialised_requests(self):
        return [_format_time(t) for t in sorted(self.requests)]


class CheckpointStore:
    """The backfill checkpoint file (rate-limit log + per-symbol progress).

    ``path`` is the checkpoint file. ``load()`` tolerates a missing file
    (fresh run) but fails loudly on a corrupt one — silently restarting
    a 12-hour backfill would burn the rate budget twice. ``save()`` is
    atomic.
    """

    def __init__(self, path, limiter=None):
        self.path = str(path)
        self.limiter = limiter or RateLimiter()
        self.symbols = {}

    def load(self):
        if not os.path.exists(self.path):
            return self
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
            raise RuntimeError(f"unsupported checkpoint file: {self.path}")
        self.limiter = RateLimiter(
            hourly_limit=data.get("hourly_limit", HOURLY_LIMIT),
            daily_limit=data.get("daily_limit", DAILY_LIMIT),
            requests=data.get("requests", []),
            now=self.limiter._now, sleep=self.limiter._sleep)
        self.symbols = data.get("symbols", {}) or {}
        return self

    def save(self):
        data = {
            "version": CHECKPOINT_VERSION,
            "hourly_limit": self.limiter.hourly_limit,
            "daily_limit": self.limiter.daily_limit,
            "requests": self.limiter.serialised_requests(),
            "symbols": self.symbols,
            "updated_at": _format_time(datetime.now(UTC)),
        }
        tmp_path = self.path + ".tmp"
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)

    def is_complete(self, symbol, base):
        entry = (self.symbols.get(symbol) or {}).get(base) or {}
        return entry.get("status") == "complete"

    def mark_base(self, symbol, base, status, pages=0, bars=0,
                  first_bar=None, last_bar=None, requests=0):
        entry = self.symbols.setdefault(symbol, {})
        entry[base] = {
            "status": status,
            "pages": pages,
            "bars": bars,
            "first_bar": first_bar,
            "last_bar": last_bar,
            "requests": requests,
            "completed_at": _format_time(datetime.now(UTC)),
        }

    def symbol_status(self, symbol):
        return self.symbols.get(symbol) or {}

    def summary(self):
        """JSON-friendly progress snapshot for ``status``."""
        complete = sum(
            1 for symbol in self.symbols
            if all((self.symbols[symbol].get(base) or {}).get("status") == "complete"
                   for base in ("1m", "1h", "1d")))
        return {
            "symbols_tracked": len(self.symbols),
            "symbols_complete": complete,
            "symbols": self.symbols,
        }
