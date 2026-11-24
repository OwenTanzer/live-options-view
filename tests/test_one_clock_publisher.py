"""Offline immutable publisher failure tests. No real credentials or network."""
import io
import json
import unittest
import uuid
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from market_data.live_market.publications import PublicationLog, PublicationError, PublicationBudget, BudgetedS3, digest


class StoreError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Store:
    def __init__(self):
        self.data, self.calls = {}, []
        self.before = self.after = None

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.data:
            raise StoreError("NoSuchKey")
        return {"Body": io.BytesIO(self.data[Key]), "ETag": digest(self.data[Key])}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfMatch=None, IfNoneMatch=None, **kwargs):
        self.calls.append(("put", Key))
        if self.before:
            self.before(Key)
        if IfNoneMatch == "*" and Key in self.data or IfMatch and (Key not in self.data or digest(self.data[Key]) != IfMatch):
            raise StoreError("PreconditionFailed")
        self.data[Key] = Body
        if self.after:
            self.after(Key)


def log(store):
    return PublicationLog(store, "fixture", source="fixture", instrument="QQQ", owner="fixture-owner", outputs={"price": {"spot": "number"}})


def args(i=0, revision=1):
    return {"publication_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture/{i}/{revision}")),
            "records": [{"source": "price", "id": f"snapshot/{i}", "revision": revision, "session": "2026-11-20", "eventAt": "2026-11-20T15:00:00Z", "value": {"spot": 700 + i}}],
            "evidence": [(b"fixture market evidence", "text/csv", "fixture.v1")],
            "created_at": "2026-11-20T15:00:01Z"}


