import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


try:
    import boto3  # noqa: F401
except ModuleNotFoundError:
    boto3 = types.ModuleType("boto3")
    boto3.client = Mock()
    sys.modules["boto3"] = boto3

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests = types.ModuleType("requests")
    requests.RequestException = type("RequestException", (Exception,), {})
    requests.HTTPError = type("HTTPError", (requests.RequestException,), {})
    requests.ConnectionError = type("ConnectionError", (requests.RequestException,), {})
    requests.Session = Mock
    sys.modules["requests"] = requests

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import moo144_tradier_probe as probe


ET = ZoneInfo("America/New_York")


class FakeTradier:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/markets/clock":
            return {"clock": {
                "date": "2026-09-03",
                "state": "open",
                "next_change": "16:00",
            }}
        if path == "/markets/quotes":
            return {"quotes": {"quote": {"symbol": "QQQ", "last": 600}}}
        if path == "/markets/options/expirations":
            return {"expirations": {"date": ["2026-09-03"]}}
        if path == "/markets/options/chains":
            return {"options": {"option": [
                {"symbol": "QQQ-C599", "strike": 599, "option_type": "call"},
                {"symbol": "QQQ-P599", "strike": 599, "option_type": "put"},
                {"symbol": "QQQ-C600", "strike": 600, "option_type": "call"},
                {"symbol": "QQQ-P600", "strike": 600, "option_type": "put"},
                {"symbol": "QQQ-C6005", "strike": 600.5, "option_type": "call"},
                {"symbol": "QQQ-C601", "strike": 601, "option_type": "call"},
                {"symbol": "QQQ-P601", "strike": 601, "option_type": "put"},
            ]}}
        raise AssertionError(path)


class FakeR2:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **_kwargs):
        if _kwargs.get("IfNoneMatch") == "*" and (Bucket, Key) in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {
                "ResponseMetadata": {"HTTPStatusCode": 412},
                "Error": {"Code": "PreconditionFailed"},
            }
            raise error
        self.objects[(Bucket, Key)] = bytes(Body)

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class FakeResponse:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        return iter(())


class FakeStreamClient:
    def __init__(self):
        self.sessions = 0
        self.session = Mock()
        self.session.get.side_effect = lambda *_args, **_kwargs: FakeResponse()

    def create_market_session(self):
        self.sessions += 1
        return f"session-{self.sessions}"


