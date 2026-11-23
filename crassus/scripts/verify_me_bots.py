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
from crassus.strategies.me_bots_inputs import (
    Inputs,
    closed_minutes,
    previous_minutes,
    validate_prior,
)
from crassus.exchange_calendar import preceding_session
from crassus.strategies.me_bots_rules import (
    Bar,
    MINUTE,
    candles,
    entry,
    exit_reason,
    qualified_trigger,
    admitted,
    target_for,
)

START = datetime(2026, 11, 23, 9, 30, tzinfo=ET)
LEVELS = {
    "root": "QQQ",
    "session": START.date().isoformat(),
    "levels": [
        {
            "price": 102,
            "side": "long",
            "source": "fixture shelf",
            "first_target": 110,
            "premarket_rejection": True,
        }
    ],
}
PLAN = {"first_seen": (START - MINUTE).isoformat(), "levels": LEVELS}


def minutes():
    data = []
    for i in range(15 * 9):
        group = i // 9
        span = {13: 2, 14: 3}.get(group, 1)
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
            if kind in ("opening", "runner"):
                end = START + (30 if kind == "runner" else 3) * MINUTE
                start = START if kind == "runner" else end - MINUTE
                early = Bar(start, end, 100, 103, 100, 103, 900)
                prior = [
                    replace(
                        b,
                        start=b.start - timedelta(days=3),
                        end=b.end - timedelta(days=3),
                    )
                    for b in candles(rows, 9)[:-1]
                ]
                seq = (
                    [replace(early, high=101), replace(early, high=102), early]
                    if kind == "opening"
                    else [early]
                )
                if kind == "runner":
                    last = START - timedelta(days=3)
                    prior = [
                        Bar(
                            last + i * 30 * MINUTE,
                            last + (i + 1) * 30 * MINUTE,
                            100,
                            102,
                            100,
                            101,
                            500,
                        )
                        for i in range(13)
                    ]
                self.assertIsNotNone(qualified_trigger(kind, early, prior, seq, LEVELS))
                self.assertIsNone(entry(kind, rows[:30], LEVELS))
                self.assertIsNone(entry(kind, rows, LEVELS))
                continue
            self.assertEqual(entry(kind, rows, LEVELS)["side"], 1, kind)
            self.assertIsNone(entry(kind, rows, {}))
            put = [
                replace(
                    b,
                    open=200 - b.open,
                    high=200 - b.low,
                    low=200 - b.high,
                    close=200 - b.close,
                )
                for b in rows
            ]
            short = {
                "levels": [
                    dict(
                        price=98, side="short", first_target=90, source="fixture shelf"
                    )
                ]
            }
            self.assertEqual(entry(kind, put, short)["side"], -1, kind)
        self.assertIsNone(
            entry("phelps", [replace(b, volume=100) for b in rows], LEVELS)
        )
        self.assertIsNone(entry("golden", rows[:-1], LEVELS))

    def test_research_examples_targets_and_nova(self):
        self.assertFalse(admitted(728.72, 730, 728, -1))
        self.assertTrue(admitted(719.1, 720, 717, -1))
        level = dict(price=720, side="short", source="documented shelf")
        self.assertIsNone(target_for(level, [level]))
        self.assertEqual(
            target_for(level, [level, dict(price=717), dict(price=715)]), 717
        )
        self.assertFalse(admitted(718.4, 720, 715, -1))
        rows = minutes()
        self.assertIsNone(
            entry("golden", rows, {"levels": [{**LEVELS["levels"][0], "price": 101.5}]})
        )
        seed = {"side": -1, "stop": 725, "nova": 720}
        hold = [
            Bar(
                START + i * MINUTE,
                START + (i + 1) * MINUTE,
                719,
                719.4,
                718.9,
                719.15,
                100,
            )
            for i in range(9)
        ]
        self.assertIsNone(exit_reason("runner", hold, seed, START, 719.1, hold[-1].end))
        self.assertIn(
            "Nova",
            exit_reason(
                "runner",
                [replace(b, high=721, close=720.3) for b in hold],
                seed,
                START,
                719.1,
                hold[-1].end,
            ),
        )

    def test_postfill_nova_in_partial_minute_and_predeclared_fallback(self):
        rows = [
            Bar(
                START + i * MINUTE,
                START + (i + 1) * MINUTE,
                100.1,
                100.2,
                99.6,
                99.8,
                100,
            )
            for i in range(9)
        ]
        seed = {"side": 1, "stop": 98, "nova": 100}
        self.assertIn(
            "Nova",
            exit_reason(
                "golden", rows, seed, START + 8.5 * MINUTE, 100.05, rows[-1].end
            ),
        )
        level = {"price": 730, "side": "short", "source": "acceptance shelf"}
        target = target_for(level, [level, {"price": 728}, {"price": 726}])
        self.assertEqual(target, 728)
        self.assertFalse(admitted(727.8, 730, target, -1))

    def test_exact_previous_exchange_session_coverage(self):
        # Monday after Thanksgiving 2026 follows the Friday half day.
        for day, prior, count in (
            ("2026-11-30", "2026-11-27", 210),
            ("2026-11-27", "2026-11-25", 390),
            ("2026-11-23", "2026-11-20", 390),
        ):
            target = datetime.fromisoformat(day).date()
            op, cl = preceding_session(target)
            self.assertEqual(op.date().isoformat(), prior)
            raw = [
                [(op + i * MINUTE).timestamp(), 100, 101, 99, 100, 1]
                for i in range(count)
            ]
            bars = validate_prior(raw, op, cl)
            self.assertEqual(len(candles(bars, 9)), count // 9)
            self.assertEqual(len(candles(bars, 30)), count // 30)
            with self.assertRaises(ValueError):
                validate_prior(raw[:-1], op, cl)
            with self.assertRaises(ValueError):
                validate_prior(raw[:10] + raw[9:-1], op, cl)
        self.assertIsNone(
            preceding_session(datetime.fromisoformat("2026-11-26").date())
        )

    def test_runner_requires_explicit_target_full_history_and_receipt(self):
        previous = START - timedelta(days=3)
        prior = [
            Bar(
                previous + i * 30 * MINUTE,
                previous + (i + 1) * 30 * MINUTE,
                90,
                92,
                90,
                91,
                100,
            )
            for i in range(13)
        ]
        current = [
            Bar(START + i * MINUTE, START + (i + 1) * MINUTE, 100, 103, 100, 103, 100)
            for i in range(30)
        ]

        def resolve(bars=prior, seen=START, plan=LEVELS, current_rows=current):
            reference = dict(
                for_session=START.date().isoformat(),
                session=previous.date().isoformat(),
                first_seen=seen,
                thirty=bars,
                nine=[],
            )
            return entry("runner", current_rows, plan, reference)

        self.assertIsNotNone(
            resolve()
        )  # No prior-range edge or rising-range condition.
        # A long breakout may gap up and finish on the top quartile of a red candle.
        # The Runner's enumerated test is independent of candle-body direction.
        exact = [
            replace(b, open=100.9, high=101, low=100, close=100.75) for b in current
        ]
        exact_prior = [replace(b, high=91) for b in prior]
        self.assertIsNotNone(
            resolve(
                exact_prior,
                current_rows=exact,
                plan={
                    "levels": [
                        {
                            "price": 100.5,
                            "side": "long",
                            "first_target": 102,
                            "source": "fixture",
                        }
                    ]
                },
            )
        )
        self.assertIsNone(resolve(prior[:7]))
        self.assertIsNone(resolve(prior[:-1]))
        self.assertIsNone(resolve(seen=current[-1].end + timedelta(microseconds=1)))
        self.assertIsNone(
            resolve(
                plan={
                    "levels": [
                        {"price": 102, "side": "long", "source": "shelf"},
                        {"price": 110, "side": "long", "source": "target"},
                    ]
                }
            )
        )
        self.assertIsNone(resolve(current_rows=current[:-1]))
        self.assertIsNone(
            resolve(current_rows=[replace(b, close=101) for b in current])
        )
        self.assertIsNone(resolve(bars=[replace(b, high=95) for b in prior]))

    def test_prior_windows_exclude_trigger_and_truncated_tail(self):
        op, cl = preceding_session(START.date())
        raw = [
            [(op + i * MINUTE).timestamp(), 100, 101, 100, 101, 100] for i in range(390)
        ]
        # Last full nine-minute bar expands; the final three minutes are not a candle.
        for i in range(378, 387):
            raw[i][2] = 102
        for i in range(387, 390):
            raw[i][2] = 140
        bars = validate_prior(raw, op, cl)
        nine = candles(bars, 9)
        self.assertEqual(nine[-1].high, 102)
        current = [
            Bar(START + i * MINUTE, START + (i + 1) * MINUTE, 100, 103, 100, 103, 100)
            for i in range(9)
        ]
        ref = dict(
            for_session=START.date().isoformat(),
            session=op.date().isoformat(),
            first_seen=START,
            nine=nine,
            thirty=candles(bars, 30),
        )
        signal = entry("golden", current, LEVELS, ref)
        self.assertEqual(signal["prior_session"], op.date().isoformat())
        self.assertEqual(signal["reference_count"], 14)
        self.assertIsNone(entry("golden", current, LEVELS, dict(ref, nine=nine[-13:])))
        self.assertIsNone(
            entry("golden", current, LEVELS, dict(ref, first_seen=START + 10 * MINUTE))
        )

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
        stamps.append(stamps[-1] + 12)
        now = START + 3 * MINUTE + timedelta(seconds=15)
        self.assertEqual(len(closed_minutes(payload, now)), 3)
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
                    spot=103, spot_ts=snap.timestamp, freshness="live"
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
                nova=99,
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
                        nova=99,
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
