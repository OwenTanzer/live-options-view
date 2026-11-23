"""Export the existing producer NYSE calendar for the Worker (offline, no feed).

Run explicitly when updating the calendar policy; retain its library version.
Unknown dates fail closed. This file is outside producer deployment watch scopes.
"""
import json
import sys
from pathlib import Path
import pandas_market_calendars as mcal

start, end = '2024-01-01', '2030-12-31'
schedule = mcal.get_calendar('NYSE').schedule(start_date=start, end_date=end)
payload = dict(calendar='NYSE', library='pandas_market_calendars', version=mcal.__version__,
               start=start, end=end, sessions={})
for day, row in schedule.iterrows():
    payload['sessions'][str(day.date())] = [int(row.market_open.timestamp()*1000), int(row.market_close.timestamp()*1000)]
target=Path(__file__).resolve().parents[1].joinpath('backend/one-clock-nyse-calendar.json')
if '--check' in sys.argv:
    retained=json.loads(target.read_text(encoding='utf8'))
    assert retained['sessions']==payload['sessions'], 'Producer calendar differs; regenerate and review the Worker calendar policy.'
    print('Worker calendar matches the producer calendar for the retained range.')
else:
    target.write_text(json.dumps(payload,separators=(',',':'))+'\n',encoding='utf8')
