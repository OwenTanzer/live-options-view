"""Offline fixtures for the inactive Me Bots. Run from the crassus directory."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from replay_me_trails import paired
from crassus.config import load_accounts

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crassus.clock import ET
from crassus.strategy import REGISTRY, StrategyContext
from crassus.strategies.me_bots import IDS, completed_losses
from crassus.strategies.me_bots_inputs import Inputs, closed_minutes
from crassus.strategies.me_bots_rules import Bar, MINUTE, candles, entry, exit_reason

START = datetime(2026, 11, 23, 9, 30, tzinfo=ET)
LEVELS = {
    "root": "QQQ",
    "session": START.date().isoformat(),
    "long": {
        "first_target": 150,
        "max_trigger_range_points": 10,
        "opening_key_level": 99,
        "premarket_rejection_level": 100,
    },
}
PLAN = {"first_seen": (START - MINUTE).isoformat(), "levels": LEVELS}


def minutes():
    data = []
    for i in range(15 * 9):
        group = i // 9
        span = {13: 2, 14: 4}.get(group, 1)
        start = START + i * MINUTE
        data.append(
            Bar(
                start,
                start + MINUTE,
                100,
                100 + span,
                100,
                100 + span,
                400 if group == 14 else 100,
            )
        )
    return data


class MeBots(unittest.TestCase):
    def test_all_five_registered_inactive_and_reachable(self):
        roster = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "accounts.me-bots.provisional.json"
            ).read_text()
        )
        self.assertEqual(set(IDS), {row["strategy_id"] for row in roster["accounts"]})
        self.assertTrue(all(not row["active"] for row in roster["accounts"]))
        rows = minutes()
        for id, kind in IDS.items():
            self.assertIn(id, REGISTRY)
            trigger = (
                [replace(rows[i], high=101 + i, close=101 + i) for i in range(3)]
                if kind == "opening"
                else rows
            )
            self.assertEqual(entry(kind, trigger, LEVELS)["side"], 1, kind)
            self.assertIsNone(entry(kind, trigger, {}))
            put = [
                replace(
                    b,
                    open=200 - b.open,
                    high=200 - b.low,
                    low=200 - b.high,
                    close=200 - b.close,
                )
                for b in trigger
            ]
            short = {
                "short": dict(
                    first_target=50,
                    max_trigger_range_points=10,
                    opening_key_level=101,
                    premarket_rejection_level=100,
                )
            }
            self.assertEqual(entry(kind, put, short)["side"], -1, kind)
        self.assertIsNone(
            entry(
                "runner", rows, {"long": {**LEVELS["long"], "opening_key_level": 150}}
            )
        )
        self.assertIsNone(
            entry("phelps", [replace(b, volume=100) for b in rows], LEVELS)
        )
        self.assertIsNone(entry("golden", rows[:-1], LEVELS))

    def test_input_identity_gaps_and_forming_row(self):
        stamps = [int((START + i * MINUTE).timestamp()) for i in range(4)]
        q = {
            k: [v] * 4
            for k, v in dict(open=100, high=102, low=99, close=101, volume=100).items()
        }
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"symbol": "QQQ", "dataGranularity": "1m"},
                        "timestamp": stamps,
                        "indicators": {"quote": [q]},
                    }
                ]
            }
        }
        self.assertEqual(len(closed_minutes(payload, START + 3 * MINUTE)), 3)
        stamps[2] += 30
        with self.assertRaises(ValueError):
            closed_minutes(payload, START + 3 * MINUTE)

    def test_plan_first_seen_and_restored_session(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "plan-2026-11-23.json").write_text(json.dumps(LEVELS))
            feed = Inputs(d)
            self.assertFalse(feed.plan(START))
            self.assertEqual(feed.plan(START - MINUTE), PLAN)
            (d / "sealed-2026-11-23.json").write_text(
                json.dumps({**PLAN, "first_seen": "2026-11-22T08:45:00-05:00"})
            )
            with self.assertRaises(ValueError):
                feed.plan(START)

    def test_exit_clocks_and_ambiguity(self):
        b = Bar(START, START + MINUTE, 102, 104, 97, 98, 100)
        seed = {"side": 1, "stop": 99}
        self.assertIn(
            "closed through",
            exit_reason("golden", [b], seed, START + MINUTE / 2, 102, b.end),
        )
        self.assertIn(
            "unknown",
            exit_reason(
                "golden", [replace(b, close=102)], seed, START + MINUTE / 2, 102, b.end
            ),
        )
        flat = [
            Bar(START + i * MINUTE, START + (i + 1) * MINUTE, 100, 100.5, 100, 100, 100)
            for i in range(25)
        ]
        self.assertEqual(
            exit_reason("phelps", flat, seed, START, 100, flat[-1].end),
            "Phelps time stop",
        )
        self.assertEqual(
            exit_reason(
                "ratchet", flat, seed, START, 100, START.replace(hour=15, minute=30)
            ),
            "15:30 ET cutoff",
        )

    def test_entry_quote_restart_and_no_retrospective_entry(self):
        rows = minutes()
        symbol = "QQQ261123C00104000"
        with tempfile.TemporaryDirectory() as td:
            symbol_quote = SimpleNamespace(
                bid=0.98, ask=1.02, is_executable=True, age_seconds=2
            )
            snap = SimpleNamespace(
                expiration="2026-11-23",
                underlying_price=104,
                atm=lambda _: {"OptionSymbol": symbol},
            )
            ctx = StrategyContext(
                snap,
                {},
                SimpleNamespace(trades=[], positions={}),
                START,
                "open",
                lambda _: {symbol: symbol_quote},
                account_alias="Golden",
            )

            def decide(bars):
                ctx.now_et = bars[-1].end + timedelta(seconds=5)
                snap.timestamp = ctx.now_et.isoformat()
                snap.underlying_market = SimpleNamespace(
                    spot=104, spot_ts=snap.timestamp, freshness="live"
                )
                ctx.params = {
                    "_me_directory": Path(td),
                    "_me_inputs": (PLAN, bars, None),
                }
                return REGISTRY["me_golden_qqq"](ctx)

            self.assertEqual(decide(rows[:-2]).action, "no_trade")
            self.assertEqual(decide(rows[:-1]).action, "no_trade")
            symbol_quote.age_seconds = -1
            self.assertEqual(decide(rows).action, "no_trade")
            symbol_quote.age_seconds = 2
            decision = decide(rows)
            self.assertEqual(
                (
                    decision.action,
                    decision.symbol,
                    decision.quantity,
                    decision.limit_price,
                ),
                ("buy", symbol, 1, 1.02),
            )
            self.assertIn("already attempted", decide(rows).reason)
        with tempfile.TemporaryDirectory() as other:
            ctx.params["_me_directory"] = Path(other)
            self.assertEqual(REGISTRY["me_golden_qqq"](ctx).action, "no_trade")

    def test_paired_widths_same_arming_and_definite_exit(self):
        entry_time = START + 9 * MINUTE
        bars = [
            (100, 102.5, 100, 102.5),
            (102.5, 103, 101.7, 102),
            (102, 102, 100, 100),
        ]
        rows = [
            dict(
                start=(entry_time + i * MINUTE).isoformat(),
                open=o,
                high=h,
                low=l,
                close=c,
            )
            for i, (o, h, l, c) in enumerate(bars)
        ]
        result = paired(
            dict(
                side=1,
                entry_spot=100,
                entry_minute=entry_time.isoformat(),
                initial_stop=98,
                rows=rows,
            )
        )
        self.assertEqual(result[1]["points_range"], [1.5, 2])
        self.assertEqual(result[2]["points_range"], [0.5, 1])
        self.assertEqual(result[1]["possible_exit_minutes"], [0, 1])
        self.assertEqual(result[2]["possible_exit_minutes"], [0, 2])

    def test_replay_session_boundaries_and_scoped_roster(self):
        for start in (START, START.replace(day=22, minute=39), START.replace(hour=17)):
            bar = dict(start=start.isoformat(), open=100, high=101, low=99, close=101)
            with self.assertRaises(ValueError):
                paired(
                    dict(
                        side=1,
                        entry_spot=100,
                        entry_minute=start.isoformat(),
                        initial_stop=98,
                        rows=[bar],
                    )
                )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "roster.json"
            p.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "alias": "test",
                                "username": "crassus_me_golden",
                                "strategy_id": "me_golden_qqq",
                                "active": True,
                                "password": "fixture",
                            }
                        ]
                    }
                )
            )
            with patch.dict(
                os.environ,
                {"CRASSUS_PW_OA233": "fixture", "CRASSUS_NEWTON_CONFIRMED": "1"},
            ):
                self.assertEqual(
                    [a.username for a in load_accounts(p, me_paper_only=True)],
                    ["crassus_me_golden"],
                )
                p.write_text(p.read_text().replace("crassus_me_golden", "foreign_bot"))
                with self.assertRaises(ValueError):
                    load_accounts(p, me_paper_only=True)

    def test_three_realized_losses(self):
        ts = START
        trades = []
        for i in range(3):
            for side, price in (("buy", 1.2), ("sell", 1.1)):
                trades.append(
                    dict(ts=ts.isoformat(), sym="QQQ", side=side, qty=1, price=price)
                )
                ts += MINUTE
        self.assertEqual(completed_losses(trades, START.date()), (3, None))


if __name__ == "__main__":
    unittest.main()
