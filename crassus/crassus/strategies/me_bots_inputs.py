"""Completed QQQ one-minute OHLCV, read through the runner's existing chart origin."""
import json
from datetime import datetime, time
from math import isfinite

import requests

from ..clock import ET
from ..durability import atomic_json
from ..skew_bars import SOURCE
from .me_bots_rules import Bar, MINUTE


def closed_minutes(payload, now):
    chart = payload["chart"]["result"][0]
    if chart["meta"]["symbol"] != "QQQ" or chart["meta"]["dataGranularity"] != "1m":
        raise ValueError("unexpected minute series")
    stamps = chart["timestamp"]
    if stamps != sorted(set(stamps)):
        raise ValueError("minute timestamps are unordered")
    q = chart["indicators"]["quote"][0]
    rows = []
    opening = datetime.combine(now.date(), time(9, 30), ET)
    for i, stamp in enumerate(stamps[:-1]):
        start = datetime.fromtimestamp(stamp, ET)
        if start < opening or start.date() != now.date():
            continue
        end = start + MINUTE
        if end > now or stamps[i + 1] > now.timestamp():
            break
        if start != opening + len(rows) * MINUTE or end.timestamp() > stamps[i + 1]:
            raise ValueError("incomplete regular-session minute sequence")
        numbers = [q[key][i] for key in ("open", "high", "low", "close", "volume")]
        if any(type(x) not in (float, int) or not isfinite(x) for x in numbers):
            raise ValueError("incomplete OHLCV")
        op, high, low, close, volume = numbers
        if not 0 < low <= min(op, close) <= max(op, close) <= high or volume < 0:
            raise ValueError("invalid OHLCV")
        rows.append(Bar(start, end, op, high, low, close, volume))
    return rows


class Inputs:
    """One chart read per cycle across all five accounts; freeze the pre-open plan."""
    def __init__(self, directory):
        self.directory = directory
        self.cache_key = self.result = None

    def plan(self, now):
        date = now.date().isoformat()
        frozen = self.directory / ("sealed-" + date + ".json")
        if frozen.exists():
            return json.loads(frozen.read_text())
        supplied = self.directory / ("plan-" + date + ".json")
        if now.time() >= time(9, 30) or not supplied.exists():
            return {}
        plan = json.loads(supplied.read_text())
        if plan.get("session") != date or plan.get("root") != "QQQ":
            raise ValueError("plan session or instrument mismatch")
        atomic_json(frozen, {"first_seen": now.isoformat(), "levels": plan})
        return json.loads(frozen.read_text())

    def read(self, cycle, now, phase):
        key = (now.date(), cycle)
        if self.cache_key == key:
            return self.result
        self.cache_key = key
        try:
            plan = self.plan(now)
            rows = []
            if phase == "open":
                resp = requests.get(SOURCE, params={"range": "1d", "interval": "1m",
                                    "includePrePost": "false"}, headers={"User-Agent": "Mozilla/5.0"},
                                    timeout=4, allow_redirects=False)
                resp.raise_for_status()
                rows = closed_minutes(resp.json(), now)
                path = self.directory / ("minutes-" + now.date().isoformat() + ".json")
                prior = json.loads(path.read_text()) if path.exists() else []
                if len(prior) > len(rows):
                    raise ValueError("chart regressed")
                fresh = [[b.start.isoformat(), b.open, b.high, b.low, b.close, b.volume,
                          now.isoformat()] for b in rows[len(prior):]]
                if fresh:
                    atomic_json(path, prior + fresh)
                # Retain the first observed version across restarts and vendor revisions.
                rows = [Bar(datetime.fromisoformat(t).astimezone(ET),
                            datetime.fromisoformat(t).astimezone(ET) + MINUTE, o, h, l, c, v)
                        for t, o, h, l, c, v, seen in prior + fresh]
            self.result = (plan, rows, None)
        except (ValueError, IndexError, KeyError, TypeError, OSError, requests.RequestException) as exc:
            self.result = ({}, [], "minute or plan input unavailable: " + type(exc).__name__)
        return self.result
