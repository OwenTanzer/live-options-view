"""Prove SPY collection (#121) is isolated from QQQ: chain parsing, snapshot
keys and payload identity, per-symbol VWAP/RVOL/momentum/volume-delta state,
restart recovery and RVOL baselines.

Same conventions as tests/verify_collector_vwap_rvol.py (import collector.py
directly, in-memory FakeS3, no tastytrade/DXLink/R2 access).

    python tests/verify_collector_spy.py
"""

import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collector  # noqa: E402

TODAY = date(2026, 10, 6)
EXP = TODAY.isoformat()


class FakeBody:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    def __init__(self, seed: dict | None = None):
        self.store: dict[str, bytes] = {}
        for key, value in (seed or {}).items():
            self.store[key] = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.writes: list[str] = []

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise RuntimeError(f"missing key: {Key}")
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        self.writes.append(Key)

    def list_objects_v2(self, Bucket, Prefix):  # noqa: N803
        return {"Contents": [{"Key": k} for k in sorted(self.store) if k.startswith(Prefix)]}

    def json(self, key):
        return json.loads(self.store[key].decode())


class FakeFeed:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return {k: dict(v) for k, v in self.state.items()}

    def get_health(self):
        return {"connected": True, "authorized": True, "channel_open": True,
                "reconnect_count": 0, "last_feed_event_time": datetime.now(timezone.utc),
                "last_error": None, "last_close_code": None}

    def get_subscription_delivery(self):
        return {"pending_event_pairs": 0, "last_error": None}


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


def strike(root, price, yymmdd="261006"):
    k = f"{int(price * 1000):08d}"
    return {
        "strike": float(price),
        "call_sym": f".{root}{yymmdd}C{int(price)}",
        "put_sym": f".{root}{yymmdd}P{int(price)}",
        "call_occ": f"{root:<6}{yymmdd}C{k}",
        "put_occ": f"{root:<6}{yymmdd}P{k}",
    }


QQQ_STRIKES = [strike("QQQ", 600)]
SPY_STRIKES = [strike("SPY", 670)]


def feed_state(qqq_vol=100, spy_vol=1000, spy_quote=True, qqq_day=5_000_000, spy_day=9_000_000):
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "QQQ": {"bid": 600.0, "ask": 600.2, "bid_ts": now, "ask_ts": now, "volume": qqq_day},
        ".QQQ261006C600": {"bid": 1.0, "ask": 1.2, "oi": 10, "volume": qqq_vol},
        ".QQQ261006P600": {"bid": 1.1, "ask": 1.3, "oi": 11, "volume": qqq_vol},
        ".SPY261006C670": {"bid": 2.0, "ask": 2.2, "oi": 20, "volume": spy_vol},
        ".SPY261006P670": {"bid": 2.1, "ask": 2.3, "oi": 21, "volume": spy_vol},
    }
    if spy_quote:
        state["SPY"] = {"bid": 670.0, "ask": 670.4, "bid_ts": now, "ask_ts": now, "volume": spy_day}
    return state


def snap(s3, state, us, strikes):
    collector.take_snapshot(s3, FakeFeed(state), strikes, EXP, "0DTE_Regular", TODAY,
                            collector.Counters(), collector.SnapshotTracker(), us)


def sessions():
    return collector.UnderlyingSession("QQQ"), collector.UnderlyingSession("SPY")


def test_session_keys_keep_qqq_layout():
    qqq, spy = sessions()
    assert_equal(qqq.latest_key, "intraday/latest.json", "QQQ latest key unchanged")
    assert_equal(qqq.day_prefix("20261006"), "intraday/20261006/", "QQQ archive prefix unchanged")
    assert_equal(qqq.rvol_baseline_key, "baselines/qqq_rvol_buckets.json", "QQQ RVOL key unchanged")
    assert_equal(spy.latest_key, "intraday/spy/latest.json", "SPY latest key")
    assert_equal(spy.day_prefix("20261006"), "intraday/spy/20261006/", "SPY archive prefix")
    assert_equal(spy.rvol_baseline_key, "baselines/spy_rvol_buckets.json", "SPY RVOL key")
    assert_true(not spy.day_prefix("20261006").startswith(qqq.day_prefix("20261006")),
                "SPY archive never falls under a QQQ date prefix")
    assert_equal(set(collector.UNDERLYING_SESSIONS), {"QQQ", "SPY"}, "configured underlyings")
    assert_true(collector.UNDERLYING_SESSIONS["QQQ"] is collector.QQQ_SESSION, "QQQ keeps the module session")


