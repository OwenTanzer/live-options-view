import json
import unittest
from pathlib import Path

from research.options_replay.participant_models import simulate


class ParticipantModelTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "research/options_replay/participant_example.json"
        self.case = json.loads(path.read_text())

    def test_matched_trades_and_cash_settlement(self):
        rows = simulate(self.case, "flow_adjusted", "delta_target")
        self.assertEqual([r["units"] for r in rows], [2, 3, 2, 2])
        self.assertEqual([sum(f["option_volume"] for f in r["matched_legs"]) for r in rows],
                         [4, 2, 2, 4])
        for row in rows:
            for fill in row["matched_legs"]:
                self.assertEqual(fill["manager"] + fill["dealer"], 0)

    def test_same_options_different_stock_hedges(self):
        target = simulate(self.case, "scheduled", "delta_target")
        band = simulate(self.case, "scheduled", "delta_band")
        self.assertEqual([r["matched_legs"] for r in target], [r["matched_legs"] for r in band])
        self.assertEqual([r["units"] for r in target], [2, 2, 2, 2])
        self.assertEqual((target[0]["dealer_stock_order"], band[0]["dealer_stock_order"]), (100, 90))
        self.assertAlmostEqual(band[2]["dealer_stock_order"], 0)
        for row in band:
            self.assertLessEqual(abs(row["residual_delta_shares"]), 10.000001)

    def test_prefix_and_invalid_flow(self):
        full = simulate(self.case, "flow_adjusted", "delta_band")
        self.case["steps"] = self.case["steps"][:2]
        self.assertEqual(simulate(self.case, "flow_adjusted", "delta_band"), full[:2])
        self.case["steps"][1]["cash_flow_units"] = -3
        with self.assertRaisesRegex(ValueError, "redemptions"):
            simulate(self.case, "flow_adjusted", "delta_band")
