"""Compare trails on one identical, preselected QQQ breakout entry.

Pass JSON with entry_minute, entry_spot, initial_stop, side (+1 call, -1 put)
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
    if (entered.weekday() >= 5 or is_holiday(entered.date())
            or entered < opening + timedelta(minutes=9)
            or entered.time() >= session_close(entered.date())
            or (entered-opening).total_seconds() % 540):
        raise ValueError("entry must follow a completed session-aligned nine-minute candle")
    outcomes = {}
    for width in (1,2):
        best = side * anchor
        stop = side * case["initial_stop"]
        if not math.isfinite(stop) or stop >= best:
            raise ValueError("invalid initial stop")
        exits = []
        nine_open = None
        rows = case["rows"]
        if not rows:
            raise ValueError("no replay path")
        for index,row in enumerate(rows):
            t = datetime.fromisoformat(row["start"])
            if (t.tzinfo is None or t != entered + timedelta(minutes=index)
                    or t + timedelta(minutes=1) > datetime.combine(entered.date(), session_close(entered.date()), ET)):
                raise ValueError("incomplete minute path")
            op, hi, lo, cl = [row[key] for key in ("open","high","low","close")]
            if not (all(math.isfinite(n) for n in (op,hi,lo,cl)) and 0 < lo <= min(op,cl) <= max(op,cl) <= hi):
                raise ValueError("invalid OHLC")
            if not index and op != anchor:
                raise ValueError("entry price must match first bar open")
            op, hi, lo, cl = (op,hi,lo,cl) if side == 1 else (-op,-lo,-hi,-cl)
            if index % 9 == 0:
                nine_open = op
            if op <= stop:
                exits.append((index,op))
                break
            if op >= side*anchor + 2:
                stop = max(stop, op-width)
            new_best = max(best, hi)
            raised = max(stop, new_best-width) if new_best >= side*anchor + 2 else stop
            if lo <= stop:
                exits.append((index,stop))
                if raised > stop:
                    exits.append((index,raised))
                break
            if lo <= raised:
                exits.append((index,raised))
                if cl <= raised:
                    break
            best,stop=new_best,raised
            if (index+1)%9 == 0 and cl < nine_open or index == len(rows)-1:
                exits.append((index,cl))
                break
        points=[p-side*anchor for _,p in exits]
        outcomes[width]={"points_range":[min(points),max(points)],
                         "possible_exit_minutes":sorted({idx for idx,p in exits})}
    return outcomes


if __name__ == "__main__":
    print(json.dumps(paired(json.loads(Path(sys.argv[1]).read_text())),indent=2))