def test_chain_parse_is_symbol_bound():
    expiration = {"expiration-date": EXP, "strikes": [
        {"strike-price": "670", "call": {"symbol": "SPY   261006C00670000", "streamer-symbol": ".SPY261006C670"},
         "put": {"symbol": "SPY   261006P00670000", "streamer-symbol": ".SPY261006P670"}},
        # A foreign contract in a SPY response must be dropped, never labeled SPY.
        {"strike-price": "600", "call": {"symbol": "QQQ   261006C00600000", "streamer-symbol": ".QQQ261006C600"},
         "put": {"symbol": "QQQ   261006P00600000", "streamer-symbol": ".QQQ261006P600"}},
        {"strike-price": "671", "call": {}, "put": {}},
    ]}
    rows = collector._parse_expiration_strikes(expiration, "SPY")
    assert_equal([r["strike"] for r in rows], [670.0, 671.0], "foreign QQQ strike dropped")
    assert_equal(rows[1]["call_sym"], ".SPY261006C671", "built symbols use the requested root")
    assert_equal(collector._occ_root("SPY   261006C00670000"), "SPY", "OCC root")
    qqq_rows = collector._parse_expiration_strikes(expiration)
    assert_equal([r["strike"] for r in qqq_rows], [600.0, 671.0], "QQQ default keeps only QQQ contracts")


def test_streamer_alias_and_occ_fields_cannot_cross_symbols():
    expiration = {"expiration-date": EXP, "strikes": [
        {"strike-price": "670", "call": {"symbol": "SPY   261006C00670000", "streamer-symbol": ".SPY261006C670"},
         "put": {"symbol": "SPY   261006P00670000", "streamer-symbol": ".SPY261006P670"}},
        {"strike-price": "671", "call": {"symbol": "SPY   261006C00671000", "streamer-symbol": ".QQQ261006C600"},
         "put": {"symbol": "SPY   261006P00671000", "streamer-symbol": ".QQQ261006P600"}},
        {"strike-price": "672", "call": {"symbol": "SPY   261007C00672000"},
         "put": {"symbol": "SPY   261006P00672000"}},
        {"strike-price": "673", "call": {"symbol": "SPY   261006P00673000"},
         "put": {"symbol": "SPY   261006P00673000"}},
        {"strike-price": "674", "call": {"symbol": "SPY   261006C00675000"},
         "put": {"symbol": "SPY   261006P00674000"}},
    ]}
    parsed = collector._parse_expiration_strikes(expiration, "SPY")
    assert_equal([s["strike"] for s in parsed], [670.0], "full contract identity checked")
    s3 = FakeS3()
    snap(s3, feed_state(spy_vol=20, qqq_vol=9999), sessions()[1], parsed)
    rows = s3.json("intraday/spy/latest.json")["rows"]
    assert_equal(len(rows), 2, "only validated SPY call and put enter actual snapshot")
    assert_true(all(r["OptionSymbol"].replace(" ", "").startswith("SPY") for r in rows),
                "QQQ streamer aliases cannot seed SPY rows")
    assert_equal({r["Volume"] for r in rows}, {20}, "QQQ feed volume cannot be SPY-labeled")


def test_load_chain_uses_requested_ticker_and_skips_weekly():
    seen = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": [{"expirations": [
                {"expiration-date": EXP, "strikes": [
                    {"strike-price": "670", "call": {"symbol": "SPY   261006C00670000"},
                     "put": {"symbol": "SPY   261006P00670000"}}]},
                {"expiration-date": "2026-10-09", "strikes": []},
            ]}]}}

    original = collector.requests.get
    collector.requests.get = lambda url, **kw: (seen.append(url), Resp())[1]
    try:
        strikes, exp, weekly, weekly_exp = collector.load_chain("token", TODAY, ticker="SPY", include_weekly=False)
    finally:
        collector.requests.get = original
    assert_true(seen[0].endswith("/option-chains/SPY/nested"), "SPY chain endpoint")
    assert_equal((exp, weekly, weekly_exp), (EXP, [], ""), "no SPY weekly chain")
    assert_equal(strikes[0]["call_sym"], ".SPY261006C670", "SPY streamer symbol")


