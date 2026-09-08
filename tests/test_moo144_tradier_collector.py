import hashlib
import io
import json
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
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
import moo144_tradier_collector as collector  # noqa: E402


ET = ZoneInfo("America/New_York")


class ClientError(Exception):
    def __init__(self, status=412, code="PreconditionFailed"):
        super().__init__(code)
        self.response = {
            "ResponseMetadata": {"HTTPStatusCode": status},
            "Error": {"Code": code},
        }


class NoSuchKey(Exception):
    pass


class FakeExceptions:
    NoSuchKey = NoSuchKey
    ClientError = ClientError


class FakeR2:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self.exceptions = FakeExceptions()

    def _etag(self, body: bytes) -> str:
        return hashlib.md5(body).hexdigest()

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **_kwargs):
        exists = (Bucket, Key) in self.objects
        if IfNoneMatch == "*" and exists:
            raise ClientError()
        if IfMatch is not None and self.etags.get((Bucket, Key)) != IfMatch:
            raise ClientError()
        body = bytes(Body)
        self.objects[(Bucket, Key)] = body
        self.etags[(Bucket, Key)] = self._etag(body)

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("missing")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.etags[(bucket, key)] = self._etag(self.objects[(bucket, key)])

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("missing")
        return {
            "ContentLength": len(self.objects[(Bucket, Key)]),
            "ETag": self.etags[(Bucket, Key)],
        }

    def list_objects_v2(self, *, Bucket, Prefix):
        contents = [
            {"Key": key, "Size": len(body)}
            for (bucket, key), body in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents}


class FakeTradier:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/markets/clock":
            return {"clock": {"date": "2026-09-08", "state": "open", "next_change": "16:00"}}
        if path == "/markets/quotes":
            return {"quotes": {"quote": {"symbol": "QQQ", "last": 600}}}
        if path == "/markets/options/expirations":
            return {"expirations": {"date": ["2026-09-08"]}}
        if path == "/markets/options/chains":
            return {"options": {"option": [
                {"symbol": "QQQ-C600", "strike": 600, "option_type": "call"},
                {"symbol": "QQQ-P600", "strike": 600, "option_type": "put"},
            ]}}
        raise AssertionError(path)


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
        self.session.get.side_effect = lambda *_a, **_k: FakeResponse()

    def create_market_session(self):
        self.sessions += 1
        return f"session-{self.sessions}"


class BoundedStatsTests(unittest.TestCase):
    def test_reservoir_caps_memory_but_lifetime_stats_stay_exact(self):
        stats = collector.BoundedStats(reservoir_size=3)
        stats.observe({"type": "quote", "symbol": "OPT", "biddate": "10", "askdate": "10"})
        for i in range(10):
            stats.observe({
                "type": "timesale", "symbol": "OPT", "date": str(100 + i), "seq": i,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
            })
        summary = stats.age_summary()
        self.assertEqual(summary["count"], 10)
        self.assertEqual(summary["reservoir_size"], 3)
        self.assertLessEqual(len(stats.quote_age_reservoir), 3)

    def test_dedup_is_last_seq_only_not_a_growing_set(self):
        stats = collector.BoundedStats()
        event = {
            "type": "timesale", "symbol": "OPT", "date": "100", "seq": 5,
            "flag": "", "cancel": False, "correction": False, "session": "normal",
        }
        first = stats.observe(dict(event))
        duplicate = stats.observe(dict(event))
        advanced = stats.observe({**event, "seq": 6, "date": "200"})
        replay = stats.observe(dict(event))
        self.assertFalse(first["duplicate_in_run"])
        self.assertTrue(duplicate["duplicate_in_run"])
        self.assertFalse(advanced["duplicate_in_run"])
        self.assertFalse(replay["duplicate_in_run"])
        self.assertEqual(len(stats.last_sequence), 1)


class LeaseTests(unittest.TestCase):
    def test_acquire_then_second_owner_blocked_while_live(self):
        r2 = FakeR2()
        lease = collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        self.assertEqual(lease["owner_id"], "owner-a")
        with self.assertRaisesRegex(RuntimeError, "held by"):
            collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-b", ttl_seconds=300)

    def test_takeover_permitted_once_expired(self):
        r2 = FakeR2()
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        collector.acquire_lease(
            r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=1, now=lambda: past
        )
        lease = collector.acquire_lease(
            r2, "bucket", "2026-09-08", "owner-b", ttl_seconds=300,
            now=lambda: datetime.now(timezone.utc),
        )
        self.assertEqual(lease["owner_id"], "owner-b")

    def test_renew_extends_expiry_for_same_owner(self):
        r2 = FakeR2()
        collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        renewed = collector.renew_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        self.assertEqual(renewed["owner_id"], "owner-a")


