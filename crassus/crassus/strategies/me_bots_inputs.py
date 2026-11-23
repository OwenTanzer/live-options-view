"""Completed QQQ one-minute OHLCV, read through the runner's existing chart origin."""

import json
from datetime import datetime, time, timedelta
from math import isfinite

import requests

from ..clock import ET
from ..durability import atomic_json
from ..exchange_calendar import preceding_session, session_close
from ..skew_bars import SOURCE
from .me_bots_rules import Bar, MINUTE, candles


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
        if start != opening + len(rows) * MINUTE:
            raise ValueError("incomplete regular-session minute sequence")
        if (
            end.time() > session_close(now.date())
            or end > now
            or stamps[i + 1] > now.timestamp()
        ):
            break
        if end.timestamp() > stamps[i + 1]:
            raise ValueError("next timestamp does not witness candle rollover")
        numbers = [q[key][i] for key in ("open", "high", "low", "close", "volume")]
        if any(type(x) not in (float, int) or not isfinite(x) for x in numbers):
            raise ValueError("incomplete OHLCV")
        op, high, low, close, volume = numbers
        if not 0 < low <= min(op, close) <= max(op, close) <= high or volume < 0:
            raise ValueError("invalid OHLCV")
        rows.append(Bar(start, end, op, high, low, close, volume))
    return rows


def previous_minutes(payload, opening, closing):
    """A prior regular session must cover every expected minute exactly once."""
    chart = payload["chart"]["result"][0]
    meta = chart["meta"]
    if meta["symbol"] != "QQQ" or meta["dataGranularity"] != "1m":
        raise ValueError("unexpected baseline instrument or granularity")
    stamps = chart["timestamp"]
    if stamps != sorted(set(stamps)):
        raise ValueError("duplicate or unordered baseline data")
    q = chart["indicators"]["quote"][0]
    selected = [
        i
        for i, t in enumerate(stamps)
        if opening.timestamp() <= t < closing.timestamp()
    ]
    return validate_prior(
        [
            [stamps[i], *(q[k][i] for k in ("open", "high", "low", "close", "volume"))]
            for i in selected
        ],
        opening,
        closing,
    )


def validate_prior(raw, opening, closing):
    count = int((closing - opening) / MINUTE)
    if opening.time() != time(9, 30) or not 0 < count <= 390 or len(raw) != count:
        raise ValueError("incomplete or unrecognized prior session")
    bars = []
    for index, (ts, op, hi, lo, cl, v) in enumerate(raw):
        start = opening + index * MINUTE
        if ts != start.timestamp() or not all(
            type(n) in (float, int) and isfinite(n) for n in (op, hi, lo, cl, v)
        ):
            raise ValueError("missing, malformed or misaligned prior minute")
        if not 0 < lo <= min(op, cl) <= max(op, cl) <= hi or v < 0:
            raise ValueError("invalid prior OHLCV")
        bars.append(Bar(start, start + MINUTE, op, hi, lo, cl, v))
    return bars


