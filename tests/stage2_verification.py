import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
    assert_true(state["bid_ts"] is not None, "bid timestamp set")
    assert_true(state["ask_ts"] is not None, "ask timestamp set")
    assert_true(feed.get_health()["last_feed_event_time"] is not None, "last event time set")


def test_per_side_quote_timestamps_and_registry_lifecycle():
    feed = collector.DXLinkFeed("ws://example.invalid", "token")
    feed._ingest([
        {"eventType": "Quote", "eventSymbol": ".QQQ260717C600", "bidPrice": 1.0, "askPrice": 1.2},
    ])
    first = feed.get_state()[".QQQ260717C600"]
    first_bid_ts = first["bid_ts"]
    first_ask_ts = first["ask_ts"]
    time.sleep(0.002)
    feed._ingest([
        {"eventType": "Quote", "eventSymbol": ".QQQ260717C600", "askPrice": 1.3},
    ])
    second = feed.get_state()[".QQQ260717C600"]
    assert_equal(second["bid_ts"], first_bid_ts, "ask-only event must not refresh bid timestamp")
    assert_true(second["ask_ts"] > first_ask_ts, "ask-only event refreshes ask timestamp")

    registry = collector.LiveQuoteRegistry()
    assert_equal(registry.health()["state"], "offline", "registry starts offline")
    contracts = {
        "QQQ260717C00600000": {
            "streamer_symbol": ".QQQ260717C600",
            "strike": 600.0,
            "type": "call",
            "exp": "2026-07-17",
        },
    }
    live_feed = FakeFeed(
        state={".QQQ260717C600": second},
        last_event_time=datetime.now(timezone.utc),
    )
    connecting_feed = FakeFeed(
        health={"connected": False, "authorized": False, "channel_open": False},
    )
    registry.set_session(connecting_feed, contracts)
    assert_equal(registry.health()["state"], "connecting", "unready feed reports connecting")
    stale_feed = FakeFeed(
        state={".QQQ260717C600": second},
        last_event_time=datetime.now(timezone.utc) - timedelta(seconds=collector.STALE_FEED_SECS + 1),
    )
    registry.set_session(stale_feed, contracts)
    assert_equal(registry.health()["state"], "stale", "old feed reports stale")
    registry.set_session(live_feed, contracts)
    assert_equal(registry.health()["state"], "live", "ready feed reports live")
    payload = registry.quote_payload(["QQQ260717C00600000"])
    assert_equal(payload["returned"], 1, "registry returns exact requested contract")
    assert_equal(payload["quotes"][0]["bid_ts"], first_bid_ts, "registry preserves bid timestamp")
    assert_equal(payload["quotes"][0]["ask_ts"], second["ask_ts"], "registry preserves ask timestamp")
    ticker_ts = datetime.now(timezone.utc).isoformat()
    live_feed.state["QQQ"] = {
        "bid": 500.24, "ask": 500.26,
        "bid_ts": ticker_ts, "ask_ts": ticker_ts,
        "last": 500.25, "last_ts": ticker_ts, "prev_close": 499.0,
    }
    ticker = registry.quote_payload(["QQQ"])["quotes"][0]
    assert_equal(ticker["kind"], "ticker", "registry identifies ticker payloads")
    assert_equal(ticker["source"], "dxlink", "ticker payload exposes its source")
    assert_equal(ticker["quote_ts"], ticker_ts, "ticker payload exposes observation time")
    assert_equal(ticker["bid_ts"], ticker_ts, "ticker payload preserves bid timestamp")
    assert_equal(ticker["ask_ts"], ticker_ts, "ticker payload preserves ask timestamp")
    registry.clear_session()
    assert_equal(registry.health()["state"], "offline", "cleared session reports offline")


def test_prices_feed_stale_flags():
    # Populate every configured DXLink ticker so this unit test never reaches
    # the external yfinance fallback.
    state = {
        symbol: {"bid": 100.0, "ask": 100.2, "prev_close": 99.0}
        for symbol in collector.PRICE_TICKERS.values()
    }

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


def test_last_known_price_preserves_observation_time():
    original_prices = dict(collector._last_prices)
    original_fetch = collector.fetch_yf_prices_bounded
    observed = "2026-07-17T12:00:00+00:00"
    try:
        collector._last_prices.clear()
        collector._last_prices["QQQ"] = {"price": 499.5, "quote_ts": observed}
        collector.fetch_yf_prices_bounded = lambda: {
            label: None for label in collector.PRICE_TICKERS
        }
        state = {
            symbol: {"bid": 100.0, "ask": 100.2, "bid_ts": observed, "ask_ts": observed}
            for label, symbol in collector.PRICE_TICKERS.items() if label != "QQQ"
        }
        s3 = FakeS3()
        collector.push_prices(
            s3, FakeFeed(state=state, last_event_time=datetime.now(timezone.utc)),
            collector.Counters(),
        )
        qqq = s3.json_objects("intraday/prices.json")[-1]["prices"]["QQQ"]
        assert_equal(qqq["source"], "last-known", "missing provider uses last-known source")
        assert_equal(qqq["quote_ts"], observed, "last-known observation time is unchanged")
    finally:
        collector._last_prices.clear()
        collector._last_prices.update(original_prices)
        collector.fetch_yf_prices_bounded = original_fetch


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
    snapshot_keys = [key for key in csv_keys if "/snapshot_" in key]
    first_keys = [key for key in csv_keys if key.endswith("/first.csv")]
    assert_equal(len(snapshot_keys), 2, "two timestamped snapshot writes")
    assert_equal(len(set(snapshot_keys)), 2, "unique timestamped snapshot keys")
    assert_equal(len(first_keys), 1, "one session-open first.csv mirror")


