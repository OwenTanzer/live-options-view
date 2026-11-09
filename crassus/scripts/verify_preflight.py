#!/usr/bin/env python3
"""Verify the bot provisioning preflight and interrupted-sync recovery.

The probe's accepted proof is the exact current Worker response: an empty
registration body with a valid key passes the key guard and then fails username
validation with a 400 and the contract's exact error. Every other auth,
rate-limit, routing or unknown response fails closed.

A local fake Worker models the real sequence: registration creates the account
but membership sync is interrupted with 503; retry login performs the sync and
metadata repair without creating a duplicate.

    python scripts/verify_preflight.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
sys.path.insert(0, str(ROOT / "crassus" / "scripts"))

import preflight_accounts as preflight  # noqa: E402
from crassus.client import AccountSession, ConfigError, TransportError  # noqa: E402
from crassus.config import Account, account_gaps, load_accounts  # noqa: E402
from crassus.strategy import REGISTRY  # noqa: E402
from preflight_accounts import probe_registration_key  # noqa: E402

ACCEPTED_ERROR = "Username must be 3-40 characters: letters, numbers, underscore"
REJECTED_ERROR = "Invalid bot registration key"


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _json(status, error):
    return json.dumps({"error": error})


def _catalog(tmp: Path, entries: list[dict]) -> Path:
    path = tmp / "accounts.json"
    path.write_text(json.dumps({"accounts": entries}))
    return path


class PreflightTests(unittest.TestCase):
    def test_reports_every_missing_env_and_skips_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _catalog(Path(tmp), [
                {"alias": "A", "username": "a", "password_env": "PW_A", "strategy_id": "x"},
                {"alias": "B", "username": "b", "password_env": "PW_B", "strategy_id": "x"},
                {"alias": "C", "username": "c", "password_env": "PW_C", "strategy_id": "x", "active": False},
            ])
            gaps = account_gaps(path, environ={})
            self.assertEqual([gap["missing"] for gap in gaps], ["PW_A", "PW_B"])
            with self.assertRaises(ValueError) as caught:
                load_accounts(path)
            message = str(caught.exception)
            self.assertIn("A→$PW_A", message)
            self.assertIn("B→$PW_B", message)
            self.assertNotIn("PW_C", message)

    def test_load_with_provisioned_values_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _catalog(Path(tmp), [
                {"alias": "A", "username": "a", "password_env": "PW_A", "strategy_id": "x"},
            ])
            with mock.patch.dict(os.environ, {"PW_A": "s3cret"}, clear=False):
                accounts = load_accounts(path)
            self.assertEqual([a.alias for a in accounts], ["A"])
            self.assertNotIn("s3cret", repr(accounts[0]))

    def test_probe_accepts_only_the_exact_worker_validation_contract(self):
        def call(status, body):
            return probe_registration_key("https://w", "k", request=lambda *a, **k: _Response(status, body))

        self.assertEqual(call(400, _json(400, ACCEPTED_ERROR))["status"], "accepted")
        self.assertEqual(call(403, _json(403, REJECTED_ERROR))["status"], "rejected")
        self.assertEqual(call(401, _json(401, "Unauthorized"))["status"], "unauthorized")
        self.assertEqual(call(403, _json(403, "Forbidden"))["status"], "forbidden")
        self.assertEqual(call(404, _json(404, "Not Found"))["status"], "not_found")
        self.assertEqual(call(429, _json(429, "Rate Limited"))["status"], "rate_limited")
        self.assertEqual(call(400, _json(400, "Username is invalid"))["status"], "unexpected")
        self.assertEqual(call(200, _json(200, "ok"))["status"], "unexpected")
        self.assertEqual(call(400, "<html>gateway</html>")["status"], "unexpected")

        def offline(*args, **kwargs):
            raise requests.ConnectionError("offline")

        self.assertEqual(probe_registration_key("https://w", "k", request=offline)["status"], "unreachable")

    def test_missing_key_fails_configuration_and_never_probes(self):
        with mock.patch.object(preflight, "account_gaps", return_value=[]), \
             mock.patch.object(preflight, "_entries_for", return_value=[]), \
             mock.patch.object(preflight, "BOT_REGISTRATION_KEY", None), \
             mock.patch.object(preflight, "probe_registration_key") as probe:
            self.assertEqual(preflight.main(["--json"]), 2)
            self.assertEqual(preflight.main(["--json", "--probe"]), 2)
            probe.assert_not_called()

    def test_main_exit_codes_follow_probe_status(self):
        entries = [{"alias": "A", "strategy_id": next(iter(REGISTRY)), "active": True}]
        cases = {"accepted": 0, "rejected": 3, "unauthorized": 4,
                 "forbidden": 4, "not_found": 4, "rate_limited": 4,
                 "unexpected": 4, "unreachable": 4}
        for status, expected in cases.items():
            with mock.patch.object(preflight, "account_gaps", return_value=[]), \
                 mock.patch.object(preflight, "_entries_for", return_value=entries), \
                 mock.patch.object(preflight, "BOT_REGISTRATION_KEY", "k"), \
                 mock.patch.object(preflight, "probe_registration_key",
                                   return_value={"status": status}):
                self.assertEqual(preflight.main(["--json", "--probe"]), expected, status)


class _Worker(BaseHTTPRequestHandler):
    """Minimal fake of the current Worker register/login/sync contract."""

    key = "shared-key"
    accounts: dict[str, dict] = {}
    fail_sync = True
    register_calls = 0
    register_creations = 0
    login_calls = 0
    metadata_calls = 0

    def log_message(self, *args):
        pass

    def _send(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def do_POST(self):
        if self.path == "/api/register":
            _Worker.register_calls += 1
            if self.headers.get("X-Bot-Registration-Key") != _Worker.key:
                return self._send(403, {"error": REJECTED_ERROR})
            body = self._body()
            if not body.get("username"):
                return self._send(400, {"error": ACCEPTED_ERROR})
            _Worker.register_creations += 1
            user = body["username"]
            if user in _Worker.accounts:
                return self._send(409, {"error": "Username already taken"})
            _Worker.accounts[user] = {"username": user, "password": body["password"],
                                      "is_bot": True, "trades": [], "balance_cash": 100000.0}
            if _Worker.fail_sync:
                return self._send(503, {"error": "Bot membership update failed; login to retry"})
            return self._send(201, {"username": user, "balance_cash": 100000.0, "trades": []},
                              {"Set-Cookie": "session=ok; Path=/"})
        if self.path == "/api/login":
            _Worker.login_calls += 1
            body = self._body()
            account = _Worker.accounts.get(body.get("username"))
            if not account or account["password"] != body.get("password"):
                return self._send(401, {"error": "Invalid username or password"})
            if _Worker.fail_sync:
                return self._send(503, {"error": "Bot membership update failed; retry login"})
            return self._send(200, {"username": account["username"],
                                    "balance_cash": account["balance_cash"], "trades": account["trades"]},
                              {"Set-Cookie": "session=ok; Path=/"})
        if self.path == "/api/bot-metadata":
            _Worker.metadata_calls += 1
            if self.headers.get("X-Bot-Registration-Key") != _Worker.key:
                return self._send(403, {"error": REJECTED_ERROR})
            if _Worker.fail_sync:
                return self._send(503, {"error": "Bot membership update failed; retry"})
            return self._send(200, {"username": self._body().get("username")})
        return self._send(404, {"error": "Not Found"})

    def do_GET(self):
        if self.path == "/api/me":
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                return self._send(401, {"error": "Unauthorized"})
            name = next(iter(_Worker.accounts))
            account = _Worker.accounts[name]
            return self._send(200, {"username": name, "balance_cash": account["balance_cash"],
                                    "trades": account["trades"]})
        return self._send(404, {"error": "Not Found"})


class RecoveryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Worker)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Worker.accounts = {}
        _Worker.fail_sync = True
        _Worker.register_calls = 0
        _Worker.register_creations = 0
        _Worker.login_calls = 0
        _Worker.metadata_calls = 0

    def test_interrupted_membership_sync_then_recovery_without_duplicate(self):
        account = Account("A", "crassus_a", "password123", next(iter(REGISTRY)))
        session = AccountSession(account, base_url=self.base, bot_registration_key=_Worker.key)
        self.assertEqual(probe_registration_key(self.base, _Worker.key)["status"], "accepted")

        with self.assertRaises(TransportError):
            session.ensure_session()
        self.assertEqual(len(_Worker.accounts), 1)
        self.assertEqual(_Worker.register_creations, 1)

        _Worker.fail_sync = False
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(len(_Worker.accounts), 1)
        self.assertEqual(_Worker.register_creations, 1)
        self.assertEqual(_Worker.metadata_calls, 1)

    def test_key_mismatch_fails_configuration_without_new_account(self):
        account = Account("A", "crassus_a", "password123", next(iter(REGISTRY)))
        session = AccountSession(account, base_url=self.base, bot_registration_key="wrong")
        with self.assertRaises(ConfigError):
            session.ensure_session()
        self.assertEqual(_Worker.accounts, {})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PreflightTests)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryWorkflowTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
