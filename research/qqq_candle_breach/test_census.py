import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from census import Bar, ET, MINUTE, census, classify, load_sessions, outcome, prior_atr

START = datetime(2026, 10, 29, 9, 30, tzinfo=ET)


def bar(i, o, h, l, c):
    return Bar(START + i * MINUTE, *map(Decimal, (o, h, l, c)))


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.red = bar(0, 10, 11, 8, 9)
        self.green = bar(0, 9, 11, 8, 10)

    def test_touches_dojis_and_same_color_keep_trial(self):
        for second in [bar(1, 9, 11, 8, 10), bar(1, 9, 12, 8, 9), bar(1, 10, 13, 8, 9)]:
            self.assertFalse(classify(self.red, second)["event"])
        self.assertFalse(classify(self.green, bar(1, 10, 11, 8, 9))["event"])
        self.assertIsNone(classify(bar(0, 9, 11, 8, 9), self.green))

    def test_close_on_level_and_overlap(self):
        t = classify(self.red, bar(1, 10, 12, 7, 11))
        self.assertTrue(t["event"] and t["both_extremes"])
        self.assertEqual(t["close_class"], "at_or_inside")
        self.assertEqual(t["decision_at"], (START + 2 * MINUTE).isoformat())

    def test_down_gap_and_close_beyond(self):
        t = classify(self.green, bar(1, 7, 11, 5, 6))
        self.assertTrue(t["event"] and t["open_beyond"])
        self.assertFalse(t["both_extremes"])
        self.assertEqual(t["close_class"], "beyond")

    def test_up_gap_and_gap_between_timestamps(self):
        self.assertTrue(classify(self.red, bar(1, 12, 14, 11, 13))["open_beyond"])
        self.assertIsNone(classify(self.red, bar(2, 12, 14, 11, 13)))
        self.assertIsNone(classify(self.red, bar(1440, 9, 12, 7, 11)))

    def test_exact_endpoint_and_sign(self):
        second = bar(1, 10, 11, 6, 7)
        endpoint = bar(6, 8, 9, 5, 6)
        self.assertEqual(outcome(second, endpoint, "down"), 1)
        self.assertEqual(outcome(second, endpoint, "up"), -1)
        for missing in [None, bar(7, 8, 9, 5, 6), bar(1446, 8, 9, 5, 6)]:
            self.assertIsNone(outcome(second, missing, "up"))

    def test_atr_prior_only(self):
        bars = {b.start: b for b in (bar(i, 8, 10, 6, 9) for i in range(15))}
        self.assertEqual(prior_atr(bars, START + 15 * MINUTE), 4)
        self.assertIsNone(prior_atr(bars, START + 14 * MINUTE))

    def test_fixture(self):
        sessions = load_sessions(sorted((Path(__file__).parent / "data").glob("*.csv")))
        self.assertEqual(len(sessions), 3)
        expected = [(44,172,36,195,31,32,12,1), (37,184,35,163,21,13,7,2),
                    (42,196,47,147,36,23,12,1)]
        fields = "up_events up_eligible down_events down_eligible at_or_inside both_extremes inside_and_both unknown_5m".split()
        for bars, values in zip(sessions.values(), expected):
            self.assertEqual(len(bars), 390)
            rows, counts = census(bars)
            self.assertEqual(tuple(counts[f] for f in fields), values)
            self.assertEqual(len(rows), counts["up_eligible"] + counts["down_eligible"])


if __name__ == "__main__":
    unittest.main()