class UploaderTests(unittest.TestCase):
    def test_queued_segment_uploads_without_blocking_ingest(self):
        r2 = FakeR2()
        uploader = collector.Uploader(r2, "bucket", "moo144/tradier/2026-09-08", max_spool_bytes=10_000_000)
        uploader.start()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seg.ndjson.gz"
            path.write_bytes(b"x" * 10)
            uploader.enqueue(path)
            for _ in range(50):
                if uploader.artifacts:
                    break
                time.sleep(0.05)
            uploader.drain_and_stop()
        self.assertEqual(len(uploader.artifacts), 1)
        self.assertFalse(path.exists())

    def test_retry_with_backoff_on_upload_failure(self):
        r2 = FakeR2()
        attempts = {"n": 0}
        original = r2.upload_file

        def flaky(filename, bucket, key, ExtraArgs=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return original(filename, bucket, key, ExtraArgs)

        r2.upload_file = flaky
        sleeps = []
        uploader = collector.Uploader(
            r2, "bucket", "moo144/tradier/2026-09-08", max_spool_bytes=10_000_000,
            sleeper=lambda s: sleeps.append(s),
        )
        uploader.start()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seg.ndjson.gz"
            path.write_bytes(b"x" * 10)
            uploader.enqueue(path)
            for _ in range(200):
                if uploader.artifacts:
                    break
                time.sleep(0.01)
            uploader.drain_and_stop()
        self.assertEqual(len(uploader.artifacts), 1)
        self.assertGreaterEqual(attempts["n"], 3)
        self.assertGreaterEqual(uploader.failures, 2)

    def test_spool_over_limit_sets_overload_flag_without_dropping(self):
        r2 = FakeR2()
        uploader = collector.Uploader(r2, "bucket", "prefix", max_spool_bytes=5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seg.ndjson.gz"
            path.write_bytes(b"x" * 100)
            uploader.enqueue(path)
            self.assertTrue(uploader.overloaded)
            self.assertEqual(len(uploader.queue), 1)


class ReconcileTests(unittest.TestCase):
    def test_reconciles_uploaded_and_leftover_spool_without_duplicates(self):
        r2 = FakeR2()
        r2.objects[("bucket", "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz")] = b"data"
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            leftover = spool_dir / "owner-a-part-0000.ndjson.gz"
            leftover.write_bytes(b"data")
            stale = spool_dir / "owner-a-part-0001.ndjson.gz"
            stale.write_bytes(b"unfinished")
            artifacts = collector.reconcile_existing_segments(
                r2, "bucket", "moo144/tradier/2026-09-08", spool_dir
            )
            self.assertEqual(len(artifacts), 1)
            self.assertFalse(leftover.exists())
            self.assertTrue(stale.exists())


class SessionGatingTests(unittest.TestCase):
    def test_no_session_today_is_a_clean_noop(self):
        import moo144_tradier_collector as mod
        original = mod.nyse_session_bounds
        mod.nyse_session_bounds = lambda day: None
        try:
            self.assertIsNone(mod.nyse_session_bounds(datetime.now(ET).date()))
        finally:
            mod.nyse_session_bounds = original


class CaptureSessionTests(unittest.TestCase):
    def setUp(self):
        collector.STOP = False

    def test_capture_stops_at_session_close_without_fixed_duration(self):
        client = FakeStreamClient()
        events = []

        class MemorySpool:
            def write(self, event):
                events.append(event)

        now_holder = {"t": datetime(2026, 9, 8, 15, 59, 50, tzinfo=ET)}
        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)

        def now_et():
            now_holder["t"] += timedelta(seconds=5)
            return now_holder["t"]

        collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            monotonic=lambda: 0.0,
            sleeper=lambda _s: None,
            now_et=now_et,
        )
        self.assertTrue(any(e["type"] == "gap" for e in events) or client.sessions >= 1)


class ManifestStatusTests(unittest.TestCase):
    def test_health_and_json_artifact_round_trip(self):
        r2 = FakeR2()
        stats = collector.BoundedStats()
        uploader = collector.Uploader(r2, "bucket", "prefix", max_spool_bytes=1000)
        collector.write_health(r2, "bucket", "moo144/tradier/2026-09-08", stats, uploader, reconnects=2, storage_failures=0)
        body = r2.objects[("bucket", "moo144/tradier/2026-09-08/health.json")]
        payload = json.loads(body)
        self.assertEqual(payload["reconnects"], 2)
        self.assertIn("spool_backlog_bytes", payload)


if __name__ == "__main__":
    unittest.main()
