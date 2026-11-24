"""Build regular NASDAQ sessions for staging; rerun with current calendar data before expiry."""
import argparse
from datetime import date
import json

import pandas_market_calendars as calendars

parser = argparse.ArgumentParser()
parser.add_argument("start", type=date.fromisoformat)
parser.add_argument("end", type=date.fromisoformat)
args = parser.parse_args()
schedule = calendars.get_calendar("NASDAQ").schedule(start_date=args.start, end_date=args.end)
sessions = {d.date().isoformat(): [r.market_open.isoformat(), r.market_close.isoformat()]
            for d, r in schedule.iterrows()}
print(json.dumps(dict(start=args.start.isoformat(), end=args.end.isoformat(), sessions=sessions)))
