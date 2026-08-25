"""Focused offline verification for MOO-149 weekly-expiry collection."""

import csv
import io
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collector


class FakeBody:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload


class MissingKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, manifest=None):
        self.store = {}
        self.put_calls = []
        if manifest is not None:
            self.store["manifest.json"] = json.dumps(manifest).encode()

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise MissingKey(Key)
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else Body.encode()
        self.put_calls.append(Key)

    def list_objects_v2(self, Bucket, Prefix):  # noqa: N803
        return {
            "Contents": [
                {"Key": key} for key in sorted(self.store) if key.startswith(Prefix)
            ]
        }


class FakeFeed:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return {key: dict(value) for key, value in self.state.items()}


class FakeResponse:
    def __init__(self, expirations):
        self.expirations = expirations

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"items": [{"expirations": self.expirations}]}}


def _weekday_calendar(start: date, end: date, holidays=()):
    holidays = set(holidays)
    days = set()
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            days.add(current)
        current += timedelta(days=1)
    return days


def _expiration(exp_date: str, strike: int):
    compact = exp_date.replace("-", "")[2:]
    return {
        "expiration-date": exp_date,
        "strikes": [{
            "strike-price": str(strike),
            "call": {"symbol": f"QQQ   {compact}C{strike * 1000:08d}"},
            "put": {"symbol": f"QQQ   {compact}P{strike * 1000:08d}"},
        }],
    }


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def test_nearest_weekly_selection_matches_pipeline_behavior():
    normal_days = _weekday_calendar(date(2026, 8, 24), date(2026, 8, 28))
    assert_equal(
        collector.nearest_weekly_expiration(date(2026, 8, 25), normal_days),
        date(2026, 8, 28),
        "normal Friday EoW",
    )

    holiday_days = _weekday_calendar(
        date(2026, 6, 29), date(2026, 7, 3), holidays={date(2026, 7, 3)}
    )
    assert_equal(
        collector.nearest_weekly_expiration(date(2026, 7, 2), holiday_days),
        date(2026, 7, 2),
        "holiday-shortened EoW",
    )


def test_chain_load_selects_current_and_actual_weekly_expiration():
    original_get = collector.requests.get
    original_calendar = collector._load_calendar
    expirations = [
        _expiration("2026-08-26", 700),
        _expiration("2026-08-28", 700),
    ]
    collector.requests.get = lambda *args, **kwargs: FakeResponse(expirations)
    collector._load_calendar = lambda: _weekday_calendar(
        date(2026, 8, 24), date(2026, 8, 28)
    )
    try:
        current, current_exp, weekly, weekly_exp = collector.load_chain(
            "session-token", date(2026, 8, 26)
        )
        assert_equal(current_exp, "2026-08-26", "current expiration")
        assert_equal(weekly_exp, "2026-08-28", "weekly expiration")
        assert_equal(current[0]["strike"], 700.0, "current strike parsed")
        assert_equal(weekly[0]["strike"], 700.0, "weekly strike parsed")
    finally:
        collector.requests.get = original_get
        collector._load_calendar = original_calendar


def test_missing_weekly_chain_does_not_break_current_expiration():
    original_get = collector.requests.get
    original_calendar = collector._load_calendar
    collector.requests.get = lambda *args, **kwargs: FakeResponse([
        _expiration("2026-08-26", 700),
    ])
    collector._load_calendar = lambda: _weekday_calendar(
        date(2026, 8, 24), date(2026, 8, 28)
    )
    try:
        current, current_exp, weekly, weekly_exp = collector.load_chain(
            "session-token", date(2026, 8, 26)
        )
        assert_equal(current_exp, "2026-08-26", "current expiration preserved")
        assert_equal(len(current), 1, "current chain preserved")
        assert_equal(weekly_exp, "2026-08-28", "intended weekly expiration reported")
        assert_equal(weekly, [], "weekly collection disabled only")
    finally:
        collector.requests.get = original_get
        collector._load_calendar = original_calendar


