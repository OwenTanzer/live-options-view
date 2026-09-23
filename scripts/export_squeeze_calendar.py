"""Run with the scanner producer environment; exports read-only display metadata."""
import argparse
import importlib.metadata
import json
from datetime import timedelta
from pathlib import Path
from squeeze_scanner.scheduler import slots, instant, stamp

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--start', required=True, help='Exact immutable producer activation timestamp')
p.add_argument('--until', required=True, help='Exclusive coverage end, with timezone offset')
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
start, end = instant(a.start), instant(a.until)
if end <= start:
    p.error('until must follow start')
document = {'schema_version': 1, 'calendar': 'XNYS',
            'calendar_version': importlib.metadata.version('exchange_calendars'),
            'timezone': 'America/New_York', 'valid_from': stamp(start), 'valid_until': stamp(end),
            'slots': [stamp(s) for s in slots(start, end - timedelta(microseconds=1))]}
a.out.write_text(json.dumps(document, indent=2) + '\n')
