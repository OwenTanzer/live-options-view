import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import date, datetime, timedelta, timezone
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
        self.list_page_size = None  # set to force pagination in a test

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

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matches = [
            {"Key": key, "Size": len(body), "ETag": f'"{self.etags[(bucket, key)]}"'}
            for (bucket, key), body in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        matches.sort(key=lambda item: item["Key"])
        if self.list_page_size is None:
            return {"Contents": matches, "IsTruncated": False}
        start = int(ContinuationToken) if ContinuationToken else 0
        page = matches[start:start + self.list_page_size]
        end = start + len(page)
        truncated = end < len(matches)
        result = {"Contents": page, "IsTruncated": truncated}
        if truncated:
            result["NextContinuationToken"] = str(end)
        return result


def make_fake_tradier(trade_date="2026-09-08", spot=600):
    class FakeTradier:
        def __init__(self):
            self.calls = []

        def get(self, path, **params):
            self.calls.append((path, params))
            if path == "/markets/clock":
                return {"clock": {"date": trade_date, "state": "open", "next_change": "16:00"}}
            if path == "/markets/quotes":
                return {"quotes": {"quote": {"symbol": "QQQ", "last": spot}}}
            if path == "/markets/options/expirations":
                return {"expirations": {"date": [trade_date]}}
            if path == "/markets/options/chains":
                return {"options": {"option": [
                    {"symbol": "QQQ-C600", "strike": 600, "option_type": "call"},
                    {"symbol": "QQQ-P600", "strike": 600, "option_type": "put"},
                    {"symbol": "QQQ-C601", "strike": 601, "option_type": "call"},
                    {"symbol": "QQQ-P601", "strike": 601, "option_type": "put"},
                ]}}
            raise AssertionError(path)
    return FakeTradier()


FakeTradier = make_fake_tradier


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

    def test_renew_after_takeover_raises_lease_lost_instead_of_clobbering(self):
        r2 = FakeR2()
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=1, now=lambda: past)
        collector.acquire_lease(
            r2, "bucket", "2026-09-08", "owner-b", ttl_seconds=300,
            now=lambda: datetime.now(timezone.utc),
        )
        with self.assertRaises(collector.LeaseLost):
            collector.renew_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        current, _ = collector._read_lease(r2, "bucket", "2026-09-08")
        self.assertEqual(current["owner_id"], "owner-b")

    def test_renew_missing_lease_raises_lease_lost(self):
        r2 = FakeR2()
        with self.assertRaises(collector.LeaseLost):
            collector.renew_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)


class LeaseHeartbeatTests(unittest.TestCase):
    """The review's exact reproduction: a transient renewal exception must
    never kill the heartbeat thread silently. It must either keep retrying
    (if the last confirmed ownership window hasn't elapsed) or explicitly
    set lease_lost (once it has) -- never just vanish."""

    def test_transient_failure_before_deadline_keeps_retrying_without_raising(self):
        calls = {"n": 0}

        def flaky_renew(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("simulated network timeout")

        with patch.object(collector, "renew_lease", side_effect=flaky_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
            wait_calls = {"n": 0}

            def wait(_timeout):
                wait_calls["n"] += 1
                return wait_calls["n"] > 3  # stop after 3 retry ticks

            # now() never reaches confirmed_until across all 3 ticks.
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=wait,
            )
        self.assertEqual(calls["n"], 3)
        self.assertFalse(lease_lost.is_set())

    def test_transient_failure_past_deadline_sets_lease_lost_without_raising(self):
        def flaky_renew(*_a, **_k):
            raise TimeoutError("simulated network timeout")

        with patch.object(collector, "renew_lease", side_effect=flaky_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
            # now() is already past confirmed_until on the very first tick --
            # ownership can no longer be guaranteed.
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc),
                wait=lambda _timeout: False if not lease_lost.is_set() else True,
            )
        self.assertTrue(lease_lost.is_set())

    def test_lease_lost_from_renewal_stops_immediately(self):
        def losing_renew(*_a, **_k):
            raise collector.LeaseLost("taken by another owner")

        with patch.object(collector, "renew_lease", side_effect=losing_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=lambda _timeout: False,
            )
        self.assertTrue(lease_lost.is_set())

    def test_successful_renewal_advances_confirmed_deadline(self):
        renewed = {"expires_at": "2026-09-08T13:00:00+00:00"}

        with patch.object(collector, "renew_lease", return_value=renewed):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
            wait_calls = {"n": 0}

            def wait(_timeout):
                wait_calls["n"] += 1
                return wait_calls["n"] > 1

            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=wait,
            )
        self.assertFalse(lease_lost.is_set())


