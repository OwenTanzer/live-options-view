"""Prove `restore_state` selects the newest snapshot from a realistic day
prefix, not the newest key of S3's first list page.

WHY this suite exists: one minute snapshot writes two objects (its CSV plus
its `snapshot_provenance.v1` sidecar), so a QQQ session runs to roughly 1,800
keys under `intraday/YYYYMMDD/`. `list_objects_v2` caps a page at 1,000 keys
and returns them in ascending order, so the pre-repair call saw only the
morning and `sorted(...)[-1]` restored from a mid-morning snapshot -- while
reporting a successful recovery. The fixture below builds a prefix past that
boundary, witnesses what the old selection would have chosen on the same
data, and then checks the repaired path.

Same conventions as tests/verify_collector_spy.py (import the collector
package, in-memory fake S3, no tastytrade/DXLink/R2 access).

    python tests/verify_collector_restore_paging.py
"""

import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import market_data.live_market as collector  # noqa: E402
from market_data.live_market.config import ET  # noqa: E402
from market_data.live_market.state import (  # noqa: E402
    _latest_snapshot_key,
    _list_prefix,
    _snapshot_digits,
    _snapshot_order,
)

TODAY = date(2026, 10, 6)
DAY = TODAY.strftime("%Y%m%d")
PREFIX = f"intraday/{DAY}/"

# 620 snapshots x (CSV + provenance sidecar) + first.csv + vwap_state.json +
# momentum_log.jsonl = 1,244 objects: comfortably past S3's 1,000-key page, and
# shaped the way a real session is -- mostly CSV/sidecar pairs, a few singletons.
SNAPSHOT_COUNT = 620
FIRST_SNAPSHOT_SECOND = 9 * 3600 + 30 * 60      # 09:30:00 ET
SNAPSHOT_STEP_SECS = 37                          # 620 * 37s = 15:51:43 ET, inside RTH
LAST_SNAPSHOT_SECOND = FIRST_SNAPSHOT_SECOND + SNAPSHOT_STEP_SECS * (SNAPSHOT_COUNT - 1)
VWAP_CUM_VOL = 4321


class FakeBody:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw


class PagedFakeS3:
    """In-memory R2 stub with real `list_objects_v2` paging semantics.

    Keys come back in ascending order, at most `MaxKeys` (default 1,000) per
    response, with `IsTruncated` and `NextContinuationToken` set when more
    remain. A caller that ignores the continuation token therefore sees exactly
    what S3 would give it: the oldest page.
    """

    DEFAULT_MAX_KEYS = 1000

    def __init__(self, seed: dict | None = None, max_keys: int | None = None):
        self.store: dict[str, bytes] = {}
        for key, value in (seed or {}).items():
            self.store[key] = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.writes: list[str] = []
        self.list_calls: list[dict] = []
        self.max_keys = max_keys or self.DEFAULT_MAX_KEYS
        self.truncate_without_token = False

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise RuntimeError(f"missing key: {Key}")
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        self.writes.append(Key)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, **kwargs):  # noqa: N803
        self.list_calls.append({"Prefix": Prefix, "ContinuationToken": ContinuationToken})
        matching = sorted(key for key in self.store if key.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = matching[start:start + self.max_keys]
        response = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": start + self.max_keys < len(matching),
        }
        if self.truncate_without_token:
            response["IsTruncated"] = True
            response["NextContinuationToken"] = None
        elif response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + self.max_keys)
        return response

    def page(self, prefix: str) -> list[str]:
        """The keys one unpaginated call would have returned."""
        self.list_calls.clear()
        return [item["Key"] for item in
                self.list_objects_v2(Bucket="b", Prefix=prefix)["Contents"]]


def _snapshot_time(index: int) -> str:
    seconds = FIRST_SNAPSHOT_SECOND + SNAPSHOT_STEP_SECS * index
    return f"{seconds // 3600:02d}{(seconds % 3600) // 60:02d}{seconds % 60:02d}000000"


