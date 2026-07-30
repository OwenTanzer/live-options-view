"""Prove collector.py's VWAP/RVOL wiring: session reset, restart recovery,
RVOL baseline load/finalize, and the `underlying_market` block itself.

Mirrors tests/stage2_verification.py's conventions (import collector.py
directly, FakeS3/FakeBody, assert_equal/assert_true, run()) so collector.py's
first extracted pure-math module (market_signals.py) and the state it feeds
get exercised the same way the rest of collector.py already is -- without
tastytrade credentials, a DXLink connection, or real R2 access.

    python tests/verify_collector_vwap_rvol.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collector  # noqa: E402
import market_signals as ms  # noqa: E402


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeS3:
    """Same shape as stage2_verification.py's FakeS3, extended with a
    per-key store so `get_object` can return whatever was last `put_object`'d
    under that key -- needed to test restore/finalize round-trips, not just
    inspect what was written.
    """

    def __init__(self, seed: dict | None = None):
        self.store = dict(seed or {})
        self.objects = []

    def get_object(self, Bucket, Key):  # noqa: N803 (matches boto3's call signature)
        if Key not in self.store:
            raise RuntimeError(f"missing key: {Key}")
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        payload = json.loads(Body) if isinstance(Body, (bytes, str)) else Body
        self.store[Key] = payload
        self.objects.append({"Key": Key, "Body": Body})

    def json_objects(self, key):
        out = []
        for obj in self.objects:
            if obj["Key"] == key:
                body = obj["Body"]
                if isinstance(body, bytes):
                    body = body.decode()
                out.append(json.loads(body))
        return out


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


def _reset_module_state():
    collector._vwap_state = ms.VwapState()
    collector._rvol_baseline = {}
    collector._rvol_today.clear()


def test_vwap_accumulates_across_snapshots_and_resets_session():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        ts_et = collector.ET.localize(datetime(2026, 7, 30, 10, 30, 0))
        ts_utc = datetime(2026, 7, 30, 14, 30, 0, tzinfo=timezone.utc)
        s3 = FakeS3()

        qqq = {"volume": 1_000_000, "last_ts": ts_utc.isoformat()}
        um1 = collector._compute_underlying_market(s3, qqq, 400.0, ts_et, ts_utc, today)
        assert_equal(um1["vwap"], None, "first tick has no delta to weight yet")
        assert_equal(um1["session_volume"], 1_000_000, "session_volume reflects raw dayVolume")
        assert_equal(um1["symbol"], "QQQ", "symbol is always QQQ today")

        qqq2 = {"volume": 1_100_000, "last_ts": ts_utc.isoformat()}
        um2 = collector._compute_underlying_market(s3, qqq2, 402.0, ts_et, ts_utc + timedelta(minutes=1), today)
        assert_equal(um2["vwap"], 402.0, "vwap after one delta is just the tick's own price")
        assert_true(um2["price_vs_vwap_abs"] is not None, "price_vs_vwap computed once vwap exists")

        # A new day resets the accumulator to a fresh, single-tick state.
        tomorrow = date(2026, 7, 31)
        qqq3 = {"volume": 500, "last_ts": ts_utc.isoformat()}
        um3 = collector._compute_underlying_market(s3, qqq3, 410.0, ts_et, ts_utc, tomorrow)
        assert_equal(um3["vwap"], None, "new session resets vwap to no-delta-yet state")
        assert_equal(um3["vwap_session_date"], "2026-07-31", "vwap_session_date tracks the reset")
    finally:
        _reset_module_state()


def test_vwap_state_persisted_and_restored_after_restart():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        ts_et = collector.ET.localize(datetime(2026, 7, 30, 10, 30, 0))
        ts_utc = datetime(2026, 7, 30, 14, 30, 0, tzinfo=timezone.utc)
        s3 = FakeS3()

        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, ts_et, ts_utc, today)
        collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts_et, ts_utc + timedelta(minutes=1), today,
        )
        accumulated_vwap = collector._vwap_state.vwap
        assert_true(accumulated_vwap is not None, "vwap accumulated before simulated restart")

        # Simulate a process restart: a fresh module-level accumulator, then
        # recover it from what was persisted to the FakeS3 store above.
        collector._vwap_state = ms.VwapState()
        collector._restore_vwap_state(s3, today, today.strftime("%Y%m%d"))
        assert_equal(collector._vwap_state.vwap, accumulated_vwap, "vwap recovered after restart")
        assert_equal(collector._vwap_state.session_date, today, "session_date recovered after restart")
    finally:
        _reset_module_state()


def test_vwap_state_not_restored_across_a_day_boundary():
    _reset_module_state()
    try:
        yesterday = date(2026, 7, 29)
        today = date(2026, 7, 30)
        ts_et = collector.ET.localize(datetime(2026, 7, 29, 10, 30, 0))
        ts_utc = datetime(2026, 7, 29, 14, 30, 0, tzinfo=timezone.utc)
        s3 = FakeS3()
        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, ts_et, ts_utc, yesterday)
        collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts_et, ts_utc + timedelta(minutes=1), yesterday,
        )

        collector._vwap_state = ms.VwapState()
        # Restoring for *today* using yesterday's persisted key must not
        # resurrect yesterday's accumulator -- restore_state always reads
        # from today's own date-prefixed key, so this proves the session_date
        # guard inside _restore_vwap_state, not a wrong key being read.
        collector._restore_vwap_state(s3, today, yesterday.strftime("%Y%m%d"))
        assert_equal(collector._vwap_state.vwap, None, "a mismatched session_date is not recovered")
    finally:
        _reset_module_state()


def test_load_rvol_baseline_defaults_when_missing():
    _reset_module_state()
    baseline = collector.load_rvol_baseline(FakeS3())
    assert_equal(baseline["buckets"], {}, "no baseline yet -- empty buckets, not an error")
    assert_equal(baseline["min_days_required"], collector.RVOL_MIN_DAYS_REQUIRED, "default min_days_required")
    assert_equal(baseline["updated_through"], None, "no prior session recorded yet")


def test_load_rvol_baseline_returns_persisted_data():
    seeded = {
        "symbol": "QQQ", "bucket_minutes": 5, "lookback_days": 20, "min_days_required": 5,
        "updated_through": "2026-07-29",
        "buckets": {"10:30": {"samples": [{"date": "2026-07-29", "cum_volume": 900000}]}},
    }
    s3 = FakeS3(seed={collector.RVOL_BASELINE_KEY: seeded})
    baseline = collector.load_rvol_baseline(s3)
    assert_equal(baseline["updated_through"], "2026-07-29", "loads the persisted baseline, not a fresh one")
    assert_equal(len(baseline["buckets"]["10:30"]["samples"]), 1, "loads existing bucket samples")


def test_finalize_rvol_baseline_appends_and_prunes():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        old_date = (today - timedelta(days=30)).isoformat()  # older than the 20-day lookback -- must be pruned
        recent_date = (today - timedelta(days=5)).isoformat()  # within the lookback -- must be kept
        seeded = {
            "symbol": "QQQ", "bucket_minutes": 5, "lookback_days": 20, "min_days_required": 5,
            "updated_through": (today - timedelta(days=1)).isoformat(),
            "buckets": {
                "10:30": {"samples": [
                    {"date": old_date, "cum_volume": 111},
                    {"date": recent_date, "cum_volume": 222},
                ]},
            },
        }
        s3 = FakeS3(seed={collector.RVOL_BASELINE_KEY: seeded})
        collector._rvol_today = {"10:30": 999999}

        collector.finalize_rvol_baseline(s3, today)

        written = s3.json_objects(collector.RVOL_BASELINE_KEY)[-1]
        samples = written["buckets"]["10:30"]["samples"]
        dates = {s["date"] for s in samples}
        assert_true(old_date not in dates, "sample older than lookback_days is pruned")
        assert_true(recent_date in dates, "sample within lookback_days is kept")
        assert_true(today.isoformat() in dates, "today's reading is appended")
        assert_equal(written["updated_through"], today.isoformat(), "updated_through bumped to today")
    finally:
        _reset_module_state()


def test_finalize_rvol_baseline_skips_when_no_readings():
    _reset_module_state()
    s3 = FakeS3()
    collector.finalize_rvol_baseline(s3, date(2026, 7, 30))
    assert_equal(s3.objects, [], "no readings this session -- baseline file is not touched")


def test_underlying_market_freshness_reflects_feed_staleness():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        ts_et = collector.ET.localize(datetime(2026, 7, 30, 10, 30, 0))
        now = datetime(2026, 7, 30, 14, 30, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(seconds=collector.STALE_FEED_SECS + 30)).isoformat()

        s3 = FakeS3()
        um = collector._compute_underlying_market(
            s3, {"volume": 1000, "last_ts": old_ts}, 400.0, ts_et, now, today,
        )
        assert_equal(um["freshness"], "stale", "an old last_ts is reported as stale, not silently live")

        fresh_um = collector._compute_underlying_market(
            s3, {"volume": 1500, "last_ts": now.isoformat()}, 401.0, ts_et, now, today,
        )
        assert_equal(fresh_um["freshness"], "live", "a current last_ts is reported as live")
    finally:
        _reset_module_state()


def run():
    tests = [
        test_vwap_accumulates_across_snapshots_and_resets_session,
        test_vwap_state_persisted_and_restored_after_restart,
        test_vwap_state_not_restored_across_a_day_boundary,
        test_load_rvol_baseline_defaults_when_missing,
        test_load_rvol_baseline_returns_persisted_data,
        test_finalize_rvol_baseline_appends_and_prunes,
        test_finalize_rvol_baseline_skips_when_no_readings,
        test_underlying_market_freshness_reflects_feed_staleness,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    run()
