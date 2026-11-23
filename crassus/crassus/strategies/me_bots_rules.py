"""QQQ Me Bots. All prices in this module are underlying points."""

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from math import isfinite
from statistics import mean

from ..clock import ET

MINUTE = timedelta(minutes=1)
KINDS = ("golden", "runner", "opening", "phelps", "ratchet")


@dataclass(frozen=True)
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def span(self):
        return self.high - self.low


def prices(bar, side):
    """Reflect put paths so each comparison is written in favorable points."""
    if side == 1:
        return bar.open, bar.high, bar.low, bar.close
    return -bar.open, -bar.low, -bar.high, -bar.close


def candles(rows, n):
    if not rows:
        return []
    opening = datetime.combine(rows[0].start.date(), time(9, 30), ET)
    result = []
    for index, bar in enumerate(rows):
        if (bar.end - opening) % (n * MINUTE) or index + 1 < n:
            continue
        group = rows[index + 1 - n : index + 1]
        if group[0].start + n * MINUTE != bar.end:
            continue
        result.append(
            Bar(
                group[0].start,
                bar.end,
                group[0].open,
                max(b.high for b in group),
                min(b.low for b in group),
                bar.close,
                sum(b.volume for b in group),
            )
        )
    return result


def entry(kind, rows, plan):
    size = 1 if kind == "opening" else 9
    series = candles(rows, size)
    if len(series) < 3 or series[-1].end != rows[-1].end:
        return None
    a, b, trigger = series[-3:]
    for side, name in ((1, "long"), (-1, "short")):
        levels = plan.get(name, {})
        if not isinstance(levels, dict):
            continue
        # Each direction's first target and vertical cutoff must be supplied
        # by a research owner before this session. No inferred target or fill.
        target = levels.get("first_target")
        max_span = levels.get("max_trigger_range_points")
        if not all(
            type(x) in (float, int) and isfinite(x) and x > 0
            for x in (target, max_span)
        ):
            continue
        op, hi, lo, cl = prices(trigger, side)
        if hi >= side * target or trigger.span > max_span or cl <= op:
            continue
        prev = [prices(x, side) for x in series[:-1]]
        if kind == "opening":
            if trigger.end.time() > time(10, 15) or not a.span < b.span < trigger.span:
                continue
            level = levels.get("premarket_rejection_level")
            if type(level) not in (int, float) or not isfinite(level):
                continue
            # The rejection must occur within this three-bar setup.
            if not any(
                prices(x, side)[2] <= side * level < prices(x, side)[3]
                for x in (a, b, trigger)
            ):
                continue
        else:
            if len(series) < 15:
                continue
            prior = series[-15:-1]
            if trigger.span <= 0 or trigger.span < 1.5 * mean(x.span for x in prior):
                continue
            if cl < hi - trigger.span / 4 or not a.span < b.span < trigger.span:
                continue
            if kind in ("golden", "runner") and cl <= max(p[1] for p in prev[-14:]):
                continue
            if kind == "runner":
                key = levels.get("opening_key_level")
                if type(key) not in (int, float) or not isfinite(key) or len(rows) < 30:
                    continue
                if side * rows[29].close <= side * key:
                    continue
            if kind == "phelps":
                normal = mean(x.volume for x in prior)
                if (
                    normal <= 0
                    or trigger.volume < 2 * normal
                    or cl <= max(p[1] for p in prev)
                ):
                    continue
        return dict(
            side=side,
            trigger_end=trigger.end.isoformat(),
            stop=lo * side,
            trigger_close=trigger.close,
            kind=kind,
        )
    return None


def exit_reason(kind, rows, seed, entry_at, entry_spot, now):
    """Check only complete bars fully after the entry observation.

    A bar that could have both raised and crossed a new trail has unknown
    intrabar order. Report it separately and propose a protective exit now.
    """
    side = seed["side"]
    stop = side * seed["stop"]
    anchor = side * entry_spot
    best = anchor
    distance = 2 if kind in ("runner", "phelps") else 1
    size = 1 if kind == "opening" else 9
    decision_bars = {
        bar.end: bar for bar in candles(rows, size) if bar.start >= entry_at
    }
    for bar in rows:
        if bar.end <= entry_at:
            continue
        if bar.start < entry_at:
            _, hi, lo, cl = prices(bar, side)
            if cl <= stop:
                return "underlying closed through stop after fill"
            if lo <= stop:
                return "fill-minute stop ordering unknown; protective exit"
            if kind != "ratchet" and hi >= anchor + distance and lo <= hi - distance:
                return "fill-minute trail ordering unknown; protective exit"
            best = max(best, cl)
            if kind != "ratchet" and best >= anchor + distance:
                stop = max(stop, best - distance)
            continue
        _, hi, lo, _ = prices(bar, side)
        if lo <= stop:
            return "underlying stop crossed"
        deadline = entry_at + 25 * MINUTE
        if kind == "phelps" and bar.start < deadline < bar.end and best < anchor + 1:
            if prices(bar, side)[0] < anchor + 1 <= hi:
                return "Phelps threshold ordering at deadline unknown; protective exit"
        best = max(best, hi)
        if kind != "ratchet" and best >= anchor + distance:
            raised = max(stop, best - distance)
            if lo <= raised and raised > stop:
                return "trail touch ordering unknown; protective exit at next quote"
            stop = raised
        candle = decision_bars.get(bar.end)
        if candle:
            op, _, adverse, cl = prices(candle, side)
            if cl < op:
                return "completed adverse candle"
            if kind == "ratchet" and cl > op:
                stop = max(stop, adverse - 1)
        if kind == "phelps" and bar.end >= entry_at + 25 * MINUTE and best < anchor + 1:
            return "Phelps time stop"
    if kind == "ratchet" and now.time() >= time(15, 30):
        return "15:30 ET cutoff"
    return None
