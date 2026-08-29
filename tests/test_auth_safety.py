import os
import sys
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

    pyotp = types.ModuleType("pyotp")
    pyotp.TOTP = lambda _secret: types.SimpleNamespace(now=lambda: "123456")
    sys.modules.setdefault("pyotp", pyotp)

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


def _success_response():
    return FakeResponse(201, {
        "data": {
            "session-token": "session-secret",
            "remember-token": "rotated-secret",
        }
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
            "TASTY_LOGIN": "trader@example.com",
            "TASTY_PASSWORD": "correct-horse-battery-staple",
            "TASTY_TOTP_SECRET": "totp-seed-secret",
            "TASTY_REMEMBER_TOKEN": "remember-secret",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_400_and_403_with_challenge_header_take_totp_path_once(self):
        for status in (400, 403):
            with self.subTest(status=status), \
                    patch.object(collector, "_load_remember_token", return_value=None), \
                    patch.object(collector, "_save_remember_token"):
                collector.requests.post.reset_mock()
                collector.requests.get.reset_mock()
                collector.requests.post.side_effect = [
                    FakeResponse(status, {"error": {"code": "challenge-required"}}, {
                        "X-Tastyworks-Challenge-Token": "challenge-secret"
                    }),
                    FakeResponse(204),
                    _success_response(),
                ]
                collector.requests.get.return_value = _quote_response()

                auth = collector.tasty_auth("trader@example.com", object())

                self.assertEqual(auth["session_token"], "session-secret")
                self.assertEqual(collector.requests.post.call_count, 3)
                otp_calls = [
                    call for call in collector.requests.post.call_args_list
                    if call.kwargs.get("headers", {}).get("X-Tastyworks-OTP")
                ]
                self.assertEqual(len(otp_calls), 1)

    def test_4xx_without_challenge_fails_after_one_password_request(self):
        with patch.object(collector, "_load_remember_token", return_value=None):
            collector.requests.post.reset_mock()
            collector.requests.get.reset_mock()
            collector.requests.post.return_value = FakeResponse(
                400, {"error": {"code": "invalid-request", "message": "rejected"}}
            )

            with self.assertRaises(collector.TastyAuthError) as raised:
                collector.tasty_auth("trader@example.com", object())

            self.assertEqual(raised.exception.phase, "password")
            self.assertEqual(collector.requests.post.call_count, 1)
            collector.requests.get.assert_not_called()

    def test_failed_remember_and_password_chain_is_bounded(self):
        with patch.object(collector, "_load_remember_token", return_value="remember-secret"):
            collector.requests.post.reset_mock()
            collector.requests.get.reset_mock()
            collector.requests.post.side_effect = [
                FakeResponse(401, {"error": {"code": "expired-token"}}),
                FakeResponse(401, {"error": {"code": "invalid-credentials"}}),
            ]

            with self.assertRaises(collector.TastyAuthError):
                collector.tasty_auth("trader@example.com", object())

            self.assertEqual(collector.requests.post.call_count, 2)
            collector.requests.get.assert_not_called()

    def test_diagnostics_allowlist_fields_and_redact_configured_secrets(self):
        unconfigured_long_token = "abcdefghijklmnopqrstuvwxyz0123456789TOKEN"
        response = FakeResponse(400, {
            "message": (
                "login trader@example.com password correct-horse-battery-staple "
                f"token={unconfigured_long_token}"
            ),
            "token": "this-must-not-be-logged",
            "error": {
                "reason": "remember-secret expired",
                "debug": "totp-seed-secret",
            },
        })

        diagnostic = collector._safe_broker_error(response)

        self.assertNotIn("trader@example.com", diagnostic)
        self.assertNotIn("correct-horse-battery-staple", diagnostic)
        self.assertNotIn("remember-secret", diagnostic)
        self.assertNotIn("this-must-not-be-logged", diagnostic)
        self.assertNotIn("totp-seed-secret", diagnostic)
        self.assertNotIn(unconfigured_long_token, diagnostic)
        self.assertIn("[redacted]", diagnostic)


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
