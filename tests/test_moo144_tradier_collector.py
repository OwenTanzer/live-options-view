import gzip
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

    def iter_lines(self, decode_unicode=True, chunk_size=None):
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

    def test_transient_storage_error_is_not_treated_as_confirmed_absence(self):
        """The review's exact reproduction: a 500/InternalError reading the
        lease must never be silently folded into 'no lease exists' -- that
        conflation is what let a storage hiccup masquerade as a confirmed
        takeover."""
        r2 = FakeR2()
        collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        r2.head_object = Mock(side_effect=ClientError(status=500, code="InternalError"))
        with self.assertRaises(collector.LeaseUnavailable):
            collector._read_lease(r2, "bucket", "2026-09-08")

    def test_confirmed_404_is_still_treated_as_absent(self):
        r2 = FakeR2()
        r2.head_object = Mock(side_effect=ClientError(status=404, code="NoSuchKey"))
        current, etag = collector._read_lease(r2, "bucket", "2026-09-08")
        self.assertIsNone(current)
        self.assertIsNone(etag)

    def test_renew_propagates_transient_failure_instead_of_declaring_loss(self):
        """A transient storage error during renewal must not be reported as
        LeaseLost -- it proves nothing about ownership either way."""
        r2 = FakeR2()
        collector.acquire_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)
        r2.head_object = Mock(side_effect=ClientError(status=503, code="ServiceUnavailable"))
        with self.assertRaises(collector.LeaseUnavailable):
            collector.renew_lease(r2, "bucket", "2026-09-08", "owner-a", ttl_seconds=300)


class LeaseHeartbeatTests(unittest.TestCase):
    """The heartbeat no longer enforces the confirmed deadline itself (that
    moved to run_lease_deadline_watchdog, so a slow/blocked renewal request
    can't postpone stopping intake past the deadline -- see
    LeaseDeadlineWatchdogTests). Its job here is just: retry transient
    failures forever without dying silently, and classify a genuine
    LeaseLost into the right kind so main() can decide whether restarting
    is safe."""

    def test_transient_failure_keeps_retrying_indefinitely_without_setting_lease_lost(self):
        calls = {"n": 0}

        def flaky_renew(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("simulated network timeout")

        with patch.object(collector, "renew_lease", side_effect=flaky_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            wait_calls = {"n": 0}

            def wait(_timeout):
                wait_calls["n"] += 1
                return wait_calls["n"] > 3  # stop after 3 retry ticks

            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc),  # already "past deadline"
                wait=wait,
            )
        # Even with now() past confirmed_until, the heartbeat itself must
        # never set lease_lost for a transient failure -- that's the
        # watchdog's exclusive responsibility now.
        self.assertEqual(calls["n"], 3)
        self.assertFalse(lease_lost.is_set())

    def test_lease_unavailable_is_treated_as_transient_and_retried(self):
        """LeaseUnavailable (raised for a transient storage error) must
        flow through the same retry-forever path as any other Exception --
        never mistaken for a confirmed LeaseLost subclass."""
        calls = {"n": 0}

        def flaky_renew(*_a, **_k):
            calls["n"] += 1
            raise collector.LeaseUnavailable("simulated 500 reading lease")

        with patch.object(collector, "renew_lease", side_effect=flaky_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            wait_calls = {"n": 0}

            def wait(_timeout):
                wait_calls["n"] += 1
                return wait_calls["n"] > 3

            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc),
                wait=wait,
            )
        self.assertEqual(calls["n"], 3)
        self.assertFalse(lease_lost.is_set())

    def test_confirmed_takeover_stops_immediately_with_reason(self):
        def losing_renew(*_a, **_k):
            raise collector.LeaseTakenByAnotherOwner("taken by another owner")

        with patch.object(collector, "renew_lease", side_effect=losing_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            loss_reason: dict[str, str] = {}
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=lambda _timeout: False,
                loss_reason=loss_reason,
            )
        self.assertTrue(lease_lost.is_set())
        self.assertEqual(loss_reason["reason"], "confirmed_takeover")

    def test_confirmed_absence_stops_immediately_with_distinct_reason(self):
        """The review's exact finding: a missing lease is NOT the same as a
        verified competing owner, and must recover differently (restart is
        appropriate; a real takeover must not restart)."""
        def missing_renew(*_a, **_k):
            raise collector.LeaseMissing("lease is confirmed absent")

        with patch.object(collector, "renew_lease", side_effect=missing_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            loss_reason: dict[str, str] = {}
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=lambda _timeout: False,
                loss_reason=loss_reason,
            )
        self.assertTrue(lease_lost.is_set())
        self.assertEqual(loss_reason["reason"], "confirmed_absence")

    def test_bare_lease_lost_fallback_requires_recovery(self):
        """Defensive coverage: an unforeseen bare LeaseLost (neither
        specific subclass) must still stop ingestion, and conservatively
        use the recoverable classification because takeover is unverified."""
        def bare_lost_renew(*_a, **_k):
            raise collector.LeaseLost("unclassified loss")

        with patch.object(collector, "renew_lease", side_effect=bare_lost_renew):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            loss_reason: dict[str, str] = {}
            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=lambda _timeout: False,
                loss_reason=loss_reason,
            )
        self.assertTrue(lease_lost.is_set())
        self.assertEqual(loss_reason["reason"], "ownership_uncertain")

    def test_successful_renewal_updates_shared_confirmed_until_ref(self):
        renewed = {"expires_at": "2026-09-08T13:00:00+00:00"}

        with patch.object(collector, "renew_lease", return_value=renewed):
            stop = threading.Event()
            lease_lost = threading.Event()
            confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
            wait_calls = {"n": 0}

            def wait(_timeout):
                wait_calls["n"] += 1
                return wait_calls["n"] > 1

            collector.run_lease_heartbeat(
                None, "bucket", "2026-09-08", "owner-a", 300, confirmed_until_ref,
                stop, lease_lost,
                now=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc),
                wait=wait,
            )
        self.assertFalse(lease_lost.is_set())
        self.assertEqual(
            confirmed_until_ref["value"],
            datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc),
        )