def test_spy_missing_today_expiration_is_explicit_and_qqq_fallback_is_unchanged():
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": [{"expirations": [
                {"expiration-date": "2026-10-07", "strikes": []}]}]}}

    original = collector.requests.get
    collector.requests.get = lambda *args, **kwargs: Resp()
    try:
        try:
            collector.load_chain("token", TODAY, ticker="SPY", include_weekly=False)
            raise AssertionError("SPY accepted a next-day expiration as 0DTE")
        except RuntimeError as exc:
            assert_true("SPY 0DTE unavailable" in str(exc), "missing-today status is explicit")
        _, qqq_exp, _, _ = collector.load_chain("token", TODAY, ticker="QQQ", include_weekly=False)
        assert_equal(qqq_exp, "2026-10-07", "QQQ nearest-upcoming behavior preserved")
    finally:
        collector.requests.get = original


def test_spy_subscription_window_is_bounded_and_deterministic():
    expiration = {"expiration-date": EXP, "strikes": [
        {"strike-price": str(k), "call": {}, "put": {}} for k in range(500, 800)
    ]}
    strikes = collector._parse_expiration_strikes(expiration, "SPY")
    selected = collector._select_spy_strikes(strikes, 670.5)
    symbols = [s[f"{side}_sym"] for s in selected for side in ("call", "put")]
    feed = collector.DXLinkFeed("unused", "unused")  # no start/network call
    feed.set_subscriptions([], ["SPY"])
    feed.add_option_subscriptions(symbols)
    assert_equal(len(strikes), 300, "offline 300-strike fixture")
    assert_equal((len(selected), selected[0]["strike"], selected[-1]["strike"]),
                 (67, 637.0, 703.0), "67 nearest, lower strike wins final tie")
    assert_equal((len(symbols), len(feed._subs)), (134, 540),
                 "134 options x four events plus four SPY spot events")
    assert_equal([r["strike"] for r in collector._select_spy_strikes(strikes[:3], 670)],
                 [500.0, 501.0, 502.0], "small chain subscribes every available strike")
    assert_equal(len(collector._select_spy_strikes(strikes + strikes[:5], 670)), 67,
                 "duplicate strikes cannot expand the cap")
    assert_equal([r["strike"] for r in collector._select_spy_strikes(strikes, 1000)],
                 list(map(float, range(733, 800))), "far-away spot still caps at 67")


def test_spy_startup_requires_fresh_timed_spot_and_reports_coverage():
    now = datetime.now(timezone.utc)
    fresh = now.isoformat()
    old = (now - timedelta(seconds=collector.STALE_FEED_SECS + 1)).isoformat()
    assert_equal(collector._fresh_spy_spot({"bid": 670, "ask": 670.4,
                                            "bid_ts": fresh, "ask_ts": fresh}, now),
                 (670.2, fresh), "fresh complete quote accepted")
    for quote in ({"bid": 670, "ask": 670.4, "bid_ts": old, "ask_ts": fresh},
                  {"bid": 670, "ask": 670.4, "ask_ts": fresh},
                  {"last": 670, "last_ts": old},
                  {"last": 670},
                  {"last": float("nan"), "last_ts": fresh}):
        assert_equal(collector._fresh_spy_spot(quote, now), (None, None),
                     "stale, incomplete, untimed and nonfinite spots rejected")
    coverage = {"min_strike": 637.0, "max_strike": 703.0,
                "selected_strikes": 67, "selected_option_symbols": 134}
    assert_equal(collector._spy_subscription_coverage(coverage, None)["status"],
                 "spot_unavailable", "missing current spot is explicit")
    assert_equal(collector._spy_subscription_coverage(coverage, 704)["status"],
                 "out_of_range", "spot beyond fixed window is explicit")
    assert_equal(collector._spy_subscription_coverage(coverage, 670)["status"],
                 "within_window", "spot within fixed window")


