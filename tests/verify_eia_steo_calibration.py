"""Prove collector.py's EIA STEO vintage-log I/O: a transient R2 read
failure must never be treated as "no history" (which would destroy the real
log on the next write), and a new vintage entry must only be minted when
the fetched data actually changed, not just because the calendar month
rolled over.

Mirrors tests/verify_collector_vwap_rvol.py's conventions (import
collector.py directly, a local FakeS3, assert_equal/assert_true, run()) --
no network, no EIA API key, no real R2 access. Uses its own FakeS3 (not the
shared one in verify_collector_vwap_rvol.py) because this needs an
`exceptions.NoSuchKey` shape matching real boto3's, which the shared double
doesn't provide -- same "duplicate small test doubles rather than share"
convention this repo's strategies already follow for production code.

    python tests/verify_eia_steo_calibration.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collector  # noqa: E402


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


class NoSuchKey(Exception):
    pass


class _Exceptions:
    NoSuchKey = NoSuchKey


class FakeBody:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    """Boto3-shaped enough for load_steo_vintage_log/save_steo_vintage_log:
    a per-key byte store, `exceptions.NoSuchKey` for a genuinely missing
    key, and an optional `read_error` to simulate any *other* failure
    (network, auth, corrupt body) on the next get_object call.
    """

    exceptions = _Exceptions

    def __init__(self, seed: dict | None = None):
        self.store: dict[str, bytes] = dict(seed or {})
        self.put_calls: list[str] = []
        self.read_error: Exception | None = None

    def get_object(self, Bucket, Key):  # noqa: N803
        if self.read_error is not None:
            raise self.read_error
        if Key not in self.store:
            raise self.exceptions.NoSuchKey()
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        self.put_calls.append(Key)


class FrozenDatetime(datetime):
    """Stand-in for collector.datetime with a fixed `.now()`, so
    push_eia_steo's calendar-month release label is deterministic."""

    _frozen_now: datetime = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._frozen_now.astimezone(tz) if tz else cls._frozen_now


def _freeze(now: datetime):
    FrozenDatetime._frozen_now = now
    collector.datetime = FrozenDatetime


def _unfreeze():
    collector.datetime = datetime


BRENT = collector.cc.BRENT_SERIES_ID
WTI = collector.cc.WTI_SERIES_ID
BALANCE = collector.cc.BALANCE_SERIES_ID


def _rows(period: str, brent: float, wti: float, balance: float) -> list[dict]:
    return [
        {"period": period, "seriesId": BRENT, "value": str(brent)},
        {"period": period, "seriesId": WTI, "value": str(wti)},
        {"period": period, "seriesId": BALANCE, "value": str(balance)},
    ]


def test_load_returns_empty_on_genuinely_missing_key():
    s3 = FakeS3()  # nothing ever written -- a fresh deployment
    entries = collector.load_steo_vintage_log(s3)
    assert_equal(entries, [], "no log yet -- empty list, not an error")


def test_load_raises_on_a_real_read_failure_instead_of_returning_empty():
    s3 = FakeS3(seed={collector.EIA_STEO_LOG_KEY: json.dumps([{"release_period": "2026-06", "points": []}]).encode()})
    s3.read_error = RuntimeError("simulated transient R2 failure")
    try:
        collector.load_steo_vintage_log(s3)
        raise AssertionError("expected load_steo_vintage_log to raise, not swallow the read failure")
    except RuntimeError:
        pass  # the real bug: this used to return [] here instead


def test_transient_read_failure_never_destroys_the_real_log():
    real_log = [{"release_period": "2026-06", "points": [{"period": "2026-07", "brent": 80.0, "wti": 75.0, "balance": 1.0}]}]
    s3 = FakeS3(seed={collector.EIA_STEO_LOG_KEY: json.dumps(real_log).encode()})
    s3.read_error = RuntimeError("simulated transient R2 failure")

    original_fetch = collector.fetch_eia_steo_rows
    collector.fetch_eia_steo_rows = lambda api_key: _rows("2026-08", 82.0, 77.0, 2.0)
    _freeze(datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))
    try:
        try:
            collector.push_eia_steo(s3)
            raise AssertionError("expected push_eia_steo to raise on a transient read failure")
        except RuntimeError:
            pass
        assert_equal(
            json.loads(s3.store[collector.EIA_STEO_LOG_KEY]), real_log,
            "the real log in R2 must be untouched -- the old bug would have overwritten it with a single-entry stub",
        )
        assert_true(collector.EIA_STEO_LOG_KEY not in s3.put_calls, "no write was ever attempted against the log key")
    finally:
        collector.fetch_eia_steo_rows = original_fetch
        _unfreeze()