def _weekday_calendar(anchor, back_days, fwd_days):
    """Stand-in for the NYSE calendar: every weekday in [anchor-back, anchor+fwd]."""
    days, d = set(), anchor - timedelta(days=back_days)
    while d <= anchor + timedelta(days=fwd_days):
        if d.weekday() < 5:
            days.add(d)
        d += timedelta(days=1)
    return days


def test_classify_tier_survives_past_opex_lookup():
    """Regression: 4th/5th-week Thursdays look up a monthly-opex Friday that is
    already in the past. With a forward-only calendar the backwards walk ran off
    to date.min and raised OverflowError, killing the whole collector session."""
    original = collector._load_calendar
    # Every Thursday whose following Friday is the end of week, across a full year.
    crashers = [date(2026, 7, 23), date(2026, 7, 30), date(2026, 8, 27),
                date(2026, 9, 24), date(2026, 10, 22), date(2026, 12, 24)]
    try:
        for day in crashers:
            # Reproduce the old forward-only window: nothing before today.
            collector._load_calendar = lambda d=day: _weekday_calendar(d, 0, 90)
            tier = collector.classify_tier(day)
            assert_true(tier.startswith("0DTE_"),
                        f"tier for {day} with forward-only calendar ({tier})")

            # And with the shipped lookback window, the real answer is reachable.
            collector._load_calendar = lambda d=day: _weekday_calendar(
                d, collector._CALENDAR_LOOKBACK_DAYS, collector._CALENDAR_LOOKAHEAD_DAYS)
            assert_true(collector.classify_tier(day).startswith("0DTE_"),
                        f"tier for {day} with lookback calendar")
    finally:
        collector._load_calendar = original


def test_classify_tier_defaults_when_calendar_unavailable():
    """An empty calendar (import/network failure inside _load_calendar) must degrade
    to a default tier, never take down market-data streaming."""
    original = collector._load_calendar
    try:
        collector._load_calendar = lambda: set()
        for day in (date(2026, 7, 23), date(2026, 7, 27), date(2026, 11, 26)):
            assert_equal(collector.classify_tier(day), collector.DEFAULT_TIER,
                         f"empty-calendar fallback for {day}")
    finally:
        collector._load_calendar = original


def test_classify_tier_still_labels_monthly_opex():
    """The fallbacks must not mask real classification: the Thursday before a
    monthly-opex Friday still classifies as 0DTE_Monthly."""
    original = collector._load_calendar
    try:
        # 2026-08-21 is the third Friday of August; 08-20 is the Thursday before it.
        collector._load_calendar = lambda: _weekday_calendar(
            date(2026, 8, 20), collector._CALENDAR_LOOKBACK_DAYS, collector._CALENDAR_LOOKAHEAD_DAYS)
        assert_equal(collector.classify_tier(date(2026, 8, 20)), "0DTE_Monthly",
                     "Thursday before August monthly opex")
        # 2026-08-13 -> Friday 08-14 is a weekly, not the monthly.
        collector._load_calendar = lambda: _weekday_calendar(
            date(2026, 8, 13), collector._CALENDAR_LOOKBACK_DAYS, collector._CALENDAR_LOOKAHEAD_DAYS)
        assert_equal(collector.classify_tier(date(2026, 8, 13)), "0DTE_Weekly",
                     "Thursday before a weekly expiry")
    finally:
        collector._load_calendar = original


def test_dxlink_unauthorized_greeting_is_not_an_auth_failure():
    """dxLink sends AUTH_STATE:UNAUTHORIZED unprompted after SETUP. Counting it as a
    rejection walked _auth_fail_count toward needs_reauth() on every healthy session."""
    feed = collector.DXLinkFeed.__new__(collector.DXLinkFeed)
    feed._lock = __import__("threading").Lock()
    feed._auth_fail_count = 0
    feed._authorized = False
    feed._connected = True
    feed._channel_open = False
    feed._last_close_code = None
    feed._ready = __import__("threading").Event()
    feed._token = "tok"
    feed._ws = None
    sent = []
    feed._send = sent.append

    feed._on_message(None, json.dumps({"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"}))
    assert_equal(feed._auth_fail_count, 0, "greeting does not count as an auth failure")
    assert_true(not feed.needs_reauth(), "greeting alone does not trigger reauth")

    feed._on_message(None, json.dumps({"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"}))
    assert_true(feed._authorized, "AUTHORIZED sets the authorized flag")

    # A close after authorizing is a normal disconnect, not an auth failure.
    feed._on_close(None, 1000, "bye")
    assert_equal(feed._auth_fail_count, 0, "clean close is not an auth failure")

    # Three connections that never authorize do trip needs_reauth().
    for _ in range(3):
        feed._on_close(None, None, "")
    assert_true(feed.needs_reauth(), "repeated unauthorized closes trip needs_reauth")


def run():
    tests = [
        test_session_window_timing,
        test_startup_classification,
        test_classify_tier_survives_past_opex_lookup,
        test_classify_tier_defaults_when_calendar_unavailable,
        test_classify_tier_still_labels_monthly_opex,
        test_dxlink_unauthorized_greeting_is_not_an_auth_failure,
        test_dxlink_ingest_health,
        test_per_side_quote_timestamps_and_registry_lifecycle,
        test_prices_feed_stale_flags,
        test_last_known_price_preserves_observation_time,
        test_health_schema_and_counters,
        test_snapshot_archive_key_uniqueness,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    run()