def test_spy_dynamic_subscriptions_survive_reconnect_without_resetting_qqq():
    feed = collector.DXLinkFeed("unused", "unused")
    messages = []
    feed._send = lambda message: messages.append(message)
    feed.set_subscriptions([".QQQ261006C600"], ["QQQ", "SPY"])
    feed._on_message(None, json.dumps({"type": "FEED_CONFIG"}))
    initial = len(feed._subs)
    assert_equal(initial, 12, "QQQ option and two underlying symbols retained")
    added = feed.add_option_subscriptions([".SPY261006C670", ".SPY261006P670"])
    assert_equal(added, 8, "only two SPY options dynamically added")
    assert_true(messages[-1]["reset"] is False, "SPY addition cannot reset QQQ subscriptions")
    assert_equal(feed.add_option_subscriptions([".SPY261006C670"]), 0, "repeat add idempotent")
    feed._subscribed = False  # reconnect requires a complete re-subscription
    messages.clear()
    feed._on_message(None, json.dumps({"type": "FEED_CONFIG"}))
    assert_equal(len(feed._subs), 20, "fixed window retained for reconnect")
    assert_true(messages[0]["reset"], "reconnect resets to complete QQQ and SPY set")
    assert_equal(len(messages[0]["add"]), 20, "reconnect includes all subscribed events")
    feed._send = lambda message: (_ for _ in ()).throw(OSError("offline socket"))
    assert_equal(feed.add_option_subscriptions([".SPY261006C671"]), 4,
                 "interrupted SPY delivery does not stop QQQ collection")
    assert_equal(len(feed._subs), 24, "interrupted delivery retained for reconnect")
    assert_equal(feed.get_subscription_delivery()["pending_event_pairs"], 4,
                 "incomplete delivery is explicit")


def test_spy_failed_middle_batch_retries_unsent_pairs_without_reset():
    feed = collector.DXLinkFeed("unused", "unused")
    feed.set_subscriptions([".QQQ261006C600"], ["QQQ", "SPY"])
    sent = []
    attempts = 0

    def sometimes_fail(message):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("second batch interrupted")
        sent.append(message)

    feed._send = sometimes_fail
    feed._subscribed = True
    symbols = [f".SPY261006C{k}" for k in range(500, 634)]
    assert_equal(feed.add_option_subscriptions(symbols), 536, "67-strike cap event-pair load")
    assert_equal(feed.get_subscription_delivery()["pending_event_pairs"], 336,
                 "failed second batch and third batch remain pending")
    assert_equal([len(m["add"]) for m in sent], [200], "first batch sent once")
    s3 = FakeS3()
    collector.push_health(s3, feed, collector.Counters(), collector.SnapshotTracker(),
                          "run", datetime.now(timezone.utc), "clean_start", TODAY,
                          {"status": "subscription_pending", "reason": "subscription_delivery_incomplete"})
    assert_equal(s3.json("intraday/health.json")["spy"]["subscription_delivery"]["pending_event_pairs"],
                 336, "health exposes incomplete SPY delivery")
    assert_equal(feed.add_option_subscriptions(symbols), 0, "duplicate request adds no pairs")
    assert_equal([len(m["add"]) for m in sent], [200, 200, 136],
                 "duplicate request safely retries only unsent batches")
    assert_true(all(m["reset"] is False for m in sent), "QQQ subscriptions never reset")
    assert_equal(feed.get_subscription_delivery(), {"pending_event_pairs": 0, "last_error": None},
                 "fully delivered state is explicit")


def test_spy_snapshot_is_identified_and_isolated():
    qqq, spy = sessions()
    spy.subscription_coverage = {
        "policy": "fixed_startup_nearest_67_strikes", "fixed_at_startup": True,
        "reference_spot": 670.2, "reference_spot_observed_at": datetime.now(timezone.utc).isoformat(),
        "total_chain_strikes": 1, "selected_strikes": 1, "selected_option_symbols": 2,
        "min_strike": 670.0, "max_strike": 670.0,
    }
    s3 = FakeS3()
    snap(s3, feed_state(), qqq, QQQ_STRIKES)
    qqq_before = (dict(qqq.prev_vol), qqq.vwap_state.cum_vol, dict(qqq.rvol_today), len(qqq.momentum_history))
    qqq_latest_before = s3.store["intraday/latest.json"]
    snap(s3, feed_state(), spy, SPY_STRIKES)

    payload = s3.json("intraday/spy/latest.json")
    assert_equal(payload["symbol"], "SPY", "SPY payload symbol")
    assert_equal(payload["underlying_market"]["symbol"], "SPY", "SPY underlying_market symbol")
    assert_equal(payload["underlying_price"], 670.2, "SPY spot from the SPY quote, not QQQ")
    assert_equal(payload["subscription_coverage"]["status"], "out_of_range",
                 "snapshot reports fixed-window range against current observed spot")
    assert_true(payload["snapshot_key"].startswith("intraday/spy/20261006/snapshot_"), "SPY snapshot key")
    assert_true(all(r["OptionSymbol"].startswith("SPY") for r in payload["rows"]), "only SPY contracts")
    assert_equal(s3.store["intraday/latest.json"], qqq_latest_before, "QQQ latest.json untouched by SPY")
    assert_true("intraday/spy/20261006/first.csv" in s3.store, "SPY first.csv")
    assert_true("intraday/spy/20261006/vwap_state.json" in s3.store, "SPY vwap_state")
    assert_equal(s3.json("intraday/spy/20261006/vwap_state.json")["symbol"], "SPY", "vwap_state tagged")
    spy_writes = [k for k in s3.writes if "spy" in k]
    assert_true(spy_writes and all(k.startswith("intraday/spy/") for k in spy_writes), "SPY writes stay under intraday/spy/")
    assert_equal((dict(qqq.prev_vol), qqq.vwap_state.cum_vol, dict(qqq.rvol_today), len(qqq.momentum_history)),
                 qqq_before, "QQQ state unchanged by a SPY snapshot")
    assert_equal(s3.json("intraday/latest.json")["symbol"], "QQQ", "QQQ payload now names its symbol")


