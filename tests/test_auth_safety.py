import inspect
import os
import sys
import threading
import types
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch


def _install_import_stubs():
    boto3 = types.ModuleType("boto3")
    boto3.client = Mock()
    sys.modules.setdefault("boto3", boto3)

    requests = types.ModuleType("requests")
    requests.Response = type("Response", (), {})
    requests.post = Mock()
    requests.get = Mock()
    sys.modules.setdefault("requests", requests)

    websocket = types.ModuleType("websocket")
    websocket.WebSocketApp = type("WebSocketApp", (), {})
    sys.modules.setdefault("websocket", websocket)

    yfinance = types.ModuleType("yfinance")
    yfinance.Tickers = Mock()
    sys.modules.setdefault("yfinance", yfinance)

    crude_calibration = types.ModuleType("crude_calibration")
    sys.modules.setdefault("crude_calibration", crude_calibration)

    market_signals = types.ModuleType("market_signals")
    market_signals.VwapState = type("VwapState", (), {})
    sys.modules.setdefault("market_signals", market_signals)


_install_import_stubs()
import collector


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _oauth_response(access_token="access-secret", expires_in=900, token_type="Bearer"):
    return FakeResponse(200, {
        "access_token": access_token,
        "expires_in": expires_in,
        "token_type": token_type,
    })


def _quote_response():
    return FakeResponse(200, {
        "data": {
            "token": "stream-secret",
            "dxlink-url": "wss://example.invalid/realtime",
        }
    })


class TastyAuthSafetyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TASTY_OAUTH_CLIENT_SECRET": "client-secret-value",
            "TASTY_OAUTH_REFRESH_TOKEN": "refresh-token-value",
            "TASTY_OAUTH_SCOPES": "read",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        collector.requests.post.reset_mock(return_value=True, side_effect=True)
        collector.requests.get.reset_mock(return_value=True, side_effect=True)

    def _manager(self, *, clock=lambda: 0.0):
        return collector.OAuthTokenManager(
            "client-secret-value", "refresh-token-value", "read", clock=clock
        )

    def test_successful_refresh_exchange_and_quote_token_use_bearer_auth(self):
        collector.requests.post.return_value = _oauth_response()
        collector.requests.get.return_value = _quote_response()

        auth = collector.tasty_auth(self._manager())

        self.assertEqual(auth["access_token"], "access-secret")
        self.assertEqual(auth["streamer_token"], "stream-secret")
        collector.requests.post.assert_called_once_with(
            f"{collector.TASTY_BASE}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_secret": "client-secret-value",
                "refresh_token": "refresh-token-value",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": collector.TASTY_USER_AGENT,
            },
            timeout=15,
        )
        quote_call = collector.requests.get.call_args
        self.assertEqual(
            quote_call.kwargs["headers"]["Authorization"],
            "Bearer access-secret",
        )

    def test_rejected_refresh_token_fails_closed_without_quote_request(self):
        collector.requests.post.return_value = FakeResponse(
            401, {"error": {"code": "invalid-grant", "message": "rejected"}}
        )

        with self.assertRaises(collector.TastyAuthError) as raised:
            collector.tasty_auth(self._manager())

        self.assertEqual(raised.exception.phase, "oauth-token")
        self.assertEqual(collector.requests.post.call_count, 1)
        collector.requests.get.assert_not_called()

    def test_malformed_oauth_success_response_fails_closed(self):
        malformed = (
            FakeResponse(200, {}),
            _oauth_response(access_token=""),
            _oauth_response(expires_in=30),
            _oauth_response(token_type="MAC"),
        )
        for response in malformed:
            with self.subTest(response=response._payload):
                collector.requests.post.reset_mock()
                collector.requests.get.reset_mock()
                collector.requests.post.return_value = response
                with self.assertRaises(collector.TastyAuthError) as raised:
                    self._manager().get_access_token()
                self.assertEqual(raised.exception.detail, "malformed-success-response")
                collector.requests.get.assert_not_called()

    def test_access_token_is_refreshed_before_expiry(self):
        now = [0.0]
        collector.requests.post.side_effect = [
            _oauth_response("access-one", expires_in=900),
            _oauth_response("access-two", expires_in=900),
        ]
        manager = self._manager(clock=lambda: now[0])

        self.assertEqual(manager.get_access_token(), "access-one")
        now[0] = 839.0
        self.assertEqual(manager.get_access_token(), "access-one")
        now[0] = 840.0
        self.assertEqual(manager.get_access_token(), "access-two")
        self.assertEqual(collector.requests.post.call_count, 2)

    def test_access_token_refresh_is_single_flight_under_concurrent_demand(self):
        collector.requests.post.return_value = _oauth_response("shared-access")
        manager = self._manager()
        barrier = threading.Barrier(8)
        results = []
        failures = []

        def worker():
            try:
                barrier.wait()
                results.append(manager.get_access_token())
            except Exception as exc:  # pragma: no cover - assertion below reports it
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(failures)
        self.assertEqual(results, ["shared-access"] * 8)
        self.assertEqual(collector.requests.post.call_count, 1)

    def test_quote_token_401_forces_one_refresh_then_stops(self):
        collector.requests.post.side_effect = [
            _oauth_response("access-one"),
            _oauth_response("access-two"),
        ]
        collector.requests.get.side_effect = [
            FakeResponse(401, {"error": {"code": "unauthorized"}}),
            _quote_response(),
        ]

        auth = collector.tasty_auth(self._manager())

        self.assertEqual(auth["access_token"], "access-two")
        self.assertEqual(collector.requests.post.call_count, 2)
        self.assertEqual(collector.requests.get.call_count, 2)
        self.assertEqual(
            collector.requests.get.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer access-two",
        )

    def test_configuration_rejects_any_scope_beyond_read(self):
        for scopes in ("", "trade", "read trade", "read,openid"):
            with self.subTest(scopes=scopes), self.assertRaises(collector.TastyAuthError):
                collector.OAuthTokenManager("client", "refresh", scopes)

    def test_production_auth_contains_no_legacy_session_fallback(self):
        source = inspect.getsource(collector)
        self.assertNotIn('f"{TASTY_BASE}/sessions"', source)
        self.assertNotIn("TASTY_LOGIN", source)
        self.assertNotIn("TASTY_PASSWORD", source)
        self.assertNotIn("TASTY_TOTP_SECRET", source)
        self.assertNotIn("TASTY_REMEMBER_TOKEN", source)

    def test_diagnostics_allowlist_fields_and_redact_configured_secrets(self):
        unconfigured_long_token = "abcdefghijklmnopqrstuvwxyz0123456789TOKEN"
        response = FakeResponse(400, {
            "message": (
                "secret client-secret-value token refresh-token-value "
                f"token={unconfigured_long_token}"
            ),
            "token": "this-must-not-be-logged",
            "error": {
                "reason": "refresh-token-value expired",
                "debug": "client-secret-value",
            },
        })

        diagnostic = collector._safe_broker_error(response)

        self.assertNotIn("client-secret-value", diagnostic)
        self.assertNotIn("refresh-token-value", diagnostic)
        self.assertNotIn("this-must-not-be-logged", diagnostic)
        self.assertNotIn(unconfigured_long_token, diagnostic)
        self.assertIn("[redacted]", diagnostic)

    def test_permanent_auth_failure_opens_session_day_circuit(self):
        class StopLoop(Exception):
            pass

        manager = self._manager()
        wait_calls = []

        def wait_once_then_stop(blocked_session_date=None):
            wait_calls.append(blocked_session_date)
            if len(wait_calls) == 2:
                raise StopLoop()

        with patch.object(collector.OAuthTokenManager, "from_env", return_value=manager), \
                patch.object(collector, "start_live_quote_server"), \
                patch.object(collector, "make_s3", return_value=object()), \
                patch.object(collector.threading, "Thread") as thread_cls, \
                patch.object(collector, "wait_for_premarket", side_effect=wait_once_then_stop), \
                patch.object(
                    collector,
                    "_run_session",
                    side_effect=collector.TastyAuthError("oauth-token", status=401),
                ) as run_session:
            thread_cls.return_value.start.return_value = None
            with self.assertRaises(StopLoop):
                collector.main()

        self.assertEqual(run_session.call_count, 1)
        self.assertIsNone(wait_calls[0])
        self.assertIsInstance(wait_calls[1], date)


class ExchangeSessionSafetyTests(unittest.TestCase):
    def test_saturday_is_not_eligible_even_inside_wall_clock_window(self):
        saturday = collector.ET.localize(datetime(2026, 8, 29, 10, 0))
        with patch.object(collector, "_exchange_session_dates", return_value=set()):
            self.assertFalse(collector._session_is_eligible(saturday))

    def test_next_start_skips_weekend_and_blocked_session_date(self):
        saturday = collector.ET.localize(datetime(2026, 8, 29, 10, 0))
        monday = date(2026, 8, 31)
        tuesday = date(2026, 9, 1)
        with patch.object(
            collector, "_exchange_session_dates", return_value={monday, tuesday}
        ):
            next_start = collector._next_session_start(
                saturday, blocked_session_date=monday
            )

        self.assertEqual(next_start, collector.ET.localize(datetime(2026, 9, 1, 6, 0)))

    def test_open_auth_circuit_rejects_same_session_without_calendar_lookup(self):
        monday = collector.ET.localize(datetime(2026, 8, 31, 10, 0))
        with patch.object(collector, "_exchange_session_dates") as exchange_days:
            self.assertFalse(
                collector._session_is_eligible(monday, blocked_session_date=monday.date())
            )
        exchange_days.assert_not_called()


if __name__ == "__main__":
    unittest.main()
