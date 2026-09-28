import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capture_diagnostics import exception_details
from moo144_tradier_collector import BoundedStats


class DiagnosticsTests(unittest.TestCase):
    def test_nested_errors_redact_secrets_and_keep_types(self):
        inner = TimeoutError("read timeout https://provider/stream?sessionid=secret Bearer credential")
        outer = ConnectionError(inner)
        details = exception_details(outer, ("secret", "credential"))
        text = json.dumps(details)
        self.assertNotIn("secret", text)
        self.assertNotIn("credential", text)
        self.assertEqual([e["type"] for e in details], ["ConnectionError", "TimeoutError"])
        inner.__cause__ = outer
        self.assertEqual(len(exception_details(outer)), 2)

    def test_trade_latency_does_not_use_quote_age(self):
        stats = BoundedStats()
        receipt = "2026-09-28T13:30:10+00:00"
        stats.observe({"type": "timesale", "symbol": "QQQ", "date": 1790602200000,
                       "collector_receipt_timestamp": receipt})
        lag = stats.summary()["trade_delivery_lag_ms"]
        self.assertEqual(lag["latest"], 10000)
        self.assertEqual(lag["over_5000ms_count"], 1)
        stats.observe({"type": "quote", "symbol": "QQQ", "biddate": 1,
                       "collector_receipt_timestamp": receipt})
        self.assertEqual(stats.summary()["trade_delivery_lag_ms"], lag)
