import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from panel import (
    is_regular_hours,
    load_raw_snapshots,
    parse_snapshot_key,
    recompute_interval_volume,
)
from r2_source import SnapshotSource


def test_parse_snapshot_key_is_eastern_time():
    yyyymmdd, ts_et, ts_utc = parse_snapshot_key("intraday/20260910/snapshot_060002893036.csv")
    assert yyyymmdd == "20260910"
    assert (ts_et.hour, ts_et.minute, ts_et.second) == (6, 0, 2)
    assert ts_et.microsecond == 893036
    assert str(ts_et.tzinfo) == "America/New_York"
    # Early Sept is EDT (UTC-4): 06:00 ET -> 10:00 UTC.
    assert ts_utc.hour == 10


def test_is_regular_hours_boundaries():
    day = pd.Timestamp(2026, 9, 10, tz="America/New_York")
    assert not is_regular_hours(day.replace(hour=9, minute=29, second=59))
    assert is_regular_hours(day.replace(hour=9, minute=30, second=0))
    assert is_regular_hours(day.replace(hour=15, minute=59, second=59))
    assert not is_regular_hours(day.replace(hour=16, minute=0, second=0))


class FakeSource(SnapshotSource):
    """In-memory SnapshotSource for tests -- never touches R2 or disk cache."""

    def __init__(self, sessions: dict[str, list[tuple[str, list[dict]]]]):
        # sessions: {yyyymmdd: [(hhmmssffffff, [row, ...]), ...]}
        self.sessions = sessions
        self.bucket = "test"
        self.cache_dir = Path("unused")

    def list_all_objects(self, prefix):
        yyyymmdd = prefix.strip("/").split("/")[1]
        return [
            {"key": f"intraday/{yyyymmdd}/snapshot_{t}.csv", "size": 1, "etag": "x"}
            for t, _ in self.sessions.get(yyyymmdd, [])
        ]

    def snapshot_objects(self, yyyymmdd):
        return self.list_all_objects(f"intraday/{yyyymmdd}/")

    def snapshot_csv_rows(self, key):
        yyyymmdd = key.split("/")[1]
        hhmmssffffff = key.split("/")[2].removeprefix("snapshot_").removesuffix(".csv")
        for t, rows in self.sessions[yyyymmdd]:
            if t == hhmmssffffff:
                return rows
        return []


def make_row(symbol="QQQ260910C00700000", strike="700.0", opt_type="call",
             oi="10", volume="0", bid="1.0", ask="1.2", last="", spot="700.0",
             delta="0.5", gamma="0.01", theta="-0.01", vega="0.02"):
    return {
        "TradeDate": "2026-09-10", "Expiration": "2026-09-10", "Strike": strike,
        "Type": opt_type, "OptionSymbol": symbol, "DTE": "0",
        "OpenInterest": oi, "Volume": volume, "VolDelta": "0",
        "Bid": bid, "Mid": str((float(bid) + float(ask)) / 2), "Ask": ask, "Last": last,
        "IV": "0.4", "Delta": delta, "Gamma": gamma, "Theta": theta, "Vega": vega,
        "UnderlyingPrice": spot,
    }


def test_load_raw_snapshots_dedupes_spot_and_builds_audit():
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(volume="0"), make_row(symbol="QQQ260910P00700000", opt_type="put", volume="0")]),
            ("093100000000", [make_row(volume="5"), make_row(symbol="QQQ260910P00700000", opt_type="put", volume="2")]),
        ],
    })
    option_rows, spot_series, audits = load_raw_snapshots(source, ["20260910"])

    assert len(option_rows) == 4
    assert len(spot_series) == 2  # one spot row per snapshot, not per option row
    assert set(spot_series["underlying_price"]) == {700.0}

    audit = audits[0]
    assert audit.date == "20260910"
    assert audit.snapshot_count == 2
    assert audit.regular_hours_snapshots == 2
    assert audit.distinct_contracts == 2
    assert audit.distinct_strikes == 1
    assert audit.reported_count == 595


def test_recompute_interval_volume_basic_diff():
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(volume="10")]),
            ("093100000000", [make_row(volume="15")]),
        ],
    })
    option_rows, _, _ = load_raw_snapshots(source, ["20260910"])
    result = recompute_interval_volume(option_rows)

    first = result[result["ts_et"] == result["ts_et"].min()].iloc[0]
    second = result[result["ts_et"] == result["ts_et"].max()].iloc[0]
    assert first["dv_flag"] == "first_observation"
    assert pd.isna(first["dV"])
    assert second["dv_flag"] == "ok"
    assert second["dV"] == 5.0


def test_recompute_interval_volume_flags_decrease_as_reset():
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(volume="20")]),
            ("093100000000", [make_row(volume="5")]),  # decreased
        ],
    })
    option_rows, _, _ = load_raw_snapshots(source, ["20260910"])
    result = recompute_interval_volume(option_rows)
    second = result[result["ts_et"] == result["ts_et"].max()].iloc[0]
    assert second["dv_flag"] == "reset_or_decrease"
    assert pd.isna(second["dV"])


def test_recompute_interval_volume_flags_long_gap():
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(volume="10")]),
            ("093300000000", [make_row(volume="20")]),  # 180s later > 90s cap
        ],
    })
    option_rows, _, _ = load_raw_snapshots(source, ["20260910"])
    result = recompute_interval_volume(option_rows)
    second = result[result["ts_et"] == result["ts_et"].max()].iloc[0]
    assert second["dv_flag"] == "long_gap"
    assert pd.isna(second["dV"])


def test_recompute_interval_volume_flags_re_entry():
    """A contract absent from an intervening global snapshot (even though
    other contracts had one) must be flagged re_entry, not silently diffed
    across the gap in its own appearance."""
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(volume="10"), make_row(symbol="OTHER", volume="1")]),
            ("093100000000", [make_row(symbol="OTHER", volume="2")]),  # our contract missing here
            ("093200000000", [make_row(volume="30"), make_row(symbol="OTHER", volume="3")]),
        ],
    })
    option_rows, _, _ = load_raw_snapshots(source, ["20260910"])
    result = recompute_interval_volume(option_rows)
    target = result[result["OptionSymbol"] == "QQQ260910C00700000"].sort_values("ts_et")
    assert list(target["dv_flag"]) == ["first_observation", "re_entry"]
    assert target["dV"].isna().all()


def test_crossed_quote_and_negative_volume_flagged_in_audit():
    source = FakeSource({
        "20260910": [
            ("093000000000", [make_row(bid="2.0", ask="1.0", volume="-1")]),
        ],
    })
    _, _, audits = load_raw_snapshots(source, ["20260910"])
    audit = audits[0]
    assert audit.crossed_quotes == 1
    assert audit.negative_volumes == 1