def test_volume_delta_and_vwap_do_not_cross():
    qqq, spy = sessions()
    s3 = FakeS3()
    snap(s3, feed_state(qqq_vol=100, spy_vol=1000), qqq, QQQ_STRIKES)
    snap(s3, feed_state(qqq_vol=100, spy_vol=1000), spy, SPY_STRIKES)
    snap(s3, feed_state(qqq_vol=130, spy_vol=1500, qqq_day=5_000_100, spy_day=9_000_900), qqq, QQQ_STRIKES)
    snap(s3, feed_state(qqq_vol=130, spy_vol=1500, qqq_day=5_000_100, spy_day=9_000_900), spy, SPY_STRIKES)
    q_rows = s3.json("intraday/latest.json")["rows"]
    s_rows = s3.json("intraday/spy/latest.json")["rows"]
    assert_equal({r["VolDelta"] for r in q_rows}, {30}, "QQQ volume delta from QQQ history only")
    assert_equal({r["VolDelta"] for r in s_rows}, {500}, "SPY volume delta from SPY history only")
    assert_equal(qqq.vwap_state.last_dayvolume, 5_000_100, "QQQ VWAP fed by QQQ day volume")
    assert_equal(spy.vwap_state.last_dayvolume, 9_000_900, "SPY VWAP fed by SPY day volume")
    q_vwap, s_vwap = qqq.vwap_state.vwap, spy.vwap_state.vwap
    assert_true(q_vwap is not None and 590 < q_vwap < 610, f"QQQ VWAP near QQQ ({q_vwap})")
    assert_true(s_vwap is not None and 660 < s_vwap < 680, f"SPY VWAP near SPY ({s_vwap})")


def test_missing_spy_quote_is_reported_not_substituted():
    _, spy = sessions()
    s3 = FakeS3()
    snap(s3, feed_state(spy_quote=False), spy, SPY_STRIKES)
    payload = s3.json("intraday/spy/latest.json")
    um = payload["underlying_market"]
    assert_equal(payload["underlying_price"], None, "no QQQ price substituted for missing SPY spot")
    assert_equal((um["spot"], um["spot_ts"], um["vwap"]), (None, None, None), "SPY readings unavailable")
    assert_equal(um["momentum"]["status"], "no_data", "momentum reports no data")
    assert_true(um["freshness"] != "live", f"freshness not live ({um['freshness']})")


