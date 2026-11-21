"""Synthetic FULL wire events through the real collector, shared by API/UI tests.

No credentials, sockets, provider requests, or restricted market observations.
Run as a script to emit deterministic scenarios (epochs intentionally random).
"""
import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

logging.disable(logging.CRITICAL)  # stdout is a machine-readable fixture.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from market_data.live_market.feed import DXLinkFeed
from market_data.live_market.btc_options import BtcOptions, normalize

NOW = datetime(2026, 11, 21, 18, tzinfo=timezone.utc)


def scenarios():
    row = {'symbol': './BTCX6 BTCX6 261127C90000', 'root-symbol': '/BTC',
           'exchange': 'CME', 'exercise-style': 'American', 'is-exercisable-weekly': True,
           'settlement-type': 'Future', 'future-option-product':
           {'expiration-type': 'End-Of-Month', 'code': 'BTC', 'exchange': 'CME'},
           'underlying-symbol': '/BTCX6', 'streamer-symbol': './BTCX26C90000:XCME',
           'expiration-date': '2026-11-27', 'expires-at': '2026-11-27T16:00:00Z',
           'stops-trading-at': '2026-11-27T16:00:00Z', 'strike-price': '90000', 'option-type': 'C'}
    future = {'symbol': '/BTCX6', 'expiration-date': '2026-11-27', 'maturity-date': None,
              'notional-multiplier': '5.0', 'streamer-symbol': '/BTCX26:XCME'}
    c = normalize(row, future)
    feed = DXLinkFeed('wss://fixture.invalid', 'fixture')
    feed._send = lambda msg: None
    feed._connected = feed._authorized = feed._channel_open = True
    product = BtcOptions(lambda *a, **k: (_ for _ in ()).throw(AssertionError('No network')))
    product.feed = feed
    product.contracts = {c['contract_key']: c}
    product.entries = [c['contract_key']]
    product.status = 'fixture'
    product._subscribe(NOW)
    event = {'eventType': 'Quote', 'eventSymbol': c['streamer_symbol'],
             'bidPrice': 99, 'askPrice': 101, 'bidSize': 2, 'askSize': 1,
             'bidTime': 0, 'askTime': '0'}
    result = []

    def emit(name, sec, buy, sell, changes=None, ingest=True):
        event.update(changes or {})
        at = NOW + timedelta(seconds=sec)
        if ingest:
            with patch('market_data.live_market.feed.datetime') as date:
                date.now.return_value = at
                feed._ingest([dict(event)])
        result.append({'name': name, 'now': int(at.timestamp() * 1000),
                       'buy': buy, 'sell': sell, 'payload': product.payload([], [], at)})

    emit('snapshot', 0, False, False)
    emit('replay', 1, False, False)
    emit('bid_only', 2, False, True, {'bidPrice': 98})
    emit('ask_price', 3, True, True, {'askPrice': 100})
    emit('unchanged', 4, True, True)
    emit('aged_replay', 20, False, False)
    emit('ask_size', 21, True, False, {'askSize': 2})
    emit('stale_receipt', 37, False, False, ingest=False)
    emit('missing_bid', 38, False, False, {'bidPrice': None, 'bidSize': None})
    emit('bid_baseline', 39, False, False, {'bidPrice': 119, 'bidSize': 2})
    emit('close_bid', 40, True, True, {'bidPrice': 120, 'askPrice': 121})
    feed._on_close(None, 1006, 'fixture')
    emit('disconnected', 41, False, False, ingest=False)
    feed._on_open(None)
    feed._connected = feed._authorized = feed._channel_open = True
    emit('reconnect_snapshot', 42, False, False, {'bidPrice': 118, 'askPrice': 122})
    emit('reconnect_replay', 43, False, False)
    emit('reconnect_ask_size', 44, True, False, {'askSize': 3})
    return result


if __name__ == '__main__':
    import logging
    logging.basicConfig(stream=sys.stderr, force=True)
    print(json.dumps(scenarios(), allow_nan=False))
