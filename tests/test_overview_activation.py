"""Activation regression checks without provider calls or credentials."""
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from market_data.live_market import overview_sources as sources, prices
from market_data.overview import INSTRUMENTS, COMPACT_LABELS, producer_maps


class Activation(unittest.TestCase):
    def test_exact_universe_and_preserved_strip(self):
        self.assertEqual(len(INSTRUMENTS), 100)
        self.assertTrue(all(r['enabled'] for r in INSTRUMENTS))
        self.assertEqual(len(COMPACT_LABELS), 18)
        dx, yahoo = producer_maps()
        self.assertEqual(len(dx), 99)
        self.assertIsNone(dx['USD/JPY'])
        self.assertNotIn('JPY/USD', yahoo)
        self.assertEqual(yahoo['USD/JPY'], 'JPY=X')

    def test_resolver_uses_active_contract_and_preserves_it_on_failure(self):
        items = [{'active-month': True, 'active': True, 'expiration-date': '2099-01-20',
            'future-product': {'code': 'CL'}, 'streamer-symbol': '/CLF99:XNYM'},
            {'active-month': False, 'active': True, 'expiration-date': '2099-02-20',
            'future-product': {'code': 'CL'}, 'streamer-symbol': '/CLG99:XNYM'}]
        with patch.object(sources, 'PRICE_TICKERS', {}), patch.object(sources, 'RESOLVED_CONTRACTS', {}), patch.object(sources, '_RESOLVED_FOR_DATE', None), patch.object(sources, '_RESOLVE_RETRY_AT', None), patch.object(sources, '_RESOLVE_ATTEMPTS', 0), patch.object(sources.requests, 'get') as get:
            get.return_value.json.return_value = {'data': {'items': items}}
            sources.resolve_futures('test')
            self.assertEqual(sources.PRICE_TICKERS['/CL'], '/CLF99:XNYM')
            self.assertEqual(sources.RESOLVED_CONTRACTS['/CL']['root'], 'CL')
            # A same-day repeat is a cache read, so the shared lane/session
            # caller cannot clear the other's symbols mid-publish.
            sources.resolve_futures('test')
            self.assertEqual(get.call_count, 1)
            # A failed forced refresh keeps the last good contract subscribed
            # and records a bounded retry. Clearing it here is exactly the
            # November 9 defect that froze every futures row for an evening.
            get.side_effect = RuntimeError('provider unavailable')
            sources.resolve_futures('test', force=True)
            self.assertEqual(sources.PRICE_TICKERS['/CL'], '/CLF99:XNYM')
            self.assertEqual(sources.RESOLVED_CONTRACTS['/CL']['root'], 'CL')
            self.assertIsNotNone(sources._RESOLVE_RETRY_AT)
            self.assertEqual(sources.resolution_status()['last_error'], 'RuntimeError')

    def test_treasury_dates_sort_and_yield_units_are_preserved(self):
        xml = '<root><properties><NEW_DATE>2026-11-06T00:00:00</NEW_DATE><BC_2YEAR>4.12</BC_2YEAR></properties><properties><NEW_DATE>2026-11-05T00:00:00</NEW_DATE><BC_2YEAR>4.10</BC_2YEAR></properties></root>'
        row = sources.treasury_observation(xml)
        self.assertEqual(row['price'], 4.12)
        self.assertEqual(row['reference']['value'], 4.10)
        self.assertEqual(row['observation_date'], '2026-11-06')
        self.assertIsNone(row['observed_at'])

    def test_late_success_is_consumed_without_second_fetch(self):
        release = threading.Event()
        def delayed(labels):
            release.wait(2)
            return {'AAPL': {'price': 123, 'observation_date': '2026-11-06'}}
        with patch.object(prices, '_yf_cache', {'ts': None, 'data': None}), patch.object(prices, '_yf_inflight', None), patch.object(prices, '_yf_result', None), patch.object(prices, 'YF_FETCH_TIMEOUT_SECS', 0.01), patch.object(prices, 'fetch_yf_prices', side_effect=delayed) as fetch:
            first = prices.fetch_yf_prices_bounded(['AAPL'])
            self.assertIsNone(first['AAPL']['price'])
            worker = prices._yf_inflight
            release.set()
            worker.join(2)
            second = prices.fetch_yf_prices_bounded(['AAPL'])
            self.assertEqual(second['AAPL']['price'], 123)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args, ({'AAPL'},))


if __name__ == '__main__':
    unittest.main()
