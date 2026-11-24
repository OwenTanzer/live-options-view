"""Offline One Clock QQQ reversal/breach census. Inputs are finalized, start-labeled bars."""
import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MINUTE = timedelta(minutes=1)
RULE = "qqq.1m.opposite_color_breach.v1"


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def color(self):
        return (self.close > self.open) - (self.close < self.open)


def classify(first, second):
    """One observer trial from two finalized regular-session QQQ bars (ET)."""
    if (second.start - first.start != MINUTE or first.start.date() != second.start.date()
            or first.start.time() < time(9, 30) or second.start.time() >= time(16)
            or not first.color):
        return None
    sign = -first.color
    up = sign == 1
    level = first.high if up else first.low
    depth = second.high - level if up else level - second.low
    event = second.color == sign and depth > 0
    decision = second.start + MINUTE
    return dict(rule=RULE, symbol="QQQ", decision_at=decision.isoformat(),
                trial_id=f"{RULE}/QQQ/{decision.isoformat()}", available_at=None,
                first_start=first.start.isoformat(), second_start=second.start.isoformat(),
                direction="up" if up else "down", level=str(level), event=event,
                second_close=str(second.close),
                penetration_points=str(depth) if event else None,
                close_class=("beyond" if sign * (second.close-level) > 0 else "at_or_inside")
                if event else None,
                open_beyond=sign * (second.open-level) > 0 if event else None,
                both_extremes=second.high > first.high and second.low < first.low if event else None)


def outcome(second, endpoint, direction):
    if (endpoint is None or endpoint.start != second.start + 5 * MINUTE
            or endpoint.start.date() != second.start.date()):
        return None
    return (endpoint.close - second.close) * (1 if direction == "up" else -1)


def load_sessions(paths):
    sessions = defaultdict(dict)
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for raw in csv.DictReader(f):
                row = {k.lower().strip(): v for k, v in raw.items()}
                stamp = datetime.fromisoformat(row.get("timestamp") or row["datetime"])
                if stamp.tzinfo is None or stamp.second or stamp.microsecond:
                    raise ValueError(f"Expected offset-aware minute start: {stamp}")
                stamp = stamp.astimezone(ET)
                if not time(9, 30) <= stamp.time() < time(16):
                    continue
                prices = [Decimal(row[k]) for k in ("open", "high", "low", "close")]
                b = Bar(stamp, *prices)
                if not all(v.is_finite() for v in prices) or not (
                    b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
                ):
                    raise ValueError(f"Invalid OHLC: {stamp}")
                if stamp in sessions[stamp.date()]:
                    raise ValueError(f"Duplicate input minute: {stamp}")
                sessions[stamp.date()][stamp] = b
    return sessions


def prior_atr(bars, start):
    """Simple average of 14 prior true ranges, with one extra close for the first TR."""
    history = [bars.get(start - i * MINUTE) for i in range(15, 0, -1)]
    if any(b is None or b.start.date() != start.date() for b in history):
        return None
    ranges = [max(b.high, a.close) - min(b.low, a.close) for a, b in zip(history, history[1:])]
    return sum(ranges) / 14


def census(bars):
    counts = Counter()
    rows = []
    for stamp, second in sorted(bars.items()):
        first = bars.get(stamp - MINUTE)
        trial = classify(first, second) if first else None
        if trial is None:
            continue
        direction = trial["direction"]
        counts[direction + "_eligible"] += 1
        if trial["event"]:
            counts[direction + "_events"] += 1
            counts[trial["close_class"]] += 1
            for flag in ("open_beyond", "both_extremes"):
                counts[flag] += trial[flag]
            counts["inside_and_both"] += trial["close_class"] == "at_or_inside" and trial["both_extremes"]
            atr = prior_atr(bars, stamp)
            trial["prior_atr14_points"] = str(atr) if atr is not None else None
            trial["penetration_atr14"] = str(Decimal(trial["penetration_points"]) / atr) if atr else None
            target = stamp + 5 * MINUTE
            change = outcome(second, bars.get(target), direction)
            counts["unknown_5m"] += change is None
            trial.update(target_close_at=(target + MINUTE).isoformat(),
                         signed_change_5m_points=None if change is None else str(change))
        rows.append(trial)
    return rows, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="QQQ OHLC CSVs of regular, full-length exchange sessions")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sessions = load_sessions(args.files)
    if not sessions:
        parser.error("No regular-session bars found")
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = []
    total = Counter()
    with (args.output / "trials.jsonl").open("w") as stream:
        for day, bars in sorted(sessions.items()):
            if len(bars) != 390 or day.weekday() >= 5:
                summaries.append(dict(session=str(day), bars=len(bars), status="excluded_incomplete_or_nonstandard"))
                continue
            rows, counts = census(bars)
            for row in rows:
                print(json.dumps(row), file=stream)
            summaries.append(dict(session=str(day), bars=len(bars), status="included", **counts))
            total.update(counts)
    report = json.dumps(dict(rule=RULE, sessions=summaries, total=dict(total)), indent=2)
    (args.output / "counts.json").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