class LeaseDeadlineWatchdogTests(unittest.TestCase):
    """The review's exact finding: deadline enforcement must run
    independently of whatever the heartbeat's renewal request is doing --
    a slow/blocked request must never be able to postpone stopping intake
    past the confirmed deadline. This watchdog polls on its own fixed
    cadence and never blocks on a network call."""

    def test_fires_once_deadline_passes(self):
        confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
        stop = threading.Event()
        lease_lost = threading.Event()
        loss_reason: dict[str, str] = {}
        collector.run_lease_deadline_watchdog(
            confirmed_until_ref, stop, lease_lost,
            now=lambda: datetime(2026, 9, 8, 12, 0, 1, tzinfo=timezone.utc),
            wait=lambda _timeout: False if not lease_lost.is_set() else True,
            loss_reason=loss_reason,
        )
        self.assertTrue(lease_lost.is_set())
        self.assertEqual(loss_reason["reason"], "ownership_uncertain")

    def test_does_not_fire_before_deadline(self):
        confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
        stop = threading.Event()
        lease_lost = threading.Event()
        wait_calls = {"n": 0}

        def wait(_timeout):
            wait_calls["n"] += 1
            return wait_calls["n"] > 3  # stop the loop after 3 polls

        collector.run_lease_deadline_watchdog(
            confirmed_until_ref, stop, lease_lost,
            now=lambda: datetime(2026, 9, 8, 11, 59, tzinfo=timezone.utc),
            wait=wait,
        )
        self.assertFalse(lease_lost.is_set())

    def test_extended_deadline_observed_before_firing(self):
        """A renewal that succeeds and pushes confirmed_until_ref out must
        be picked up on the watchdog's very next poll -- it must not fire
        using a stale deadline it read once at startup."""
        confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
        stop = threading.Event()
        lease_lost = threading.Event()
        poll_count = {"n": 0}

        def wait(_timeout):
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                # Simulate the heartbeat renewing successfully between the
                # 1st and 2nd poll, extending the deadline well past "now".
                confirmed_until_ref["value"] = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
            return poll_count["n"] > 3

        collector.run_lease_deadline_watchdog(
            confirmed_until_ref, stop, lease_lost,
            now=lambda: datetime(2026, 9, 8, 12, 0, 30, tzinfo=timezone.utc),  # past the ORIGINAL deadline
            wait=wait,
        )
        self.assertFalse(lease_lost.is_set())

    def test_does_not_overwrite_a_more_specific_reason_already_set(self):
        """If the heartbeat has already classified the loss more
        specifically (e.g. a confirmed takeover discovered concurrently),
        the watchdog's generic fail-safe label must not clobber it."""
        confirmed_until_ref = {"value": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)}
        stop = threading.Event()
        lease_lost = threading.Event()
        loss_reason = {"reason": "confirmed_takeover"}
        collector.run_lease_deadline_watchdog(
            confirmed_until_ref, stop, lease_lost,
            now=lambda: datetime(2026, 9, 8, 12, 0, 1, tzinfo=timezone.utc),
            wait=lambda _timeout: False if not lease_lost.is_set() else True,
            loss_reason=loss_reason,
        )
        self.assertTrue(lease_lost.is_set())
        self.assertEqual(loss_reason["reason"], "confirmed_takeover")

    def test_concurrent_blocked_renewal_does_not_delay_the_watchdog(self):
        """Owen's exact reproduction, run for real: a renewal request that
        blocks past the confirmed deadline must not be able to postpone
        stopping intake. Runs the heartbeat and watchdog as real concurrent
        threads with real wall-clock time (compressed to fractions of a
        second) -- not fake clocks -- so this actually exercises two
        threads racing, not just each function's logic in isolation.
        """
        real_now = lambda: datetime.now(timezone.utc)  # noqa: E731
        confirmed_until_ref = {"value": real_now() + timedelta(seconds=0.1)}
        stop = threading.Event()
        lease_lost = threading.Event()
        loss_reason: dict[str, str] = {}

        def blocked_renew(*_a, **_k):
            # Simulates a renewal request that hangs well past the
            # deadline (e.g. the review's 80-second stall) before failing.
            time.sleep(0.4)
            raise TimeoutError("simulated slow/hung renewal request")

        with patch.object(collector, "renew_lease", side_effect=blocked_renew):
            heartbeat_thread = threading.Thread(
                target=collector.run_lease_heartbeat,
                # ttl_seconds tiny so the first renewal attempt starts almost
                # immediately (wait cadence = ttl/3) -- it must actually be
                # mid-blocked-call when the watchdog's deadline hits.
                args=(None, "bucket", "2026-09-08", "owner-a", 0.03, confirmed_until_ref, stop, lease_lost),
                kwargs={"now": real_now, "loss_reason": loss_reason},
                daemon=True,
            )
            watchdog_thread = threading.Thread(
                target=collector.run_lease_deadline_watchdog,
                args=(confirmed_until_ref, stop, lease_lost),
                kwargs={"now": real_now, "poll_seconds": 0.02, "loss_reason": loss_reason},
                daemon=True,
            )
            heartbeat_thread.start()
            watchdog_thread.start()
            try:
                # The blocked renewal doesn't return for 0.4s; the deadline
                # is 0.1s out. If deadline enforcement depended on the
                # heartbeat (the pre-fix bug), lease_lost would stay unset
                # this entire time. Asserting well before 0.4s proves the
                # watchdog acted independently.
                fired = lease_lost.wait(timeout=0.3)
            finally:
                stop.set()
                heartbeat_thread.join(timeout=1.0)
                watchdog_thread.join(timeout=1.0)

        self.assertTrue(fired, "watchdog must fire without waiting for the blocked renewal")
        self.assertEqual(loss_reason["reason"], "ownership_uncertain")


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

    def test_gap_excludes_the_preceding_healthy_connection_duration(self):
        """Owen's exact reproduction: a healthy quote long before the
        disconnect, then disconnect, then a quote 1 second later, must
        report ~1 second of outage -- not the entire healthy connection's
        lifetime up to that point."""
        clock = {"t": 0.0}

        def timed_lines(pairs):
            for line, t in pairs:
                clock["t"] = t
                yield line

        client = FakeStreamClient()
        client.session.get.side_effect = [
            FakeResponseWithLines(timed_lines([
                ('{"type": "quote", "symbol": "QQQ"}', 10.0),
            ])),
            FakeResponseWithLines(timed_lines([
                ('{"type": "quote", "symbol": "QQQ"}', 11.0),
            ])),
        ]
        events = []

        class MemorySpool:
            def write(self, event):
                events.append(event)

        close = datetime(2026, 9, 8, 16, 0, 0, tzinfo=ET)
        now_calls = {"n": 0}

        def now_et():
            now_calls["n"] += 1
            return datetime(2026, 9, 8, 10, 0, tzinfo=ET) if now_calls["n"] <= 5 else close

        result = collector.capture_session(
            client, ["QQQ"], MemorySpool(), collector.BoundedStats(), close,
            max_consecutive_reconnects=5,
            monotonic=lambda: clock["t"],
            sleeper=lambda _s: None,
            now_et=now_et,
        )
        resumes = [e for e in events if e.get("type") == "gap" and e["reason"] == "stream_reconnect_resumed"]
        self.assertEqual(len(resumes), 1)
        self.assertAlmostEqual(resumes[0]["outage_seconds"], 1.0)
        self.assertAlmostEqual(result.gap_seconds, 1.0)

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

    def iter_lines(self, decode_unicode=True, chunk_size=None):
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
                    uploader_sleeper=time.sleep,
                    drain_timeout_seconds=drain_timeout_seconds,
                )
            if expect_failure:
                with self.assertRaises(RuntimeError):
                    call()
                return None, r2
            result = call()
        return result, r2

    def test_renewal_conflict_recovery_through_main(self):
        # Exercise actual storage classification, heartbeat, and main exit
        # semantics together, including deletion AFTER a successful read.
        cases = [
            ("missing_at_read", "confirmed_absence", True),
            ("deleted_during_write", "confirmed_absence", True),
            ("live_competitor", "confirmed_takeover", False),
            ("expired_competitor", "ownership_uncertain", True),
            ("unreadable_after_conflict", "ownership_uncertain", True),
            ("same_owner_after_conflict", "ownership_uncertain", True),
            ("invalid_after_conflict", "ownership_uncertain", True),
        ]
        real_heartbeat = collector.run_lease_heartbeat
        session_open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 9, 8, 16, 0, tzinfo=ET)
        for scenario, expected_reason, recoverable in cases:
            with self.subTest(scenario=scenario):
                r2 = FakeR2()
                ready = threading.Event()
                errors = []

                def heartbeat(storage, bucket, day, owner, ttl, ref, stop, lost, **kwargs):
                    try:
                        key = collector.lease_key(day)
                        original_put = storage.put_object
                        if scenario == "missing_at_read":
                            storage.objects.pop((bucket, key))
                        else:
                            def conflict(**args):
                                if args.get("IfMatch") is not None:
                                    if scenario == "deleted_during_write":
                                        storage.objects.pop((bucket, key))
                                        storage.etags.pop((bucket, key))
                                    elif scenario == "unreadable_after_conflict":
                                        original_head = storage.head_object
                                        def unavailable_lease(**head_args):
                                            if head_args["Key"] == key:
                                                raise ClientError(500, "InternalError")
                                            return original_head(**head_args)
                                        storage.head_object = unavailable_lease
                                    elif scenario != "same_owner_after_conflict":
                                        lease = json.loads(storage.objects[(bucket, key)])
                                        lease["owner_id"] = "other-owner"
                                        lease["expires_at"] = (
                                            "invalid" if scenario == "invalid_after_conflict" else
                                            (datetime.now(timezone.utc) + timedelta(
                                                seconds=-70 if scenario == "expired_competitor" else 300
                                            )).isoformat()
                                        )
                                        original_put(Bucket=bucket, Key=key, Body=json.dumps(lease).encode())
                                    raise ClientError(412, "PreconditionFailed")
                                return original_put(**args)
                            storage.put_object = conflict
                        real_heartbeat(storage, bucket, day, owner, ttl, ref, stop, lost,
                                       wait=lambda _: False, **kwargs)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        ready.set()

                def capture(_client, _symbols, _spool, _stats, _close, *_a, **kwargs):
                    self.assertTrue(ready.wait(2), "heartbeat did not finish")
                    if errors:
                        raise errors[0]
                    self.assertTrue(kwargs["lease_lost"].is_set())
                    result = collector.CaptureResult()
                    result.stop_reason = "lease_lost"
                    return result

                with tempfile.TemporaryDirectory() as tmp, patch.object(
                    collector, "run_lease_heartbeat", side_effect=heartbeat
                ):
                    result, storage = self._run_main(
                        Path(tmp), capture, session_open, session_close,
                        [session_open] * 8, r2=r2, expect_failure=recoverable,
                    )
                self.assertEqual(errors, [])
                summary = next(json.loads(v) for (_b, k), v in storage.objects.items()
                               if "/summary-" in k)
                self.assertIn("lease_lost:" + expected_reason, summary["partial_reasons"])
                self.assertEqual(summary["status"], "partial")
                if not recoverable:
                    self.assertEqual(result, 0)

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
            result = collector.CaptureResult()
            result.opening_stream_ready_at = session_open - timedelta(seconds=30)
            return result

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