class UniverseTests(unittest.TestCase):
    def test_first_call_selects_and_persists(self):
        r2 = FakeR2()
        client = FakeTradier()
        symbols, universe = collector.load_or_select_universe(
            client, r2, "bucket", "moo144/tradier/2026-09-08", 2, "2026-09-08",
            datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        self.assertIn("QQQ", symbols)
        self.assertTrue(len(client.calls) > 0)

    def test_restart_reloads_persisted_universe_without_reselecting(self):
        r2 = FakeR2()
        client = FakeTradier()
        first_symbols, _ = collector.load_or_select_universe(
            client, r2, "bucket", "moo144/tradier/2026-09-08", 2, "2026-09-08",
            datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        calls_after_first = len(client.calls)
        second_symbols, _ = collector.load_or_select_universe(
            client, r2, "bucket", "moo144/tradier/2026-09-08", 2, "2026-09-08",
            datetime(2026, 9, 8, 15, 0, tzinfo=ET),  # later, spot could have moved
        )
        self.assertEqual(first_symbols, second_symbols)
        self.assertEqual(len(client.calls), calls_after_first)  # no re-selection


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
        self.assertTrue(uploader.fully_drained())

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


class SegmentSpoolExhaustionTests(unittest.TestCase):
    def test_write_raises_and_flags_overload_once_cap_exceeded(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            uploader = collector.Uploader(r2, "bucket", "prefix", max_spool_bytes=50)
            spool = collector.SegmentSpool(Path(tmp), "owner-a", uploader, checkpoint_seconds=9999)
            big_event = {"type": "timesale", "symbol": "OPT", "payload": "x" * 200}
            try:
                with self.assertRaises(collector.SpoolExhausted):
                    for _ in range(20):
                        spool.write(dict(big_event))
                self.assertTrue(uploader.overloaded)
            finally:
                spool.handle.close()

    def test_writes_under_cap_succeed_without_exception(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            uploader = collector.Uploader(r2, "bucket", "prefix", max_spool_bytes=1_000_000)
            spool = collector.SegmentSpool(Path(tmp), "owner-a", uploader, checkpoint_seconds=9999)
            try:
                spool.write({"type": "quote", "symbol": "QQQ"})
                self.assertFalse(uploader.overloaded)
            finally:
                spool.handle.close()


class ReconcileTests(unittest.TestCase):
    def test_verified_upload_removes_local_copy(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            leftover = spool_dir / "owner-a-part-0000.ndjson.gz"
            leftover.write_bytes(b"data")
            r2.upload_file(str(leftover), "bucket", "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz")
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertFalse(leftover.exists())
            self.assertEqual(result["needs_review"], [])

    def test_orphaned_readable_local_segment_is_queued_for_resume(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            orphan = spool_dir / "owner-a-part-0001.ndjson.gz"
            import gzip
            with gzip.open(orphan, "wt") as handle:
                handle.write('{"type":"quote"}\n')
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(result["resume"], [orphan])
            self.assertEqual(result["needs_review"], [])
            self.assertTrue(orphan.exists())  # not deleted -- queued for upload instead

    def test_corrupt_local_segment_flagged_not_resumed_or_deleted(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            corrupt = spool_dir / "owner-a-part-0002.ndjson.gz"
            corrupt.write_bytes(b"not actually gzip data")
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(result["resume"], [])
            self.assertEqual(len(result["needs_review"]), 1)
            self.assertTrue(corrupt.exists())

    def test_same_name_different_content_is_flagged_not_silently_trusted(self):
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            local = spool_dir / "owner-a-part-0000.ndjson.gz"
            local.write_bytes(b"local-bytes")
            r2.objects[("bucket", "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz")] = b"different-remote-bytes"
            r2.etags[("bucket", "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz")] = hashlib.md5(b"different-remote-bytes").hexdigest()
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(len(result["needs_review"]), 1)
            self.assertTrue(local.exists())

    def test_pagination_across_multiple_listing_pages(self):
        r2 = FakeR2()
        for i in range(5):
            r2.objects[("bucket", f"moo144/tradier/2026-09-08/owner-a-part-{i:04d}.ndjson.gz")] = f"data{i}".encode()
            r2.etags[("bucket", f"moo144/tradier/2026-09-08/owner-a-part-{i:04d}.ndjson.gz")] = hashlib.md5(f"data{i}".encode()).hexdigest()
        r2.list_page_size = 2
        with tempfile.TemporaryDirectory() as tmp:
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", Path(tmp))
            self.assertEqual(len(result["artifacts"]), 5)

    def test_multipart_etag_with_matching_content_is_verified_by_download_and_deleted(self):
        """A multipart/missing ETag can't be trusted from the listing alone
        -- the review's exact finding. Matching content must still be
        established by actually fetching and hashing the remote bytes."""
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            local = spool_dir / "owner-a-part-0000.ndjson.gz"
            local.write_bytes(b"identical-bytes")
            key = "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz"
            r2.objects[("bucket", key)] = b"identical-bytes"
            # A multipart-style ETag contains a dash and can't be compared
            # directly to a single-part MD5.
            r2.etags[("bucket", key)] = "abcdef0123456789-3"
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertEqual(result["needs_review"], [])
            self.assertFalse(local.exists())

    def test_multipart_etag_with_mismatched_content_is_retained_not_deleted(self):
        """Unknown/unverifiable identity must never authorize deletion --
        the review's exact finding. A multipart ETag whose actual remote
        content differs must be flagged and the local evidence kept."""
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            local = spool_dir / "owner-a-part-0000.ndjson.gz"
            local.write_bytes(b"local-bytes")
            key = "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz"
            r2.objects[("bucket", key)] = b"different-remote-bytes"
            r2.etags[("bucket", key)] = "abcdef0123456789-3"
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(len(result["needs_review"]), 1)
            self.assertTrue(local.exists())

    def test_unfetchable_remote_object_is_retained_not_deleted(self):
        """If the remote object can't even be fetched to verify, that is
        definitionally unverifiable identity -- retain, don't delete."""
        r2 = FakeR2()
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = Path(tmp)
            local = spool_dir / "owner-a-part-0000.ndjson.gz"
            local.write_bytes(b"local-bytes")
            key = "moo144/tradier/2026-09-08/owner-a-part-0000.ndjson.gz"
            r2.objects[("bucket", key)] = b"local-bytes"
            r2.etags[("bucket", key)] = "abcdef0123456789-3"
            r2.get_object = Mock(side_effect=RuntimeError("network down"))
            result = collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", spool_dir)
            self.assertEqual(len(result["needs_review"]), 1)
            self.assertTrue(local.exists())

    def test_listing_failure_propagates_instead_of_certifying_empty_archive(self):
        r2 = FakeR2()
        r2.list_objects_v2 = Mock(side_effect=RuntimeError("network down"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                collector.reconcile_existing_segments(r2, "bucket", "moo144/tradier/2026-09-08", Path(tmp))


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

        result = collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            monotonic=lambda: 0.0,
            sleeper=lambda _s: None,
            now_et=now_et,
        )
        self.assertIsNone(result.stop_reason)

    def test_lease_lost_stops_capture_with_explicit_reason(self):
        client = FakeStreamClient()

        class MemorySpool:
            def write(self, event):
                pass

        lease_lost = threading.Event()
        lease_lost.set()
        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)
        result = collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5, lease_lost=lease_lost,
            now_et=lambda: datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        self.assertEqual(result.stop_reason, "lease_lost")

    def test_spool_exhausted_stops_capture_without_raising(self):
        client = FakeStreamClient()
        client.session.get.side_effect = lambda *_a, **_k: FakeResponseWithLines(['{"type": "quote", "symbol": "QQQ"}'])

        class ExhaustingSpool:
            def write(self, event):
                raise collector.SpoolExhausted("cap exceeded")

        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)
        result = collector.capture_session(
            client, ["QQQ"], ExhaustingSpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            now_et=lambda: datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        self.assertEqual(result.stop_reason, "spool_exhausted")

    def test_gap_brackets_actual_outage_with_disconnect_then_resumed(self):
        """The gap must close only once an event is actually received again
        -- not merely after the backoff sleep for a connection attempt that
        hasn't even happened yet."""
        client = FakeStreamClient()
        client.session.get.side_effect = [
            requests.ConnectionError("boom"),
            FakeResponseWithLines(['{"type": "quote", "symbol": "QQQ"}']),
        ]
        events = []

        class MemorySpool:
            def write(self, event):
                events.append(event)

        close_holder = {"n": 0}
        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)

        def now_et():
            close_holder["n"] += 1
            # 3 "still open" reads: the failed attempt's while-check, the
            # successful attempt's while-check, and the in-loop stop-check
            # right before that attempt's one line is processed -- then the
            # post-line stop-check can safely see the session has closed.
            return datetime(2026, 9, 8, 15, 0, tzinfo=ET) if close_holder["n"] <= 3 else close

        result = collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            monotonic=lambda: close_holder["n"] * 1.0,
            sleeper=lambda _s: None,
            now_et=now_et,
        )
        gap_events = [e for e in events if e["type"] == "gap"]
        disconnects = [e for e in gap_events if e["reason"] == "stream_disconnect"]
        resumes = [e for e in gap_events if e["reason"] == "stream_reconnect_resumed"]
        self.assertEqual(len(disconnects), 1)
        self.assertEqual(len(resumes), 1)
        self.assertGreater(result.gap_seconds, 0.0)

    def test_gap_stays_open_across_repeated_failed_reconnects(self):
        """Multiple consecutive failed reconnect attempts within the same
        outage must emit exactly one stream_disconnect (not one per retry)
        and no stream_reconnect_resumed until data actually flows again."""
        client = FakeStreamClient()
        client.session.get.side_effect = [
            requests.ConnectionError("boom-1"),
            requests.ConnectionError("boom-2"),
            requests.ConnectionError("boom-3"),
        ]
        events = []

        class MemorySpool:
            def write(self, event):
                events.append(event)

        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)
        now_calls = {"n": 0}

        def now_et():
            # Exactly 3 "still open" reads (one per queued connection
            # attempt), then closed -- so the loop stops cleanly right
            # after the 3rd failure instead of attempting a 4th connection
            # the fake has no side_effect queued for.
            now_calls["n"] += 1
            return datetime(2026, 9, 8, 10, 0, tzinfo=ET) if now_calls["n"] <= 3 else close

        result = collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            monotonic=lambda: 0.0,
            sleeper=lambda _s: None,
            now_et=now_et,
        )
        gap_events = [e for e in events if e["type"] == "gap"]
        disconnects = [e for e in gap_events if e["reason"] == "stream_disconnect"]
        resumes = [e for e in gap_events if e["reason"] == "stream_reconnect_resumed"]
        self.assertEqual(len(disconnects), 1)
        self.assertEqual(len(resumes), 0)
        self.assertEqual(result.reconnects, 3)

    def test_exceeding_reconnect_budget_raises_with_counters_attached(self):
        client = FakeStreamClient()

        class MemorySpool:
            def write(self, event):
                pass

        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)
        with self.assertRaises(RuntimeError) as ctx:
            collector.capture_session(
                client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
                max_consecutive_reconnects=1,
                monotonic=lambda: 0.0,
                sleeper=lambda _s: None,
                now_et=lambda: datetime(2026, 9, 8, 10, 0, tzinfo=ET),
            )
        self.assertEqual(ctx.exception.reconnects, 2)


class FakeResponseWithLines:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        return iter(self.lines)


class WriteHealthTests(unittest.TestCase):
    def test_health_round_trip(self):
        r2 = FakeR2()
        stats = collector.BoundedStats()
        uploader = collector.Uploader(r2, "bucket", "prefix", max_spool_bytes=1000)
        collector.write_health(r2, "bucket", "moo144/tradier/2026-09-08", stats, uploader, reconnects=2, gap_seconds=12.5)
        body = r2.objects[("bucket", "moo144/tradier/2026-09-08/health.json")]
        payload = json.loads(body)
        self.assertEqual(payload["reconnects"], 2)
        self.assertEqual(payload["gap_seconds"], 12.5)
        self.assertIn("spool_backlog_bytes", payload)


class MainLifecycleTests(unittest.TestCase):
    """Integration-style tests that exercise main() end-to-end against
    simulated clock/provider/storage, per the review's request that these
    be real acceptance gates rather than isolated unit checks."""

    def setUp(self):
        collector.STOP = False
        self.env = {
            "TRADIER_TOKEN": "token",
            "MOO144_STRIKE_COUNT": "2",
            "MOO144_CHECKPOINT_SECONDS": "30",
            "MOO144_MAX_CONSECUTIVE_RECONNECTS": "5",
            "MOO144_LEASE_TTL_SECONDS": "300",
        }

    def _run_main(
        self, tmp_spool, capture_fn, session_open, session_close, clock_sequence,
        r2=None, drain_timeout_seconds=5.0, expect_failure=False,
    ):
        r2 = r2 if r2 is not None else FakeR2()
        client = FakeTradier()
        env = dict(self.env, MOO144_SPOOL_DIR=str(tmp_spool))
        clock_iter = iter(clock_sequence)
        last = {"t": clock_sequence[0]}

        def clock_et():
            try:
                last["t"] = next(clock_iter)
            except StopIteration:
                pass
            return last["t"]

        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(collector, "r2_client", return_value=(r2, "bucket")),
            patch.object(collector, "Tradier", return_value=client),
            patch.object(collector, "capture_session", side_effect=capture_fn),
        ):
            def call():
                return collector.main(
                    clock_et=clock_et,
                    sleeper=lambda _s: None,
                    session_bounds=lambda _day: (session_open, session_close),
                    uploader_sleeper=lambda _s: None,
                    drain_timeout_seconds=drain_timeout_seconds,
                )
            if expect_failure:
                with self.assertRaises(RuntimeError):
                    call()
                return None, r2
            result = call()
        return result, r2

    def test_holiday_is_a_clean_noop(self):
        r2 = FakeR2()
        with (
            patch.dict(os.environ, dict(self.env, TRADIER_TOKEN="token"), clear=False),
            patch.object(collector, "r2_client", return_value=(r2, "bucket")),
        ):
            result = collector.main(
                clock_et=lambda: datetime(2026, 9, 8, 8, 0, tzinfo=ET),
                sleeper=lambda _s: None,
                session_bounds=lambda _day: None,
            )
        self.assertEqual(result, 0)
        self.assertEqual(r2.objects, {})

    def test_full_session_no_gaps_reports_complete(self):
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
            )
        self.assertEqual(result, 0)
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "complete")
        self.assertEqual(summaries[0]["partial_reasons"], [])

    def test_late_start_reports_partial(self):
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)
        late_now = datetime(2026, 9, 8, 9, 32, tzinfo=ET)  # 2 minutes late

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            # An event this attempt, so this test isolates the late-start
            # finding from the separate no_events_captured one.
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[late_now] * 6 + [session_close],
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertTrue(any("late_start" in reason for reason in summaries[0]["partial_reasons"]))

    def test_setup_delay_after_open_wait_counts_toward_lateness(self):
        """A late_start_seconds of 0 (the open-wait itself was on time) must
        not certify an on-time session if everything AFTER that wait --
        lease acquisition, universe selection, reconciliation -- took long
        enough that capture didn't actually start until well after open.
        The review's exact finding: late_start_seconds is measured too
        early to catch this."""
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)
        slow_setup_done = datetime(2026, 9, 8, 9, 40, tzinfo=ET)  # 10 min of setup

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                # calls: today, now_et(open-check), late_start_seconds,
                # close-check, universe-selection, capture_started_at, final
                # close-check -- only the capture_started_at read reflects
                # the slow setup.
                clock_sequence=[
                    session_open, session_open, session_open, session_open,
                    session_open, slow_setup_done, session_close,
                ],
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertEqual(summaries[0]["late_start_seconds"], 0.0)
        self.assertEqual(summaries[0]["effective_late_start_seconds"], 600.0)
        self.assertTrue(any("late_start_seconds=600.0" in reason for reason in summaries[0]["partial_reasons"]))

    def test_zero_events_over_a_full_session_reports_partial_and_requires_restart(self):
        """Reaching wall-clock close alone must not certify a complete
        capture -- a connection that established but received nothing all
        day is exactly the case the review's completeness finding named."""
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, _spool, _stats, _close, *_a, **_k):
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
                expect_failure=True,
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertIn("no_events_captured", summaries[0]["partial_reasons"])

    def test_spool_exhausted_requires_restart(self):
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            result = collector.CaptureResult()
            result.stop_reason = "spool_exhausted"
            return result

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
                expect_failure=True,
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertIn("spool_exhausted", summaries[0]["partial_reasons"])

    def test_lease_lost_does_not_trigger_a_competing_restart(self):
        """Unlike spool_exhausted, losing the lease means another owner is
        legitimately active for this run_date -- restarting would only
        fight that owner instead of recovering anything, so this must exit
        cleanly rather than raise."""
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            result = collector.CaptureResult()
            result.stop_reason = "lease_lost"
            return result

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
            )
        self.assertEqual(result, 0)
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertIn("lease_lost", summaries[0]["partial_reasons"])

    def test_reconnects_report_partial_even_when_recovered(self):
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            result = collector.CaptureResult()
            result.reconnects = 3
            return result

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertTrue(any("reconnects=3" in reason for reason in summaries[0]["partial_reasons"]))

    def test_pending_unuploaded_segment_reports_partial(self):
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)
        r2 = FakeR2()
        r2.upload_file = Mock(side_effect=RuntimeError("R2 is unreachable"))

        def fake_capture(_client, _symbols, spool, _stats, _close, *_a, **_k):
            # Simulate a segment that was rotated (enqueued) but the uploader
            # can never actually drain it (storage outage) before the run ends.
            phantom = Path(spool.spool_dir) / "phantom.ndjson.gz"
            phantom.write_bytes(b"x" * 10)
            spool.uploader.enqueue(phantom)
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
                r2=r2, drain_timeout_seconds=0.5, expect_failure=True,
            )
        summaries = [json.loads(v) for (_b, k), v in r2.objects.items() if "/summary-" in k]
        self.assertEqual(summaries[0]["status"], "partial")
        self.assertTrue(any("spool_not_fully_drained" in reason for reason in summaries[0]["partial_reasons"]))

    def test_restart_resumes_orphaned_local_segment_same_day(self):
        """A prior owner's crash can leave a finalized-but-unuploaded segment
        on the persistent spool volume for TODAY's date. The next run (any
        owner_id, since a crash always starts a fresh process) must resume
        and upload it under today's own prefix, not strand it.
        """
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            import gzip
            # main() spools under <MOO144_SPOOL_DIR>/moo144-collector-spool/<run_date>.
            todays_spool_dir = Path(tmp) / "moo144-collector-spool" / "2026-09-08"
            todays_spool_dir.mkdir(parents=True)
            orphan = todays_spool_dir / "previous-owner-part-0000.ndjson.gz"
            with gzip.open(orphan, "wt") as handle:
                handle.write('{"type":"quote"}\n')
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
            )
        self.assertEqual(result, 0)
        uploaded_names = {Path(k).name for (_b, k) in r2.objects if k.endswith(".ndjson.gz")}
        self.assertIn("previous-owner-part-0000.ndjson.gz", uploaded_names)
        self.assertFalse(orphan.exists())
        uploaded_keys = {k for (_b, k) in r2.objects if k.endswith(".ndjson.gz")}
        self.assertTrue(any(k.startswith("moo144/tradier/2026-09-08/") for k in uploaded_keys))

    def test_restart_recovers_stale_prior_date_under_its_own_prefix(self):
        """A crash on 2026-09-07 that never uploaded must be recovered under
        moo144/tradier/2026-09-07/ when today's run is 2026-09-08 -- never
        under today's prefix. This is the review's exact reproduction:
        session identity was previously lost across a date rollover.
        """
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)

        def fake_capture(_client, _symbols, spool, stats, _close, *_a, **_k):
            spool.write(stats.observe({
                "type": "timesale", "symbol": "OPT", "date": "1000", "seq": 1,
                "flag": "", "cancel": False, "correction": False, "session": "normal",
                "collector_receipt_timestamp": collector.utc_now(),
            }))
            return collector.CaptureResult()

        with tempfile.TemporaryDirectory() as tmp:
            import gzip
            stale_spool_dir = Path(tmp) / "moo144-collector-spool" / "2026-09-07"
            stale_spool_dir.mkdir(parents=True)
            stale_orphan = stale_spool_dir / "prior-owner-part-0000.ndjson.gz"
            with gzip.open(stale_orphan, "wt") as handle:
                handle.write('{"type":"quote","note":"september 7 receipt"}\n')
            result, r2 = self._run_main(
                Path(tmp), fake_capture, session_open, session_close,
                clock_sequence=[session_open] * 6 + [session_close],
            )
        self.assertEqual(result, 0)
        self.assertFalse(stale_orphan.exists())
        stale_keys = [k for (_b, k) in r2.objects if k.endswith("prior-owner-part-0000.ndjson.gz")]
        self.assertEqual(len(stale_keys), 1)
        self.assertTrue(stale_keys[0].startswith("moo144/tradier/2026-09-07/"))
        self.assertFalse(stale_keys[0].startswith("moo144/tradier/2026-09-08/"))
        # Today's own summary must not double-count or omit the recovered
        # stale segment -- it's neither this attempt's own coverage nor
        # already-remote-before-this-run for TODAY's prefix.
        today_summaries = [
            json.loads(v) for (_b, k), v in r2.objects.items()
            if k.startswith("moo144/tradier/2026-09-08/summary-")
        ]
        self.assertEqual(len(today_summaries), 1)
        recovered = today_summaries[0]["stale_sessions_recovered"]
        self.assertIn("2026-09-07", recovered)
        self.assertEqual(len(recovered["2026-09-07"]["resumed_artifacts"]), 1)


if __name__ == "__main__":
    unittest.main()
