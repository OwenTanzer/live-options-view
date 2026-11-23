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


def number(value):
    return type(value) in (float, int) and isfinite(value) and value > 0


def target_for(level, declared):
    side = 1 if level["side"] == "long" else -1
    target = level.get("first_target")
    if target is not None:
        return (
            target if number(target) and side * (target - level["price"]) > 0 else None
        )
    ahead = [
        row["price"] for row in declared if side * (row["price"] - level["price"]) > 0
    ]
    return min(ahead, key=lambda price: side * price) if ahead else None


def admitted(close, level, target, side):
    """Jayden's provisional 1.5 reward/risk and 1.5-point extension."""
    risk = side * (close - level)
    reward = side * (target - close)
    return 0 < risk <= 1.5 and reward >= 1.5 * risk


def qualified_trigger(kind, trigger, previous, sequence, plan):
    """Apply the frozen trigger using only complete reference candles."""
    runner = kind == "runner"
    size = 30 if runner else 1 if kind == "opening" else 9
    if trigger.span <= 0 or trigger.end - trigger.start != size * MINUTE:
        return None
    if any(b.end > trigger.start for b in previous):
        return None
    if runner:
        if (
            trigger.start.time() != time(9, 30)
            or trigger.end.time() != time(10)
            or len(previous) != 13
            or any(b.end - b.start != 30 * MINUTE for b in previous)
        ):
            return None
        avg = mean(b.span for b in previous)
        if avg <= 0 or trigger.span < avg:
            return None
    else:
        if len(previous) < 14 or any(
            b.end - b.start != 9 * MINUTE for b in previous[-14:]
        ):
            return None
        previous = previous[-14:]
        avg = mean(b.span for b in previous)
        if avg <= 0 or trigger.span > 3 * avg:
            return None
        if kind == "opening":
            if (
                trigger.end.time() > time(10, 15)
                or len(sequence) < 3
                or not sequence[-3].span < sequence[-2].span < trigger.span
            ):
                return None
        elif (
            trigger.span < 1.5 * avg
            or not previous[-2].span < previous[-1].span < trigger.span
        ):
            return None
    levels = plan.get("levels")
    if not isinstance(levels, list) or not levels:
        return None
    if any(
        not isinstance(row, dict)
        or not number(row.get("price"))
        or row.get("side") not in ("long", "short")
        or not isinstance(row.get("source"), str)
        or not row["source"].strip()
        for row in levels
    ):
        return None
    candidates = []
    for level in levels:
        side = 1 if level["side"] == "long" else -1
        price = level["price"]
        if runner and not number(level.get("first_target")):
            continue
        target = target_for(level, levels)
        if target is None or not admitted(trigger.close, price, target, side):
            continue
        op, hi, lo, cl = prices(trigger, side)
        if hi >= side * target or cl <= op:
            continue
        if kind != "opening" and cl < hi - trigger.span / 4:
            continue
        if kind == "golden":
            edge = max(prices(x, side)[1] for x in previous[-6:])
            if abs(side * price - edge) > 0.25 or cl <= edge:
                continue
        elif kind == "opening":
            if not level.get("premarket_rejection") is True:
                continue
            if not any(
                prices(x, side)[2] <= side * price < prices(x, side)[3]
                for x in sequence[-3:]
            ):
                continue
        elif kind == "phelps":
            volume = mean(b.volume for b in previous)
            if volume <= 0 or trigger.volume < 2 * volume:
                continue
            if not sequence[:-1] or cl <= max(
                prices(b, side)[1] for b in sequence[:-1]
            ):
                continue
        candidates.append(
            dict(
                side=side,
                trigger_end=trigger.end.isoformat(),
                stop=lo * side,
                trigger_close=trigger.close,
                kind=kind,
                nova=price,
                first_target=target,
                level_source=level["source"],
            )
        )
    # Multiple qualifying structural theses require a choice before the open.
    return candidates[0] if len(candidates) == 1 else None


def entry(kind, rows, plan, reference=None):
    nine = candles(rows, 9)
    size = 1 if kind == "opening" else 30 if kind == "runner" else 9
    series = candles(rows, size) if size != 9 else nine
    if not series or series[-1].end != rows[-1].end:
        return None
    trigger = series[-1]
    local = [b for b in nine if b.end <= trigger.start]
    # A previous session can only inform an actionable trigger after our
    # own first sight of that history. Late retrieval cannot repair the past.
    known = (
        reference is not None
        and reference["for_session"] == trigger.start.date().isoformat()
        and reference["first_seen"] <= trigger.end
    )
    use_prior = kind == "runner" or len(local) < 14
    if kind == "runner":
        previous = reference["thirty"] if known else []
    else:
        previous = (reference["nine"] if known and use_prior else []) + local
    signal = qualified_trigger(kind, trigger, previous, series, plan)
    if signal:
        signal["reference_count"] = 13 if kind == "runner" else 14
        signal["reference_minutes"] = 30 if kind == "runner" else 9
        if use_prior and known:
            signal["prior_session"] = reference["session"]
            signal["prior_first_seen"] = reference["first_seen"].isoformat()
    return signal


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
    decision_bars = {bar.end: bar for bar in candles(rows, size) if bar.end > entry_at}

    def candle_exit(bar):
        if bar is None:
            return None
        op, _, _, cl = prices(bar, side)
        if seed.get("nova") is not None and cl < side * seed["nova"]:
            return "completed candle closed back through fixed Nova"
        if kind not in ("golden", "runner") and cl < op:
            return "completed adverse candle"
        return None

    for bar in rows:
        if bar.end <= entry_at:
            continue
        if bar.start < entry_at:
            _, hi, lo, cl = prices(bar, side)
            known_close = candle_exit(decision_bars.get(bar.end))
            if known_close:
                return known_close
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
            known_close = candle_exit(candle)
            if known_close:
                return known_close
            if kind == "ratchet" and cl > op:
                stop = max(stop, adverse - 1)
        if kind == "phelps" and bar.end >= entry_at + 25 * MINUTE and best < anchor + 1:
            return "Phelps time stop"
    if kind == "ratchet" and now.time() >= time(15, 30):
        return "15:30 ET cutoff"
    return None
