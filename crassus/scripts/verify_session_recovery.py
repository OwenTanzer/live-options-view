#!/usr/bin/env python3
"""Verify bounded login/registration classification and runtime re-auth (#41).

Production showed `login=500/503` followed by `register=409` for every account,
every cycle: `ensure_session` treated any login failure as "account missing", so
a Worker outage produced a registration attempt that could only fail, doubling
requests and KV reads. A second gap was invisible until it mattered: `/api/me`
returning 401 (Worker restart, cleared session) was raised as a transport error
forever, so a healthy account could stay dark until the process restarted.

These tests script only the HTTP layer and exercise the real `AccountSession`
logic:

* transient login (408/425/429/5xx) never registers and retries on a later
  cycle, recovering when the Worker does;
* provisioning happens only on 401, once, with bounded backoff on recoverable
  registration failures and a cooldown after a credential mismatch;
* 401-then-409 is reported as a credential mismatch, not retried;
* `/api/me` 401 re-authenticates exactly once, then either recovers or raises a
  configuration error instead of looping;
* transient `/api/me` failures stay transport errors.

    python scripts/verify_session_recovery.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

from crassus.client import (  # noqa: E402
    CREDENTIAL_MISMATCH_COOLDOWN_S,
    PROVISION_BACKOFF_S,
    AccountSession,
    AmbiguousExecution,
    ConfigError,
    TransportError,
    is_transient,
)
from crassus.config import Account  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body
        self.headers: dict[str, str] = {}

    def json(self):
        return json.loads(self.text or "{}")


def _ok(status: int = 200) -> FakeResponse:
    return FakeResponse(status, json.dumps({"username": "crassus_a", "balance_cash": 10000.0,
                                           "trades": []}))


class ScriptedSession(AccountSession):
    """Real session logic; only the transport is scripted and recorded."""

    def __init__(self, script: dict[tuple[str, str], list], *, now=None):
        super().__init__(
            Account(alias="A", username="crassus_a", password="pw",
                    strategy_id="reddit_sentiment_qqq"),
            base_url="http://fake",
            bot_registration_key="test-key",
            now=now,
        )
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, **kw):
        self.calls.append((method, path))
        queue = self.script.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected request {method} {path}; calls={self.calls}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def count(self, method: str, path: str) -> int:
        return self.calls.count((method, path))


class LoginClassificationTests(unittest.TestCase):
    def test_transient_login_never_registers(self):
        for status in (408, 425, 429, 500, 501, 502, 503, 504, 505, 599):
            with self.subTest(status=status):
                session = ScriptedSession({("POST", "/api/login"): [FakeResponse(status)]})
                with self.assertRaises(TransportError):
                    session.ensure_session()
                self.assertEqual(session.count("POST", "/api/register"), 0)
                self.assertEqual(session.count("POST", "/api/login"), 1)

    def test_every_5xx_is_transient_by_class(self):
        # The documented policy is "5xx is the server's fault"; this pins the
        # class rather than an enumerated list, so 501/505/599 behave like 503.
        for status in range(500, 600, 7):
            with self.subTest(status=status):
                self.assertTrue(is_transient(status))
        for status in (400, 401, 403, 404, 409, 422):
            with self.subTest(status=status):
                self.assertFalse(is_transient(status))

    def test_transient_login_recovers_on_a_later_cycle(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(503), _ok()],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("GET", "/api/me"): [_ok()],
        })
        with self.assertRaises(TransportError):
            session.ensure_session()
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("POST", "/api/register"), 0)

    def test_401_provisions_once_and_later_cycles_only_log_in(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401), _ok()],
            ("POST", "/api/register"): [_ok(201)],
            ("GET", "/api/me"): [_ok(), _ok()],
            ("POST", "/api/bot-metadata"): [_ok()],
        })
        session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)
        session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)

    def test_401_then_409_is_a_credential_mismatch_with_a_cooldown(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401), FakeResponse(401)],
            ("POST", "/api/register"): [FakeResponse(409, "Username already taken")],
        })
        with self.assertRaises(ConfigError) as first:
            session.ensure_session()
        self.assertIn("Credential mismatch", str(first.exception))
        # Next cycle: a login is attempted, but registration is not.
        with self.assertRaises(ConfigError) as second:
            session.ensure_session()
        self.assertIn("exists on the Worker", str(second.exception))
        self.assertEqual(session.count("POST", "/api/register"), 1)

    def test_mismatch_cooldown_expires_and_never_retries_registration_early(self):
        now = [1000.0]
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401)] * 3,
            ("POST", "/api/register"): [FakeResponse(409, "Username already taken")] * 2,
        }, now=lambda: now[0])
        with self.assertRaises(ConfigError):
            session.ensure_session()
        now[0] += CREDENTIAL_MISMATCH_COOLDOWN_S - 1
        with self.assertRaises(ConfigError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)
        now[0] += 2  # past the cooldown: exactly one more bounded attempt
        with self.assertRaises(ConfigError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 2)


class ProvisionBackoffTests(unittest.TestCase):
    def test_transient_register_backoff_allows_recovery(self):
        now = [500.0]
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401)] * 4,
            ("POST", "/api/register"): [FakeResponse(503), _ok(201)],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("GET", "/api/me"): [_ok()],
        }, now=lambda: now[0])

        with self.assertRaises(TransportError) as first:
            session.ensure_session()
        self.assertIn(f"{PROVISION_BACKOFF_S[0]:.0f}s", str(first.exception))
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += 10  # still inside backoff: login only, no register
        with self.assertRaises(TransportError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += PROVISION_BACKOFF_S[0]  # backoff elapsed: retry succeeds
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("POST", "/api/register"), 2)

    def test_backoff_grows_but_stays_bounded(self):
        now = [500.0]
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401)] * 5,
            ("POST", "/api/register"): [FakeResponse(503)] * 3,
        }, now=lambda: now[0])
        for expected in PROVISION_BACKOFF_S:
            with self.assertRaises(TransportError) as failure:
                session.ensure_session()
            self.assertIn(f"{expected:.0f}s", str(failure.exception))
            now[0] += expected
        self.assertEqual(session.count("POST", "/api/register"), 3)

    def test_register_transport_exception_uses_the_same_backoff(self):
        # A connection failure or timeout on the registration POST used to
        # escape before any accounting, so a refused connection could be
        # retried every cycle with no spacing. It now takes the same schedule.
        now = [700.0]
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401)] * 4,
            ("POST", "/api/register"): [TransportError("connection refused"), _ok(201)],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("GET", "/api/me"): [_ok()],
        }, now=lambda: now[0])

        with self.assertRaises(TransportError) as first:
            session.ensure_session()
        self.assertIn(f"{PROVISION_BACKOFF_S[0]:.0f}s", str(first.exception))
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += 10  # inside backoff: login only
        with self.assertRaises(TransportError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += PROVISION_BACKOFF_S[0]  # elapsed: retry succeeds
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("POST", "/api/register"), 2)

    def test_register_timeout_is_ambiguous_but_still_backed_off(self):
        # A timeout may mean the account was created. Recovery is the next
        # cycle's login; meanwhile the attempt is spaced like any transient.
        now = [900.0]
        session = ScriptedSession({
            ("POST", "/api/login"): [FakeResponse(401), FakeResponse(401), _ok()],
            ("POST", "/api/register"): [AmbiguousExecution("POST /api/register timed out")],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("GET", "/api/me"): [_ok()],
        }, now=lambda: now[0])

        with self.assertRaises(TransportError) as first:
            session.ensure_session()
        self.assertIn("AmbiguousExecution", str(first.exception))
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += 5
        with self.assertRaises(TransportError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/register"), 1)

        now[0] += PROVISION_BACKOFF_S[0]
        # The account exists now (the timed-out POST landed): login succeeds.
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("POST", "/api/register"), 1)

    def test_register_key_mismatch_and_validation_are_config_errors(self):
        cases = [
            (FakeResponse(403, json.dumps({"error": "Invalid bot registration key"})),
             "BOT_REGISTRATION_KEY"),
            (FakeResponse(400, json.dumps({"error": "Password must be 8-30 characters"})),
             "not recoverable"),
        ]
        for response, expected in cases:
            with self.subTest(status=response.status_code):
                session = ScriptedSession({
                    ("POST", "/api/login"): [FakeResponse(401)],
                    ("POST", "/api/register"): [response],
                })
                with self.assertRaises(ConfigError) as failure:
                    session.ensure_session()
                self.assertIn(expected, str(failure.exception))
                self.assertEqual(session.count("POST", "/api/register"), 1)


class RuntimeReauthTests(unittest.TestCase):
    """`ensure_session` always logs in first; these script both phases."""

    def test_me_401_reauthenticates_and_recovers_in_the_same_cycle(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [_ok(), _ok()],
            ("POST", "/api/bot-metadata"): [_ok(), _ok()],
            ("GET", "/api/me"): [FakeResponse(401), _ok()],
        })
        state = session.ensure_session()
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("GET", "/api/me"), 2)
        self.assertEqual(session.count("POST", "/api/login"), 2)

    def test_session_recovers_on_a_later_cycle_after_a_transient_login(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [_ok(), FakeResponse(503), _ok()],
            ("POST", "/api/bot-metadata"): [_ok(), _ok()],
            ("GET", "/api/me"): [FakeResponse(401), _ok()],
        })
        with self.assertRaises(TransportError):
            session.ensure_session()          # first cycle: re-auth hits a 503
        state = session.ensure_session()      # next cycle: Worker recovered
        self.assertEqual(state.username, "crassus_a")
        self.assertEqual(session.count("POST", "/api/login"), 3)

    def test_me_401_after_a_fresh_login_is_config_not_a_loop(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [_ok(), _ok()],
            ("POST", "/api/bot-metadata"): [_ok(), _ok()],
            ("GET", "/api/me"): [FakeResponse(401), FakeResponse(401)],
        })
        with self.assertRaises(ConfigError) as failure:
            session.ensure_session()
        self.assertIn("even after a fresh login", str(failure.exception))
        # Exactly two `/api/me` reads and two logins: one bounded retry, no loop.
        self.assertEqual(session.count("GET", "/api/me"), 2)
        self.assertEqual(session.count("POST", "/api/login"), 2)

    def test_me_401_with_rejected_credentials_is_a_mismatch(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [_ok(), FakeResponse(401)],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("POST", "/api/register"): [FakeResponse(409, "Username already taken")],
            ("GET", "/api/me"): [FakeResponse(401)],
        })
        with self.assertRaises(ConfigError) as failure:
            session.ensure_session()
        self.assertIn("Credential mismatch", str(failure.exception))

    def test_transient_me_failure_does_not_trigger_reauthentication(self):
        session = ScriptedSession({
            ("POST", "/api/login"): [_ok()],
            ("POST", "/api/bot-metadata"): [_ok()],
            ("GET", "/api/me"): [FakeResponse(503)],
        })
        with self.assertRaises(TransportError):
            session.ensure_session()
        self.assertEqual(session.count("POST", "/api/login"), 1)  # startup login only


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (LoginClassificationTests, ProvisionBackoffTests, RuntimeReauthTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
