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

        spot_ts1 = ts_utc.isoformat()
        um1 = collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, spot_ts1, ts_et, ts_utc, today)
        assert_equal(um1["vwap"], None, "first tick has no delta to weight yet")
        assert_equal(um1["session_volume"], 1_000_000, "session_volume reflects raw dayVolume")
        assert_equal(um1["symbol"], "QQQ", "symbol is always QQQ today")

        ts_utc_2 = ts_utc + timedelta(minutes=1)
        um2 = collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts_utc_2.isoformat(), ts_et, ts_utc_2, today,
        )
        assert_equal(um2["vwap"], 402.0, "vwap after one delta is just the tick's own price")
        assert_true(um2["price_vs_vwap_abs"] is not None, "price_vs_vwap computed once vwap exists")

        # A new day resets the accumulator to a fresh, single-tick state.
        tomorrow = date(2026, 7, 31)
        um3 = collector._compute_underlying_market(s3, {"volume": 500}, 410.0, ts_utc.isoformat(), ts_et, ts_utc, tomorrow)
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

        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, ts_utc.isoformat(), ts_et, ts_utc, today)
        ts_utc_2 = ts_utc + timedelta(minutes=1)
        collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts_utc_2.isoformat(), ts_et, ts_utc_2, today,
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
        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, ts_utc.isoformat(), ts_et, ts_utc, yesterday)
        ts_utc_2 = ts_utc + timedelta(minutes=1)
        collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts_utc_2.isoformat(), ts_et, ts_utc_2, yesterday,
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
        um = collector._compute_underlying_market(s3, {"volume": 1000}, 400.0, old_ts, ts_et, now, today)
        assert_equal(um["freshness"], "stale", "an old spot_ts is reported as stale, not silently live")

        fresh_um = collector._compute_underlying_market(
            s3, {"volume": 1500}, 401.0, now.isoformat(), ts_et, now, today,
        )
        assert_equal(fresh_um["freshness"], "live", "a current spot_ts is reported as live")
    finally:
        _reset_module_state()


def test_resolve_underlying_spot_pairs_price_with_its_own_timestamp():
    # bid/ask present -> mid price, paired with bid/ask timestamps, not last_ts
    # (which could be stale/unrelated if only a Quote event, no Trade, fired).
    spot, ts = collector._resolve_underlying_spot(
        {"bid": 400.0, "ask": 400.2, "bid_ts": "2026-07-30T14:00:00+00:00",
         "ask_ts": "2026-07-30T14:00:01+00:00", "last_ts": "2026-07-29T10:00:00+00:00"},
        last_spot_price=None, last_spot_ts=None,
    )
    assert_equal(spot, 400.1, "mid of bid/ask")
    assert_equal(ts, "2026-07-30T14:00:01+00:00", "paired with bid/ask timestamps, not the unrelated last_ts")


def test_resolve_underlying_spot_last_price_paired_with_last_ts():
    spot, ts = collector._resolve_underlying_spot(
        {"last": 401.5, "last_ts": "2026-07-30T14:05:00+00:00"},
        last_spot_price=None, last_spot_ts=None,
    )
    assert_equal(spot, 401.5, "falls back to last")
    assert_equal(ts, "2026-07-30T14:05:00+00:00", "paired with last's own timestamp")


def test_resolve_underlying_spot_last_resort_fallback_paired_with_its_own_timestamp():
    # No DXLink data at all this cycle -- falls back to _last_spot, and must
    # use *that* fallback's own timestamp (which may be None, e.g. yfinance),
    # never a leftover DXLink timestamp from qqq's state.
    spot, ts = collector._resolve_underlying_spot(
        {"last_ts": "2026-07-29T09:00:00+00:00"},  # stale leftover DXLink timestamp, must not be used
        last_spot_price=405.25, last_spot_ts=None,  # yfinance fallback -- no trustworthy timestamp
    )
    assert_equal(spot, 405.25, "uses the fallback price")
    assert_equal(ts, None, "paired with the fallback's own (missing) timestamp, not qqq's stale last_ts")

    spot2, ts2 = collector._resolve_underlying_spot(
        {}, last_spot_price=406.0, last_spot_ts="2026-07-30T13:00:00+00:00",  # CSV-restored fallback, has a real ts
    )
    assert_equal(spot2, 406.0, "uses the fallback price")
    assert_equal(ts2, "2026-07-30T13:00:00+00:00", "uses the fallback's own real timestamp when it has one")