def test_spy_price_tile_and_health_use_only_observed_spy_quote():
    now = datetime.now(timezone.utc).isoformat()
    state = {sym: {"bid": 100.0, "ask": 100.2, "bid_ts": now, "ask_ts": now}
             for sym in collector.PRICE_TICKERS.values()}
    state["SPY"] = {"bid": 670.0, "ask": 670.4, "bid_ts": now, "ask_ts": now}
    s3 = FakeS3()
    qqq_prices_before = dict(collector._last_prices)
    try:
        collector.push_prices(s3, FakeFeed(state), collector.Counters())
        spy = s3.json("intraday/prices.json")["prices"]["SPY"]
        assert_equal((spy["price"], spy["source"], spy["quote_ts"]),
                     (670.2, "dxlink", now), "SPY tile carries its own observed price")
        assert_true("SPY" not in collector._last_prices, "SPY does not enter QQQ macro CSV fallback state")
        ctx = {"status": "collecting", "reason": None,
               "counters": collector.Counters(), "tracker": collector.SnapshotTracker()}
        collector.push_health(s3, FakeFeed(state), collector.Counters(), collector.SnapshotTracker(),
                              "run", datetime.now(timezone.utc), "clean_start", TODAY, ctx)
        health = s3.json("intraday/health.json")
        assert_equal(health["spy"]["spot"]["status"], "live", "fresh SPY quote health")
        assert_equal(health["spy"]["spot"]["price"], 670.2, "SPY health price")
        assert_equal(health["symbols"]["expected_price_symbols"], len(collector.PRICE_TICKERS),
                     "QQQ legacy health counts unchanged")
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        state["SPY"]["bid_ts"] = old
        collector.push_prices(s3, FakeFeed(state), collector.Counters())
        assert_equal(s3.json("intraday/prices.json")["prices"]["SPY"]["quote_ts"], old,
                     "SPY midpoint is aged by its older side")
        collector.push_health(s3, FakeFeed(state), collector.Counters(), collector.SnapshotTracker(),
                              "run", datetime.now(timezone.utc), "clean_start", TODAY, ctx)
        assert_equal(s3.json("intraday/health.json")["spy"]["spot"]["status"], "stale",
                     "one stale quote side cannot make SPY health live")
        registry = collector.LiveQuoteRegistry()
        registry.set_session(FakeFeed(state), {})
        assert_equal(registry.quote_payload(["SPY"])["quotes"][0]["quote_ts"], old,
                     "live quote endpoint uses the same SPY observation time")
        state["SPY"]["bid_ts"] = None
        collector.push_prices(s3, FakeFeed(state), collector.Counters())
        assert_equal(s3.json("intraday/prices.json")["prices"]["SPY"]["price"], None,
                     "half-untimed SPY midpoint is unavailable")
        del state["SPY"]
        collector.push_prices(s3, FakeFeed(state), collector.Counters())
        assert_equal(s3.json("intraday/prices.json")["prices"]["SPY"]["price"], None,
                     "missing SPY quote has no QQQ or yfinance substitution")
        collector.push_health(s3, FakeFeed(state), collector.Counters(), collector.SnapshotTracker(),
                              "run", datetime.now(timezone.utc), "clean_start", TODAY, ctx)
        assert_equal(s3.json("intraday/health.json")["spy"]["spot"]["status"], "unavailable",
                     "missing SPY quote health")
        ctx.update(status="missing_today_expiration", reason="SPY 0DTE unavailable")
        collector.push_health(s3, FakeFeed(state), collector.Counters(), collector.SnapshotTracker(),
                              "run", datetime.now(timezone.utc), "clean_start", TODAY, ctx)
        assert_equal(s3.json("intraday/health.json")["spy"]["collection_status"],
                     "missing_today_expiration", "explicit SPY chain status")
    finally:
        collector._last_prices.clear()
        collector._last_prices.update(qqq_prices_before)


def test_spy_rvol_starts_with_insufficient_history_and_rejects_foreign_baseline():
    _, spy = sessions()
    qqq_baseline = {"symbol": "QQQ", "buckets": {"10:30": {"samples": [{"date": "2026-10-01", "cum_volume": 1}]}}}
    s3 = FakeS3({"baselines/spy_rvol_buckets.json": qqq_baseline, "baselines/qqq_rvol_buckets.json": qqq_baseline})
    loaded = collector.load_rvol_baseline(s3, spy)
    assert_equal((loaded["symbol"], loaded["buckets"]), ("SPY", {}), "a QQQ baseline at the SPY key is not used")
    spy.rvol_baseline = loaded
    snap(s3, feed_state(), spy, SPY_STRIKES)
    rvol = s3.json("intraday/spy/latest.json")["underlying_market"]["rvol"]
    assert_true(rvol["status"] in ("insufficient_history", "no_data"), f"SPY RVOL warming up ({rvol['status']})")
    spy.rvol_today = {"10:30": 1234}
    collector.finalize_rvol_baseline(s3, TODAY, spy)
    written = s3.json("baselines/spy_rvol_buckets.json")
    assert_equal(written["symbol"], "SPY", "SPY baseline written under its own symbol")
    assert_equal(s3.json("baselines/qqq_rvol_buckets.json"), qqq_baseline, "QQQ baseline untouched")


