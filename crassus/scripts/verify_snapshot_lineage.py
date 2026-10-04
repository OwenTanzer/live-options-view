#!/usr/bin/env python3
"""Hermetic snapshot identity, runner ledger and crash-recovery regressions."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.audit import MANDATORY_FIELDS, Outcome
from crassus.client import AccountLiquidated, AccountState, ExecutionClient, TransportError
from crassus.market import MarketSnapshot, QuoteRateLimited, SnapshotReader
from crassus.runner import Runner
from verify_runner_flatten_attribution import FakeAccount, LONG_CALL, make_runner

KEY = "intraday/20261002/snapshot_120000123456.csv"
KEY2 = "intraday/20261002/snapshot_120000123457.csv"
URL = "https://fixture.invalid/intraday/latest.json"
NOW = datetime(2026, 10, 2, 12, tzinfo=ZoneInfo("America/New_York"))
ROW = {"OptionSymbol": "QQQ261002C00400000", "Strike": 400.0,
       "Type": "call", "Bid": 1.0, "Ask": 1.1, "IV": 0.2, "OpenInterest": 0}


def payload(**changes):
    return {"timestamp": "2026-10-02T16:00:00.123499+00:00",
            "snapshot_time": "12:00 ET", "expiration": "2026-10-02",
            "underlying_price": 400.0, "rows": [dict(ROW)], **changes}


def snapshot(**changes):
    p = payload(**changes)
    return MarketSnapshot.from_payload(URL, p, json.dumps(p).encode())


class SnapshotIdentity(unittest.TestCase):
    def test_exact_key_and_exact_json_byte_hash(self):
        p = payload(snapshot_key=KEY)
        raw = json.dumps(p, indent=2).encode()
        s = MarketSnapshot.from_payload(URL, p, raw)
        self.assertEqual(s.snapshot_key, KEY)
        self.assertEqual(s.lineage, {"schema_version": "market_snapshot_lineage.v1",
                                   "snapshot_key": KEY, "status": "valid"})
        self.assertEqual(s.provenance, URL + "#sha256:" + hashlib.sha256(raw).hexdigest())
        compact = MarketSnapshot.from_payload(URL, p, json.dumps(p).encode())
        self.assertNotEqual(s.sha256, compact.sha256)
        self.assertEqual(s.rows, p["rows"])

    def test_legacy_payload_and_direct_constructor(self):
        s = snapshot()
        self.assertIsNone(s.snapshot_key)
        self.assertEqual(s.snapshot_key_status, "absent")
        direct = MarketSnapshot(s.url, s.fetched_at, s.timestamp, s.snapshot_time,
                                s.expiration, s.underlying_price, s.rows, s.sha256)
        self.assertEqual(direct.lineage, s.lineage)

    def test_legacy_coarse_keys_do_not_claim_immutable_identity(self):
        for time_part in ("1200", "120000"):
            key = f"intraday/20261002/snapshot_{time_part}.csv"
            s = snapshot(snapshot_key=key)
            self.assertEqual(s.snapshot_key, key)
            self.assertEqual(s.snapshot_key_status, "legacy_key")

    def test_malformed_key_never_changes_strategy_inputs(self):
        for key in (None, False, 7, [], {}, "", " " + KEY, KEY + "\n", KEY + "?x=1",
                    "https://fixture.invalid/" + KEY, "intraday/latest.json",
                    "intraday/../snapshot_120000123456.csv",
                    KEY.replace("20261002", "20260230"), KEY.replace("120000", "250000"),
                    KEY.replace("120000", "126100"), KEY.replace("123456.csv", "1.csv")):
            with self.subTest(key=key):
                s = snapshot(snapshot_key=key)
                self.assertEqual(s.lineage["status"], "invalid")
                self.assertIsNone(s.snapshot_key)
                self.assertEqual(s.rows, snapshot().rows)
                self.assertEqual(s.atm("call"), snapshot().atm("call"))
                self.assertEqual(s.underlying_price, 400.0)

    def test_rapid_snapshots_do_not_join_on_nearest_timestamp(self):
        a = snapshot(snapshot_key=KEY)
        b = snapshot(snapshot_key=KEY2, timestamp=a.timestamp)  # intentionally identical payload times
        self.assertNotEqual(a.snapshot_key, b.snapshot_key)
        self.assertNotEqual(a.sha256, b.sha256)
        moved = snapshot(snapshot_key=KEY, timestamp="2026-10-02T16:00:00.999999+00:00")
        self.assertEqual(a.lineage, moved.lineage)

    def test_identity_survives_latest_advancement_and_cached_read(self):
        responses = [Mock(content=json.dumps(payload(snapshot_key=k)).encode()) for k in (KEY, KEY2)]
        reader = SnapshotReader(URL)
        with patch("crassus.market.requests.get", side_effect=responses) as get:
            a = reader.read(force=True)
            self.assertIs(reader.read(), a)
            b = reader.read(force=True)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(a.snapshot_key, KEY)
        self.assertEqual(b.snapshot_key, KEY2)
        self.assertNotEqual(a.provenance, b.provenance)


class LedgerLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = FakeAccount("test", "test", "smoke_atm_roundtrip")
        self.state = AccountState("test", 10000.0, [])
        self.runner, self.executor = make_runner(self.account, state=self.state,
                                                ledger_dir=Path(self.tmp.name))
        self.now = patch("crassus.clock.now_et", return_value=NOW)
        self.now.start()
        self.addCleanup(self.now.stop)

    def records(self):
        return [json.loads(s) for s in self.runner.ledger.paths.ledger.read_text().splitlines()]

    def assert_lineage(self, record, expected=None):
        self.assertEqual(record["market_snapshot_lineage"], expected or snapshot(snapshot_key=KEY).lineage)
        self.assertTrue(set(MANDATORY_FIELDS).issubset(record))
        self.assertEqual(len(MANDATORY_FIELDS), 19)
        self.assertEqual(record["schema_version"], "crassus_audit.v1")

    def test_trade_no_trade_and_dry_run_keep_identical_decisions(self):
        for phase, dry, action in (("open", False, "buy"), ("closed", False, "no_trade"),
                                   ("open", True, "buy")):
            decisions, submissions = [], []
            for key in (None, KEY, "bad"):
                self.runner.dry_run = dry
                self.executor.submit_calls.clear()
                s = snapshot() if key is None else snapshot(snapshot_key=key)
                self.runner._run_account(self.account, s, phase)
                r = self.records()[-1]
                self.assert_lineage(r, s.lineage)
                self.assertEqual(r["decision"]["action"], action)
                self.assertEqual(r["market_snapshot_url_or_hash"], s.provenance)
                decisions.append(r["decision"])
                submissions.append([{k: v for k, v in call.items()
                                     if k not in {"decision_id", "market_snapshot_lineage", "market_snapshot_url_or_hash"}}
                                    for call in self.executor.submit_calls])
            self.assertEqual(decisions[0], decisions[1])
            self.assertEqual(decisions[0], decisions[2])
            self.assertEqual(submissions[0], submissions[1])
            self.assertEqual(submissions[0], submissions[2])

    def test_strategy_and_quote_errors_retain_available_snapshot(self):
        for error, outcome in ((RuntimeError("fixture"), Outcome.RUNNER_ERROR),
                               (QuoteRateLimited("fixture", 2), Outcome.RATE_LIMITED)):
            with patch("crassus.runner.get_strategy", return_value=Mock(side_effect=error)):
                self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
            r = self.records()[-1]
            self.assertEqual(r["outcome_class"], outcome)
            self.assert_lineage(r)

    def test_reconciliation_errors_and_liquidation_retain_snapshot(self):
        for error, outcome in ((TransportError("fixture"), Outcome.TRANSPORT_ERROR),
                               (AccountLiquidated("fixture"), Outcome.ACCOUNT_LIQUIDATED)):
            self.runner.sessions[self.account.alias].me = Mock(side_effect=error)
            self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
            self.assertEqual(self.records()[-1]["outcome_class"], outcome)
            self.assert_lineage(self.records()[-1])

    def test_execution_outcomes_and_submission_liquidation(self):
        for outcome in (Outcome.FILLED, Outcome.REJECTED, Outcome.AMBIGUOUS,
                        Outcome.TRANSPORT_ERROR, Outcome.RATE_LIMITED, Outcome.RUNNER_ERROR,
                        Outcome.RECONCILED_AFTER_AMBIGUITY):
            self.executor._result.outcome_class = outcome
            self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
            self.assertEqual(self.records()[-1]["outcome_class"], outcome)
            self.assert_lineage(self.records()[-1])
            self.assertEqual(self.executor.submit_calls[-1]["market_snapshot_lineage"], snapshot(snapshot_key=KEY).lineage)
        self.executor.submit = Mock(side_effect=AccountLiquidated("fixture"))
        self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
        self.assert_lineage(self.records()[-1])
        self.assertEqual(self.records()[-1]["outcome_class"], Outcome.ACCOUNT_LIQUIDATED)

    def test_closed_account_with_snapshot_and_startup_without_one(self):
        closed = replace(self.state, account_closed=True, closure_reason="fixture")
        self.runner.sessions[self.account.alias].me = lambda: closed
        self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
        self.assert_lineage(self.records()[-1])
        self.runner._retire_closed(self.account, closed)
        self.assertIsNone(self.records()[-1]["market_snapshot_lineage"])

    def test_snapshot_outage_never_borrows_cached_identity(self):
        self.runner._run_account(self.account, None, "open")
        r = self.records()[-1]
        self.assertEqual(r["outcome_class"], Outcome.RUNNER_ERROR)
        self.assertIsNone(r["market_snapshot_lineage"])
        self.assertIsNone(r["market_snapshot_url_or_hash"])

    def test_flatten_preserves_snapshot_identity(self):
        self.state.trades.append(LONG_CALL)
        with patch("crassus.clock.now_et", return_value=NOW.replace(hour=15, minute=50)):
            self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "open")
        r = self.records()[-1]
        self.assertEqual(r["strategy_id"], "eod_flatten")
        self.assert_lineage(r)

    def test_recovery_preserves_original_identity_and_never_rewrites_old_ledger(self):
        self.runner._run_account(self.account, snapshot(snapshot_key=KEY), "closed")
        original_bytes = self.runner.ledger.paths.ledger.read_bytes()
        for extension in (snapshot(snapshot_key=KEY).lineage, None):
            self.executor._pending = {"execution_request_id": "old-request-" + str(extension is None),
                                      "market_snapshot_timestamp": "old-time",
                                      "market_snapshot_url_or_hash": "old-provenance"}
            self.executor._result.execution_request_id = self.executor._pending["execution_request_id"]
            if extension is not None:
                self.executor._pending["market_snapshot_lineage"] = extension
            self.runner._recover_pending = Runner._recover_pending.__get__(self.runner)
            self.runner._run_account(self.account, snapshot(snapshot_key=KEY2), "open")
            r = self.records()[-1]
            self.assertEqual(r["market_snapshot_lineage"], extension)
            self.assertEqual(r["market_snapshot_url_or_hash"], "old-provenance")
            self.assertEqual(r["market_snapshot_timestamp"], "old-time")
            self.assertEqual(r["execution_request_id"], self.executor._pending["execution_request_id"])
        self.assertTrue(self.runner.ledger.paths.ledger.read_bytes().startswith(original_bytes))

    def test_real_runner_crash_recovery_keeps_original_lineage_across_both_boundaries(self):
        original = snapshot(snapshot_key=KEY)
        advanced = snapshot(snapshot_key=KEY2)
        for already_filled in (False, True):
            with self.subTest(already_filled=already_filled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger_dir, state_dir = root / "logs", root / "state"
                state = AccountState("test", 10000.0, [])
                session = SimpleNamespace(account=self.account, _mutation_lock=threading.Lock(),
                                          me=Mock(return_value=state), _request=Mock())
                session._request.return_value = Mock(status_code=200, json=lambda: {"ok": True})

                def restart():
                    runner, _ = make_runner(self.account, state=state, ledger_dir=ledger_dir,
                                             stub_recover=False)
                    client = ExecutionClient(session, state_dir, backoff_base_s=0)
                    runner.sessions[self.account.alias] = session
                    runner.executors[self.account.alias] = client
                    return runner, client

                runner, client = restart()
                # Crash after the HTTP response, before a durable ledger record.
                with patch.object(runner.ledger, "record", side_effect=OSError("fixture disk failure")):
                    with self.assertRaises(OSError):
                        runner._run_account(self.account, original, "open")
                pending = client.pending_intent()
                self.assertIsNotNone(pending)
                self.assertEqual(pending["market_snapshot_lineage"], original.lineage)
                first_body = session._request.call_args.kwargs["json"].copy()
                if already_filled:
                    state.trades.append({"execution_request_id": pending["execution_request_id"]})
                session._request.reset_mock()

                # New runner/client instances must recover from disk, not old objects.
                runner, client = restart()
                with patch.object(client, "finalize", side_effect=OSError("fixture cleanup failure")):
                    with self.assertRaises(OSError):
                        runner._run_account(self.account, advanced, "open")
                recovered = runner.ledger.find_by_execution_request_id(pending["execution_request_id"])
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered["decision_id"], pending["decision_id"])
                self.assertEqual(recovered["decision"], pending["decision"])
                self.assertEqual(recovered["market_snapshot_lineage"], original.lineage)
                self.assertEqual(recovered["market_snapshot_timestamp"], original.timestamp)
                self.assertEqual(recovered["market_snapshot_url_or_hash"], original.provenance)
                self.assertIsNotNone(client.pending_intent())
                if already_filled:
                    self.assertEqual(recovered["outcome_class"], Outcome.RECONCILED_AFTER_AMBIGUITY)
                    session._request.assert_not_called()
                else:
                    self.assertEqual(recovered["outcome_class"], Outcome.FILLED)
                    session._request.assert_called_once_with("POST", "/api/paper-trade", json=first_body)

                # Crash after the ledger write, before cleanup: restart must neither
                # append a second outcome nor resend the HTTP request, even on a new board.
                ledger_bytes = {p.name: p.read_bytes() for p in ledger_dir.glob("decisions-*.jsonl")}
                session._request.reset_mock()
                session.me.reset_mock()
                runner, client = restart()
                runner._run_account(self.account, advanced, "open")
                self.assertIsNone(client.pending_intent())
                session._request.assert_not_called()
                session.me.assert_not_called()
                self.assertEqual(ledger_bytes,
                                 {p.name: p.read_bytes() for p in ledger_dir.glob("decisions-*.jsonl")})

    def test_real_intent_persistence_replay_and_http_body(self):
        session = SimpleNamespace(account=self.account, _mutation_lock=threading.Lock(),
                                  me=Mock(return_value=self.state), _request=Mock())
        session._request.return_value = Mock(status_code=200, json=lambda: {"ok": True})
        client = ExecutionClient(session, Path(self.tmp.name) / "state", backoff_base_s=0)
        for extension in (snapshot(snapshot_key=KEY).lineage, None):
            session._request.reset_mock()
            client.submit(symbol=ROW["OptionSymbol"], side="buy", quantity=1,
                          execution_request_id="fixed-id", market_snapshot_lineage=extension)
            intent = client.pending_intent()
            self.assertEqual(intent["market_snapshot_lineage"], extension)
            expected_body = {"execution_request_id": "fixed-id", "sym": ROW["OptionSymbol"],
                             "side": "buy", "qty": 1}
            session._request.assert_called_once_with("POST", "/api/paper-trade", json=expected_body)
            if extension is None:  # genuinely pre-extension persisted intent
                del intent["market_snapshot_lineage"]
                client._persist_intent(intent)
            session._request.reset_mock()
            client.recover_pending()
            self.assertEqual(client.pending_intent()["market_snapshot_lineage"], extension)
            session._request.assert_called_once_with("POST", "/api/paper-trade", json=expected_body)
            client.finalize("fixed-id")


if __name__ == "__main__":
    unittest.main()
