"""Exercise the fill boundary and account isolation through the forward adapter."""

import json
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
from crassus.order_flow import ContractFlow, FlowSnapshot
from crassus.strategies import flow_skew
from crassus.market import Quote
from crassus.skew_bars import ingest
from crassus.strategy import Decision

SYM = "QQQ261116C00680000"
IBIT_SYM = "IBIT261120C00060000"
ROOT_SYMS = {
    "QQQ": SYM,
    "IBIT": IBIT_SYM,
    "SLV": "SLV261120C00030000",
    "DRAM": "DRAM261120C00040000",
}
START = datetime(2026, 11, 16, 10, 30, tzinfo=ET)


class Comparison(unittest.TestCase):
    def test_matched_policy(self):
        self.assertIsNone(
            f.arm_for(SimpleNamespace(username="crassus_oiskew", params={}))
        )
        for root, family in f.FAMILIES.items():
            for user, arm in family["arms"].items():
                account = SimpleNamespace(
                    username=user,
                    strategy_id=family["strategy_id"],
                    params={"forward_skew_arm": arm},
                )
                self.assertEqual(f.arm_for(account), arm)
                self.assertEqual(f.root_for(account), root)
        # An IBIT arm cannot run the QQQ signal, and vice versa.
        for user, sid in (
            ("crassus_oif_ibit_protect", "oi_skew_qqq"),
            ("crassus_oif_protect", "oi_skew_ibit_adaptive"),
        ):
            with self.assertRaises(ValueError):
                f.arm_for(
                    SimpleNamespace(
                        username=user,
                        strategy_id=sid,
                        params={"forward_skew_arm": "protect_only"},
                    )
                )
        with self.assertRaises(ValueError):
            f.arm_for(
                SimpleNamespace(
                    username="crassus_oiskew", params={"forward_skew_arm": "stop"}
                )
            )

    def scenario(self, arm, root="QQQ"):
        arms = f.FAMILIES[root]["arms"]
        strategy_id = f.FAMILIES[root]["strategy_id"]
        sym = ROOT_SYMS[root]
        signal_name = "oi_skew_qqq" if root == "QQQ" else "reference_signal"
        account = SimpleNamespace(username=next(k for k, v in arms.items() if v == arm))
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
                        sym: Quote(
                            sym, bid, bid + 0.01, now.isoformat(), now.isoformat()
                        )
                    },
                )
                signal = (
                    Decision("buy", "valid skew", strategy_id, "1", sym, 1)
                    if ctx.book.is_flat
                    else Decision.no_trade("supported", strategy_id, "1")
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
                with patch.object(f, signal_name, return_value=signal), patch.object(
                    f, "direction", return_value=row
                ):
                    d = f.decide(ctx, account, arm, bars, Path(folder), str(n))
                    self.assertEqual(d.strategy_id, strategy_id)
                    return d

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

    def test_flow_protection_reentry(self):
        catalog = Path(__file__).resolve().parents[1] / "accounts.example.json"
        account = SimpleNamespace(**next(a for a in json.loads(catalog.read_text())["accounts"]
                                         if a["username"] == f.FLOW_RECOVERY_USER))
        arm = f.arm_for(account)
        self.assertEqual(arm, "protect_reentry")
        self.assertFalse(account.active)
        self.assertEqual(f.root_for(account), "QQQ")
        with self.assertRaises(ValueError):
            f.arm_for(SimpleNamespace(**{**vars(account), "strategy_id": "oi_skew_qqq"}))

        trades = []
        bars = SimpleNamespace(rows=[], error=None)
        flow_skew.reset_state()
        self.addCleanup(flow_skew.reset_state)
        with TemporaryDirectory() as folder:
            def cycle(n, bid, phase=None, gap=False):
                now = START + timedelta(minutes=5*n)
                end = int(now.timestamp()*1000) - 1000
                contracts = {}
                for strike in range(678, 683):
                    symbol = f"QQQ261116C{strike*1000:08d}"
                    contracts[symbol] = ContractFlow(symbol, "call", strike, 600*n, 0,
                                                     3000 if n >= 7 else 0, 0)
                projection = FlowSnapshot(now.date().isoformat(), end,
                                          ((end-5000, None),) if gap else (), contracts)
                bars.rows.append({"event_end": now.isoformat()})
                row = dict(phase=phase, row_id=str(n), event_end=now.isoformat()) if phase else None
                snapshot = SimpleNamespace(timestamp=now.isoformat(), underlying_price=680,
                                           atm=lambda side: {"OptionSymbol": SYM, "Strike": 680})
                ctx = SimpleNamespace(now_et=now, book=Book(trades), session_phase="open",
                                      account_alias=account.alias, params=account.params, snapshot=snapshot,
                                      quotes=lambda symbols: {SYM: Quote(SYM, bid, bid+.01,
                                                                          now.isoformat(), now.isoformat())})
                with patch.object(flow_skew._reader, "read", return_value=projection), patch.object(
                    f, "direction", return_value=row
                ):
                    d = f.decide(ctx, account, arm, bars, Path(folder), str(n))
                self.assertIn("flow_signal", d.metadata)
                self.assertEqual(d.strategy_id, "flow_skew_qqq")
                if d.is_trade:
                    trades.append(dict(sym=d.symbol, side=d.action, qty=d.quantity, price=bid,
                                       execution_request_id=str(n)))
                return d

            for n in range(3):
                self.assertEqual(cycle(n, 1).action, "no_trade")
            self.assertEqual(cycle(3, 1).action, "buy")
            self.assertEqual(cycle(4, 1.25, "continuation").action, "no_trade")
            self.assertEqual(cycle(5, 1.15, "breakdown").action, "sell")
            self.assertEqual(cycle(6, 1.15, "recovery").action, "no_trade")
            thin = cycle(7, 1.15, "recovery")
            self.assertEqual(thin.metadata["flow_signal"]["metadata"]["flow"]["lean"], "thin")
            self.assertEqual(thin.action, "no_trade")
            self.assertEqual(cycle(8, 1.15, "recovery", gap=True).action, "no_trade")
            self.assertEqual(cycle(9, 1.15, "recovery").action, "no_trade")
            recovered = cycle(10, 1.15, "recovery")
            self.assertEqual((recovered.action, recovered.symbol), ("buy", SYM))
            self.assertEqual(cycle(11, .74, gap=True).action, "sell")

    def test_stop(self):
        self.scenario("stop")

    def test_protect(self):
        self.scenario("protect_only")

    def test_recovery(self):
        self.scenario("protect_reentry")

    def test_ibit_protect(self):
        self.scenario("protect_only", "IBIT")

    def test_ibit_recovery(self):
        self.scenario("protect_reentry", "IBIT")

    def test_ibit_stop_still_applies(self):
        # No stop-only IBIT arm, but the protection arms keep the 35% stop.
        self.assertNotIn("stop", f.IBIT_ARMS.values())

    def test_slv_dram_arms(self):
        for root in ("SLV", "DRAM"):
            family = f.FAMILIES[root]
            self.assertEqual(
                sorted(family["arms"].values()),
                ["baseline", "protect_only", "protect_reentry"],
            )
            self.assertEqual(
                family["strategy_id"], f"oi_skew_{root.lower()}_adaptive"
            )
            for arm in ("protect_only", "protect_reentry"):
                with self.subTest(root=root, arm=arm):
                    self.scenario(arm, root)

    def test_signal_routing_per_family(self):
        ctx = SimpleNamespace()
        with patch.object(f, "reference_signal", return_value="ref") as ref:
            for root in ("IBIT", "SLV", "DRAM"):
                self.assertEqual(f.signal_for(root, ctx), "ref")
                ref.assert_called_with(ctx, f.FAMILIES[root]["strategy_id"])

    def test_no_cross_family_signal(self):
        # A silver arm cannot run the memory signal (or IBIT's), and vice versa.
        for user, sid in (
            ("crassus_oif_slv_protect", "oi_skew_dram_adaptive"),
            ("crassus_oif_dram_protect", "oi_skew_slv_adaptive"),
            ("crassus_oif_slv_baseline", "oi_skew_ibit_adaptive"),
            ("crassus_oif_dram_recover", "oi_skew_qqq"),
        ):
            with self.assertRaises(ValueError):
                f.arm_for(
                    SimpleNamespace(
                        username=user,
                        strategy_id=sid,
                        params={"forward_skew_arm": f.FAMILIES[f.root_for(SimpleNamespace(username=user))]["arms"][user]},
                    )
                )

    def test_chart_identity_per_symbol(self):
        t = int(START.timestamp())
        data = {
            "chart": {
                "result": [
                    {
                        "meta": {"symbol": "IBIT", "dataGranularity": "5m"},
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
        received = (START + timedelta(minutes=5, seconds=3)).astimezone(timezone.utc)
        rows = []
        ingest(data, received, rows, "IBIT")
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            ingest(data, received, [], "QQQ")

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