class PublisherTests(unittest.TestCase):
    def test_shared_javascript_wire_fixture_regenerates_exactly(self):
        directory = Path(__file__).resolve().parent
        actual = json.loads(subprocess.check_output([sys.executable, str(directory / "helpers/one_clock_publisher_fixture.py")]))
        expected = json.loads((directory / "fixtures/one_clock_publications.json").read_text())
        self.assertEqual(actual, expected)

    def test_append_restart_and_old_intent_idempotency(self):
        s = Store()
        first = log(s).publish(**args())
        second = log(s).publish(**args(1))
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.assertEqual(log(s).publish(**args()), first)
        self.assertEqual(json.loads(s.data[second["key"]])["previousDigest"], first["digest"])
        self.assertEqual(json.loads(s.data[log(s).head_key])["sequence"], 2)

    def test_failure_before_evidence_or_commit_never_claims_publication(self):
        for suffix in ("blobs/", "entries/"):
            with self.subTest(suffix=suffix):
                s = Store()
                s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if suffix in key else None
                with self.assertRaises(PublicationError):
                    log(s).publish(**args())
                self.assertNotIn(log(s).key(1), s.data)
                s.before = None
                self.assertEqual(log(s).publish(**args())["sequence"], 1)

    def test_ambiguous_put_after_commit_and_intent_is_resolved_from_bytes(self):
        for suffix in ("blobs/", "intents/", "entries/"):
            with self.subTest(suffix=suffix):
                s = Store()
                s.after = lambda key: (_ for _ in ()).throw(StoreError("LostAck")) if suffix in key else None
                self.assertEqual(log(s).publish(**args())["sequence"], 1)
                self.assertEqual(log(s).publish(**args())["sequence"], 1)

    def test_crash_after_commit_before_head_recovers_without_list_or_republish(self):
        s = Store()
        s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if key.endswith("head.json") else None
        first = log(s).publish(**args())
        self.assertNotIn(log(s).head_key, s.data)
        s.before = None
        self.assertEqual(log(s).publish(**args(1))["sequence"], 2)
        self.assertEqual(log(s).publish(**args()), first)

    def test_competing_publishers_cannot_overwrite_reserved_intent_sequence(self):
        s = Store()
        # A prepared intent whose commit was interrupted still targets slot 1.
        s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if "entries/" in key else None
        with self.assertRaises(PublicationError):
            log(s).publish(**args())
        s.before = None
        winner = log(s).publish(**args(1))
        with self.assertRaisesRegex(PublicationError, "collision"):
            log(s).publish(**args())
        self.assertEqual(digest(s.data[winner["key"]]), winner["digest"])

    def test_wrong_blob_and_changed_retry_payload_fail_before_commit(self):
        s = Store(); original = args()
        result = log(s).publish(**original)
        changed = args(); changed["records"][0]["value"]["spot"] = 900
        with self.assertRaisesRegex(PublicationError, "intent mismatch"):
            log(s).publish(**changed)
        blob_key = next(k for k in s.data if "/blobs/" in k)
        s.data[blob_key] = b"corrupt"
        with self.assertRaisesRegex(PublicationError, "collision"):
            log(s).publish(**original)
        self.assertEqual(digest(s.data[result["key"]]), result["digest"])

    def test_late_correction_and_retraction_are_current_sequence_commits(self):
        s = Store(); log(s).publish(**args())
        self.assertEqual(log(s).publish(**args(revision=2))["sequence"], 2)
        retract = args(revision=3); retract["records"][0]["value"] = None
        self.assertEqual(log(s).publish(**retract)["sequence"], 3)

    def test_bounded_restart_repairs_hint_in_increments(self):
        s = Store()
        for i in range(6):
            log(s).publish(**args(i))
        del s.data[log(s).head_key]
        with patch("market_data.live_market.publications.RECOVERY_STEPS", 2):
            for sequence in (2, 4, 6):
                with self.assertRaisesRegex(PublicationError, "continuation"):
                    log(s).recover()
                self.assertEqual(json.loads(s.data[log(s).head_key])["sequence"], sequence)
            self.assertEqual(log(s).recover()[0], 6)

    def test_private_extra_field_unknown_source_and_future_event_rejected_before_any_write(self):
        for mutate in (lambda r: r["value"].update(secret="private"), lambda r: r.update(source="private"), lambda r: r.update(eventAt="2026-11-21T15:00:00Z"), lambda r: r.update(revision=True)):
            s = Store(); a = args(); mutate(a["records"][0])
            with self.assertRaises(PublicationError):
                log(s).publish(**a)
            self.assertEqual(s.calls, [])

    def test_snapshot_commit_precedes_latest_and_survives_compatibility_write_failure(self):
        from test_snapshot_provenance import snapshot, FRESH
        from market_data.live_market import market
        quote = {"bid": 600, "ask": 600.2, "bid_ts": FRESH, "ask_ts": FRESH}
        for fail_latest in (False, True):
            s = Store()
            if fail_latest:
                s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if key == "intraday/latest.json" else None
            with patch.object(market, "ONE_CLOCK_PUBLICATIONS", True):
                if fail_latest:
                    with self.assertRaises(StoreError):
                        snapshot(s, quote)
                else:
                    snapshot(s, quote)
            entry = next(key for key in s.data if "/entries/" in key)
            self.assertLess(s.calls.index(("put", entry)), s.calls.index(("put", "intraday/latest.json")))
            value = json.loads(s.data[entry])
            self.assertEqual({r["source"] for r in value["records"]}, {"price", "skew"})
            self.assertEqual(len(value["evidence"]), 2)
            for ref in value["evidence"]:
                self.assertEqual(digest(s.data[ref["key"]]), ref["sha256"])

    def test_snapshot_commit_failure_does_not_advance_latest_and_records_gap(self):
        from test_snapshot_provenance import snapshot, FRESH
        from market_data.live_market import market
        s = Store(); s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if "/entries/" in key else None
        with patch.object(market, "ONE_CLOCK_PUBLICATIONS", True), self.assertRaises(PublicationError):
            snapshot(s, {"bid": 600, "ask": 600.2, "bid_ts": FRESH, "ask_ts": FRESH})
        self.assertNotIn("intraday/latest.json", s.data)
        sidecar = json.loads(next(raw for key, raw in s.data.items() if key.endswith(".provenance.json")))
        self.assertEqual(sidecar["publication"]["reason"], "one_clock_commit_failed")

    def test_snapshot_projection_rejects_stale_unknown_and_future_receipts(self):
        from market_data.live_market.one_clock import project_snapshot
        payload = {"symbol": "QQQ", "snapshot_key": "intraday/snapshot.csv", "timestamp": "2026-11-20T15:00:00Z", "date": "2026-11-20", "underlying_price": 700,
                   "underlying_market": {"freshness": "live", "spot_ts": "2026-11-20T14:59:59Z"}}
        self.assertEqual(project_snapshot(payload)[0]["value"], {"spot": 700})
        for ts in (None, "2026-11-20T14:50:00Z", "2026-11-20T15:00:01Z"):
            payload["underlying_market"]["spot_ts"] = ts
            self.assertIsNone(project_snapshot(payload)[0]["value"])

    def test_utf16_limits_and_offset_normalization_match_javascript_wire(self):
        s = Store(); a = args(); a["records"][0]["id"] = "😀" * 500
        with self.assertRaisesRegex(PublicationError, "identity"):
            log(s).publish(**a)
        self.assertEqual(s.calls, [])
        a["records"][0]["id"] = "😀" * 250
        a["records"][0]["eventAt"] = "2026-11-20T15:00:00+0000"
        a["created_at"] = "2026-11-20T15:00:01+0000"
        entry = json.loads(s.data[log(s).publish(**a)["key"]])
        self.assertEqual(entry["createdAt"], "2026-11-20T15:00:01.000Z")
        self.assertEqual(entry["records"][0]["eventAt"], "2026-11-20T15:00:00.000Z")
        p = PublicationLog(s, "fixture", source="text", instrument="QQQ", owner="fixture-owner", outputs={"price": {"label": "string"}})
        a["records"][0]["value"] = {"label": "😀" * 1001}
        with self.assertRaisesRegex(PublicationError, "field type"):
            p.publish(**a)

    def test_identity_preflight_rejects_duplicate_decreasing_and_moved_session_after_restart(self):
        s = Store(); log(s).publish(**args(revision=2))
        for revision, moved in ((2, False), (1, False), (3, True)):
            a = args(revision=revision); a["publication_id"] = str(uuid.uuid4())
            if moved:
                a["records"][0].update(session="2026-11-19", eventAt="2026-11-19T15:00:00Z")
            with self.assertRaisesRegex(PublicationError, "revision conflict"):
                log(s).publish(**a)
            self.assertNotIn(log(s).key(2), s.data)
        self.assertEqual(log(s).publish(**args(revision=3))["sequence"], 2)

    def test_crash_after_commit_repairs_identity_heads_before_any_next_commit(self):
        s = Store(); s.before = lambda key: (_ for _ in ()).throw(StoreError("Unavailable")) if "/records/" in key else None
        self.assertEqual(log(s).publish(**args())["sequence"], 1)
        self.assertNotIn(log(s).head_key, s.data)
        with self.assertRaisesRegex(PublicationError, "repair unavailable"):
            log(s).publish(**args(1))
        self.assertNotIn(log(s).key(2), s.data)
        s.before = None
        self.assertEqual(log(s).publish(**args(1))["sequence"], 2)
        duplicate = args(); duplicate["publication_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(PublicationError, "revision conflict"):
            log(s).publish(**duplicate)

    def test_request_time_and_streamed_body_budgets_stop_further_calls(self):
        s = Store(); limited = BudgetedS3(s, requests=2)
        with self.assertRaises(PublicationError):
            log(limited).publish(**args())
        self.assertEqual(limited.requests, 2)
        self.assertNotIn(log(s).key(1), s.data)
        clock = [0]
        timed = BudgetedS3(s, clock=lambda: clock[0])
        clock[0] = 6
        with self.assertRaises(PublicationBudget):
            timed.get_object(Bucket="fixture", Key="absent")
        self.assertEqual(timed.requests, 0)
        class SlowBody(io.BytesIO):
            def read(self, n):
                result = super().read(n); clock[0] += 6; return result
        class SlowStore:
            def get_object(self, **kwargs):
                return {"Body": SlowBody(b"x" * 40000)}
        clock[0] = 0; timed = BudgetedS3(SlowStore(), clock=lambda: clock[0])
        body = timed.get_object(Bucket="fixture", Key="fixture")["Body"]
        with self.assertRaises(PublicationBudget):
            body.read(40000)
        body.close()

    def test_publication_transport_has_socket_timeouts_and_no_sdk_retries(self):
        import os
        from market_data.live_market.prices import make_s3
        with patch.dict(os.environ, {"R2_ACCOUNT_ID": "fixture", "R2_ACCESS_KEY_ID": "fixture", "R2_SECRET_ACCESS_KEY": "fixture"}), patch("market_data.live_market.prices.boto3.client") as client:
            make_s3(publication=True)
            config = client.call_args.kwargs["config"]
            self.assertEqual((config.connect_timeout, config.read_timeout), (1, 1))
            self.assertEqual(config.retries, {"total_max_attempts": 1})
            make_s3()
            self.assertNotIn("config", client.call_args.kwargs)



class PendingQueueTests(unittest.TestCase):
    def test_unregistered_roots_keep_archive_and_latest_without_a_publication_worker(self):
        from datetime import datetime, timezone
        from market_data.live_market import market, runner, state
        from test_snapshot_provenance import FakeFeed, strike

        captured = datetime(2026, 11, 23, 19, 33, tzinfo=timezone.utc)
        for symbol in ("SLV", "DRAM"):
            lane = state.UnderlyingSession(symbol)
            with self.subTest(symbol=symbol), patch.object(runner, "ONE_CLOCK_PUBLICATIONS", True), patch.object(runner, "make_s3") as client:
                self.assertIsNone(runner._publication_queue(lane, captured.date(), state.Counters()))
                client.assert_not_called()
            option = strike(symbol, 40)
            book = {symbol: {"bid": 40, "ask": 40.2, "bid_ts": captured.isoformat(), "ask_ts": captured.isoformat()},
                    option["call_sym"]: {"bid": 1, "ask": 1.2}, option["put_sym"]: {"bid": 1, "ask": 1.2}}
            for queue in (None, Mock()):
                with self.subTest(symbol=symbol, queued=queue is not None), patch.object(market, "ONE_CLOCK_PUBLICATIONS", True):
                    store = Store()
                    market.take_snapshot(store, FakeFeed(book), [option], captured.date().isoformat(),
                                         "Regular", captured.date(), state.Counters(), state.SnapshotTracker(),
                                         lane, publication_queue=queue, capture=(captured, book))
                    if queue is not None:
                        queue.enqueue.assert_not_called()
                    payload = json.loads(store.data[lane.latest_key])
                    self.assertEqual(payload["symbol"], symbol)
                    self.assertIn(payload["snapshot_key"], store.data)
                    provenance = json.loads(store.data[payload["snapshot_key"].replace(".csv", ".provenance.json")])
                    self.assertEqual(provenance["publication"]["status"], "published")
                    self.assertFalse(any(key.startswith("publications/") for key in store.data))

    def test_admission_retry_survives_restart_and_keeps_capture_and_commit_clocks(self):
        from datetime import datetime, timedelta, timezone
        from tempfile import TemporaryDirectory
        from market_data.live_market import market, state
        from market_data.live_market.publication_queue import PublicationQueue
        from test_snapshot_provenance import FakeFeed, strike

        for symbol, minute in (("QQQ", 33), ("SPY", 34)):
            with self.subTest(symbol=symbol), TemporaryDirectory() as directory:
                captured = datetime(2026, 11, 23, 19, minute, 0, 2291, timezone.utc)
                published = captured + timedelta(minutes=4)
                class Storage(Store):
                    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
                        keys = sorted(key for key in self.data if key.startswith(Prefix))
                        return {"Contents": [{"Key": key} for key in keys[:MaxKeys]]}
                    def delete_object(self, *, Bucket, Key):
                        del self.data[Key]
                    def get_object(self, **kwargs):
                        return {**super().get_object(**kwargs), "LastModified": published}

                s = Storage()
                def queue():
                    return PublicationQueue(s, market.R2_BUCKET, symbol, spool_dir=directory)
                q = queue()
                def reject_admission(key):
                    if key.startswith(q.prefix):
                        raise StoreError("ServiceUnavailable")
                s.before = reject_admission
                option = strike(symbol, 600)
                spot = {"bid": 600, "ask": 600.2, "bid_ts": captured.isoformat(),
                        "ask_ts": captured.isoformat()}
                book = {symbol: spot, option["call_sym"]: {"bid": 1, "ask": 1.2},
                        option["put_sym"]: {"bid": 1, "ask": 1.2}}
                lane = state.UnderlyingSession(symbol)
                with patch.object(market, "ONE_CLOCK_PUBLICATIONS", True):
                    market.take_snapshot(s, FakeFeed(book), [option], captured.date().isoformat(),
                                         "0DTE_Regular", captured.date(), state.Counters(),
                                         state.SnapshotTracker(), lane, publication_queue=q,
                                         capture=(captured, book))
                sidekey = next(k for k in s.data if k.endswith(".provenance.json"))
                waiting = json.loads(s.data[sidekey])
                self.assertEqual(waiting["publication"]["reason"], "awaiting_queue_admission")
                self.assertFalse(any(k.startswith(q.prefix) for k in s.data))
                with self.assertRaises(StoreError):
                    queue().publish_oldest()
                q = queue()
                s.before = None
                def lost_ack(key):
                    if key.startswith(q.prefix) or "/entries/" in key:
                        raise StoreError("LostAck")
                s.after = lost_ack
                self.assertTrue(q.publish_oldest())  # durable admission, even on lost ack
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return published.astimezone(tz)
                s.after = None
                with patch("market_data.live_market.publication_queue.datetime", Clock):
                    self.assertTrue(queue().publish_oldest())
                    self.assertFalse(queue().publish_oldest())
                final = json.loads(s.data[sidekey])
                market._put_snapshot_provenance(s, waiting, create_only=True)
                self.assertEqual(json.loads(s.data[sidekey]), final)
                entries = [key for key in s.data if "/entries/" in key]
                self.assertEqual(entries, [final["one_clock_publication"]["key"]])
                entry = json.loads(s.data[entries[0]])
                for record in entry["records"]:
                    self.assertEqual(record["eventAt"], captured.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
                self.assertEqual(final["constructed_at"], captured.isoformat())
                self.assertEqual(final["latest_published_at"], published.isoformat())
                self.assertEqual(final["snapshot_key"], waiting["snapshot_key"])

    def test_exchange_minute_schedule_and_real_overrun(self):
        from market_data.live_market.runner import next_capture_delay
        from market_data.live_market.state import SnapshotTracker
        from datetime import datetime, timezone
        start = 0
        tracker = SnapshotTracker()
        occupied = []
        for i in range(390):
            occupied.append(int(start // 60))
            tracker.record(datetime.fromtimestamp(start, timezone.utc))
            completed = start + (3, 22, 50)[i % 3]
            start = completed + next_capture_delay(start, completed)
        self.assertEqual(occupied, list(range(390)))
        self.assertEqual(tracker.get()["missed_snapshot_count"], 0)
        completed = start + 145
        self.assertEqual(next_capture_delay(start, completed), 0)
        tracker.record(datetime.fromtimestamp(completed, timezone.utc))
        self.assertEqual(tracker.get()["missed_snapshot_count"], 2)
        self.assertEqual(next_capture_delay(completed, completed + 10), 25)

    def test_capture_through_stall_restart_and_ambiguous_latest(self):
        from datetime import datetime, timedelta, timezone
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from test_snapshot_provenance import QQQ_STRIKES, FakeFeed, state_for
        from market_data.live_market import market, state
        from market_data.live_market.publication_queue import PublicationQueue

        from research.market_readings_series import collect
        from market_data.overview.readings import readings_digest
        now = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
        worker_time = now + timedelta(minutes=3)
        class WorkerClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return worker_time.astimezone(tz)
        self.enterContext(patch("market_data.live_market.publication_queue.datetime", WorkerClock))

        class QueueStore(Store):
            def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
                keys = sorted(k for k in self.data if k.startswith(Prefix))
                return {"Contents": [{"Key": k} for k in keys[:MaxKeys]]}

            def delete_object(self, *, Bucket, Key):
                del self.data[Key]

            def get_object(self, **kwargs):
                return {**super().get_object(**kwargs), "LastModified": worker_time}

        s = QueueStore()
        queue = PublicationQueue(s, market.R2_BUCKET, "QQQ")
        counters, tracker = state.Counters(), state.SnapshotTracker()
        us = state.UnderlyingSession("QQQ")
        def capture(instant):
            feed = FakeFeed(state_for({"bid": 600, "ask": 600.2, "bid_ts": instant.isoformat(),
                                       "ask_ts": instant.isoformat()}))
            with patch.object(market, "ONE_CLOCK_PUBLICATIONS", True):
                market.take_snapshot(s, feed, QQQ_STRIKES, now.date().isoformat(), "0DTE_Regular", now.date(),
                                     counters, tracker, us, publication_queue=queue,
                                     capture=(instant, feed.get_state()))
        capture(now)
        first_sidecar = next(key for key in s.data if key.endswith(".provenance.json"))
        pending = json.loads(s.data[first_sidecar])
        self.assertTrue(all(r["publication"]["status"] == "pending" and r["published_at"] is None
                            for r in pending["market_readings"]))
        self.assertTrue(all(r["reading_published_at"] is None for r in collect([(first_sidecar, pending)])))
        stalled, release = Event(), Event()
        def block_entry(key):
            if "/entries/" in key:
                stalled.set()
                self.assertTrue(release.wait(timeout=5))
                raise StoreError("Unavailable")
        s.before = block_entry
        with ThreadPoolExecutor(max_workers=1) as executor:
            inflight = executor.submit(queue.publish_oldest)
            try:
                self.assertTrue(stalled.wait(timeout=5))
                capture(now + timedelta(minutes=1))
            finally:
                release.set()
            with self.assertRaises(PublicationError):
                inflight.result(timeout=5)
        self.assertEqual(tracker.get()["snapshot_sequence"], 2)
        self.assertNotIn(us.latest_key, s.data)
        self.assertEqual(len([k for k in s.data if k.startswith(queue.prefix)]), 2)

        self.assertEqual(json.loads(s.data[first_sidecar]), pending)
        worker_time += timedelta(minutes=7)
        queue = PublicationQueue(s, market.R2_BUCKET, "QQQ")
        s.before = None
        def lose_ack(key):
            if key == us.latest_key:
                raise StoreError("LostAck")
        s.after = lose_ack
        with self.assertRaises(StoreError):
            queue.publish_oldest()
        first = json.loads(s.data[us.latest_key])
        s.after = None
        self.assertTrue(queue.publish_oldest())
        self.assertTrue(queue.publish_oldest())
        self.assertFalse(queue.publish_oldest())
        latest = json.loads(s.data[us.latest_key])
        self.assertEqual([first["one_clock_publication"]["sequence"],
                          latest["one_clock_publication"]["sequence"]], [1, 2])
        self.assertEqual(latest["timestamp"], (now + timedelta(minutes=1)).isoformat())
        self.assertEqual(len([k for k in s.data if "/entries/" in k]), 2)
        self.assertIsNone(queue.advance_latest(us.latest_key, first))
        self.assertEqual(json.loads(s.data[us.latest_key]), latest)
        for row in (first, latest):
            key = row["snapshot_key"]
            sidecar = json.loads(s.data[key[:-4] + ".provenance.json"])
            self.assertEqual(sidecar["market_readings_digest"], row["market_readings_digest"])
            self.assertEqual(sidecar["publication"]["status"], "published")
            self.assertEqual(sidecar["constructed_at"], row["timestamp"])
            self.assertEqual(sidecar["latest_published_at"], worker_time.isoformat())
            expected_attempt = now + timedelta(minutes=3) if row is first else worker_time
            self.assertTrue(all(r["published_at"] == expected_attempt.isoformat() for r in row["market_readings"]))
            entry = json.loads(s.data[row["one_clock_publication"]["key"]])
            immutable = json.loads(s.data[entry["evidence"][1]["key"]])
            self.assertEqual(immutable["market_readings"], row["market_readings"])
            self.assertEqual(readings_digest(immutable["market_readings"]), sidecar["market_readings_digest"])
            self.assertTrue(all(r["reading_published_at"] == worker_time.isoformat() for r in collect([(key, sidecar)])))

if __name__ == "__main__":
    unittest.main()