def test_unchanged_data_does_not_mint_a_phantom_calendar_release():
    prior_points = [{"period": "2026-08", "brent": 80.0, "wti": 75.0, "balance": 1.0}]
    real_log = [{"release_period": "2026-07", "points": prior_points}]
    s3 = FakeS3(seed={collector.EIA_STEO_LOG_KEY: json.dumps(real_log).encode()})

    original_fetch = collector.fetch_eia_steo_rows
    # Same data EIA would still be serving if July's STEO hasn't actually
    # been superseded yet -- but the calendar has already rolled to August.
    collector.fetch_eia_steo_rows = lambda api_key: _rows("2026-08", 80.0, 75.0, 1.0)
    _freeze(datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))
    try:
        collector.push_eia_steo(s3)
        logged = json.loads(s3.store[collector.EIA_STEO_LOG_KEY])
        assert_equal(len(logged), 1, "no new entry minted -- data was unchanged")
        assert_equal(logged[0]["release_period"], "2026-07", "kept the real prior label, not a phantom 2026-08")
        assert_true(collector.EIA_STEO_LOG_KEY not in s3.put_calls, "unchanged data -- no write to the log at all")

        steo_payload = json.loads(s3.store[collector.EIA_STEO_KEY])
        assert_equal(steo_payload["current_release"], "2026-07", "the display feed also reports the real label")
    finally:
        collector.fetch_eia_steo_rows = original_fetch
        _unfreeze()


def test_genuinely_new_data_still_mints_a_new_release_normally():
    prior_points = [{"period": "2026-08", "brent": 80.0, "wti": 75.0, "balance": 1.0}]
    real_log = [{"release_period": "2026-07", "points": prior_points}]
    s3 = FakeS3(seed={collector.EIA_STEO_LOG_KEY: json.dumps(real_log).encode()})

    original_fetch = collector.fetch_eia_steo_rows
    # Genuinely revised numbers -- a real new STEO release.
    collector.fetch_eia_steo_rows = lambda api_key: _rows("2026-08", 85.0, 79.0, -2.0)
    _freeze(datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc))
    try:
        collector.push_eia_steo(s3)
        logged = json.loads(s3.store[collector.EIA_STEO_LOG_KEY])
        assert_equal(len(logged), 2, "a real new vintage was appended")
        assert_equal(logged[-1]["release_period"], "2026-08", "labeled with the current calendar month")
        assert_true(collector.EIA_STEO_LOG_KEY in s3.put_calls, "changed data -- the log was written")

        steo_payload = json.loads(s3.store[collector.EIA_STEO_KEY])
        assert_equal(steo_payload["prior_release"], "2026-07", "revision computed against the real prior vintage")
        assert_true(len(steo_payload["revisions"]) > 0, "a revision was computed for the changed period")
    finally:
        collector.fetch_eia_steo_rows = original_fetch
        _unfreeze()


class FakeResponse:
    """Just enough of a requests.Response to exercise fetch_eia_steo_rows:
    raise_for_status() + json()."""

    def __init__(self, data: list[dict], total: int | str | None):
        self._data = data
        self._total = total

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        payload = {"data": self._data}
        if self._total is not None:
            payload["total"] = self._total
        return {"response": payload}


def test_fetch_eia_steo_rows_raises_when_server_reports_more_than_returned():
    print("\nRegression: a truncated EIA response (server total > rows actually returned) raises, not silently used")
    original_get = collector.requests.get
    # total=200 but only 5 rows actually came back -- exactly the shape a
    # too-small `length` produced before this fix (length=36 capped total
    # rows across all 3 series combined, not periods-per-series).
    collector.requests.get = lambda *a, **kw: FakeResponse(data=[{"period": "2026-08"}] * 5, total=200)
    try:
        try:
            collector.fetch_eia_steo_rows("fake-key")
            raise AssertionError("expected fetch_eia_steo_rows to raise on a truncated response")
        except RuntimeError as e:
            assert_true("truncated" in str(e).lower(), str(e))
    finally:
        collector.requests.get = original_get


def test_fetch_eia_steo_rows_accepts_a_complete_response():
    print("\nRegression guard: a complete response (total == rows returned) is accepted normally")
    original_get = collector.requests.get
    rows = [{"period": "2026-08"}] * 12
    collector.requests.get = lambda *a, **kw: FakeResponse(data=rows, total=12)
    try:
        result = collector.fetch_eia_steo_rows("fake-key")
        assert_equal(len(result), 12, "all rows returned, none dropped")
    finally:
        collector.requests.get = original_get


def test_fetch_eia_steo_rows_degrades_gracefully_without_a_total_field():
    print("\nRegression guard: a response with no 'total' field (unexpected schema) still returns data, doesn't crash")
    original_get = collector.requests.get
    collector.requests.get = lambda *a, **kw: FakeResponse(data=[{"period": "2026-08"}], total=None)
    try:
        result = collector.fetch_eia_steo_rows("fake-key")
        assert_equal(len(result), 1, "returns what it got -- can't verify completeness, but doesn't fail closed")
    finally:
        collector.requests.get = original_get


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