def _csv(rows):
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue().encode()


def test_restore_is_per_symbol():
    qqq, spy = sessions()
    spy_key = "intraday/spy/20261006/snapshot_103000000000.csv"
    s3 = FakeS3({
        spy_key: _csv([
            {"OptionSymbol": "SPY   261006C00670000", "Volume": 777, "UnderlyingPrice": 670.1, "VIX": 99.0},
            # A foreign row in a SPY archive must not seed SPY state.
            {"OptionSymbol": "QQQ   261006C00600000", "Volume": 5, "UnderlyingPrice": 600.1, "VIX": 99.0},
        ]),
        "intraday/spy/20261006/vwap_state.json": {"symbol": "QQQ", "session_date": EXP, "cum_pv": 1.0,
                                                  "cum_vol": 1, "vwap": 600.0},
    })
    prices_before = dict(collector._last_prices)
    collector.restore_state(s3, TODAY, spy)
    assert_equal(spy.prev_vol, {".SPY261006C670": 777}, "SPY volume restored, foreign row ignored")
    assert_equal(spy.last_spot[0], 670.1, "SPY spot restored")
    assert_equal(spy.last_spot[1], "2026-10-06T14:30:00+00:00", "SPY spot keeps its own archive timestamp")
    assert_equal(spy.vwap_state.vwap, None, "vwap_state tagged QQQ is not restored into SPY")
    assert_true(spy.first_snapshot_written, "SPY first.csv not rewritten after restart")
    assert_equal(collector._last_prices, prices_before, "shared price strip restored from QQQ only")
    assert_equal((qqq.prev_vol, qqq.first_snapshot_written), ({}, False), "QQQ restore state untouched")
    collector.restore_state(s3, TODAY, qqq)
    assert_equal(qqq.prev_vol, {}, "QQQ restore never reads the SPY archive")


def test_spy_new_day_resets_session_state_and_writes_new_first_csv():
    qqq, spy = sessions()
    s3 = FakeS3()
    collector.restore_state(s3, TODAY, spy)
    snap(s3, feed_state(), spy, SPY_STRIKES)
    day1 = spy.day_prefix(TODAY.strftime("%Y%m%d")) + "first.csv"
    assert_true(day1 in s3.store and spy.first_snapshot_written, "first day mirror written")
    qqq.prev_vol["qqq-only"] = 42
    qqq.first_snapshot_written = True
    next_day = TODAY + timedelta(days=1)
    collector.restore_state(s3, next_day, spy)
    assert_equal(spy.prev_vol, {}, "new SPY day does not retain old volume deltas")
    assert_equal(spy.last_spot, [None, None], "new SPY day does not retain old spot")
    assert_true(not spy.first_snapshot_written, "new SPY day resets first-snapshot guard")
    assert_equal(qqq.prev_vol["qqq-only"], 42, "QQQ session untouched")
    assert_true(qqq.first_snapshot_written, "QQQ first-snapshot guard untouched")
    collector.take_snapshot(s3, FakeFeed(feed_state()), SPY_STRIKES, next_day.isoformat(),
                            "0DTE_Regular", next_day, collector.Counters(),
                            collector.SnapshotTracker(), spy)
    day2 = spy.day_prefix(next_day.strftime("%Y%m%d")) + "first.csv"
    assert_true(day2 in s3.store, "second day writes its own first.csv")
    before = s3.store[day2]
    collector.restore_state(s3, next_day, spy)
    assert_true(spy.first_snapshot_written, "same-day recovery retains first-snapshot guard")
    assert_equal(s3.store[day2], before, "same-day recovery does not replace first.csv")


def test_foreign_only_archive_cannot_seed_spy_spot_or_timestamp():
    _, spy = sessions()
    s3 = FakeS3({"intraday/spy/20261006/snapshot_103100000000.csv": _csv([
        {"OptionSymbol": "QQQ   261006C00600000", "Volume": 5, "UnderlyingPrice": 600.1},
    ])})
    collector.restore_state(s3, TODAY, spy)
    assert_equal(spy.prev_vol, {}, "foreign-only archive has no SPY volume")
    assert_equal(spy.last_spot, [None, None], "foreign-only archive has no SPY price or timestamp")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} SPY collection checks passed.")


if __name__ == "__main__":
    run()

