"""Compare trails on one identical, preselected QQQ breakout entry.

Pass JSON with entry_minute, entry_spot, initial_stop, nova, side (+1 call, -1 put)
and rows [{start, open, high, low, close}]. The entry must be at the open of
the first minute. Choose entries and the end of the replay window beforehand.
Underlying-point outcomes only. A gap through the stop uses the next open.
"""

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crassus.exchange_calendar import is_holiday, session_close

ET = ZoneInfo("America/New_York")


def paired(case):
    side, anchor = case["side"], case["entry_spot"]
    if side not in (-1, 1) or not math.isfinite(anchor) or anchor <= 0:
        raise ValueError("invalid direction or underlying price")
    entered = datetime.fromisoformat(case["entry_minute"])
    if entered.tzinfo is None or entered.second or entered.microsecond:
        raise ValueError("entry must have a qualified minute-boundary timestamp")
    entered = entered.astimezone(ET)
    opening = entered.replace(hour=9, minute=30)
    if (
        entered.weekday() >= 5
        or is_holiday(entered.date())
        or entered < opening + timedelta(minutes=9)
        or entered.time() >= session_close(entered.date())
        or (entered - opening).total_seconds() % 540
    ):
        raise ValueError(
            "entry must follow a completed session-aligned nine-minute candle"
        )
    rows = case["rows"]
    if not rows:
        raise ValueError("no replay path")
    initial_stop = side * case["initial_stop"]
    nova = side * case["nova"]
    if not math.isfinite(nova) or not initial_stop <= nova < side * anchor:
        raise ValueError(
            "fixed Nova must lie between trigger invalidation and breakout entry"
        )
    entry_price = side * anchor
    if not math.isfinite(initial_stop) or initial_stop >= entry_price:
        raise ValueError("invalid initial stop")
    closing = datetime.combine(entered.date(), session_close(entered.date()), ET)

    # State is (highest favorable underlying observed, exit minute, exit price).
    # Both arms see the same order of extrema. Stops ratchet along each
    # price sequence; a gap through an existing stop exits at the open.
    def step(state, width, levels, index, finish):
        best, exited, price = state
        if exited is not None:
            return state
        for j, observed in enumerate(levels):
            stop = initial_stop
            if best >= entry_price + 2:
                stop = max(stop, best - width)
            if observed <= stop:
                return best, index, observed if j == 0 else stop
            best = max(best, observed)
        return (best, index, levels[-1]) if finish else (best, None, None)

    live = {((entry_price, None, None), (entry_price, None, None))}
    settled = set()
    for index, row in enumerate(rows):
        t = datetime.fromisoformat(row["start"])
        if (
            t.tzinfo is None
            or t != entered + timedelta(minutes=index)
            or t + timedelta(minutes=1) > closing
        ):
            raise ValueError("incomplete or out-of-session minute path")
        op, hi, lo, cl = [row[key] for key in ("open", "high", "low", "close")]
        if not (
            all(math.isfinite(n) for n in (op, hi, lo, cl))
            and 0 < lo <= min(op, cl) <= max(op, cl) <= hi
        ):
            raise ValueError("invalid OHLC")
        if index == 0 and op != anchor:
            raise ValueError("entry price must match first bar open")
        if side == -1:
            op, hi, lo, cl = -op, -lo, -hi, -cl
        finish = (index + 1) % 9 == 0 and cl < nova or index == len(rows) - 1
        successors = set()
        for first, second in live:
            for levels in ((op, hi, lo, cl), (op, lo, hi, cl)):
                pair = (
                    step(first, 1, levels, index, finish),
                    step(second, 2, levels, index, finish),
                )
                if all(arm[1] is not None for arm in pair):
                    settled.add(pair)
                else:
                    successors.add(pair)
        live = successors

    def interval(values):
        values = list(values)
        return [min(values), max(values)]

    output = {
        "ordering_model": "Both arms share O-H-L-C or O-L-H-C for each minute. Other intraminute reversals remain unknown."
    }
    for width in (1, 2):
        arms = [pair[width - 1] for pair in settled]
        output[width] = {
            "points_range": interval(price - entry_price for best, idx, price in arms),
            "possible_exit_minutes": sorted({idx for best, idx, price in arms}),
            "giveback_points_range": interval(
                best - price for best, idx, price in arms
            ),
        }
    output["two_minus_one_points_range"] = interval(b[2] - a[2] for a, b in settled)
    return output


if __name__ == "__main__":
    print(json.dumps(paired(json.loads(Path(sys.argv[1]).read_text())), indent=2))
