"""Offline BTC product source/connection acceptance. No login or network."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from market_data.live_market.btc_options import BtcOptions, normalize, session, BUDGET
from market_data.live_market.feed import DXLinkFeed

NOW = datetime(2026, 11, 21, 18, tzinfo=timezone.utc)
FUTURE = {'symbol': '/BTCX6', 'expiration-date': '2026-11-27',
          'maturity-date': None, 'notional-multiplier': '5', 'streamer-symbol': '/BTCX26:XCME'}


def row(strike=90000, side='C', expiry='2026-11-27'):
    return {'symbol': f'./BTCX6 BTCX6 {expiry}{side}{strike}', 'root-symbol': '/BTC',
            'exchange': 'CME', 'exercise-style': 'American', 'is-exercisable-weekly': True, 'settlement-type': 'Future',
            'future-option-product': {'expiration-type': 'End-Of-Month', 'code': 'BTC', 'exchange': 'CME'}, 'underlying-symbol': '/BTCX6',
            'streamer-symbol': f'./BTCX26{side}{strike}:XCME', 'expiration-date': expiry,
            'expires-at': expiry + 'T16:00:00Z', 'stops-trading-at': expiry + 'T16:00:00Z',
            'strike-price': str(strike), 'option-type': side}


class FakeResponse:
    status_code = 200
    def __init__(self, data): self.data = data
    def json(self): return {'data': self.data}


class BtcTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(80000 + i * 1000, side) for i in range(30) for side in ('C', 'P')]
        self.calls = []
        def get(url, **kw):
            self.calls.append(url)
            self.assertEqual(kw['timeout'], 10)
            if url.endswith('/futures-option-chains/BTC'): return FakeResponse({'items': self.rows})
            if '/instruments/future-options/' in url: return FakeResponse(row(50000))
            return FakeResponse(FUTURE)
        self.product = BtcOptions(get)
        self.feed = DXLinkFeed('wss://fixture.invalid', 'fixture')
        self.feed._send = lambda msg: None
        self.feed.set_subscriptions([], ['QQQ'])
        self.feed._connected = self.feed._authorized = self.feed._channel_open = True

    def test_source_identity_and_monthly_only(self):
        c = normalize(row(), FUTURE)
        self.assertEqual(c['multiplier'], 5)
        self.assertEqual(c, normalize(row(), FUTURE))
        for changed in [{'root-symbol': '/MBT'}, {'future-option-product': {'expiration-type': 'End-Of-Month', 'code': 'MBT', 'exchange': 'CME'}},
                        {'future-option-product': {'expiration-type': 'Weekly'}},
                        {'streamer-symbol': 'guess'}, {'option-type': 'X'}]:
            self.assertIsNone(normalize({**row(), **changed}, FUTURE))
        self.assertIsNone(normalize(row(), {**FUTURE, 'expiration-date': '2026-12-31'}))

    def test_bounded_window_held_priority_and_restart_resolution(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        p = self.product.payload([], [], NOW)
        self.assertLessEqual(p['subscription_count'], BUDGET)
        self.assertEqual(len(p['contracts']), 22)
        self.assertTrue(any(s['symbol'] == 'QQQ' for s in self.feed._subs))
        held = normalize(row(50000), FUTURE)
        self.product.payload([held['contract_key']], [held['provider_symbol']], NOW)
        self.product.refresh(self.feed, 'fixture', NOW + timedelta(seconds=31))
        self.assertIn(held['contract_key'], self.product.selected)
        self.assertTrue(any('/instruments/future-options/' in u for u in self.calls))
        # A later monthly discovery cannot evict a held old-month contract.
        self.rows = []
        self.product.refresh(self.feed, 'fixture', NOW + timedelta(seconds=301))
        self.assertIn(held['contract_key'], self.product.selected)

    def test_reconnect_invalidates_and_missing_side_does_not_carry(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        c = self.product.payload([], [], NOW)['contracts'][0]
        streamer = c['streamer_symbol']
        event = {'eventType': 'Quote', 'eventSymbol': streamer, 'bidPrice': 100, 'askPrice': 120,
                 'bidSize': 2, 'askSize': 3, 'bidTime': int(NOW.timestamp() * 1000), 'askTime': int(NOW.timestamp() * 1000)}
        self.feed._ingest([event])
        q = self.product.payload([c['contract_key']], [], NOW)['quotes'][0]
        self.assertEqual(q['bid_size'], 2)
        self.assertEqual(datetime.fromisoformat(q['ask_ts']), NOW)
        self.feed._ingest([{**event, 'bidPrice': None, 'bidSize': None}])
        self.assertIsNone(self.product.payload([c['contract_key']], [], NOW)['quotes'][0]['bid'])
        self.feed._on_open(None)
        self.assertNotIn(streamer, self.feed.get_state())
        self.assertFalse(self.product.payload([c['contract_key']], [], NOW)['quotes'][0]['available'])

    def test_maintenance_reopen_dst_and_provider_failure(self):
        self.assertEqual(session(datetime(2026, 11, 21, 8, tzinfo=timezone.utc)), 'maintenance')
        self.assertEqual(session(datetime(2026, 11, 21, 10, tzinfo=timezone.utc)), 'open')
        self.assertEqual(session(datetime(2026, 7, 20, 21, 1, tzinfo=timezone.utc)), 'maintenance')
        with patch.object(self.product, '_read', side_effect=ValueError('provider_http_403')):
            self.product.refresh(self.feed, 'fixture', NOW)
        self.assertEqual(self.product.status, 'provider_http_403')
        self.assertEqual(self.product.payload([], [], NOW)['quotes'], [])

    def test_monthly_roll_keeps_exact_held_identity(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        held = self.product.payload([], [], NOW)['contracts'][0]
        self.product.payload([], [held['provider_symbol']], NOW)
        later = row(90000, 'P', '2026-12-31')
        later = {k: v.replace('BTCX', 'BTCZ') if isinstance(v, str) else v for k, v in later.items()}
        future = {**FUTURE, 'symbol': '/BTCZ6', 'expiration-date': '2026-12-31',
                  'maturity-date': '2026-12-31', 'streamer-symbol': '/BTCZ26:XCME'}
        def get(url, **kw):
            return FakeResponse({'items': [later]} if '/futures-option-chains/' in url else future)
        self.product._get = get
        self.product.refresh(self.feed, 'fixture', NOW + timedelta(seconds=301))
        p = self.product.payload([], [held['provider_symbol']], NOW + timedelta(seconds=302))
        self.assertIn(held['contract_key'], [c['contract_key'] for c in p['contracts']])
        self.assertNotIn(held['contract_key'], p['entry_keys'])
        self.assertEqual(self.product.contracts[held['contract_key']], held)
        self.assertTrue(all(self.product.contracts[k]['exp'] == '2026-12-31' for k in p['entry_keys']))

    def test_subscription_removal_preserves_existing_feed_owners(self):
        self.feed.set_subscriptions([], ['QQQ', '/BTCX26:XCME'])
        original = list(self.feed._subs)
        self.feed.replace_btc_subscriptions(['/BTCX26:XCME', './BTCX26C90000:XCME'])
        self.feed.replace_btc_subscriptions([])
        self.assertEqual(self.feed._subs, original)


if __name__ == '__main__': unittest.main()
