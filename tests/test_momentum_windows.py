"""Regular-session snapshots, including late restarts and partial lookbacks."""
import unittest
from datetime import datetime, timedelta, timezone

from market_data.live_market.market_signals import SpotPoint, momentum_windows


class Windows(unittest.TestCase):
    opening = datetime(2026, 11, 25, 14, 30, tzinfo=timezone.utc)

    def readings(self, minute, points, *, observed=None, day_open=100, bounds=True):
        now = self.opening + timedelta(minutes=minute)
        return momentum_windows(
            [SpotPoint(self.opening + timedelta(minutes=m), p) for m, p in points],
            now=now, session=now.date(), bounds=(self.opening, self.opening + timedelta(hours=3.5)) if bounds else None,
            observed=now if observed is None else self.opening + timedelta(minutes=observed),
            spot=110, day_open=day_open, max_age_seconds=90, max_overshoot=10, neutral_band=.05,
        )["readings"]

    def test_independent_windows(self):
        r = self.readings(10, [(-55, 70), (0, 100), (1, 101), (5, 105), (11, 200)])
        self.assertAlmostEqual(r["5m"]["return_pct"], 4.7619)
        self.assertAlmostEqual(r["9m"]["return_pct"], 8.9109)
        self.assertEqual(r["session"]["return_pct"], 10)
        self.assertEqual(r["15m"]["status"], "warming_up")
        self.assertEqual(r["60m"]["status"], "warming_up")

    def test_gaps_restart_and_session_boundary(self):
        self.assertEqual(self.readings(100, [(0, 100)])["5m"]["status"], "stale_anchor")
        self.assertEqual(self.readings(100, [])["session"]["return_pct"], 10)
        self.assertIsNone(self.readings(100, [], day_open=None)["session"]["return_pct"])
        self.assertEqual(self.readings(10, [], observed=5)["session"]["status"], "stale")
        for minute in (-1, 211):
            self.assertIsNone(self.readings(minute, [])["session"]["return_pct"])
        self.assertIsNone(self.readings(10, [], observed=11)["session"]["return_pct"])

if __name__ == "__main__":
    unittest.main()
