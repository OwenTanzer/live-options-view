"""Offline model units, expiry calendar and the collector-to-artifact path."""
import csv
import io
import math
import unittest
from datetime import datetime, timedelta, timezone

from test_snapshot_provenance import FakeS3, QQQ_STRIKES, collector, state_for
from market_data.live_market.option_sensitivities import FIELDS, model_greeks, years_to_reference_close

NOW = datetime(2026, 11, 9, 17, 34, tzinfo=timezone.utc)


def price_delta(spot, strike, iv, years):
    width = iv * math.sqrt(years)
    d1 = math.log(spot / strike) / width + width / 2
    cdf = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
    return spot * cdf(d1) - strike * cdf(d1 - width), cdf(d1)


class OptionSensitivities(unittest.TestCase):
    def test_four_derivatives_with_units_and_time_direction(self):
        s, k, v, t = 601., 600., .25, 1 / 365
        ds, dv, dt = .001, .00001, t / 100000
        result = model_greeks(s, k, v, t)
        up = price_delta(s, k, v + dv, t)
        down = price_delta(s, k, v - dv, t)
        expected = [(price_delta(s + ds, k, v, t)[1] - price_delta(s - ds, k, v, t)[1]) / (2 * ds),
                    (up[1] - down[1]) / (2 * dv * 100), (up[0] - down[0]) / (2 * dv * 100),
                    (price_delta(s, k, v, t - dt)[1] - price_delta(s, k, v, t + dt)[1]) / (2 * dt * 365)]
        for name, value in zip(FIELDS, expected):
            self.assertAlmostEqual(result[name], value, places=7)

    def test_greeks_event_time_is_paired_with_its_iv(self):
        from test_feed_reauth import make_feed
        feed = make_feed()
        event = {"eventType": "Greeks", "eventSymbol": "C", "volatility": .25,
                 "time": int(NOW.timestamp() * 1000)}
        feed._ingest([event])
        self.assertEqual(feed.get_state()["C"]["iv_observed_at"], NOW.isoformat())
        feed._ingest([{**event, "volatility": None, "time": event["time"] + 1000}])
        self.assertEqual(feed.get_state()["C"]["iv_observed_at"], NOW.isoformat())
        feed._ingest([{**event, "volatility": .24, "time": None}])
        self.assertIsNone(feed.get_state()["C"]["iv_observed_at"])

    def test_calendar_and_missing(self):
        before = datetime(2026, 11, 27, 17, tzinfo=timezone.utc)
        self.assertAlmostEqual(years_to_reference_close("2026-11-27", before), 1 / (24 * 365))
        self.assertIsNone(years_to_reference_close("2026-11-27", before + timedelta(hours=1)))
        self.assertIsNone(years_to_reference_close("2026-11-26", before))
        for invalid in (None, 0, -1, float('nan'), float('inf')):
            self.assertTrue(all(x is None for x in model_greeks(601., 600., invalid, .001).values()))

    def test_snapshot_archives_and_publishes_same_readings(self):
        stamp = NOW.isoformat()
        state = state_for(dict(last=601., last_ts=stamp, last_observed_at=stamp))
        for row in [state['.QQQ261109C600'], state['.QQQ261109P600']]:
            row.update(volatility=.25, gamma=.12, vega=.27, iv_observed_at=stamp)
        state['.QQQ261109P600']['iv_observed_at'] = (NOW - timedelta(minutes=5)).isoformat()
        s3 = FakeS3()
        session = collector.UnderlyingSession("QQQ")
        collector.take_snapshot(s3, None, QQQ_STRIKES, "2026-11-09", "0DTE_Regular",
                                NOW.date(), collector.Counters(), collector.SnapshotTracker(),
                                session, capture=(NOW, state))
        latest = s3.json(session.latest_key)
        archived = list(csv.DictReader(io.StringIO(s3.store[latest["snapshot_key"]].decode())))
        call, put = latest["rows"]
        self.assertEqual(call['Gamma'], .12)
        self.assertEqual(call['Vega'], .27)
        self.assertEqual(call['IVObservedAt'], stamp)
        for name in FIELDS:
            self.assertTrue(math.isfinite(call[name]))
            self.assertEqual(float(archived[0][name]), call[name])
            self.assertIsNone(put[name])
            self.assertEqual(archived[1][name], '')