def _expected_iso(index: int) -> str:
    seconds = FIRST_SNAPSHOT_SECOND + SNAPSHOT_STEP_SECS * index
    moment = datetime(TODAY.year, TODAY.month, TODAY.day,
                      seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return ET.localize(moment).astimezone(timezone.utc).isoformat()


def _snapshot_csv(index: int, root: str = "QQQ", strike_digits: str = "00600000") -> bytes:
    rows = [{
        "TradeDate": TODAY.isoformat(),
        "Expiration": TODAY.isoformat(),
        "Strike": int(strike_digits) / 1000,
        "Type": "call",
        "OptionSymbol": f"{root:<6}261006C{strike_digits}",
        "DTE": 0,
        "OpenInterest": 10,
        "Volume": index + 1,                       # identifies which CSV was read
        "VolDelta": 1,
        "Bid": 1.0,
        "Mid": 1.5,
        "Ask": 2.0,
        "Last": 1.5,
        "UnderlyingPrice": round(600 + (index + 1) / 100, 2),
    }]
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue().encode()


def _session_day(max_keys: int | None = None) -> PagedFakeS3:
    """A full session's day prefix, as the collector actually writes it."""
    store: dict[str, bytes] = {}
    for index in range(SNAPSHOT_COUNT):
        base = f"{PREFIX}snapshot_{_snapshot_time(index)}"
        store[f"{base}.csv"] = _snapshot_csv(index)
        store[f"{base}.provenance.json"] = json.dumps({
            "schema_version": "snapshot_provenance.v1",
            "snapshot_key": f"{base}.csv",
            "symbol": "QQQ",
            "publication": {"status": "published", "reason": None},
        }).encode()
    store[f"{PREFIX}first.csv"] = _snapshot_csv(0)
    store[f"{PREFIX}vwap_state.json"] = json.dumps({
        "symbol": "QQQ",
        "session_date": TODAY.isoformat(),
        "session_started_at": None,
        "last_observed_at": None,
        "cum_pv": VWAP_CUM_VOL * 601.0,
        "cum_vol": VWAP_CUM_VOL,
        "last_dayvolume": 900000,
        "vwap": 601.0,
        "vwap_ts": None,
    }).encode()
    store[f"{PREFIX}momentum_log.jsonl"] = b'{"status":"warming_up"}\n'
    return PagedFakeS3(store, max_keys=max_keys)


def _fresh_qqq():
    session = collector.UnderlyingSession("QQQ")
    assert session.archive_root == "intraday", session.archive_root
    return session


def test_the_fixture_actually_exceeds_one_list_page():
    s3 = _session_day()
    objects = [key for key in s3.store if key.startswith(PREFIX)]
    assert len(objects) == SNAPSHOT_COUNT * 2 + 3, len(objects)
    assert len(objects) > 1000, "fixture must cross S3's page boundary"
    csvs = [key for key in objects if key.endswith(".csv")]
    sidecars = [key for key in objects if key.endswith(".provenance.json")]
    assert (len(csvs), len(sidecars)) == (SNAPSHOT_COUNT + 1, SNAPSHOT_COUNT), (len(csvs), len(sidecars))


def test_unpaginated_page_one_is_the_oldest_keys_and_did_miss_the_latest():
    """The failing-before witness, on the repaired fixture's own data."""
    s3 = _session_day()
    page_one = s3.page(PREFIX)
    assert len(page_one) == 1000, len(page_one)

    newest = f"{PREFIX}snapshot_{_snapshot_time(SNAPSHOT_COUNT - 1)}.csv"
    assert newest not in page_one, "the newest snapshot is outside the first page"

    # Exactly the pre-repair expression: one list call, filter .csv, take last.
    old_selection = sorted([key for key in page_one if key.endswith(".csv")])[-1]
    assert old_selection != newest, "old selection must not reach the newest snapshot"
    assert _snapshot_order(old_selection.rsplit("/", 1)[-1]) < _snapshot_order(newest.rsplit("/", 1)[-1]), \
        "old selection recovers an older snapshot, silently"


def test_paginated_listing_returns_every_key_once():
    s3 = _session_day()
    listed = _list_prefix(s3, PREFIX)
    assert len(listed) == len(set(listed)), "no key repeated across pages"
    assert len(listed) == SNAPSHOT_COUNT * 2 + 3, len(listed)
    assert len(s3.list_calls) >= 2, s3.list_calls
    assert s3.list_calls[0]["ContinuationToken"] is None
    assert all(call["ContinuationToken"] for call in s3.list_calls[1:]), s3.list_calls


def test_latest_selection_ignores_sidecars_singletons_and_stray_csvs():
    s3 = _session_day()
    s3.store[f"{PREFIX}zzz_export.csv"] = b"a,b\n1,2\n"
    listed = _list_prefix(s3, PREFIX)
    newest = f"{PREFIX}snapshot_{_snapshot_time(SNAPSHOT_COUNT - 1)}.csv"
    assert _latest_snapshot_key(listed, PREFIX) == newest
    assert _latest_snapshot_key([f"{PREFIX}first.csv"], PREFIX) is None, \
        "first.csv alone is not a restore source"
    assert _latest_snapshot_key([], PREFIX) is None


def test_restore_state_recovers_from_the_newest_snapshot_not_the_first_page():
    s3 = _session_day()
    qqq = _fresh_qqq()
    collector.restore_state(s3, TODAY, qqq)
    assert qqq.prev_vol == {".QQQ261006C600": SNAPSHOT_COUNT}, qqq.prev_vol
    assert qqq.last_spot[0] == round(600 + SNAPSHOT_COUNT / 100, 2), qqq.last_spot
    assert qqq.last_spot[1] == _expected_iso(SNAPSHOT_COUNT - 1), qqq.last_spot[1]
    assert qqq.first_snapshot_written, "prior snapshots mean first.csv already exists"
    assert qqq.vwap_state.cum_vol == VWAP_CUM_VOL, qqq.vwap_state.cum_vol
    assert qqq.vwap_state.vwap == 601.0, qqq.vwap_state.vwap


def test_restore_uses_the_snapshot_own_capture_time_not_wall_clock():
    s3 = _session_day()
    qqq = _fresh_qqq()
    collector.restore_state(s3, TODAY, qqq)
    expected_second = LAST_SNAPSHOT_SECOND
    recovered = datetime.fromisoformat(qqq.last_spot[1])
    assert recovered.utcoffset().total_seconds() == 0, "recovered timestamp is UTC"
    recovered_et = recovered.astimezone(ET)
    assert (recovered_et.hour * 3600 + recovered_et.minute * 60 + recovered_et.second
            == expected_second), qqq.last_spot


def test_other_days_and_other_symbols_cannot_seed_the_session():
    s3 = _session_day()
    other_day = "intraday/20261005/"
    other_day_key = f"{other_day}snapshot_235959000000.csv"
    s3.store[other_day_key] = _snapshot_csv(999)
    spy_prefix = f"intraday/spy/{DAY}/"
    s3.store[f"{spy_prefix}snapshot_235959000000.csv"] = _snapshot_csv(
        998, root="SPY", strike_digits="00670000")
    s3.store[f"{spy_prefix}vwap_state.json"] = json.dumps({
        "symbol": "SPY", "session_date": TODAY.isoformat(), "cum_vol": 55, "vwap": 670.0,
    }).encode()

    qqq = _fresh_qqq()
    collector.restore_state(s3, TODAY, qqq)
    assert qqq.prev_vol == {".QQQ261006C600": SNAPSHOT_COUNT}, "no foreign-day or SPY row"
    assert qqq.vwap_state.cum_vol == VWAP_CUM_VOL, "QQQ accumulator only"

    spy = collector.UnderlyingSession("SPY")
    collector.restore_state(s3, TODAY, spy)
    assert spy.prev_vol == {".SPY261006C670": 999}, "SPY reads its own prefix"
    assert spy.vwap_state.cum_vol == 55, "SPY recovers its own accumulator"


def test_foreign_symbol_vwap_state_is_not_restored():
    s3 = _session_day()
    s3.store[f"{PREFIX}vwap_state.json"] = json.dumps({
        "symbol": "SPY", "session_date": TODAY.isoformat(), "cum_vol": 777, "vwap": 670.0,
    }).encode()
    qqq = _fresh_qqq()
    collector.restore_state(s3, TODAY, qqq)
    assert qqq.prev_vol == {".QQQ261006C600": SNAPSHOT_COUNT}, "CSV still restores volume"
    assert qqq.vwap_state.cum_vol == 0, "a foreign-symbol accumulator is refused, not adopted"


def test_truncated_listing_without_a_token_fails_closed():
    s3 = _session_day()
    s3.truncate_without_token = True
    listed_prefix_raised = False
    try:
        _list_prefix(s3, PREFIX)
    except RuntimeError as exc:
        listed_prefix_raised = "continuation token" in str(exc)
    assert listed_prefix_raised, "an unpageable listing is an error, not a short list"

    qqq = _fresh_qqq()
    collector.restore_state(s3, TODAY, qqq)
    assert qqq.prev_vol == {}, "no partial restore from an incomplete listing"
    assert qqq.last_spot == [None, None], "no spot from an incomplete listing"
    assert not qqq.first_snapshot_written, "clean miss leaves the session fresh"


def test_page_cap_exhaustion_fails_closed():
    s3 = _session_day(max_keys=10)
    exceeded = False
    try:
        _list_prefix(s3, PREFIX, max_pages=3)
    except RuntimeError as exc:
        exceeded = "exceeded 3 pages" in str(exc)
    assert exceeded, "the page cap stops a runaway listing loudly"
    assert _list_prefix(s3, PREFIX, max_pages=200) == _list_prefix(_session_day(), PREFIX), \
        "a larger cap returns the same keys"


def test_snapshot_ordering_handles_every_accepted_timestamp_width():
    assert _snapshot_order("snapshot_1200.csv") == _snapshot_order("snapshot_120000000000.csv")
    assert _snapshot_order("snapshot_0930.csv") < _snapshot_order("snapshot_1200.csv")
    assert _snapshot_order("snapshot_093000.csv") < _snapshot_order("snapshot_093001.csv")
    assert _snapshot_digits("snapshot_1200.csv") == "120000000000"
    assert _snapshot_digits("first.csv") is None
    assert _snapshot_digits("snapshot_1200.provenance.json") is None
    # Only the three widths the producers and the crassus reader use. A
    # five- or nine-digit name is nobody's output and must not be ordered as if
    # it were a capture time.
    for stray in ("snapshot_12345.csv", "snapshot_1234567.csv", "snapshot_.csv",
                  "snapshot_12345678901.csv", "snapshot_1234567890123.csv"):
        assert _snapshot_digits(stray) is None, stray
        assert not _latest_snapshot_key([f"{PREFIX}{stray}"], PREFIX), stray


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} restore-paging checks passed.")


if __name__ == "__main__":
    run()
