#!/usr/bin/env python3
"""Exercise cycle failure/interruption and safe retained-log diagnostics."""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import AccountState, ExecutionResult
from crassus.audit import Outcome
from crassus.observability import configure_logging, rejection_reason
from crassus.runner import Runner, main
from crassus.strategy import Decision
from verify_runner_flatten_attribution import FakeAccount, make_runner, make_snapshot


class OperationalDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = object.__new__(Runner)
        self.runner.accounts = [SimpleNamespace(alias="one"), SimpleNamespace(alias="two")]
        self.runner.cycle_count = 0
        self.runner.ledger = SimpleNamespace(run_id="test-run")
        self.runner.retired = set()
        self.runner._stop = threading.Event()
        self.runner.stop_file = Path(self.tmp.name) / "STOP"
        self.runner.snapshots = Mock()
        self.runner._run_account = Mock()

    @staticmethod
    def events(captured):
        return [json.loads(r.getMessage()) for r in captured.records
                if r.getMessage().startswith('{"')]

    def test_only_finished_account_pass_advances_completion(self):
        with self.assertLogs("crassus", logging.INFO) as captured:
            self.runner.run_cycle()
            self.runner.run_cycle()
        completed = [e for e in self.events(captured) if e["event"] == "cycle_completed"]
        self.assertEqual([e["cycle_sequence"] for e in completed], [1, 2])
        self.assertEqual(self.runner._run_account.call_count, 4)
        self.assertTrue(all(e["accounts_processed"] == 2 for e in completed))
        self.assertTrue(all(e["duration_seconds"] >= 0 for e in completed))
        self.assertTrue(all(e["cycle_completed_at"] >= e["cycle_started_at"] for e in completed))

    def test_account_crash_propagates_without_false_completion(self):
        self.runner._run_account.side_effect = [None, RuntimeError("private-detail")]
        with self.assertLogs("crassus", logging.INFO) as captured:
            with self.assertRaises(RuntimeError):
                self.runner.run_cycle()
        events = self.events(captured)
        self.assertEqual([e["event"] for e in events], ["cycle_started", "cycle_failed"])
        self.assertEqual(events[-1]["accounts_processed"], 1)
        self.assertEqual(events[-1]["error_type"], "RuntimeError")
        self.assertNotIn("private-detail", str(events))

    def test_stopping_mid_cycle_is_not_completion(self):
        self.runner._run_account.side_effect = lambda *args: self.runner._stop.set()
        with self.assertLogs("crassus", logging.INFO) as captured:
            self.assertFalse(self.runner.run_cycle())
        self.assertEqual([e["event"] for e in self.events(captured)],
                         ["cycle_started", "cycle_interrupted"])
        self.assertEqual(self.runner._run_account.call_count, 1)

    def test_once_reports_interruption_instead_of_success(self):
        runner = Mock()
        runner.run_cycle.return_value = False
        with patch("crassus.runner.Runner", return_value=runner), \
             patch("crassus.runner.load_accounts", return_value=[]), \
             patch("crassus.runner.configure_logging"), \
             self.assertLogs("crassus", logging.INFO) as captured:
            self.assertEqual(main(["--once"]), 130)
        runner.run_cycle.assert_called_once()
        self.assertNotIn("Single cycle complete.", str(captured.output))

    def test_retired_account_is_counted_as_skipped(self):
        self.runner.retired.add("one")
        with self.assertLogs("crassus", logging.INFO) as captured:
            self.runner.run_cycle()
        completed = self.events(captured)[-1]
        self.assertEqual(completed["accounts_skipped"], 1)
        self.assertEqual(completed["accounts_processed"], 1)

    def test_snapshot_failure_remains_visible_while_exit_processing_continues(self):
        self.runner.snapshots.read.side_effect = RuntimeError("unavailable")
        with self.assertLogs("crassus", logging.INFO) as captured:
            self.runner.run_cycle()
        self.assertFalse(self.events(captured)[-1]["snapshot_available"])
        self.assertEqual(self.runner._run_account.call_count, 2)
        self.assertIsNone(self.runner._run_account.call_args.args[1])

    def test_stuck_account_cannot_emit_completion_in_advance(self):
        def account(*args):
            names = [e["event"] for e in self.events(captured)]
            self.assertNotIn("cycle_completed", names)
        self.runner._run_account.side_effect = account
        with self.assertLogs("crassus", logging.INFO) as captured:
            self.runner.run_cycle()

    def test_info_and_errors_use_separate_streams_without_duplicates(self):
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        out, err = io.StringIO(), io.StringIO()
        try:
            with patch("sys.stdout", out), patch("sys.stderr", err):
                configure_logging()
                logging.info("normal-cycle")
                logging.error("real-failure")
            self.assertIn("normal-cycle", out.getvalue())
            self.assertNotIn("normal-cycle", err.getvalue())
            self.assertIn("real-failure", err.getvalue())
            self.assertNotIn("real-failure", out.getvalue())
        finally:
            root.handlers = old_handlers
            root.setLevel(old_level)

    def test_untrusted_rejection_payload_is_never_echoed(self):
        for response in [None, "secret", {"error": ["secret"]},
                         {"error": "Insufficient balance\nAuthorization: secret"},
                         {"error": {"token": "secret"}}]:
            self.assertEqual(rejection_reason(response), "unclassified_rejection")

    def test_real_runner_rejection_has_reason_and_durable_correlation_id(self):
        account = FakeAccount(alias="test", username="test", strategy_id="smoke_atm_roundtrip")
        runner, executor = make_runner(account, state=AccountState("test", 1.0, []),
                                      ledger_dir=Path(self.tmp.name))
        executor._result = ExecutionResult(
            outcome_class=Outcome.REJECTED, execution_request_id="request-123", http_status=400,
            server_response={"error": "Insufficient balance", "unexpected_token": "secret"})
        strategy = Mock(return_value=Decision("buy", "test", "smoke_atm_roundtrip", "1",
                                             symbol="QQQ260904C00711000", quantity=1))
        strategy.strategy_version = "1"
        with patch("crassus.runner.get_strategy", return_value=strategy), \
             patch("crassus.runner.maybe_flatten", return_value=None), \
             self.assertLogs("crassus", logging.INFO) as captured:
            runner._run_account(account, make_snapshot(), "regular")
        event = self.events(captured)[-1]
        self.assertEqual(event["rejection_reason"], "insufficient_balance")
        self.assertEqual(event["execution_request_id"], "request-123")
        ledger = runner.ledger.find_by_execution_request_id("request-123")
        self.assertEqual(event["decision_id"], ledger["decision_id"])
        self.assertEqual(executor.finalized, ["request-123"])
        self.assertNotIn("secret", str(captured.output))

    def test_closed_account_retires_before_strategy_or_execution(self):
        account = FakeAccount(alias="closed", username="closed", strategy_id="smoke_atm_roundtrip")
        state = AccountState("closed", 5.0, [], account_closed=True, closure_reason="insufficient_balance")
        runner, executor = make_runner(account, state=state, ledger_dir=Path(self.tmp.name))
        runner._run_account(account, make_snapshot(), "regular")
        self.assertIn(account.alias, runner.retired)
        self.assertEqual(executor.submit_calls, [])
        records = [json.loads(line) for line in runner.ledger.paths.ledger.read_text().splitlines()]
        self.assertTrue(records[-1]["account_closed"])
        self.assertEqual(records[-1]["account_state_before"]["balance_cash"], 5.0)

    def test_restart_records_pending_rejection_before_retiring_closed_account(self):
        account = FakeAccount(alias="closed", username="closed", strategy_id="smoke_atm_roundtrip")
        state = AccountState("closed", 5.0, [], account_closed=True, closure_reason="insufficient_balance")
        intent = dict(execution_request_id="closure-request", decision_id="closure-decision", strategy_id=account.strategy_id)
        runner, executor = make_runner(account, state=state, ledger_dir=Path(self.tmp.name),
                                       pending=intent, stub_recover=False)
        executor._result = ExecutionResult(outcome_class=Outcome.REJECTED,
            execution_request_id="closure-request", http_status=400,
            server_response={"error": "Insufficient balance", "account_closed": True})
        runner.sessions[account.alias].ensure_session = Mock(return_value=state)
        original_finalize = executor.finalize
        def finalize(request_id):
            self.assertIsNotNone(runner.ledger.find_by_execution_request_id(request_id))
            original_finalize(request_id)
            executor._pending = None  # Match the real executor's durable-marker removal.
        executor.finalize = finalize
        runner.startup()
        record = runner.ledger.find_by_execution_request_id("closure-request")
        self.assertEqual(record["outcome_class"], Outcome.REJECTED)
        self.assertTrue(record["server_response"]["account_closed"])
        self.assertIn(account.alias, runner.retired)
        self.assertEqual(executor.finalized, ["closure-request"])

    def test_closed_account_keeps_retrying_unresolved_audit_recovery(self):
        account = FakeAccount(alias="closed", username="closed", strategy_id="smoke_atm_roundtrip")
        state = AccountState("closed", 5.0, [], account_closed=True, closure_reason="insufficient_balance")
        intent = dict(execution_request_id="pending-closure", decision_id="closure-decision", strategy_id=account.strategy_id)
        runner, executor = make_runner(account, state=state, ledger_dir=Path(self.tmp.name),
                                       pending=intent, stub_recover=False)
        executor._result = ExecutionResult(outcome_class=Outcome.AMBIGUOUS,
                                          execution_request_id="pending-closure")
        runner.sessions[account.alias].ensure_session = Mock(return_value=state)
        runner.startup()
        self.assertNotIn(account.alias, runner.retired)
        self.assertIsNotNone(executor.pending_intent())
        self.assertEqual(executor.finalized, [])

    def test_terminal_rejection_retires_only_after_durable_record(self):
        account = FakeAccount(alias="test", username="test", strategy_id="smoke_atm_roundtrip")
        runner, executor = make_runner(account, state=AccountState("test", 1.0, []),
                                      ledger_dir=Path(self.tmp.name))
        executor._result = ExecutionResult(outcome_class=Outcome.REJECTED,
            execution_request_id="terminal-request", http_status=400,
            server_response={"error": "Insufficient balance", "account_closed": True})
        strategy = Mock(return_value=Decision("buy", "test", "smoke_atm_roundtrip", "1",
                                             symbol="QQQ260904C00711000", quantity=1))
        strategy.strategy_version = "1"
        original_retire = runner._retire
        def retire(account, reason):
            self.assertIsNotNone(runner.ledger.find_by_execution_request_id("terminal-request"))
            original_retire(account, reason)
        runner._retire = retire
        with patch("crassus.runner.get_strategy", return_value=strategy), \
             patch("crassus.runner.maybe_flatten", return_value=None):
            runner._run_account(account, make_snapshot(), "regular")
        self.assertIn(account.alias, runner.retired)


if __name__ == "__main__":
    unittest.main()
