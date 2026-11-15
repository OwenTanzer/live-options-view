"""Exercise the fill boundary and account isolation through the forward adapter."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crassus import forward_skew as f
from crassus.clock import ET
from crassus.client import Book
from crassus.market import Quote
from crassus.skew_bars import ingest
from crassus.strategy import Decision

SYM = "QQQ261116C00680000"
START = datetime(2026, 11, 16, 10, 30, tzinfo=ET)


class Comparison(unittest.TestCase):
    def test_matched_policy(self):
        self.assertIsNone(
            f.arm_for(SimpleNamespace(username="crassus_oiskew", params={}))
        )
        for user, arm in f.ARMS.items():
            self.assertEqual(
                f.arm_for(
                    SimpleNamespace(
                        username=user,
                        strategy_id="oi_skew_qqq",
                        params={"forward_skew_arm": arm},
                    )
                ),
                arm,
            )
        with self.assertRaises(ValueError):
            f.arm_for(
                SimpleNamespace(
                    username="crassus_oiskew", params={"forward_skew_arm": "stop"}
                )
            )

    def scenario(self, arm):
        account = SimpleNamespace(
            username=next(k for k, v in f.ARMS.items() if v == arm)
        )
        trades = []
        bars = SimpleNamespace(rows=[], error=None)
        with TemporaryDirectory() as folder:

            def cycle(n, bid, phase=None):
                now = START + timedelta(minutes=5 * n)
                bars.rows.append({"event_end": now.isoformat()})
                ctx = SimpleNamespace(
                    now_et=now,
                    book=Book(trades),
                    session_phase="open",
                    snapshot=SimpleNamespace(
                        timestamp=now.isoformat(), underlying_price=680
                    ),
                    quotes=lambda syms: {
                        SYM: Quote(
                            SYM, bid, bid + 0.01, now.isoformat(), now.isoformat()
                        )
                    },
                )
                signal = (
                    Decision("buy", "valid skew", "oi_skew_qqq", "1", SYM, 1)
                    if ctx.book.is_flat
                    else Decision.no_trade("supported", "oi_skew_qqq", "1")
                )
                signal.metadata = {
                    "imbalance_ratio": 0.5,
                    "params": {"imbalance_threshold": 0.2},
                }
                row = (
                    {"phase": phase, "row_id": str(n), "event_end": now.isoformat()}
                    if phase
                    else None
                )
                with patch.object(f, "oi_skew_qqq", return_value=signal), patch.object(
                    f, "direction", return_value=row
                ):
                    return f.decide(ctx, account, arm, bars, Path(folder), str(n))

            def fill(n, d, price):
                trades.append(
                    dict(
                        sym=d.symbol,
                        side=d.action,
                        qty=d.quantity,
                        price=price,
                        execution_request_id=str(n),
                    )
                )

            d = cycle(0, 1.4)
            self.assertEqual(d.action, "buy")
            fill(0, d, 1.4)
            if arm == "stop":
                d = cycle(1, 0.91)
                self.assertEqual(d.action, "sell")
                # Rejected proposal: book still held; retry at inclusive boundary.
                d = cycle(2, 0.91)
                self.assertEqual(d.action, "sell")
                fill(2, d, 0.90)
                self.assertEqual(cycle(7, 1).action, "no_trade")
            else:
                self.assertEqual(cycle(1, 1.70, "continuation").action, "no_trade")
                d = cycle(2, 1.62, "breakdown")
                self.assertEqual(d.action, "sell")
                fill(2, d, 1.61)
                self.assertEqual(cycle(3, 1.65, "recovery").action, "no_trade")
                d = cycle(4, 1.65, "recovery")
                self.assertEqual(
                    d.action, "buy" if arm == "protect_reentry" else "no_trade"
                )
                if d.is_trade:
                    fill(4, d, 1.80)
                    self.assertEqual(cycle(5, 1.17).action, "sell")

    def test_stop(self):
        self.scenario("stop")

    def test_protect(self):
        self.scenario("protect_only")

    def test_recovery(self):
        self.scenario("protect_reentry")

    def test_completed_chart_only(self):
        t = int(START.timestamp())
        data = {
            "chart": {
                "result": [
                    {
                        "meta": {"symbol": "QQQ", "dataGranularity": "5m"},
                        "timestamp": [t, t + 300],
                        "indicators": {
                            "quote": [
                                {k: [1, 2] for k in ("open", "high", "low", "close")}
                            ]
                        },
                    }
                ]
            }
        }
        rows = []
        received = (START + timedelta(minutes=5, seconds=3)).astimezone(timezone.utc)
        ingest(data, received, rows)
        ingest(data, received, rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 1)
        self.assertEqual(rows[0]["available_at"], received.isoformat())
        late = []
        ingest(data, received + timedelta(minutes=8), late)
        self.assertFalse(late)
        data["chart"]["result"][0]["timestamp"][1] = t + 240
        partial = []
        ingest(data, received, partial)
        self.assertEqual(partial, [])

    def test_premarket_anchor_matches_baseline(self):
        from verify_oi_skew import make_ctx, FLAT_ROWS, fresh_quote

        symbol = "QQQ240101C00400000"
        with TemporaryDirectory() as folder:
            proposals = []
            for user, arm in f.ARMS.items():
                account = SimpleNamespace(username=user)
                for phase, instant, rows in [
                    ("pre", START - timedelta(hours=2), FLAT_ROWS),
                    ("open", START, None),
                ]:
                    ctx = make_ctx(
                        session_phase=phase,
                        timestamp=instant.isoformat(),
                        rows=rows,
                        quote_map={symbol: fresh_quote(symbol)},
                    )
                    ctx.now_et, ctx.account_alias = instant, user + "-match"
                    if arm == "baseline":
                        decision = f.oi_skew_qqq(ctx)
                        meta = decision.metadata
                    else:
                        decision = f.decide(
                            ctx,
                            account,
                            arm,
                            SimpleNamespace(rows=[], error=None),
                            Path(folder),
                            phase,
                        )
                        meta = decision.metadata["oi_signal"]["metadata"]
                    if phase == "open":
                        proposals.append(
                            (
                                decision.action,
                                decision.symbol,
                                meta["anchor_observed_at"],
                            )
                        )
            self.assertEqual(len(set(proposals)), 1)
            self.assertEqual(
                proposals[0], ("buy", symbol, (START - timedelta(hours=2)).isoformat())
            )

    def test_new_signal_keeps_session_budget(self):
        previous = f.ProtectionChain("put", 1, 1.4, 0, -1)
        previous.reentries = 2
        previous.protect_exits = 3
        previous.used_recovery_events.add("old-recovery")
        previous.external_exit("signal_exit")
        state = {
            "chain": f.freeze(previous),
            "pending": {
                "id": "new",
                "after": {"chain": None},
                "action": "buy",
                "quantity": 1,
                "symbol": SYM,
                "bar": 9,
            },
        }
        book = Book(
            [dict(execution_request_id="new", sym=SYM, side="buy", qty=1, price=1.2)]
        )
        chain = f.reconcile(state, SimpleNamespace(book=book), "protect_reentry")
        self.assertEqual((chain.side, chain.basis, chain.armed), ("call", 1.2, False))
        self.assertEqual(
            (chain.reentries, chain.protect_exits, chain.used_recovery_events),
            (2, 3, {"old-recovery"}),
        )

    def test_unusable_snapshot_times(self):
        for bad in ("broken", 42, "2026-11-16T14:00:00"):
            self.assertIsNone(f.snap_time(bad))


if __name__ == "__main__":
    unittest.main()
