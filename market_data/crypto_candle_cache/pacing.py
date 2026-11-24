"""Request pacing and checkpoint/resume for the crypto candle cache.

Three budgets, three shapes — the limiter counts requests per
source, because the budgets are per provider, not per coin:

- Tiingo free Starter: 50 requests/hour, 1,000/day, shared with
  the stock backfill job and every other user of the same token.
  The limiter counts requests in the last hour/day and sleeps
  until the oldest request ages out of the hour window. A 429 is
  retried once after the server's Retry-After (capped at one
  hour); a second 429 aborts the run with the checkpoint intact.
- Coinbase public candles: no key and no documented quota; the
  client spaces requests (see :mod:`coinbase`) and a 429 aborts
  the run the same way. The parallel Crypto Backend job made 579
  Coinbase requests in one night without hitting a limit, but
  that is an observation, not a quota.
- CoinGecko public OHLC: no key, rate-limited; the client spaces
  requests (2s default) and a 429 aborts the run.

The checkpoint is local state only and is never published:
``<out>/.cache-checkpoint.json`` holds the per-source request log
(last 25 hours) and per-coin, per-base progress (``status``,
``bars``, ``first_bar``, ``last_bar``, ``requests``,
``completed_at``). Writes are atomic (temp file + rename) after
every coin+base. A run resumes by skipping completed coins. The
limiter's memory is the checkpoint file: two runs with different
``--out`` directories do not share budget knowledge.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc

TIINGO_HOURLY = 50
TIINGO_DAILY = 1000
WINDOW_HOURS = 25


def _now():
    return datetime.now(UTC)


class RateLimiter:
    """Per-source request limiter backed by a checkpoint request log.

    ``limits`` is ``{source: (hourly, daily)}``. ``acquire(source)``
    blocks (sleeping) until the source is inside both budgets, then
    records the request. ``sleep`` is injectable for tests.
    """

    def __init__(self, log=None, limits=None, sleep=time.sleep, now=_now):
        self.log = log if log is not None else {}
        self.limits = limits or {"tiingo": (TIINGO_HOURLY, TIINGO_DAILY)}
        self._sleep = sleep
        self._now = now

    def _prune(self, source, now):
        entries = [e for e in self.log.get(source, [])
                   if now - datetime.fromisoformat(e) < timedelta(hours=WINDOW_HOURS)]
        self.log[source] = entries
        return entries

    def acquire(self, source):
        """Wait until ``source`` is inside budget, then record a request."""
        if source not in self.limits:
            self.log.setdefault(source, []).append(self._now().isoformat())
            return
        hourly, daily = self.limits[source]
        while True:
            now = self._now()
            entries = self._prune(source, now)
            hour = [e for e in entries
                    if now - datetime.fromisoformat(e) < timedelta(hours=1)]
            day = [e for e in entries
                   if now - datetime.fromisoformat(e) < timedelta(days=1)]
            if len(hour) < hourly and len(day) < daily:
                entries.append(now.isoformat())
                self.log[source] = entries
                return
            oldest = min(datetime.fromisoformat(e) for e in hour) if len(hour) >= hourly \
                else min(datetime.fromisoformat(e) for e in day)
            wait = ((oldest + timedelta(hours=1)) - now).total_seconds() \
                if len(hour) >= hourly else \
                ((oldest + timedelta(days=1)) - now).total_seconds()
            self._sleep(max(wait, 0) + 1)


class CheckpointStore:
    """Atomic JSON checkpoint: request log + per-coin progress."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"requests": {}, "coins": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def record_base(self, symbol, interval, status, bars=None, first_bar=None,
                    last_bar=None, requests=0):
        """Record one coin+base outcome and save atomically."""
        coin = self.data["coins"].setdefault(symbol, {})
        coin[interval] = {
            "status": status,
            "bars": bars,
            "first_bar": first_bar,
            "last_bar": last_bar,
            "requests": requests,
            "completed_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.save()

    def completed(self, symbol, interval):
        """True when a coin+base is recorded complete."""
        entry = self.data["coins"].get(symbol, {}).get(interval, {})
        return entry.get("status") == "complete"