def test_hourly_regular_session_slots():
    et = collector.ET
    day = date(2026, 8, 25)

    def at(hour, minute):
        return et.localize(datetime(day.year, day.month, day.day, hour, minute))

    assert_equal(collector._weekly_snapshot_slot(at(9, 29)), None, "before open")
    assert_equal(collector._weekly_snapshot_slot(at(9, 30)), at(9, 30), "open slot")
    assert_equal(collector._weekly_snapshot_slot(at(10, 29)), at(9, 30), "same hour")
    assert_equal(collector._weekly_snapshot_slot(at(10, 30)), at(10, 30), "next hour")
    assert_equal(collector._weekly_snapshot_slot(at(15, 59)), at(15, 30), "last slot")
    assert_equal(collector._weekly_snapshot_slot(at(16, 0)), None, "at close")
    assert_equal(collector.SNAPSHOT_SECS, 60, "0DTE cadence unchanged")


def test_weekly_csv_manifest_and_restart_idempotency():
    trade_date = date(2026, 8, 25)
    expiration = "2026-08-28"
    observed = collector.ET.localize(datetime(2026, 8, 25, 10, 31, 2, 123456))
    call_sym = ".QQQ260828C700"
    put_sym = ".QQQ260828P700"
    strikes = [{
        "strike": 700.0,
        "call_sym": call_sym,
        "put_sym": put_sym,
        "call_occ": "QQQ260828C00700000",
        "put_occ": "QQQ260828P00700000",
    }]
    feed = FakeFeed({
        "QQQ": {"bid": 699.9, "ask": 700.1, "last": 700.0},
        call_sym: {
            "bid": 5.0, "ask": 5.2, "last": 5.1, "oi": 123, "volume": 12,
            "volatility": 0.2, "delta": 0.55, "gamma": 0.03,
            "theta": -0.4, "vega": 0.1,
        },
        put_sym: {
            "bid": 4.8, "ask": 5.0, "last": 4.9, "oi": 456, "volume": 34,
            "volatility": 0.21, "delta": -0.45, "gamma": 0.03,
            "theta": -0.4, "vega": 0.1,
        },
    })
    original_manifest = {
        "dates": ["2026-08-25"],
        "note": "authoritative pipeline metadata",
        "updated_at": "before",
    }
    s3 = FakeS3(original_manifest)

    key = collector.take_weekly_snapshot(
        s3, feed, strikes, expiration, trade_date, observed
    )
    expected_key = (
        "raw/weekly/20260828/qqq_chain_20260825_103102123456.csv"
    )
    assert_equal(key, expected_key, "expiry-dated weekly key")

    rows = list(csv.DictReader(io.StringIO(s3.store[key].decode())))
    assert_equal(list(rows[0]), collector.PIPELINE_CHAIN_COLUMNS, "pipeline CSV columns")
    assert_equal(len(rows), 2, "call and put rows")
    assert_equal(rows[0]["TradeDate"], "2026-08-25", "trade date")
    assert_equal(rows[0]["Expiration"], expiration, "actual expiration")
    assert_equal(rows[0]["DTE"], "3", "aging DTE")
    assert_equal(rows[0]["OpenInterest"], "123", "settled OI passthrough")

    manifest = json.loads(s3.store["manifest.json"])
    assert_equal(manifest["dates"], original_manifest["dates"], "dates preserved")
    assert_equal(manifest["note"], original_manifest["note"], "metadata preserved")
    assert_equal(
        manifest["weekly_expirations"][expiration], [expected_key],
        "weekly snapshot indexed by actual expiration",
    )

    second = collector.take_weekly_snapshot(
        s3, feed, strikes, expiration, trade_date,
        collector.ET.localize(datetime(2026, 8, 25, 10, 45)),
    )
    assert_equal(second, expected_key, "restart reuses same slot")
    weekly_csv_puts = [item for item in s3.put_calls if item.startswith("raw/weekly/")]
    assert_equal(len(weekly_csv_puts), 1, "one CSV per hourly slot")


def run():
    tests = [
        test_nearest_weekly_selection_matches_pipeline_behavior,
        test_chain_load_selects_current_and_actual_weekly_expiration,
        test_missing_weekly_chain_does_not_break_current_expiration,
        test_hourly_regular_session_slots,
        test_weekly_csv_manifest_and_restart_idempotency,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} weekly collection checks passed.")


if __name__ == "__main__":
    run()
