"""Conservative process-observed BTC side changes; never exchange timestamps.

One clock per existing DXLink connection. The first complete side is a baseline,
not an executable observation. Repeated snapshots and other-side updates cannot
advance its clock. No network, persistence, or new subscriptions.
"""
import math
import uuid
from collections import Counter
from datetime import datetime, timezone


def source_time(value):
    """Accept epoch milliseconds or explicit timezone-bearing ISO strings only."""
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            return None
    try:
        return (datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
                if math.isfinite(millis) and millis > 0 else None)
    except (ValueError, OverflowError, OSError):
        return None


def raw_time_class(event, field):
    if field not in event:
        return 'absent'
    value = event[field]
    kind = ('null' if value is None else 'boolean' if isinstance(value, bool)
            else 'number' if isinstance(value, (int, float))
            else 'string' if isinstance(value, str) else 'other')
    if kind not in ('number', 'string'):
        return kind
    try:
        number = float(value)
        category = ('nonfinite' if not math.isfinite(number) else 'zero' if number == 0
                    else 'negative' if number < 0 else 'positive')
    except (ValueError, TypeError):
        category = 'iso_time' if source_time(value) else 'unparseable'
    return kind + ':' + category


class BtcQuoteClock:
    def __init__(self):
        self.reset()

    def reset(self):
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.sides = {}
        self.counts = {field: Counter() for field in ('bidTime', 'askTime')}
        self.events = 0

    def forget(self, symbol):
        self.sides.pop(symbol, None)

    def next_frame(self):
        self.sequence += 1

    def observe(self, symbol, event, now, prices, sizes, diagnostic=True):
        if diagnostic:
            self.events += 1
        state = self.sides.setdefault(symbol, {})
        result = {'btc_stream_epoch': self.epoch}
        for side in ('bid', 'ask'):
            field = side + 'Time'
            if diagnostic:
                self.counts[field][raw_time_class(event, field)] += 1
            result[side + '_observed_at'] = source_time(event.get(field))
            pair = (prices[side], sizes[side])
            complete = all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in pair)
            previous = state.get(side)
            change = None
            if complete:
                baseline = {'at': now.isoformat(), 'sequence': self.sequence}
                if (previous and previous['sequence'] < self.sequence
                        and now >= previous['received']):
                    baseline = previous['baseline']
                    change = previous['change']
                    if pair != previous['pair']:
                        change = {'schema': 'observed_side_change.v1', 'at': now.isoformat(), 'stream_epoch': self.epoch,
                                  'baseline_at': baseline['at'], 'baseline_sequence': baseline['sequence'],
                                  'sequence': self.sequence, 'previous_sequence': previous['sequence'],
                                  'previous_received_at': previous['received'].isoformat(),
                                  'previous_price': previous['pair'][0], 'previous_size': previous['pair'][1],
                                  'price': pair[0], 'size': pair[1]}
                # First observation, same-frame burst, backwards clock, or missing
                # prior side establishes a new baseline and cannot qualify.
                state[side] = {'pair': pair, 'received': now, 'sequence': self.sequence,
                               'baseline': baseline, 'change': change}
            else:
                state.pop(side, None)
            # Never let an unchanged later untimed event evade a contradictory
            # (e.g. stale) source timestamp on the actual change event.
            if result[side + '_observed_at'] is not None:
                change = None
                if side in state:
                    state[side]['change'] = None
            result[side + '_change'] = change
        return result

    def diagnostics(self):
        return {'schema': 'btc_wire_time_counts.v1', 'scope': 'btc_option_quotes', 'stream_epoch': self.epoch,
                'frames': self.sequence, 'quote_events': self.events,
                'fields': {field: dict(counts) for field, counts in self.counts.items()}}
