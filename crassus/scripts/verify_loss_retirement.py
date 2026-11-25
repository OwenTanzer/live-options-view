#!/usr/bin/env python3
"""Loss boundary through real Runner/AccountSession, mocked HTTP and executions."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crassus import clock
from crassus.client import ExecutionClient, ExecutionResult, ConfigError, AmbiguousExecution
from crassus.config import Account
from crassus.market import Quote
from crassus.runner import Runner
from crassus.strategy import Decision
from crassus.audit import Outcome
from verify_session_recovery import FakeResponse

SYM = "QQQ261125C00600000"
TRADE = dict(sym=SYM, side="buy", qty=1, price=99, instrument_type="option", multiplier=100)


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = Account("Loss", "loss_fixture", "local-password", "smoke_atm_roundtrip")
        self.payload = dict(username=self.account.username, balance_cash=99.99,
                            starting_balance=10000, is_bot=True,
                            trade_history_complete=True, trades=[])
        self.strategy = Mock(return_value=self.decision("buy"))
        self.strategy.strategy_version = "test"
        self.strategy.requires_snapshot = False
        self.patches = [patch("crassus.runner.get_strategy", return_value=self.strategy),
                        patch("crassus.runner.maybe_flatten", return_value=None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.runner = self.new_runner()

    def decision(self, side, quantity=1, symbol=SYM):
        return Decision(side, "fixture", self.account.strategy_id, "test", symbol=symbol, quantity=quantity)

    def quote(self, sym=SYM, bid=0.5, ask=0.5, **kw):
        ts = clock.iso_utc()
        return Quote(**dict(dict(symbol=sym, bid=bid, ask=ask, quote_ts=ts,
                                 server_ts=ts, bid_ts=ts, ask_ts=ts), **kw))

    def new_runner(self):
        root = Path(self.tmp.name)
        runner = Runner([self.account], base_url="http://127.0.0.1:1",
                        snapshot_url="http://127.0.0.1:1/snapshot", state_dir=root / "state",
                        ledger_dir=root / "ledger", bot_registration_key="local-test-key")
        session = runner.sessions[self.account.alias]
        session._request = Mock(side_effect=lambda *a, **kw: FakeResponse(200, json.dumps(self.payload)))
        runner.quotes.quotes = Mock(side_effect=lambda symbols: {s: self.quote(s) for s in symbols})
        runner.executors[self.account.alias].submit = Mock(return_value=ExecutionResult(
            Outcome.REJECTED, "fixture-order", http_status=409))
        return runner

    def run_visit(self, runner=None):
        (runner or self.runner)._run_account(self.account, None, "open")

    def receipt(self, runner=None):
        return (runner or self.runner).loss_retirement.read(self.account.username)

    def submits(self, runner=None):
        return (runner or self.runner).executors[self.account.alias].submit.call_count

    def test_strict_unrounded_boundary(self):
        for cash, retired in [(100.01, False), (100, False), (99.999999, True)]:
            with self.subTest(cash=cash):
                self.payload["balance_cash"] = cash
                self.run_visit()
                self.assertEqual(self.receipt() is not None, retired)
        self.assertEqual(self.submits(), 2)
        self.assertIn(self.account.alias, self.runner.retired)

    def test_actual_starting_balance(self):
        self.payload.update(starting_balance=20000, balance_cash=199.99)
        self.run_visit()
        self.assertEqual(self.receipt()["valuation"]["starting_balance"], "20000")

    def test_missing_or_invalid_account_inputs_do_not_trigger(self):
        for field, value in [("balance_cash", None), ("balance_cash", "0"),
                             ("balance_cash", float("nan")), ("starting_balance", None),
                             ("starting_balance", 0), ("starting_balance", True),
                             ("starting_balance", float("inf")), ("trades", None),
                             ("is_bot", False), ("trade_history_complete", False),
                             ("username", "another_account")]:
            with self.subTest(field=field, value=value):
                before = copy.deepcopy(self.payload)
                if value is None:
                    self.payload.pop(field)
                else:
                    self.payload[field] = value
                self.run_visit()
                self.assertIsNone(self.receipt())
                self.payload = before

    def test_cash_is_not_equity(self):
        self.payload.update(balance_cash=50, trades=[TRADE])
        self.run_visit()  # 50 cash + 50 midpoint = exactly 1% of seed
        self.assertIsNone(self.receipt())
        self.assertEqual(self.submits(), 1)

    def test_open_position_stops_entries_without_forcing_exit(self):
        self.payload.update(balance_cash=49, trades=[TRADE])
        original = copy.deepcopy(self.payload)
        self.run_visit()
        self.assertIsNotNone(self.receipt())
        self.assertNotIn(self.account.alias, self.runner.retired)
        self.assertEqual(self.submits(), 0)
        self.assertEqual(self.payload, original)
        self.strategy.return_value = self.decision("sell")
        self.run_visit()
        self.assertEqual(self.submits(), 1)  # unchanged authorized exit

    def test_restart_recovery_cannot_reenter_or_reseed(self):
        self.payload.update(balance_cash=49, trades=[TRADE])
        self.run_visit()
        receipt = self.receipt()
        self.payload["balance_cash"] = 20000  # recovery does not undo retirement
        restarted = self.new_runner()
        self.run_visit(restarted)
        self.assertEqual(self.submits(restarted), 0)
        self.assertEqual(self.receipt(restarted), receipt)
        session = restarted.sessions[self.account.alias]
        session._request = Mock(return_value=FakeResponse(401))
        with self.assertRaises(ConfigError):
            session.ensure_session()
        self.assertFalse(any(call.args[1] == "/api/register" for call in session._request.call_args_list))

    def test_flat_restart_is_terminal_and_preserves_history(self):
        self.run_visit()
        restarted = self.new_runner()
        original = copy.deepcopy(self.payload)
        self.run_visit(restarted)
        self.assertEqual(self.submits(restarted), 0)
        self.assertEqual(self.payload, original)
        self.assertIn(self.account.alias, restarted.retired)

    def test_exit_cannot_flip_or_open_another_symbol(self):
        self.payload.update(balance_cash=49, trades=[TRADE])
        self.run_visit()
        for decision in [self.decision("sell", 2), self.decision("sell", symbol="SPY")]:
            self.strategy.return_value = decision
            self.run_visit()
        self.assertEqual(self.submits(), 0)
        self.strategy.return_value = self.decision("sell")
        self.run_visit()
        self.assertEqual(self.submits(), 1)

    def test_short_cover_and_share_multiplier(self):
        self.payload.update(balance_cash=100, trades=[dict(TRADE, sym="QQQ", side="sell",
                                                         instrument_type="share", multiplier=1)])
        self.strategy.return_value = self.decision("buy", symbol="QQQ")
        self.run_visit()
        self.assertEqual(self.receipt()["valuation"]["net_equity"], "99.5")
        self.assertEqual(self.submits(), 1)

    def test_missing_stale_crossed_or_incomplete_marks(self):
        self.payload.update(balance_cash=0, trades=[TRADE])
        old = clock.iso_utc(clock.now_utc() - timedelta(seconds=61))
        future = clock.iso_utc(clock.now_utc() + timedelta(seconds=30))
        cases = [{}, {SYM: self.quote(bid=None)}, {SYM: self.quote(ask=float("nan"))},
                 {SYM: self.quote(bid=2, ask=1)}, {SYM: self.quote(bid=-1)},
                 *[{SYM: self.quote(**{field: stamp})}
                   for field in ("quote_ts", "server_ts", "bid_ts", "ask_ts")
                   for stamp in (None, old, future, "broken")]]
        for marks in cases:
            with self.subTest(marks=marks):
                self.runner.quotes.quotes.side_effect = lambda _, m=marks: m
                self.run_visit()
                self.assertIsNone(self.receipt())

    def test_quote_failure_does_not_block_exit(self):
        self.payload.update(balance_cash=0, trades=[TRADE])
        self.runner.quotes.quotes.side_effect = RuntimeError("mock outage")
        self.strategy.return_value = self.decision("sell")
        self.run_visit()
        self.assertIsNone(self.receipt())
        self.assertEqual(self.submits(), 1)

    def test_malformed_or_unsupported_history_does_not_trigger(self):
        for trade in [dict(TRADE, qty=1.5), dict(TRADE, side="unknown"),
                      dict(TRADE, multiplier=50), dict(TRADE, instrument_type="futures_option"),
                      {"unexpected": "row"}]:
            self.payload["trades"] = [trade]
            self.run_visit()
            self.assertIsNone(self.receipt())

    def test_pending_recovery_precedes_retirement(self):
        self.runner._recover_pending = Mock(return_value=True)
        self.run_visit()
        self.assertIsNone(self.receipt())
        self.assertEqual(self.submits(), 0)
        self.strategy.assert_not_called()

    def test_write_failure_cannot_allow_entry(self):
        with patch("crassus.retirement.atomic_json", side_effect=OSError("mock disk failure")):
            with self.assertRaises(OSError):
                self.run_visit()
        self.assertEqual(self.submits(), 0)
        self.assertIsNone(self.receipt())
        self.run_visit()
        self.assertIsNotNone(self.receipt())

    def test_receipt_survives_crash_before_ledger_and_corruption_fails_closed(self):
        with patch.object(self.runner.ledger, "record", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.run_visit()
        restarted = self.new_runner()
        self.run_visit(restarted)
        self.assertEqual(self.submits(restarted), 0)
        self.runner.loss_retirement.path(self.account.username).write_text("broken")
        with self.assertRaises(ValueError):
            self.new_runner()

    def test_dry_run_does_not_persist_retirement(self):
        self.runner.dry_run = True
        self.run_visit()
        self.assertIsNone(self.receipt())
        self.assertEqual(self.submits(), 0)

    def test_real_executor_exit_timeout_restart_and_terminal_flat(self):
        self.payload.update(balance_cash=49, trades=[TRADE])
        self.run_visit()  # persist retirement while held; no forced sale
        self.strategy.return_value = self.decision("sell")
        session = self.runner.sessions[self.account.alias]
        self.runner.executors[self.account.alias] = ExecutionClient(session, self.runner.state_dir)
        submissions = []

        def transport(method, path, **kw):
            if method == "POST" and path == "/api/paper-trade":
                body = kw["json"]
                submissions.append(body)
                self.payload["trades"].append(dict(TRADE, side="sell", price=0.5,
                                                   execution_request_id=body["execution_request_id"]))
                self.payload["balance_cash"] += 50
                raise AmbiguousExecution("filled, response lost")
            self.assertEqual((method, path), ("GET", "/api/me"))
            return FakeResponse(200, json.dumps(self.payload))

        session._request.side_effect = transport
        # Crash after the actual reconciled fill but before its ledger append.
        with patch.object(self.runner.ledger, "record", side_effect=OSError("crash after fill")):
            with self.assertRaises(OSError):
                self.run_visit()
        self.assertEqual(len(submissions), 1)
        restarted = self.new_runner()
        restarted.executors[self.account.alias] = ExecutionClient(
            restarted.sessions[self.account.alias], restarted.state_dir)
        restarted.sessions[self.account.alias]._request.side_effect = transport
        self.run_visit(restarted)  # recover outstanding fill and finalize once
        self.assertIsNone(restarted.executors[self.account.alias].pending_intent())
        rows = [json.loads(line) for file in (Path(self.tmp.name) / "ledger").glob("*.jsonl")
                for line in file.read_text().splitlines()]
        self.assertEqual(sum(r["execution_request_id"] == submissions[0]["execution_request_id"] for r in rows), 1)
        self.run_visit(restarted)  # now flat: retire without another order
        self.assertIn(self.account.alias, restarted.retired)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(self.payload["balance_cash"], 99)
        self.assertEqual(len(self.payload["trades"]), 2)


if __name__ == "__main__":
    unittest.main()