class Inputs:
    """One chart read per cycle across all five accounts; freeze the pre-open plan."""

    def __init__(self, directory):
        self.directory = directory
        self.cache_key = self.result = None
        self.reference_cache = None
        self.reference_retry = None

    def plan(self, now, receipt_clock=None):
        date = now.date().isoformat()
        frozen = self.directory / ("sealed-" + date + ".json")
        if frozen.exists():
            return self.read_seal(frozen, now)
        supplied = self.directory / ("plan-" + date + ".json")
        if now.time() >= time(9, 30) or not supplied.exists():
            return {}
        plan = json.loads(supplied.read_text())
        if plan.get("session") != date or plan.get("root") != "QQQ":
            raise ValueError("plan session or instrument mismatch")
        seen = receipt_clock() if receipt_clock else now
        if seen < now or seen.date() != now.date() or seen.time() >= time(9, 30):
            return {}
        atomic_json(frozen, {"first_seen": seen.isoformat(), "levels": plan})
        return self.read_seal(frozen, seen)

    @staticmethod
    def read_seal(path, now):
        seal = json.loads(path.read_text())
        seen = datetime.fromisoformat(seal["first_seen"])
        if seen.tzinfo is None:
            raise ValueError("unknown plan first-seen timezone")
        seen = seen.astimezone(ET)
        if (
            seen > now
            or seen.date() != now.date()
            or seen.time() >= time(9, 30)
            or seal["levels"].get("session") != now.date().isoformat()
            or seal["levels"].get("root") != "QQQ"
        ):
            raise ValueError("plan origin, session or first-seen clock mismatch")
        return seal

    def reference(self, now):
        """Read once per session and preserve the actual first-sight clock."""
        if (
            self.reference_cache
            and self.reference_cache["for_session"] == now.date().isoformat()
        ):
            return self.reference_cache
        if self.reference_retry and now < self.reference_retry:
            return None
        self.reference_retry = now + timedelta(minutes=5)
        try:
            hours = preceding_session(now.date())
            if not hours:
                return None
            opening, closing = hours
            date = now.date().isoformat()
            path = self.directory / ("reference-" + date + ".json")
            if path.exists():
                data = json.loads(path.read_text())
                if (
                    data.get("session") != opening.date().isoformat()
                    or data.get("for_session") != date
                    or data.get("root") != "QQQ"
                    or data.get("interval") != "1m"
                ):
                    raise ValueError("wrong saved baseline identity")
                received = datetime.fromisoformat(data["first_seen"])
                bars = validate_prior(data["minutes"], opening, closing)
            else:
                resp = requests.get(
                    SOURCE,
                    params={"range": "5d", "interval": "1m", "includePrePost": "false"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=4,
                    allow_redirects=False,
                )
                resp.raise_for_status()
                payload = resp.json()
                received = datetime.now(ET)
                bars = previous_minutes(payload, opening, closing)
                data = {
                    "session": opening.date().isoformat(),
                    "for_session": date,
                    "root": "QQQ",
                    "interval": "1m",
                    "first_seen": received.isoformat(),
                    "minutes": [
                        [b.start.timestamp(), b.open, b.high, b.low, b.close, b.volume]
                        for b in bars
                    ],
                }
            checked = datetime.now(ET)
            if (
                received.tzinfo is None
                or not closing <= received <= checked
                or checked.date() != now.date()
            ):
                raise ValueError("invalid baseline receipt clock")
            if not path.exists():
                atomic_json(path, data)
            self.reference_cache = {
                "for_session": date,
                "session": opening.date().isoformat(),
                "first_seen": received,
                "nine": candles(bars, 9),
                "thirty": candles(bars, 30),
            }
            return self.reference_cache
        except (
            ValueError,
            IndexError,
            KeyError,
            TypeError,
            OSError,
            requests.RequestException,
        ):
            # The held-position path and warmed same-day windows remain usable.
            return None

    def read(self, cycle, now, phase):
        key = (now.date(), cycle)
        if self.cache_key == key:
            return self.result
        self.cache_key = key
        try:
            plan = self.plan(now, lambda: datetime.now(ET))
            if phase in ("premarket", "open"):
                reference = self.reference(now)
                if plan and reference:
                    plan = {**plan, "reference": reference}
            rows = []
            if phase == "open":
                resp = requests.get(
                    SOURCE,
                    params={"range": "1d", "interval": "1m", "includePrePost": "false"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=4,
                    allow_redirects=False,
                )
                resp.raise_for_status()
                payload = resp.json()
                received = datetime.now(ET)
                if received.date() != now.date() or received < now:
                    raise ValueError("uncertain chart receipt clock")
                rows = closed_minutes(payload, received)
                path = self.directory / ("minutes-" + now.date().isoformat() + ".json")
                prior = json.loads(path.read_text()) if path.exists() else []
                for saved, current in zip(prior, rows):
                    if (
                        datetime.fromisoformat(saved[0]) != current.start
                        or not current.end
                        <= datetime.fromisoformat(saved[-1])
                        <= received
                    ):
                        raise ValueError("stored minute identity or receipt mismatch")
                if len(prior) > len(rows):
                    raise ValueError("chart regressed")
                fresh = [
                    [
                        b.start.isoformat(),
                        b.open,
                        b.high,
                        b.low,
                        b.close,
                        b.volume,
                        received.isoformat(),
                    ]
                    for b in rows[len(prior) :]
                ]
                if fresh:
                    atomic_json(path, prior + fresh)
                if plan and (fresh or prior):
                    plan = {
                        **plan,
                        "latest_minute_first_seen": (fresh or prior)[-1][-1],
                    }
                # Retain the first observed version across restarts and vendor revisions.
                rows = [
                    Bar(
                        datetime.fromisoformat(t).astimezone(ET),
                        datetime.fromisoformat(t).astimezone(ET) + MINUTE,
                        o,
                        h,
                        l,
                        c,
                        v,
                    )
                    for t, o, h, l, c, v, seen in prior + fresh
                ]
            self.result = (plan, rows, None)
        except (
            ValueError,
            IndexError,
            KeyError,
            TypeError,
            OSError,
            requests.RequestException,
        ) as exc:
            self.result = (
                {},
                [],
                "minute or plan input unavailable: " + type(exc).__name__,
            )
        return self.result