class MemoryWriter:
    def __init__(self):
        self.events = []
        self.artifacts = []

    def write(self, event):
        self.events.append(event)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        probe.STOP = False

    def test_normalize_accepts_tradier_singleton_and_list_shapes(self):
        self.assertEqual(probe.normalize(None), [])
        self.assertEqual(probe.normalize({"x": 1}), [{"x": 1}])
        self.assertEqual(probe.normalize([{"x": 1}, "bad"]), [{"x": 1}])

    def test_validate_run_window_fails_closed(self):
        now = datetime(2026, 9, 3, 10, 0, tzinfo=ET)
        clock = {"date": "2026-09-03", "state": "open", "next_change": "16:00"}
        self.assertEqual(
            probe.validate_run_window("2026-09-03", clock, 1800, now),
            "2026-09-03",
        )
        with self.assertRaisesRegex(RuntimeError, "required"):
            probe.validate_run_window(None, clock, 1800, now)
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            probe.validate_run_window("2026-09-04", clock, 1800, now)
        with self.assertRaisesRegex(RuntimeError, "does not fit"):
            probe.validate_run_window(
                "2026-09-03",
                {"date": "2026-09-03", "state": "open", "next_change": "10:15"},
                1800,
                now,
            )

    def test_select_symbols_chooses_nearest_complete_0dte_strikes(self):
        client = FakeTradier()
        symbols, universe = probe.select_symbols(
            client,
            strike_count=2,
            duration_seconds=1800,
            run_date="2026-09-03",
            now_et=datetime(2026, 9, 3, 10, 0, tzinfo=ET),
        )
        self.assertEqual(symbols[0], "QQQ")
        self.assertEqual(set(symbols[1:]), {
            "QQQ-C599", "QQQ-P599", "QQQ-C600", "QQQ-P600",
        })
        self.assertEqual(universe["option_metadata"]["QQQ-C600"]["strike"], 600)

    def test_r2_write_and_segment_upload_are_head_verified(self):
        r2 = FakeR2()
        metadata = probe.put_bytes_verified(
            r2, "bucket", "prefix/run-started.json", b"{}", "application/json"
        )
        self.assertEqual(metadata["bytes"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            writer = probe.SegmentWriter(
                Path(tmp), "prefix", r2, "bucket", checkpoint_seconds=60
            )
            writer.write({"type": "quote", "symbol": "QQQ"})
            writer.close()
            self.assertEqual(len(writer.artifacts), 1)
            artifact = writer.artifacts[0]
            self.assertEqual(artifact["records"], 1)
            self.assertIn(("bucket", artifact["key"]), r2.objects)

    def test_stats_distinguish_empty_flag_and_measure_preceding_quote_age(self):
        stats = probe.Stats()
        stats.observe({
            "type": "quote", "symbol": "OPT", "biddate": "1000", "askdate": "1200",
        })
        event = {
            "type": "timesale",
            "symbol": "OPT",
            "date": "1500",
            "seq": 10,
            "flag": "",
            "cancel": False,
            "correction": True,
            "session": "normal",
        }
        observed = stats.observe(dict(event))
        stats.observe(dict(event))
        summary = stats.summary()
        self.assertEqual(observed["preceding_quote_age_ms"], 300)
        self.assertEqual(summary["timesale_field_population"]["flag"]["key_present"], 2)
        self.assertEqual(summary["timesale_field_population"]["flag"]["non_empty"], 0)
        self.assertEqual(summary["flag_frequencies"]["<empty>"], 2)
        self.assertEqual(summary["correction_count"], 2)
        self.assertEqual(summary["duplicate_count"], 1)
        self.assertEqual(summary["sequence_discontinuities_by_symbol"], {})

    def test_daily_claim_is_atomic_and_refuses_second_launch(self):
        r2 = FakeR2()
        first = probe.claim_run_once(
            r2, "bucket", "2026-09-03", {"run_id": "first"}
        )
        self.assertTrue(first["key"].endswith("/2026-09-03/run-claim.json"))
        with self.assertRaisesRegex(RuntimeError, "already claimed"):
            probe.claim_run_once(
                r2, "bucket", "2026-09-03", {"run_id": "second"}
            )

    def test_clean_stream_end_is_bounded_and_recorded_as_gap(self):
        client = FakeStreamClient()
        writer = MemoryWriter()
        with self.assertRaisesRegex(RuntimeError, "consecutive reconnects"):
            probe.capture(
                client,
                ["QQQ"],
                writer,
                probe.Stats(),
                duration_seconds=60,
                max_consecutive_reconnects=1,
                monotonic=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(client.sessions, 2)
        self.assertEqual([event["type"] for event in writer.events], ["gap", "gap"])

    def test_stream_payload_requests_unfiltered_diagnostics(self):
        self.assertEqual(probe.stream_payload(["QQQ", "OPT"], "session"), {
            "symbols": "QQQ,OPT",
            "sessionid": "session",
            "filter": "quote,timesale",
            "linebreak": "true",
            "validOnly": "false",
            "advancedDetails": "true",
        })

    def test_main_writes_verified_checkpoint_summary_and_manifest(self):
        r2 = FakeR2()

        def fake_capture(_client, _symbols, writer, stats, *_args):
            writer.write(stats.observe({
                "type": "timesale",
                "symbol": "OPT",
                "date": "1000",
                "seq": 1,
                "flag": "",
                "cancel": False,
                "correction": False,
                "session": "normal",
            }))
            return 0

        environment = {
            "TRADIER_TOKEN": "token",
            "MOO144_RUN_DATE": "2099-01-01",
            "MOO144_DURATION_SECONDS": "60",
            "MOO144_STRIKE_COUNT": "2",
            "MOO144_CHECKPOINT_SECONDS": "30",
            "MOO144_MAX_CONSECUTIVE_RECONNECTS": "1",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(
                probe,
                "select_symbols",
                return_value=(
                    ["QQQ", "OPT"],
                    {"expiration": "2099-01-01", "option_symbols": ["OPT"]},
                ),
            ),
            patch.object(probe, "r2_client", return_value=(r2, "bucket")),
            patch.object(probe, "capture", side_effect=fake_capture),
        ):
            self.assertEqual(probe.main(), 0)

        keys = {key for bucket, key in r2.objects if bucket == "bucket"}
        self.assertTrue(any(key.endswith("/run-started.json") for key in keys))
        self.assertTrue(any(key.endswith("/run-claim.json") for key in keys))
        self.assertTrue(any("normalized-events-part-" in key for key in keys))
        self.assertTrue(any(key.endswith("/summary.json") for key in keys))
        self.assertTrue(any(key.endswith("/manifest.json") for key in keys))

    def test_nonretryable_auth_error_is_not_retried(self):
        error = probe.requests.HTTPError("unauthorized")
        error.response = types.SimpleNamespace(status_code=401)
        self.assertFalse(probe.is_retryable(error))
        throttled = probe.requests.HTTPError("slow down")
        throttled.response = types.SimpleNamespace(status_code=429)
        self.assertTrue(probe.is_retryable(throttled))

    def test_script_entrypoint_executes_and_fails_nonzero_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "boto3.py").write_text("def client(*args, **kwargs): return None\n")
            Path(tmp, "requests.py").write_text(
                "class RequestException(Exception): pass\n"
                "class HTTPError(RequestException): pass\n"
                "class ConnectionError(RequestException): pass\n"
                "class Session: pass\n"
            )
            env = os.environ.copy()
            env.pop("TRADIER_TOKEN", None)
            env["PYTHONPATH"] = tmp
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "moo144_tradier_probe.py")],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["event"], "probe_failed")
        self.assertIn("TRADIER_TOKEN", payload["message"])


if __name__ == "__main__":
    unittest.main()
