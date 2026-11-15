"""One public, read-only QQQ chart request per open comparison cycle."""

from datetime import datetime, timedelta, timezone
from math import isfinite
from types import SimpleNamespace

import requests

from .clock import ET
from .exchange_calendar import session_close
from .durability import atomic_json
import json

SOURCE = "https://query1.finance.yahoo.com/v8/finance/chart/QQQ"


def ingest(payload, received, existing):
    chart = payload["chart"]["result"][0]
    meta = chart["meta"]
    if meta["symbol"] != "QQQ" or meta["dataGranularity"] != "5m":
        raise ValueError("unexpected chart identity")
    stamps = chart["timestamp"]
    if not stamps or stamps != sorted(set(stamps)):
        raise ValueError("unordered or empty chart")
    # A later timestamp witnesses rollover. Never consume the forming row.
    q = chart["indicators"]["quote"][0]
    day = received.astimezone(ET).date()
    for i in reversed(range(len(stamps) - 1)):
        start = datetime.fromtimestamp(stamps[i], timezone.utc)
        end = start + timedelta(minutes=5)
        local = start.astimezone(ET)
        if local.date() != day or (local.hour, local.minute) < (9, 30):
            continue
        if (
            local.minute % 5
            or local.second
            or end > received
            or (received - end).total_seconds() > 360
        ):
            continue
        if end.astimezone(ET).time() > session_close(day):
            continue
        if existing and existing[-1]["event_end"] >= end.isoformat():
            return
        values = {k: q[k][i] for k in ("open", "high", "low", "close")}
        if any(v is None or not isfinite(v) or v <= 0 for v in values.values()):
            return
        if (
            not values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
        ):
            return
        existing.append(
            dict(
                values,
                event_start=start.isoformat(),
                event_end=end.isoformat(),
                available_at=received.isoformat(),
                session=day.isoformat(),
            )
        )
        return


class ForwardBars:
    def __init__(self, directory):
        self.directory = directory
        self.cycle = None
        self.rows = []
        self.error = None

    def read(self, cycle, now):
        if cycle == self.cycle:
            return self.rows
        self.cycle = cycle
        path = self.directory / ("bars-" + now.date().isoformat() + ".json")
        self.rows = json.loads(path.read_text()) if path.exists() else []
        self.error = None
        try:
            response = requests.get(
                SOURCE,
                params={"range": "1d", "interval": "5m", "includePrePost": "false"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=4,
                allow_redirects=False,
            )
            response.raise_for_status()
            before = len(self.rows)
            ingest(response.json(), datetime.now(timezone.utc), self.rows)
            if len(self.rows) != before:
                atomic_json(path, self.rows)
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
        ) as exc:
            self.error = type(exc).__name__
        return self.rows


def direction(rows, position, now):
    from .skew_direction import ChallengerDirection

    tracker = ChallengerDirection(position)
    latest = None
    for raw in rows:
        times = {
            key: datetime.fromisoformat(raw[key])
            for key in ("event_start", "event_end", "available_at")
        }
        if times["available_at"] > now or raw["session"] != position.session:
            continue
        bar = SimpleNamespace(
            **{**raw, **times},
            quality_flags=(),
            session_close=datetime.combine(now.date(), session_close(now.date()), ET)
        )
        latest = tracker.step(bar, now)
    return latest