class PreopenReadinessTests(unittest.TestCase):
    def setUp(self):
        collector.STOP = False
        self.open = datetime(2026, 9, 8, 9, 30, tzinfo=ET)

    def capture(self, connections, close_hour=16, lease_lost=None):
        current = [self.open - timedelta(seconds=60)]
        close = self.open.replace(hour=close_hour, minute=0)
        events = []
        spool = Mock()
        spool.write.side_effect = events.append
        plans = iter(connections)
        client = FakeStreamClient()

        def response(*_a, **_k):
            plan = next(plans)
            class Response(FakeResponse):
                def iter_lines(inner, decode_unicode=True, chunk_size=None):
                    self.assertEqual(chunk_size, 1)
                    for offset, value in plan:
                        current[0] = self.open + timedelta(seconds=offset)
                        if isinstance(value, Exception):
                            raise value
                        if value == "lose_lease":
                            lease_lost.set()
                            yield '{"type":"heartbeat"}'
                        else:
                            yield value if isinstance(value, str) else json.dumps(value)
                    current[0] = close
            return Response()

        client.session.get.side_effect = response
        stats = collector.BoundedStats()
        result = collector.capture_session(
            client, ["QQQ"], spool, stats, close, 5, session_open=self.open,
            now_et=lambda: current[0], sleeper=lambda _: None,
            lease_lost=lease_lost,
        )
        return result, stats, events

    def test_preopen_stream_continues_through_exact_open_without_counting_warmup(self):
        result, stats, events = self.capture([[
            (-30, {"type": "quote", "symbol": "QQQ"}),
            (0, {"type": "timesale", "date": int(self.open.timestamp()*1000), "symbol": "QQQ", "seq": 1}),
            (1, {"type": "timesale", "date": int(self.open.timestamp()*1000), "symbol": "QQQ", "seq": 2}),
        ]])
        self.assertEqual(result.opening_stream_ready_at, self.open - timedelta(seconds=30))
        self.assertEqual(result.preopen_events_discarded, 1)
        self.assertEqual(dict(stats.counts), {"timesale": 2})
        self.assertEqual([e["seq"] for e in events], [1, 2])

    def test_http_success_without_preopen_event_does_not_prove_opening_readiness(self):
        result, _, _ = self.capture([[(3, {"type": "timesale", "date": int(self.open.timestamp()*1000), "symbol": "QQQ"})]])
        self.assertLess(result.stream_connected_at, self.open)
        self.assertEqual(result.opening_stream_ready_at, self.open + timedelta(seconds=3))

    def test_delayed_premarket_trade_excluded_but_quote_context_survives(self):
        opening_ms = int(self.open.timestamp() * 1000)
        quote = {"type": "quote", "symbol": "QQQ", "bid": 600, "ask": 600.02,
                 "biddate": opening_ms - 100, "askdate": opening_ms - 100}
        result, stats, events = self.capture([[
            (-0.1, quote),
            (0.1, {"type": "timesale", "symbol": "QQQ", "seq": 1,
                   "date": opening_ms - 50, "session": "pre"}),
            (0.2, {"type": "timesale", "symbol": "QQQ", "seq": 2,
                   "date": opening_ms + 100, "session": "normal"}),
        ]])
        self.assertEqual(dict(stats.counts), {"excluded_timesale": 1, "timesale": 1})
        self.assertEqual(stats.timesale_by_symbol["QQQ"], 1)
        self.assertEqual(stats.last_sequence["QQQ"], 2)
        self.assertEqual(events[0]["provider_payload"]["seq"], 1)
        trade = events[1]
        self.assertEqual(trade["preceding_quote_age_ms"], 200)
        self.assertEqual(trade["preceding_quote_source"], "preopen")
        context = trade["preceding_quote_context"]
        self.assertEqual(context["biddate"], opening_ms - 100)
        self.assertEqual(context["bid"], 600)
        self.assertIn("collector_receipt_timestamp", context)
        self.assertEqual(stats.first_receipt_ts, trade["collector_receipt_timestamp"])
        self.assertEqual(result.preopen_events_discarded, 1)
        self.assertEqual(result.excluded_timesales, {"outside_regular_session": 1})

    def test_provider_time_and_session_label_independently_exclude_trades(self):
        opening_ms = int(self.open.timestamp() * 1000)
        for provider_ms, session, reason in [
            (opening_ms - 1, "normal", "outside_regular_session"),
            (opening_ms - 86400000, None, "outside_regular_session"),
            (opening_ms, "pre", "non_regular_session_label"),
            (opening_ms, "post", "non_regular_session_label"),
            (None, "normal", "missing_or_invalid_provider_time"),
            ("bad", "normal", "missing_or_invalid_provider_time"),
            (int(self.open.replace(hour=13, minute=0).timestamp()*1000),
             "normal", "outside_regular_session"),
        ]:
            with self.subTest(provider_ms=provider_ms, session=session):
                result, stats, events = self.capture([[
                    (-1, {"type": "heartbeat"}),
                    (0.1, {"type": "timesale", "symbol": "QQQ", "date": provider_ms,
                           "session": session, "seq": 1}),
                ]], close_hour=13)
                self.assertEqual(stats.counts["timesale"], 0)
                self.assertFalse(stats.timesale_by_symbol)
                self.assertEqual(result.excluded_timesales, {reason: 1})
                self.assertEqual(events[0]["type"], "excluded_timesale")
                self.assertEqual(events[0]["provider_payload"]["date"], provider_ms)

    def test_latest_warmup_quote_is_replaced_by_regular_quote(self):
        opening_ms = int(self.open.timestamp() * 1000)
        def quote(offset):
            return {"type": "quote", "symbol": "QQQ", "biddate": opening_ms + offset,
                    "askdate": opening_ms + offset}
        def trade(offset):
            return {"type": "timesale", "symbol": "QQQ", "date": opening_ms + offset}
        _, stats, events = self.capture([[
            (-0.3, quote(-300)), (-0.1, quote(-100)), (-0.05, quote(-200)),
            (0, trade(0)), (0.1, quote(100)), (0.2, trade(200)),
        ]])
        self.assertEqual(events[0]["preceding_quote_age_ms"], 100)
        self.assertEqual(events[0]["preceding_quote_context"]["biddate"], opening_ms - 100)
        self.assertEqual(events[2]["preceding_quote_age_ms"], 100)
        self.assertNotIn("preceding_quote_context", events[2])
        self.assertEqual(dict(stats.counts), {"timesale": 2, "quote": 1})
        self.assertFalse(stats.preopen_quote_context)

    def test_warmup_context_is_symbol_specific_and_never_future_dated(self):
        opening_ms = int(self.open.timestamp() * 1000)
        _, stats, events = self.capture([[
            (-1, {"type": "quote", "symbol": "UNSUBSCRIBED", "biddate": opening_ms - 100}),
            (-0.5, {"type": "quote", "symbol": "QQQ", "biddate": opening_ms + 100}),
            (0, {"type": "timesale", "symbol": "QQQ", "date": opening_ms}),
        ]])
        self.assertNotIn("UNSUBSCRIBED", stats.quote_timestamps)
        self.assertNotIn("preceding_quote_age_ms", events[0])
        self.assertNotIn("preceding_quote_context", events[0])

    def test_malformed_or_unexpected_preopen_payload_does_not_establish_readiness(self):
        result, stats, _ = self.capture([[
            (-30, 'not json'), (-20, {"error": "unavailable"}),
            (-10, {"type": "quote", "symbol": "UNSUBSCRIBED"}),
            (1, {"type": "timesale", "date": int(self.open.timestamp()*1000), "symbol": "QQQ"}),
        ]])
        self.assertEqual(result.opening_stream_ready_at, self.open + timedelta(seconds=1))
        self.assertEqual(stats.malformed, 0)

    def test_preopen_readiness_is_invalidated_by_disconnect_before_open(self):
        result, _, _ = self.capture([
            [(-30, {"type": "heartbeat"}), (-1, collector.requests.ConnectionError("lost"))],
            [(2, {"type": "timesale", "date": int(self.open.timestamp()*1000), "symbol": "QQQ"})],
        ])
        self.assertEqual(result.opening_stream_ready_at, self.open + timedelta(seconds=2))
        self.assertEqual(result.reconnects, 1)

    def test_lease_loss_during_warmup_stops_without_market_capture(self):
        lost = threading.Event()
        result, stats, _ = self.capture([[(-30, "lose_lease")]], lease_lost=lost)
        self.assertEqual(result.stop_reason, "lease_lost")
        self.assertFalse(stats.counts)
        self.assertIsNone(result.opening_stream_ready_at)

    def test_early_close_preserved(self):
        result, stats, _ = self.capture([[
            (-30, {"type": "heartbeat"}), (0, {"type": "quote", "symbol": "QQQ"}),
        ]], close_hour=13)
        self.assertIsNone(result.stop_reason)
        self.assertEqual(stats.counts["quote"], 1)

    def test_stop_during_preopen_wait_performs_no_storage_or_provider_setup(self):
        def stop(_):
            collector.STOP = True
        with patch.dict(os.environ, {"TRADIER_TOKEN": "test"}), patch.object(
            collector, "r2_client"
        ) as storage, patch.object(collector, "Tradier") as provider:
            self.assertEqual(collector.main(
                clock_et=lambda: self.open - timedelta(minutes=10), sleeper=stop,
                session_bounds=lambda _: (self.open, self.open.replace(hour=16, minute=0)),
            ), 0)
        storage.assert_not_called()
        provider.assert_not_called()

    def test_full_lifecycle_prepares_before_open_and_rejects_sub_five_second_lateness(self):
        for ready_before_open, invalid_trade in ((True, False), (False, False), (True, True)):
            with self.subTest(ready_before_open=ready_before_open, invalid_trade=invalid_trade), tempfile.TemporaryDirectory() as tmp:
                current = [self.open - timedelta(seconds=70)]
                close = self.open.replace(hour=16, minute=0)
                r2 = FakeR2()
                client = FakeTradier()
                original_get = client.get
                def get(path, **kwargs):
                    if path == "/markets/clock":
                        self.assertEqual(current[0], self.open - timedelta(seconds=60))
                        return {"clock": {"date": "2026-09-08", "state": "premarket", "next_change": "09:30"}}
                    if path == "/markets/quotes":
                        return {"quotes": {"quote": {"bid": 599.9, "ask": 600.1, "last": 400,
                            "bid_date": current[0].timestamp()*1000, "ask_date": current[0].timestamp()*1000}}}
                    return original_get(path, **kwargs)
                client.get = get
                client.create_market_session = Mock(return_value="session")
                class Response(FakeResponse):
                    def iter_lines(inner, **kwargs):
                        if ready_before_open:
                            current[0] = self.open - timedelta(seconds=30)
                            yield json.dumps({"type": "quote", "symbol": "QQQ", "bid": 599.9,
                                              "ask": 600.1, "biddate": int(current[0].timestamp()*1000),
                                              "askdate": int(current[0].timestamp()*1000)})
                        current[0] = self.open + timedelta(seconds=0 if ready_before_open else 3)
                        if invalid_trade:
                            yield '{"type":"timesale","symbol":"QQQ","date":"bad"}'
                        yield json.dumps({"type": "timesale", "symbol": "QQQ", "seq": 1,
                                          "date": int(current[0].timestamp()*1000), "session": "normal"})
                        current[0] = close
                client.session = Mock()
                client.session.get.return_value = Response()
                sleeps = []
                def sleep(seconds):
                    sleeps.append(seconds)
                    current[0] += timedelta(seconds=seconds)
                with patch.dict(os.environ, {"TRADIER_TOKEN": "test", "MOO144_STRIKE_COUNT": "2",
                      "MOO144_SPOOL_DIR": tmp}), patch.object(collector, "r2_client", return_value=(r2, "bucket")), patch.object(
                      collector, "Tradier", return_value=client):
                    rc = collector.main(clock_et=lambda: current[0], sleeper=sleep,
                                        session_bounds=lambda _: (self.open, close))
                self.assertEqual(rc, 0)
                self.assertEqual(sum(sleeps), 10)
                summary = next(json.loads(v) for (_b,k),v in r2.objects.items() if "/summary-" in k)
                expected_counts = {"timesale": 1}
                if invalid_trade:
                    expected_counts["excluded_timesale"] = 1
                self.assertEqual(summary["event_counts"], expected_counts)
                self.assertEqual(summary["universe"]["spot"], 600)
                self.assertEqual(summary["status"], "complete" if ready_before_open and not invalid_trade else "partial")
                self.assertEqual(summary["effective_late_start_seconds"], 0 if ready_before_open else 3)
                archived = [json.loads(line) for (_b, key), value in r2.objects.items()
                            if key.endswith(".ndjson.gz") for line in gzip.decompress(value).splitlines()]
                self.assertEqual(len(archived), sum(expected_counts.values()))
                if ready_before_open:
                    trade = next(e for e in archived if e["type"] == "timesale")
                    self.assertEqual(trade["preceding_quote_age_ms"], 30000)
                    self.assertEqual(trade["preceding_quote_context"]["bid"], 599.9)
                if invalid_trade:
                    self.assertIn("unclassifiable_timesale_provider_time", summary["partial_reasons"])
                    self.assertEqual(summary["excluded_timesales"], {"missing_or_invalid_provider_time": 1})
                if not ready_before_open:
                    self.assertIn("late_start_seconds=3.0", summary["partial_reasons"])


if __name__ == "__main__":
    unittest.main()