def test_resolve_underlying_spot_nothing_available():
    spot, ts = collector._resolve_underlying_spot({}, last_spot_price=None, last_spot_ts=None)
    assert_equal(spot, None, "no spot available anywhere")
    assert_equal(ts, None, "no timestamp available anywhere")


def test_accumulate_vwap_rejects_out_of_order_provider_events():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        ts_et = collector.ET.localize(datetime(2026, 7, 30, 10, 30, 0))
        t1 = datetime(2026, 7, 30, 14, 30, 0, tzinfo=timezone.utc)
        t0_late_arrival = t1 - timedelta(minutes=5)  # an event that arrives after t1 but is timestamped earlier
        s3 = FakeS3()

        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, t1.isoformat(), ts_et, t1, today)
        collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, (t1 + timedelta(minutes=1)).isoformat(), ts_et, t1, today,
        )
        vwap_after_in_order = collector._vwap_state.vwap
        assert_true(vwap_after_in_order is not None, "vwap accumulated from two in-order ticks")

        # A late-arriving event timestamped *before* the last one folded in
        # must be ignored, not accumulated as if it were new information.
        collector._compute_underlying_market(
            s3, {"volume": 5_000_000}, 999.0, t0_late_arrival.isoformat(), ts_et, t1, today,
        )
        assert_equal(collector._vwap_state.vwap, vwap_after_in_order, "out-of-order event does not perturb the accumulator")
    finally:
        _reset_module_state()


def test_underlying_market_flags_partial_session_after_a_late_start():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        # Session opens at PREMARKET_HOUR (06:00 ET); accumulation doesn't
        # start until 10:30 ET -- well past the session's true open.
        late_start_et = collector.ET.localize(datetime(2026, 7, 30, 10, 30, 0))
        late_start_utc = late_start_et.astimezone(timezone.utc)
        s3 = FakeS3()

        um1 = collector._compute_underlying_market(
            s3, {"volume": 1_000_000}, 400.0, late_start_utc.isoformat(), late_start_et, late_start_utc, today,
        )
        assert_equal(um1["vwap_partial_session"], None, "no accumulation yet at all -- None, not True/False")

        ts2 = late_start_utc + timedelta(minutes=1)
        um2 = collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts2.isoformat(), late_start_et, ts2, today,
        )
        assert_equal(um2["vwap_partial_session"], True, "accumulation started ~4.5 hours after the session's true open")
        assert_true(um2["vwap_session_started_at"] is not None, "session_started_at is surfaced in the payload")
    finally:
        _reset_module_state()


def test_underlying_market_partial_session_false_when_started_at_open():
    _reset_module_state()
    try:
        today = date(2026, 7, 30)
        open_et = collector.ET.localize(datetime(2026, 7, 30, 6, 1, 0))  # one minute after PREMARKET_HOUR
        open_utc = open_et.astimezone(timezone.utc)
        s3 = FakeS3()

        collector._compute_underlying_market(s3, {"volume": 1_000_000}, 400.0, open_utc.isoformat(), open_et, open_utc, today)
        ts2 = open_utc + timedelta(minutes=1)
        um2 = collector._compute_underlying_market(
            s3, {"volume": 1_100_000}, 402.0, ts2.isoformat(), open_et, ts2, today,
        )
        assert_equal(um2["vwap_partial_session"], False, "accumulation started right at session open -- not partial")
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
        test_resolve_underlying_spot_pairs_price_with_its_own_timestamp,
        test_resolve_underlying_spot_last_price_paired_with_last_ts,
        test_resolve_underlying_spot_last_resort_fallback_paired_with_its_own_timestamp,
        test_resolve_underlying_spot_nothing_available,
        test_accumulate_vwap_rejects_out_of_order_provider_events,
        test_underlying_market_flags_partial_session_after_a_late_start,
        test_underlying_market_partial_session_false_when_started_at_open,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    run()
