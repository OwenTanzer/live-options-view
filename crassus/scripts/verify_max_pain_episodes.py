"""Exercise frozen Mouse/Rat policies, persistent resets and the paper Runner."""

import json
import socket
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_invariants as fixture
from crassus.client import Book
from crassus.clock import ET
from crassus.config import Account
from crassus.market import MarketSnapshot, Quote
from crassus.runner import Runner
from crassus.strategy import REGISTRY, StrategyContext

START = datetime(2026, 11, 24, 10, 0, tzinfo=ET)
MOUSE = "max_pain_mouse_qqq"
RAT = "max_pain_rat_qqq"


def board(when, spot, count=5):
    rows = [
        {
            "OptionSymbol": f"QQQ{when:%y%m%d}{kind[0].upper()}{k*1000:08d}",
            "Strike": k,
            "Type": kind,
            "OpenInterest": 100,
            "Bid": 1,
            "Ask": 1.01,
        }
        for k in (398, 399, 400, 401, 402)[:count]
        for kind in ("call", "put")
    ]
    return MarketSnapshot.from_payload(
        "test://board",
        {
            "timestamp": when.isoformat(),
            "underlying_price": spot,
            "expiration": when.date().isoformat(),
            "rows": rows,
        },
        b"{}",
    )


class Episodes(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fills = []

    def act(
        self,
        minute,
        spot=399,
        bid=1,
        *,
        strategy=MOUSE,
        rows=5,
        age=0,
        fill=True,
        quote_age=0,
        ask_age=None,
        ask=None,
        bid_age=None,
        corrupt=None,
        session=START,
    ):
        now = session + timedelta(minutes=minute)
        stamp = now.isoformat()
        snap = (
            board(now - timedelta(seconds=age), spot, rows)
            if spot is not None
            else None
        )
        if corrupt is not None:
            snap = replace(snap, rows=[*snap.rows, corrupt])

        def quotes(symbols):
            bid_time = now - timedelta(
                seconds=quote_age if bid_age is None else bid_age
            )
            ask_time = now - timedelta(
                seconds=quote_age if ask_age is None else ask_age
            )
            return {
                s: Quote(
                    s,
                    bid,
                    bid + 0.01 if ask is None else ask,
                    stamp,
                    stamp,
                    bid_ts=bid_time.isoformat(),
                    ask_ts=ask_time.isoformat(),
                )
                for s in symbols
            }

        request = f"{strategy}-{stamp}"
        ctx = StrategyContext(
            snap,
            {"username": strategy},
            Book(self.fills),
            now,
            "open",
            quotes,
            state_dir=self.folder,
            decision_id=request,
        )
        d = REGISTRY[strategy](ctx)
        self.assertEqual(d.strategy_id, strategy)
        if fill and d.is_trade:
            self.fills.append(
                dict(
                    sym=d.symbol,
                    side=d.action,
                    qty=1,
                    price=bid if d.action == "sell" else 1,
                    execution_request_id=request,
                    ts=stamp,
                )
            )
        return d

    def test_thresholds_bounds_and_missing_snapshot(self):
        for strategy, width in ((MOUSE, 0.15), (RAT, 0.25)):
            for sign in (-1, 1):
                with self.subTest(strategy=strategy, sign=sign):
                    self.fills.clear()
                    self.assertEqual(self.act(0, strategy=strategy).action, "buy")
                    boundary = round(1 + sign * width, 2)
                    self.assertEqual(
                        self.act(
                            1, None, boundary - sign * 0.01, strategy=strategy
                        ).action,
                        "no_trade",
                    )
                    self.assertEqual(
                        self.act(
                            2, None, boundary, quote_age=-1, strategy=strategy
                        ).action,
                        "no_trade",
                    )
                    self.assertEqual(
                        self.act(3, None, boundary, strategy=strategy).action, "sell"
                    )
                    self.assertEqual(
                        self.act(4, 398, strategy=strategy).action, "no_trade"
                    )
                    # Independent session storage for the next subcase.
                    self.tearDownState()

    def tearDownState(self):
        for path in self.folder.rglob("*.json"):
            path.unlink()

    def test_rejected_buys_and_sales(self):
        self.assertEqual(self.act(0, fill=False).action, "buy")
        self.assertEqual(self.act(1).action, "buy")
        self.assertEqual(self.act(2, bid=0.85, fill=False).action, "sell")
        self.assertEqual(self.act(3, bid=0.99).action, "no_trade")
        self.assertEqual(self.act(4, bid=0.85).action, "sell")
        self.assertEqual(self.act(5, 398).action, "no_trade")

    def test_new_strike_and_sparse_stale_or_repeated_board_cannot_reset(self):
        self.assertEqual(self.act(0).action, "buy")
        self.assertEqual(self.act(1, bid=1.15).action, "sell")
        for minute, spot, kw in [
            (2, 398, {}),
            (3, 400, {"rows": 3}),
            (4, None, {}),
            (8, 400, {"age": 421}),
            (9, 400, {"age": -1}),
            (11, 398, {}),
        ]:
            self.assertEqual(self.act(minute, spot, **kw).action, "no_trade")
        self.assertEqual(self.act(12, 400).action, "no_trade")
        self.assertEqual(
            self.act(13, 399).action, "no_trade"
        )  # original contract still on timer
        self.assertEqual(
            self.act(16, 399).action, "buy"
        )  # inclusive exact timer boundary

    def test_qualified_exit_observation_already_opposite(self):
        self.assertEqual(self.act(0).action, "buy")
        self.assertEqual(self.act(1, 401, bid=0.85).action, "sell")
        self.assertEqual(
            self.act(2, 398).action, "buy"
        )  # different call strike is already rearmed

    def test_signal_exit_has_no_episode_or_contract_cooldown(self):
        self.assertEqual(self.act(0).action, "buy")
        self.assertEqual(self.act(1, 400).action, "sell")
        self.assertEqual(self.act(2, 399).action, "buy")

    def test_traded_side_clock_and_malformed_board(self):
        self.assertEqual(self.act(0, ask=0.99).action, "no_trade")
        self.assertEqual(self.act(0, ask_age=20).action, "no_trade")
        self.assertEqual(self.act(1, bid_age=20).action, "buy")
        self.assertEqual(self.act(2, bid=0.85, bid_age=20).action, "no_trade")
        self.assertEqual(self.act(3, bid=0.85, bid_age=-1).action, "no_trade")
        self.assertEqual(
            self.act(4, bid=0.85, ask_age=20, corrupt="invalid row").action, "sell"
        )
        for value in (-1, float("nan")):
            self.assertEqual(
                self.act(
                    5,
                    400,
                    corrupt={
                        "Type": "call",
                        "Strike": 400,
                        "OptionSymbol": "bad",
                        "OpenInterest": value,
                    },
                ).action,
                "no_trade",
            )
        self.assertEqual(self.act(6, 398).action, "no_trade")

    def test_mismatched_contract_identity_cannot_contribute_or_open(self):
        good = board(START, 399).rows[0]
        for overrides in (
            {"OptionSymbol": "SPY261124C00398000"},
            {"OptionSymbol": "QQQ261125C00398000"},
            {"Type": "put"},
            {"Strike": 399},
            {"TradeDate": "2026-11-23"},
        ):
            self.assertEqual(
                self.act(0, corrupt={**good, **overrides}).action, "no_trade"
            )

    def test_corrupt_state_preserves_evidence_and_allows_risk_exit(self):
        for raw in ("{", '{"blocked":42}'):
            self.fills.clear()
            self.tearDownState()
            self.assertEqual(self.act(0).action, "buy")
            path = next(self.folder.rglob("*.json"))
            path.write_text(raw)
            self.assertEqual(self.act(1, None, bid=0.85).action, "sell")
            self.assertEqual(self.act(2, 398).action, "no_trade")
            self.assertEqual(path.read_text(), raw)

    def test_new_session_uses_fresh_episode(self):
        self.assertEqual(self.act(0).action, "buy")
        self.assertEqual(self.act(1, bid=0.85).action, "sell")
        self.assertEqual(self.act(2, 398).action, "no_trade")
        self.assertEqual(
            self.act(0, 398, session=START + timedelta(days=1)).action, "buy"
        )

    def test_runner_fill_reconciliation_after_restart(self):
        # Bind a distinct fixture port so parallel runner suites cannot share state.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        account = Account("Max_Pain_mouse", "crassus_mp_mouse", "local-fixture", MOUSE)

        def runner():
            r = Runner(
                [account],
                base_url=base,
                ledger_dir=self.folder / "ledger",
                state_dir=self.folder / "state",
                bot_registration_key=fixture.BOT_KEY,
            )
            r.startup()
            return r

        def cycle(r, minute, spot, bid):
            now = START + timedelta(minutes=minute)

            def quotes(syms):
                return {
                    s: Quote(
                        s,
                        bid,
                        bid + 0.01,
                        now.isoformat(),
                        now.isoformat(),
                        bid_ts=now.isoformat(),
                        ask_ts=now.isoformat(),
                    )
                    for s in syms
                }

            with patch("crassus.clock.now_et", return_value=now), patch.object(
                r.quotes, "quotes", quotes
            ):
                r._run_account(account, board(now, spot), phase="open")
            return json.loads(r.ledger.paths.ledger.read_text().splitlines()[-1])

        with patch.object(fixture, "PORT", port), patch.object(
            fixture, "BASE", base
        ), fixture.Mock():
            r = runner()
            entry = cycle(r, 0, 399, 8.2)
            self.assertEqual(entry["outcome_class"], "filled")
            sale = cycle(r, 1, 399, 6.5)
            self.assertEqual(sale["outcome_class"], "filled")
            self.assertEqual(sale["execution_request_id"], sale["decision_id"])
            self.assertEqual(sale["decision"]["metadata"]["fill_basis"], 8.34)
            r = runner()
            blocked = cycle(r, 2, 398, 8.2)
            self.assertIn("new directional", blocked["reason"])
            self.assertEqual(len(r.sessions[account.alias].me().trades), 2)


if __name__ == "__main__":
    unittest.main()
