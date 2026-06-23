import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collector


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeS3:
    def __init__(self, prior_health=None):
        self.prior_health = prior_health
        self.objects = []

    def get_object(self, **kwargs):
        if self.prior_health is None:
            raise RuntimeError("missing")
        return {"Body": FakeBody(self.prior_health)}

    def put_object(self, **kwargs):
        self.objects.append(kwargs)

    def json_objects(self, key):
        out = []
        for obj in self.objects:
            if obj["Key"] == key:
                body = obj["Body"]
                if isinstance(body, bytes):
                    body = body.decode()
                out.append(json.loads(body))
        return out


class FakeFeed:
    def __init__(self, state=None, last_event_time=None, health=None):
        self.state = state or {}
        self.health = {
            "connected": True,
            "authorized": True,
            "channel_open": True,
            "reconnect_count": 0,
            "last_error": None,
            "last_close_code": None,
            "last_feed_event_time": last_event_time,
        }
        if health:
            self.health.update(health)

    def get_state(self):
        return {k: dict(v) for k, v in self.state.items()}

    def get_health(self):
        return dict(self.health)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


def test_session_window_timing():
    et = collector.ET
    samples = [
        (et.localize(datetime(2026, 6, 23, 5, 0, 0)), False, "2026-06-23 06:00"),
        (et.localize(datetime(2026, 6, 23, 6, 0, 0)), True, "2026-06-23 06:00"),
        (et.localize(datetime(2026, 6, 23, 12, 0, 0)), True, "2026-06-23 06:00"),
        (et.localize(datetime(2026, 6, 23, 16, 14, 0)), True, "2026-06-23 06:00"),
        (et.localize(datetime(2026, 6, 23, 16, 15, 0)), False, "2026-06-24 06:00"),
        (et.localize(datetime(2026, 6, 23, 17, 0, 0)), False, "2026-06-24 06:00"),
    ]
    for current, inside, expected_start in samples:
        assert_equal(collector._inside_session_window(current), inside, f"inside window {current}")
        next_start = collector._next_session_start(current)
        assert_equal(next_start.strftime("%Y-%m-%d %H:%M"), expected_start, f"next start {current}")


def test_startup_classification():
    now = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cases = [
        (None, "clean_start"),
        ({"collector": {"past_stop": True}, "updated_at": (now - timedelta(minutes=5)).isoformat()}, "clean_start"),
        ({"collector": {"past_stop": False}, "updated_at": (now - timedelta(minutes=5)).isoformat()}, "recovery_after_crash"),
        ({"collector": {"past_stop": False}, "updated_at": (now - timedelta(minutes=180)).isoformat()}, "recovery_after_gap"),
    ]
    for prior, expected in cases:
        assert_equal(collector._classify_startup(FakeS3(prior), now), expected, f"classification {expected}")


def test_dxlink_ingest_health():
    feed = collector.DXLinkFeed("ws://example.invalid", "token")
    feed._ingest([
        {"eventType": "Quote", "eventSymbol": "QQQ", "bidPrice": 100.0, "askPrice": 100.2},
        {"eventType": "Summary", "eventSymbol": "QQQ", "openInterest": 123, "prevDayClosePrice": 99.0},
        {"eventType": "Trade", "eventSymbol": "QQQ", "dayVolume": 456, "price": 100.1},
        {"eventType": "Greeks", "eventSymbol": "QQQ", "gamma": 0.01},
    ])
    state = feed.get_state()["QQQ"]
    assert_equal(state["bid"], 100.0, "bid ingest")
    assert_equal(state["last"], 100.1, "trade ingest")
    assert_equal(state["oi"], 123, "summary ingest")
    assert_equal(state["gamma"], 0.01, "greeks ingest")
    assert_true(feed.get_health()["last_feed_event_time"] is not None, "last event time set")


def test_prices_feed_stale_flags():
    state = {"QQQ": {"bid": 100.0, "ask": 100.2, "prev_close": 99.0}}

    stale_s3 = FakeS3()
    collector.push_prices(
        stale_s3,
        FakeFeed(state=state, last_event_time=datetime.now(timezone.utc) - timedelta(seconds=collector.STALE_FEED_SECS + 5)),
        collector.Counters(),
    )
    assert_equal(stale_s3.json_objects("intraday/prices.json")[-1]["feed_stale"], True, "stale price flag")

    fresh_s3 = FakeS3()
    collector.push_prices(
        fresh_s3,
        FakeFeed(state=state, last_event_time=datetime.now(timezone.utc)),
        collector.Counters(),
    )
    assert_equal(fresh_s3.json_objects("intraday/prices.json")[-1]["feed_stale"], False, "fresh price flag")


def test_health_schema_and_counters():
    now = datetime.now(timezone.utc)
    counters = collector.Counters()
    counters.inc_prices(now.isoformat())
    tracker = collector.SnapshotTracker()
    tracker.record()
    s3 = FakeS3()
    collector.push_health(
        s3,
        FakeFeed(state={"QQQ": {"bid": 100.0}}, last_event_time=now),
        counters,
        tracker,
        "run-test",
        now,
        "clean_start",
        datetime(2026, 6, 23).date(),
    )
    health = s3.json_objects("intraday/health.json")[-1]
    for key in ("run_id", "trade_date", "process_start_time", "updated_at", "classification", "collector", "feed", "uploads", "cadence", "symbols"):
        assert_true(key in health, f"health key {key}")
    assert_equal(health["run_id"], "run-test", "health run id")
    assert_equal(health["uploads"]["prices_success_count"], 1, "health price counter")
    assert_equal(health["feed"]["feed_stale"], False, "health feed fresh")


def test_snapshot_archive_key_uniqueness():
    state = {
        "QQQ": {"bid": 500.0, "ask": 500.2, "last": 500.1},
        ".QQQ260623C00500000": {"bid": 1.0, "ask": 1.2, "oi": 100, "volume": 10},
        ".QQQ260623P00500000": {"bid": 1.3, "ask": 1.5, "oi": 200, "volume": 20},
    }
    strikes = [{
        "strike": 500.0,
        "call_sym": ".QQQ260623C00500000",
        "put_sym": ".QQQ260623P00500000",
        "call_occ": "QQQ   260623C00500000",
        "put_occ": "QQQ   260623P00500000",
    }]
    s3 = FakeS3()
    feed = FakeFeed(state=state, last_event_time=datetime.now(timezone.utc))
    counters = collector.Counters()
    tracker = collector.SnapshotTracker()
    collector.take_snapshot(s3, feed, strikes, "2026-06-23", "0DTE_Regular", datetime(2026, 6, 23).date(), counters, tracker)
    collector.take_snapshot(s3, feed, strikes, "2026-06-23", "0DTE_Regular", datetime(2026, 6, 23).date(), counters, tracker)
    csv_keys = [obj["Key"] for obj in s3.objects if obj["Key"].endswith(".csv")]
    assert_equal(len(csv_keys), 2, "two csv writes")
    assert_equal(len(set(csv_keys)), 2, "unique csv keys")


def run():
    tests = [
        test_session_window_timing,
        test_startup_classification,
        test_dxlink_ingest_health,
        test_prices_feed_stale_flags,
        test_health_schema_and_counters,
        test_snapshot_archive_key_uniqueness,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    run()
