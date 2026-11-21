"""Offline BTC product source/connection acceptance. No login or network."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from market_data.live_market.btc_options import BtcOptions, normalize, session, BUDGET
from market_data.live_market.feed import DXLinkFeed
from market_data.live_market.btc_quote_clock import BtcQuoteClock, source_time, raw_time_class

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

    def test_production_shape_discovery_and_held_restart(self):
        # Same classification fields as the authenticated Nov 21 response.
        # Synthetic symbols/strikes; no restricted quote data in this fixture.
        self.rows = [row(84000)]
        c = normalize(self.rows[0], FUTURE)
        self.assertEqual(c['future_month'], '2026-11')
        self.product.refresh(self.feed, 'fixture', NOW)
        self.assertEqual(self.product.entries, [c['contract_key']])
        self.assertIn(c['contract_key'], self.product.selected)
        restarted = BtcOptions(lambda url, **kw: FakeResponse(
            {'items': []} if '/futures-option-chains/' in url else
            self.rows[0] if '/instruments/future-options/' in url else FUTURE))
        restarted.payload([], [c['provider_symbol']], NOW)
        restarted.refresh(self.feed, 'fixture', NOW)
        self.assertIn(c['contract_key'], restarted.selected)

    def test_exact_product_and_deliverable_reject_other_products(self):
        for code in ('MBT', 'BFF', 'BTCW', '', None):
            self.assertIsNone(normalize({**row(), 'future-option-product':
                {'code': code, 'exchange': 'CME', 'expiration-type': 'End-Of-Month'}}, FUTURE))
        for expiry_type in ('Weekly', 'Daily', '', None):
            self.assertIsNone(normalize({**row(), 'future-option-product':
                {'code': 'BTC', 'exchange': 'CME', 'expiration-type': expiry_type}}, FUTURE))
        self.assertIsNone(normalize(row(), {**FUTURE, 'notional-multiplier': '0.1'}))
        self.assertIsNone(normalize({**row(), 'settlement-type': 'Cash'}, FUTURE))

    def test_bounded_window_held_priority_and_restart_resolution(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        p = self.product.payload([], [], NOW)
        self.assertLessEqual(p['subscription_count'], BUDGET)
        self.assertEqual(len(p['contracts']), 60)
        self.assertEqual(p['subscription_count'], 22)
        far = p['contracts'][0]['contract_key']
        self.assertIn(far, self.product.payload([far], [], NOW)['subscribed_keys'])
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

    def test_inspected_contract_survives_background_refresh_and_non_view_requests(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        p = self.product.payload([], [], NOW, expiry='front')
        outside = [c['contract_key'] for c in p['contracts'] if c['contract_key'] not in p['subscribed_keys']]
        far, ticket = outside[:2]
        self.product.payload([far], [], NOW, expiry='front')
        # Execution asks for an exact symbol without replacing the viewport.
        result = self.product.payload([ticket], [], NOW)
        self.assertTrue({ticket, far}.issubset(result['subscribed_keys']))
        self.assertIn(far, self.product.payload([], [], NOW)['subscribed_keys'])
        self.product.refresh(self.feed, 'fixture', NOW + timedelta(seconds=301))
        self.assertIn(far, self.product.selected)
        next_feed = DXLinkFeed('wss://fixture.invalid', 'fixture')
        self.product.refresh(next_feed, 'fixture', NOW + timedelta(seconds=303))
        self.assertIn(far, self.product.selected)

    def test_closing_only_expiry_survives_partial_held_resolution(self):
        self.rows = [{**row(85000), 'is-closing-only': True}]
        self.product.refresh(self.feed, 'fixture', NOW)
        held = normalize(row(50000), FUTURE)
        self.product.payload([], [held['provider_symbol']], NOW)
        self.product.refresh(self.feed, 'fixture', NOW + timedelta(seconds=31))
        result = self.product.payload([], [], NOW + timedelta(seconds=32), expiry='2026-11-27')
        self.assertEqual(result['expiries'], ['2026-11-27'])
        self.assertEqual(result['entry_keys'], [])
        self.assertEqual([c['strike'] for c in result['contracts']], [85000])
        self.assertIn(held['contract_key'], result['subscribed_keys'])

    def test_reconnect_invalidates_and_missing_side_does_not_carry(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        c = self.product.payload([], [], NOW)['quotes'][0]['contract']
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

    def test_wire_diagnostics_exclude_future_and_equity_quotes(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        c = self.product.payload([], [], NOW)['quotes'][0]['contract']
        events = [{'eventType': 'Quote', 'eventSymbol': symbol, 'bidPrice': 1, 'askPrice': 2,
                   'bidSize': 1, 'askSize': 1, 'bidTime': 0, 'askTime': None}
                  for symbol in ('QQQ', FUTURE['streamer-symbol'], c['streamer_symbol'])]
        self.feed._ingest(events)
        diag = self.product.payload([], [], NOW)['wire_time_diagnostics']
        self.assertEqual(diag['scope'], 'btc_option_quotes')
        self.assertEqual(diag['quote_events'], 1)
        self.assertEqual(diag['fields'], {'bidTime': {'number:zero': 1}, 'askTime': {'null': 1}})
        self.assertNotIn('wire_time_diagnostics', self.feed.get_health())

    def test_error_and_auth_rejection_invalidate_btc_epoch(self):
        self.product.refresh(self.feed, 'fixture', NOW)
        for invalidate in (lambda: self.feed._on_error(None, 'fixture transport error'),
                           lambda: self.feed._record_auth_rejection('fixture rejection')):
            self.feed._connected = self.feed._authorized = self.feed._channel_open = True
            c = self.product.payload([], [], NOW)['quotes'][0]['contract']
            event = {'eventType': 'Quote', 'eventSymbol': c['streamer_symbol'],
                     'bidPrice': 1, 'askPrice': 2, 'bidSize': 1, 'askSize': 1}
            self.feed._ingest([event])
            before = self.feed.get_btc_diagnostics()['stream_epoch']
            invalidate()
            self.assertNotEqual(before, self.feed.get_btc_diagnostics()['stream_epoch'])
            self.assertFalse(self.product.payload([c['contract_key']], [], NOW)['quotes'][0]['available'])


class ObservedClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = BtcQuoteClock()

    def observe(self, sec, bid=99, ask=100, bid_size=2, ask_size=1, event=None, next_frame=True):
        if next_frame: self.clock.next_frame()
        return self.clock.observe('fixture', event or {}, NOW + timedelta(seconds=sec),
                                  {'bid': bid, 'ask': ask}, {'bid': bid_size, 'ask': ask_size})

    def test_raw_time_types_and_explicit_units(self):
        for value in (int(NOW.timestamp() * 1000), str(int(NOW.timestamp() * 1000)), NOW.isoformat(),
                      '2026-11-21T10:00:00-08:00'):
            self.assertEqual(datetime.fromisoformat(source_time(value)), NOW)
        for value in (None, True, False, 0, '0', -1, 'NaN', 'Infinity', {}, [], 'bad', '2026-11-21T18:00:00'):
            self.assertIsNone(source_time(value), repr(value))
        for event, expected in [({}, 'absent'), ({'bidTime': None}, 'null'), ({'bidTime': 0}, 'number:zero'),
                                ({'bidTime': '0'}, 'string:zero'), ({'bidTime': True}, 'boolean'),
                                ({'bidTime': NOW.isoformat()}, 'string:iso_time'), ({'bidTime': {}}, 'other')]:
            self.assertEqual(raw_time_class(event, 'bidTime'), expected)
            self.observe(0, event=event)
        diag = self.clock.diagnostics()
        self.assertEqual(diag['quote_events'], 7)
        self.assertEqual(diag['fields']['askTime'], {'absent': 7})
        self.assertEqual(sum(diag['fields']['bidTime'].values()), 7)
        self.assertNotIn('fixture', str(diag))  # no symbols, prices or raw values

    def test_baseline_duplicate_and_executed_side_only(self):
        self.assertIsNone(self.observe(0)['ask_change'])
        self.assertIsNone(self.observe(1)['ask_change'])
        changed = self.observe(2, bid=98)
        self.assertIsNone(changed['ask_change'])
        self.assertEqual(changed['bid_change']['previous_price'], 99)
        self.assertEqual(changed['bid_change']['baseline_sequence'], 1)
        repeated = self.observe(3, bid=98)
        self.assertEqual(repeated['bid_change'], changed['bid_change'])
        size = self.observe(4, bid=98, ask_size=2)['ask_change']
        self.assertEqual(size['previous_price'], size['price'])
        self.assertNotEqual(size['previous_size'], size['size'])

    def test_missing_side_same_frame_and_clock_rollback_require_new_baseline(self):
        self.observe(0)
        self.assertIsNone(self.observe(1, ask=101, next_frame=False)['ask_change'])
        self.assertIsNotNone(self.observe(2, ask=102)['ask_change'])
        self.assertIsNone(self.observe(3, ask_size=None)['ask_change'])
        self.assertIsNone(self.observe(4)['ask_change'])
        self.assertIsNone(self.observe(3, ask=101)['ask_change'])
        self.assertIsNone(self.observe(5, ask=101)['ask_change'])
        self.assertIsNotNone(self.observe(6, ask=102)['ask_change'])

    def test_reconnect_and_resubscribe_do_not_compare_with_old_state(self):
        self.observe(0)
        first = self.observe(1, ask=101)
        self.clock.reset()
        after = self.observe(2, ask=102)
        self.assertNotEqual(after['btc_stream_epoch'], first['btc_stream_epoch'])
        self.assertIsNone(after['ask_change'])
        self.assertIsNone(self.observe(3, ask=102)['ask_change'])
        self.assertIsNotNone(self.observe(4, ask=103)['ask_change'])
        self.clock.forget('fixture')
        self.assertIsNone(self.observe(5, ask=104)['ask_change'])

    def test_source_time_cannot_be_evaded_by_unchanged_untimed_replay(self):
        self.observe(0)
        changed = self.observe(1, ask=101, event={'askTime': 1})
        self.assertIsNotNone(changed['ask_observed_at'])
        self.assertIsNone(changed['ask_change'])
        self.assertIsNone(self.observe(2, ask=101)['ask_change'])
        self.assertIsNotNone(self.observe(3, ask=102)['ask_change'])

    def test_real_feed_payload_connection_epoch_and_diagnostics(self):
        from helpers.btc_clock_fixture import scenarios
        cases = {s['name']: s['payload'] for s in scenarios()}
        row = lambda name: cases[name]['quotes'][0]
        self.assertIsNone(row('snapshot')['ask_ts'])
        self.assertIsNone(row('snapshot')['ask_change'])
        self.assertIsNotNone(row('ask_price')['ask_change'])
        self.assertEqual(row('aged_replay')['ask_change'], row('ask_price')['ask_change'])
        self.assertNotEqual(row('aged_replay')['ask_received_ts'], row('ask_price')['ask_received_ts'])
        self.assertFalse(row('disconnected')['available'])
        self.assertIsNone(row('reconnect_snapshot')['ask_change'])
        self.assertNotEqual(row('reconnect_snapshot')['stream_epoch'], row('close_bid')['stream_epoch'])
        self.assertEqual(cases['reconnect_ask_size']['wire_time_diagnostics']['fields'],
                         {'bidTime': {'number:zero': 3}, 'askTime': {'string:zero': 3}})


if __name__ == '__main__': unittest.main()
