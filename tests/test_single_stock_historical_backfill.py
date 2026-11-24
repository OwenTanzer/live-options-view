"""Offline tests for the single-stock historical backfill collector.

No provider calls, no R2, no clock dependence beyond fixed fixtures::

    python -m pytest tests/test_single_stock_historical_backfill.py

Covers the embedded universe (101 listings), Tiingo bar parsing
(including the absent intraday volume field), Tiingo paging (the
10,000-bar cap, the backward ``endDate`` walk, overlap dedupe),
pacing budgets and checkpoint resume, resampling (including 9m
bucketing from 1-minute bars, 2h/4h same-day grouping, weekly volume
sums), the provenance sidecar shape, publish keys, and the
backfill/update flows with a fake Tiingo client.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.single_stock_historical_backfill import (  # noqa: E402
    resample as resample_mod,
)
from market_data.single_stock_historical_backfill.collector import (  # noqa: E402
    interval_file, read_symbol_files, run, ticker_dir,
)
from market_data.single_stock_historical_backfill.pacing import (  # noqa: E402
    CheckpointStore, RateLimiter,
)
from market_data.single_stock_historical_backfill.provenance import (  # noqa: E402
    build_provenance, interval_key, sidecar_key,
)
from market_data.single_stock_historical_backfill.publish import (  # noqa: E402
    publish_symbol, read_config,
)
from market_data.single_stock_historical_backfill.symbols import (  # noqa: E402
    DUAL_CLASS_PAIRS, EXPECTED_LISTINGS, SYMBOLS, names, symbols, validate,
)
from market_data.single_stock_historical_backfill.tiingo import (  # noqa: E402
    BASE_SPECS, PAGE_CAP, TiingoClient, TiingoNotFound, TiingoRateLimited,
    parse_daily_bar, parse_intraday_bar,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday, 10:00 ET


# --- fake Tiingo -----------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeTiingoSession:
    """Emulates Tiingo paging semantics (verified live 2026-11-23).

    ``bars_by_key`` maps ``(kind, ticker)`` to raw bars in ascending
    time order. A request returns the newest 10,000 bars in the
    [startDate, endDate] window — never the oldest.
    """

    def __init__(self, bars_by_key):
        self.bars_by_key = bars_by_key
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None, allow_redirects=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "/iex/" in url:
            kind, ticker = "intraday", url.split("/iex/")[1].split("/")[0]
        else:
            kind, ticker = "daily", url.split("/tiingo/daily/")[1].split("/")[0]
        bars = self.bars_by_key.get((kind, ticker))
        if bars is None:
            return FakeResponse(404, {"detail": "Not found."})
        start = params.get("startDate") or "0000-01-01"
        end = params.get("endDate") or "9999-12-31"
        window = [b for b in bars if start <= b["date"][:10] <= end]
        return FakeResponse(200, window[-PAGE_CAP:])


def _raw_intraday(start, count, first_open=100.0):
    """Synthetic 1-minute raw Tiingo bars, ascending, no volume key."""
    base = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    bars = []
    for i in range(count):
        moment = base + timedelta(minutes=i)
        bars.append({"date": moment.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     "open": first_open + i, "high": first_open + i + 1,
                     "low": first_open + i - 1, "close": first_open + i + 0.5})
    return bars


def _raw_daily(start_date, count, first_close=100.0):
    """Synthetic daily raw Tiingo bars (with volume), ascending."""
    day = datetime.fromisoformat(start_date).date()
    bars = []
    for i in range(count):
        d = day + timedelta(days=i)
        bars.append({"date": d.strftime("%Y-%m-%dT00:00:00.000Z"),
                     "open": first_close + i, "high": first_close + i + 1,
                     "low": first_close + i - 1, "close": first_close + i,
                     "volume": 1000 * (i + 1), "adjClose": first_close + i,
                     "adjVolume": 1000 * (i + 1), "divCash": 0.0,
                     "splitFactor": 1.0})
    return bars


def _session_1m(minutes, start_iso="2026-11-23T14:30:00Z", first_open=100.0):
    """1-minute bars starting at the 09:30 ET session open."""
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    bars = []
    for i in range(minutes):
        moment = start + timedelta(minutes=i)
        bars.append({"t": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "open": first_open + i, "high": first_open + i + 1,
                     "low": first_open + i - 1, "close": first_open + i + 0.5})
    return bars


def _hourly_bars(day_iso, hours, first_open=100.0):
    """Hourly bars for one ET session day, 10:00 ET onward."""
    start = datetime.fromisoformat(day_iso.replace("Z", "+00:00"))
    bars = []
    for i in range(hours):
        moment = start + timedelta(hours=i)
        bars.append({"t": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "open": first_open + i, "high": first_open + i + 1,
                     "low": first_open + i - 1, "close": first_open + i + 0.5})
    return bars


# --- universe ---------------------------------------------------------------

def test_universe_is_101_listings():
    assert len(symbols()) == EXPECTED_LISTINGS == 101
    assert len(set(symbols())) == 101
    assert "GOOGL" in symbols() and "GOOG" in symbols()
    assert DUAL_CLASS_PAIRS == (("GOOGL", "GOOG"),)
    validate()


def test_universe_validate_fails_closed():
    with pytest.raises(ValueError):
        validate(list(SYMBOLS) + [SYMBOLS[0]])
    with pytest.raises(ValueError):
        validate(list(SYMBOLS)[:-1])


def test_universe_names_match_nasdaq_list():
    # Spot-check the dual-class pair names.
    assert names()["GOOGL"] == "Alphabet Inc. Class A Common Stock"
    assert names()["GOOG"] == "Alphabet Inc. Class C Capital Stock"


# --- Tiingo bar parsing ------------------------------------------------------

def test_intraday_bar_has_no_volume_key():
    raw = {"date": "2026-11-23T20:59:00.000Z", "open": 349.265,
           "high": 349.465, "low": 348.9, "close": 349.465}
    bar = parse_intraday_bar(raw)
    assert bar == {"t": "2026-11-23T20:59:00Z", "open": 349.265,
                   "high": 349.465, "low": 348.9, "close": 349.465}
    assert "volume" not in bar


def test_daily_bar_carries_volume_and_adjusted_fields():
    raw = {"date": "2026-11-23T00:00:00.000Z", "open": 347.87,
           "high": 352.6, "low": 345.442189, "close": 349.08,
           "volume": 56934410, "adjClose": 349.08, "adjVolume": 56934410,
           "divCash": 0.0, "splitFactor": 1.0}
    bar = parse_daily_bar(raw)
    assert bar["t"] == "2026-11-23T00:00:00Z"
    assert bar["volume"] == 56934410
    assert bar["adj_close"] == 349.08
    assert bar["close"] == 349.08


def test_intraday_bar_with_null_close_is_rejected():
    with pytest.raises(Exception):
        parse_intraday_bar({"date": "2026-11-23T20:59:00.000Z",
                            "open": 1.0, "high": 1.0, "low": 1.0,
                            "close": None})


# --- Tiingo paging -----------------------------------------------------------

def test_fetch_base_walks_backward_and_dedupes_overlap():
    # 25,000 synthetic 1-minute bars: three pages (10,000 + 10,000 + 5,000
    # after dedupe of the overlapping endDate day).
    raw = _raw_intraday("2026-08-10", 25_000)
    session = FakeTiingoSession({("intraday", "AAPL"): raw})
    client = TiingoClient(token="fake-token", session=session,
                          today=date(2026, 11, 23))
    bars, pages = client.fetch_base("AAPL", "1m")
    assert pages == 3
    assert len(bars) == 25_000
    assert len({b["t"] for b in bars}) == 25_000, "overlap must dedupe exactly"
    assert bars[0]["t"] == "2026-08-10T13:30:00Z"
    assert bars[-1]["t"] == "2026-08-27T22:09:00Z"
    assert bars == sorted(bars, key=lambda b: b["t"])
    assert "volume" not in bars[0]
    # The 1m walk always sends an explicit startDate (today minus 120
    # days): omitting it returns only the most recent session day, and
    # a placeholder like 0000-01-01 is HTTP 400.
    assert all(params.get("startDate") == "2026-07-26"
               for _, params in session.calls)


def test_fetch_base_hourly_walks_to_2017():
    raw = _raw_daily("2017-01-03", 0)  # unused; hourly uses intraday kind
    hourly = []
    moment = datetime(2017, 1, 3, 15, 0, tzinfo=UTC)
    for i in range(15_478):
        # 6 bars per weekday, wall-clock hours; spacing is irrelevant to paging.
        hourly.append({"date": (moment + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5})
    session = FakeTiingoSession({("intraday", "AAPL"): hourly})
    client = TiingoClient(token="fake-token", session=session)
    bars, pages = client.fetch_base("AAPL", "1h")
    assert pages == 2
    assert len(bars) == 15_478


def test_fetch_base_daily_is_one_request():
    raw = _raw_daily("2000-01-03", 6_764)
    session = FakeTiingoSession({("daily", "AAPL"): raw})
    client = TiingoClient(token="fake-token", session=session)
    bars, pages = client.fetch_base("AAPL", "1d")
    assert pages == 1
    assert len(bars) == 6_764
    assert bars[0]["volume"] == 1000
    assert bars[-1]["close"] == 100.0 + 6_763


def test_fetch_base_unknown_ticker_raises_not_found():
    session = FakeTiingoSession({})
    client = TiingoClient(token="fake-token", session=session)
    with pytest.raises(TiingoNotFound):
        client.fetch_base("NDX", "1m")


def test_rate_limited_carries_retry_after():
    class Once429Session(FakeTiingoSession):
        def __init__(self):
            super().__init__({})
            self.calls_made = 0

        def get(self, url, params=None, timeout=None, allow_redirects=None):
            self.calls_made += 1
            if self.calls_made == 1:
                return FakeResponse(429, [], {"Retry-After": "120"})
            return FakeResponse(200, [])

    client = TiingoClient(token="fake-token", session=Once429Session())
    with pytest.raises(TiingoRateLimited) as excinfo:
        client.fetch_base("AAPL", "1d")
    assert excinfo.value.retry_after == 120


def test_fetch_newer_returns_only_newer_bars():
    raw = _raw_intraday("2026-08-10", 100)
    session = FakeTiingoSession({("intraday", "AAPL"): raw})
    client = TiingoClient(token="fake-token", session=session)
    bars, _ = client.fetch_base("AAPL", "1m", max_pages=1)
    assert len(bars) == 100
    after = bars[-1]["t"]
    newer_raw = _raw_intraday("2026-08-10", 110)
    session.bars_by_key[("intraday", "AAPL")] = newer_raw
    new_bars, pages = client.fetch_newer("AAPL", "1m", after)
    assert pages == 1
    assert len(new_bars) == 10
    assert all(b["t"] > after for b in new_bars)


# --- pacing -------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=NOW):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def test_rate_limiter_allows_50_per_hour_then_waits():
    clock = FakeClock()
    limiter = RateLimiter(now=clock, sleep=clock.sleep)
    for _ in range(50):
        assert limiter.acquire() == 0
    hour_used, day_used = limiter.counts()
    assert hour_used == 50 and day_used == 50
    # The 51st request must wait until the oldest request ages out of
    # the hour window (plus one second), not fail.
    assert limiter.wait_seconds() > 0
    assert limiter.wait_seconds() == pytest.approx(3601.0, abs=2.0)
    limiter.acquire()
    assert clock.slept, "the limiter must sleep instead of exceeding the budget"
    # After the ~1h sleep the 50 old requests have aged out of the window.
    assert limiter.counts()[0] == 1


def test_rate_limiter_daily_cap_binds_after_1000():
    clock = FakeClock()
    limiter = RateLimiter(hourly_limit=10_000, daily_limit=1000,
                          now=clock, sleep=clock.sleep)
    for _ in range(1000):
        limiter.acquire()
    assert limiter.counts()[1] == 1000
    assert limiter.wait_seconds() > 0


def test_limiter_is_acquired_once_per_http_request():
    # Pacing is per HTTP request, not per fetch: a 3-page 1m fetch must
    # record 3 acquires, or the checkpoint would undercount and the
    # 50/hour budget could be exceeded threefold.
    raw = _raw_intraday("2026-08-10", 25_000)
    session = FakeTiingoSession({("intraday", "AAPL"): raw})
    clock = FakeClock()
    limiter = RateLimiter(now=clock, sleep=clock.sleep)
    client = TiingoClient(token="fake-token", session=session, limiter=limiter,
                          today=date(2026, 11, 23))
    bars, pages = client.fetch_base("AAPL", "1m")
    assert pages == 3
    assert len(limiter.requests) == 3
    assert client.requests_made == 3


def test_checkpoint_round_trips_and_fails_closed(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(path)
    store.load()
    assert store.is_complete("AAPL", "1m") is False
    store.mark_base("AAPL", "1m", "complete", pages=3, bars=29_250,
                    first_bar="2026-08-10T13:30:00Z",
                    last_bar="2026-11-23T20:59:00Z", requests=3)
    store.save()
    assert path.exists()
    loaded = CheckpointStore(path).load()
    assert loaded.is_complete("AAPL", "1m") is True
    assert loaded.symbol_status("AAPL")["1m"]["bars"] == 29_250
    assert loaded.summary()["symbols_complete"] == 0  # only 1m of 3 bases


def test_checkpoint_corrupt_file_raises(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("{not json")
    with pytest.raises(Exception):
        CheckpointStore(path).load()


# --- resampling ---------------------------------------------------------------

def test_aggregate_9m_from_session_open():
    # 18 one-minute bars from the 09:30 ET open make two 9m bars.
    bars = _session_1m(18)
    agg = resample_mod.aggregate_intraday(bars, "9m")
    assert len(agg) == 2
    assert agg[0]["open"] == 100.0
    assert agg[0]["close"] == 100.0 + 8 + 0.5
    assert agg[0]["bars"] == 9
    assert agg[1]["bars"] == 9
    assert "volume" not in agg[0]
    assert agg[0]["t"] == "2026-11-23T14:30:00Z"


def test_aggregate_intraday_drops_out_of_session_bars():
    bars = _session_1m(10)
    # A pre-market bar (08:00 ET = 13:00Z) must be dropped, not folded in.
    bars.append({"t": "2026-11-23T13:00:00Z", "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0})
    agg = resample_mod.aggregate_intraday(bars, "5m")
    assert len(agg) == 2
    assert all(b["bars"] == 5 for b in agg)


def test_aggregate_hourly_2h_and_4h_same_day_only():
    day1 = _hourly_bars("2026-11-23T15:00:00Z", 6)  # 10:00-15:00 ET
    day2 = _hourly_bars("2026-11-24T15:00:00Z", 6)
    bars = day1 + day2
    two = resample_mod.aggregate_hourly(bars, "2h")
    assert len(two) == 6  # three per day, never across days
    assert two[0]["bars"] == 2 and two[2]["bars"] == 2
    assert two[0]["t"] == day1[0]["t"] and two[3]["t"] == day2[0]["t"]
    four = resample_mod.aggregate_hourly(bars, "4h")
    assert len(four) == 4  # 10:00-14:00 (4 bars) + 14:00-16:00 (2 bars), per day
    assert [b["bars"] for b in four] == [4, 2, 4, 2]
    assert "volume" not in four[0]


def test_aggregate_weekly_sums_volume():
    daily = []
    start = datetime(2026, 11, 16, tzinfo=UTC)  # Monday
    for i in range(10):  # two full Mon-Fri weeks
        day = start + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        daily.append({"t": day.strftime("%Y-%m-%dT00:00:00Z"),
                      "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
                      "close": 100.5 + i, "volume": 1000})
    weekly = resample_mod.aggregate_weekly(daily)
    assert len(weekly) == 2
    assert weekly[0]["volume"] == 5000
    assert weekly[0]["open"] == 100.0 and weekly[0]["close"] == 100.5 + 4
    assert weekly[0]["bars"] == 5
    assert weekly[0]["t"] == "2026-11-16T00:00:00Z"


def test_derive_all_produces_eleven_intervals_without_intraday_volume():
    bases = {
        "1m": _session_1m(390),  # one full session day
        "1h": _hourly_bars("2026-11-23T15:00:00Z", 6),
        "1d": [{"t": "2026-11-23T00:00:00Z", "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5, "volume": 1000}],
    }
    derived = resample_mod.derive_all(bases)
    assert list(derived) == list(resample_mod.INTERVALS)
    assert len(derived["1m"]) == 390
    assert len(derived["3m"]) == 130
    assert len(derived["9m"]) == 44  # 43 full + 1 partial (390 = 43*9 + 3)
    assert len(derived["1h"]) == 6
    assert len(derived["2h"]) == 3
    assert len(derived["1w"]) == 1
    for interval in ("1m", "3m", "5m", "9m", "15m", "30m", "1h", "2h", "4h"):
        assert all("volume" not in b for b in derived[interval]), interval
    assert derived["1w"][0]["volume"] == 1000
    with pytest.raises(ValueError):
        resample_mod.derive_all({"1m": [], "1h": []})


# --- provenance + publish -------------------------------------------------------

def _files_meta():
    return {
        "1m": {"bars": 29_250, "first_bar": "2026-08-10T13:30:00Z",
               "last_bar": "2026-11-23T20:59:00Z", "derived_from": None,
               "requests": 3, "pages": 3},
        "1d": {"bars": 6_764, "first_bar": "2000-01-03T00:00:00Z",
               "last_bar": "2026-11-23T00:00:00Z", "derived_from": None,
               "requests": 1, "pages": 1},
    }


def test_provenance_sidecar_shape():
    sidecar = build_provenance(
        "AAPL", _files_meta(), generated_at="2026-11-23T21:00:00Z",
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    assert sidecar["schema"] == "snapshot_provenance.v1"
    assert sidecar["dataset"] == "single_stock_candles"
    assert sidecar["symbol"] == "AAPL"
    assert sidecar["publication"] == {"status": "not_published",
                                      "reason": "local run: --publish not given"}
    assert sidecar["files"]["1m"]["key"] == "nasdaq/candles/AAPL/1m.json"
    assert sidecar["files"]["1m"]["tiingo_requests"] == 3
    assert sidecar["coverage"]["intervals"] == 2
    assert any("no volume" in note for note in sidecar["notes"])


def test_provenance_keys():
    assert interval_key("MSFT", "1h") == "nasdaq/candles/MSFT/1h.json"
    assert sidecar_key("MSFT") == "nasdaq/candles/MSFT/provenance.json"


class FakeR2Client:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body,
                          "ContentType": ContentType})


def test_publish_writes_interval_files_then_sidecar():
    files = {iv: {"symbol": "AAPL", "interval": iv, "bars": []}
             for iv in resample_mod.INTERVALS}
    sidecar = build_provenance("AAPL", {}, generated_at="2026-11-23T21:00:00Z")
    fake = FakeR2Client()
    config = {"R2_ENDPOINT_URL": "https://example.r2.dev",
              "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret",
              "TIINGO_R2_BUCKET": "qqq-options-chain-data"}
    result = publish_symbol("AAPL", files, sidecar, config=config, client=fake)
    assert result["bucket"] == "qqq-options-chain-data"
    assert len(result["keys"]) == 12  # 11 intervals + sidecar
    assert result["keys"][-1] == "nasdaq/candles/AAPL/provenance.json"
    assert result["keys"][:-1] == sorted(result["keys"][:-1])  # intervals sorted
    assert result["keys"][0] == "nasdaq/candles/AAPL/15m.json"
    assert all(p["ContentType"] == "application/json" for p in fake.puts)


def test_publish_config_missing_env_raises():
    with pytest.raises(RuntimeError):
        read_config(env={})


# --- collector flows (fake Tiingo client) ---------------------------------------

class FakeClient:
    """A fake TiingoClient: fetch_base/fetch_newer over fixture bars."""

    def __init__(self, bases_by_symbol):
        self.bases_by_symbol = bases_by_symbol
        self.requests_made = 0
        self.unavailable = set()

    def fetch_base(self, ticker, base, max_pages=None):
        self.requests_made += 1
        if ticker in self.unavailable:
            raise TiingoNotFound(f"Tiingo has no data for {ticker}")
        return [dict(b) for b in self.bases_by_symbol[ticker][base]], 1

    def fetch_newer(self, ticker, base, after_t):
        self.requests_made += 1
        if ticker in self.unavailable:
            raise TiingoNotFound(f"Tiingo has no data for {ticker}")
        newer = [b for b in self.bases_by_symbol[ticker][base] if b["t"] > after_t]
        return [dict(b) for b in newer], 1


def _fixture_bases():
    day1 = _session_1m(390)
    return {
        "1m": day1,
        "1h": _hourly_bars("2026-11-23T15:00:00Z", 6),
        "1d": [{"t": "2026-11-23T00:00:00Z", "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5, "volume": 1000,
                "adj_close": 100.5, "adj_volume": 1000}],
    }


def test_backfill_writes_eleven_files_plus_sidecar(tmp_path):
    client = FakeClient({"AAPL": _fixture_bases(), "MSFT": _fixture_bases()})
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    summary = run(["AAPL", "MSFT"], out, mode="backfill",
                  client=client, checkpoint=checkpoint)
    assert summary["complete"] == 2
    assert summary["source_unavailable"] == 0
    assert summary["tiingo_requests"] == 6  # 3 bases x 2 symbols
    for symbol in ("AAPL", "MSFT"):
        directory = ticker_dir(out, symbol)
        for interval in resample_mod.INTERVALS:
            assert (directory / f"{interval}.json").exists(), interval
        assert (directory / "provenance.json").exists()
        content = json.loads((directory / "1m.json").read_text())
        assert content["symbol"] == symbol and content["bars_count"] == 390
        assert "volume" not in content["bars"][0]
        daily = json.loads((directory / "1d.json").read_text())
        assert daily["bars"][0]["volume"] == 1000
        sidecar = json.loads((directory / "provenance.json").read_text())
        assert sidecar["schema"] == "snapshot_provenance.v1"
        assert sidecar["publication"]["status"] == "not_published"
    assert checkpoint.is_complete("AAPL", "1m") is True


def test_backfill_resumes_and_skips_completed(tmp_path):
    bases = {"AAPL": _fixture_bases()}
    client = FakeClient(bases)
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    run(["AAPL"], out, mode="backfill", client=client, checkpoint=checkpoint)
    made_first = client.requests_made
    assert made_first == 3
    # A second run reuses the checkpoint and spends no new requests.
    summary = run(["AAPL"], out, mode="backfill",
                  client=client, checkpoint=checkpoint)
    assert client.requests_made == made_first
    assert summary["complete"] == 1


def test_backfill_force_refetches_completed(tmp_path):
    bases = {"AAPL": _fixture_bases()}
    client = FakeClient(bases)
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    run(["AAPL"], out, mode="backfill", client=client, checkpoint=checkpoint)
    assert client.requests_made == 3
    # Without --force a second run spends nothing; with force it re-fetches.
    run(["AAPL"], out, mode="backfill", client=client, checkpoint=checkpoint,
        force=True)
    assert client.requests_made == 6
    bases = {"AAPL": _fixture_bases()}
    client = FakeClient(bases)
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    run(["AAPL"], out, mode="backfill", client=client, checkpoint=checkpoint)
    # One more session day of 1-minute bars arrives; daily/hourly unchanged.
    extra_day = _session_1m(390, start_iso="2026-11-24T14:30:00Z", first_open=200.0)
    bases["AAPL"]["1m"] = bases["AAPL"]["1m"] + extra_day
    summary = run(["AAPL"], out, mode="update",
                  client=client, checkpoint=checkpoint)
    assert summary["complete"] == 1
    result = summary["results"][0]
    assert result["status"] == "updated"
    assert result["bases"]["1m"]["new_bars"] == 390
    assert result["bases"]["1m"]["bars"] == 780
    assert result["bases"]["1d"]["new_bars"] == 0
    stored = read_symbol_files(out, "AAPL")
    assert len(stored["1m"]) == 780
    assert stored["1m"][-1]["t"] == "2026-11-24T20:59:00Z"


def test_source_unavailable_symbol_is_recorded_not_substituted(tmp_path):
    client = FakeClient({"AAPL": _fixture_bases()})
    client.unavailable = {"NDX"}
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    summary = run(["AAPL", "NDX"], out, mode="backfill",
                  client=client, checkpoint=checkpoint)
    assert summary["complete"] == 1
    assert summary["source_unavailable"] == 1
    assert summary["results"][1]["status"] == "source_unavailable"
    assert not ticker_dir(out, "NDX").exists()
    assert checkpoint.symbol_status("NDX")["1m"]["status"] == "source_unavailable"


def test_rate_limited_retried_once_then_succeeds(tmp_path):
    # A single 429 is retried once (after Retry-After) and the run
    # completes; the retry is silent in the summary.
    bases = {"AAPL": _fixture_bases()}

    class FlakyClient(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failures = 0

        def fetch_base(self, ticker, base, max_pages=None):
            if self.failures == 0:
                self.failures += 1
                raise TiingoRateLimited("Tiingo rate limit hit (HTTP 429)",
                                        retry_after=0)
            return super().fetch_base(ticker, base, max_pages=max_pages)

    client = FlakyClient(bases)
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    summary = run(["AAPL"], out, mode="backfill",
                  client=client, checkpoint=checkpoint)
    assert summary["complete"] == 1
    assert client.failures == 1


def test_double_rate_limit_aborts_with_checkpoint_saved(tmp_path):
    # A second 429 aborts the run — but the checkpoint file must still
    # exist (saved by run()'s finally), so the next run resumes
    # instead of starting over.
    class AlwaysLimitedClient(FakeClient):
        def fetch_base(self, ticker, base, max_pages=None):
            raise TiingoRateLimited("Tiingo rate limit hit (HTTP 429)",
                                    retry_after=0)

    client = AlwaysLimitedClient({"AAPL": _fixture_bases()})
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json")
    out = tmp_path / "out"
    with pytest.raises(TiingoRateLimited):
        run(["AAPL"], out, mode="backfill", client=client, checkpoint=checkpoint)
    assert (tmp_path / "checkpoint.json").exists()
    assert not ticker_dir(out, "AAPL").exists()


def test_interval_file_shape():
    bars = _session_1m(3)
    content = interval_file("AAPL", "9m", bars, "2026-11-23T21:00:00Z")
    assert content["schema_version"] == 1
    assert content["dataset"] == "single_stock_candles"
    assert content["derived_from"] == "1m" and content["base"] is False
    assert content["bars_count"] == 3
    assert content["first_bar"] == bars[0]["t"]
    base = interval_file("AAPL", "1m", bars, "2026-11-23T21:00:00Z")
    assert base["base"] is True and base["derived_from"] is None
