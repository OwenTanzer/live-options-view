from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from unittest.mock import Mock, patch

import pytest

from market_data.panel_minute import StatefulCapture, after, protect_observations, publish, regular_session
from market_data.panel_prices import mark

NOW = datetime(2026, 11, 27, 17, 58, tzinfo=timezone.utc)


def doc(provider="nasdaq_primary", ts=NOW):
    return {"feed": "nasdaq", "schema_version": "panel_minute.v1", "started_at": (NOW-timedelta(seconds=9)).isoformat(),
            "completed_at": NOW.isoformat(), "baseline_at": (NOW-timedelta(minutes=10)).isoformat(),
            "prices": {"X": mark(provider, 123, ts, NOW)}, "calls": {"nasdaq": 1}}


def test_calendar_fails_closed_on_holidays_and_early_close():
    cal = {"start":"2026-01-01","end":"2026-12-31","sessions":{
        "2026-11-27":["2026-11-27T14:30:00Z","2026-11-27T18:00:00Z"]}}
    assert regular_session(cal, NOW)
    assert not regular_session(cal, NOW+timedelta(minutes=2))
    assert not regular_session(cal, NOW-timedelta(days=1))
    with pytest.raises(ValueError):
        regular_session(cal,NOW+timedelta(days=40))


def test_throttle_saved_across_new_processes(tmp_path):
    path = tmp_path/"backoff"
    with patch("market_data.panel_prices.requests.get", return_value=Mock(status_code=429,headers={"Retry-After":"600"})) as get:
        StatefulCapture(path).get("coinbase","url")
        StatefulCapture(path).get("coinbase","url")
        assert get.call_count == 1
    assert after("Fri, 27 Nov 2026 18:00:00 GMT",NOW) == NOW+timedelta(minutes=2)


def test_watermark_survives_gap_and_nasdaq_provider_switch():
    earlier = NOW-timedelta(minutes=3)
    prior = doc()
    update = doc("nasdaq_watchlist",earlier)
    protect_observations(update,prior,"nasdaq",NOW)
    assert update["prices"]["X"]["source"] == "nasdaq_primary"
    assert update["prices"]["X"]["provider_observed_at"] == NOW.isoformat()
    gap=doc()
    gap["prices"]["X"] = mark("nasdaq_watchlist")
    protect_observations(gap, update, "nasdaq",NOW)
    assert gap["prices"]["X"]["watermark_at"] == NOW.isoformat()
    late=doc("nasdaq_watchlist",earlier)
    protect_observations(late,gap,"nasdaq",NOW)
    assert late["prices"]["X"]["price_usd"] is None
    assert late["prices"]["X"]["received_at"] is None


def test_conditional_publish_and_expired_capture():
    client=Mock()
    old=doc()
    old["started_at"]=(NOW-timedelta(minutes=1)).isoformat()
    client.get_object.return_value={"Body":BytesIO(json.dumps(old).encode()),"ETag":"old"}
    assert publish(client,"bucket","nasdaq",doc(),NOW) == "published"
    args=client.put_object.call_args.kwargs
    assert args["IfMatch"] == "old" and args["Key"] == "nasdaq/prices.json"
    assert "enrichment" not in json.loads(args["Body"])
    with pytest.raises(ValueError):
        publish(client,"bucket","nasdaq",doc(),NOW+timedelta(minutes=2))
    assert client.put_object.call_count == 1


def test_future_provider_stamp_and_prior_future_watermark_cannot_hide_valid_quote():
    for label in (None, "Dec 04, 2026"):
        poisoned = doc(ts=NOW+timedelta(days=7))
        if label:
            poisoned["prices"]["X"].update(provider_time_label=label, provider_observed_at=None)
        bad = json.loads(json.dumps(poisoned))
        protect_observations(bad, {"prices":{}}, "nasdaq", NOW)
        assert bad["prices"]["X"]["price_usd"] is None
        assert "watermark_at" not in bad["prices"]["X"]
        poisoned["prices"]["X"]["watermark_at"] = (NOW+timedelta(days=7)).isoformat()
        good = doc()
        protect_observations(good, poisoned, "nasdaq", NOW)
        assert good["prices"]["X"]["watermark_at"] == NOW.isoformat()
        assert good["prices"]["X"]["price_usd"] == 123


def test_crypto_replay_uses_confirmed_baseline_and_actual_panel_rules(tmp_path, monkeypatch):
    from test_crypto_collector import FakeObjectStore
    from market_data.crypto_collector.publish import _publish_snapshot
    from market_data.crypto_collector.spot_receipts import read_as_of

    path = tmp_path / "private" / "spot.sqlite3"
    store = FakeObjectStore()
    monkeypatch.setenv("OPTIONSVIEW_CRYPTO_SPOT_RECEIPTS", str(path))
    candidate = doc("coinbase")
    candidate["feed"] = "crypto"
    baseline = {"constructed_at": candidate["baseline_at"],
                "coins": [{"symbol": "X", "source": "coinbase", "price": 100}]}
    with patch("market_data.crypto_collector.publish.utcnow", return_value=NOW-timedelta(minutes=1)):
        _publish_snapshot(baseline, store, "test")
    confirmed = NOW+timedelta(seconds=1)
    with patch("market_data.panel_minute.utcnow", return_value=confirmed):
        publish(store, "test", "crypto", candidate, NOW, crypto_receipts=path)
    assert read_as_of(path, NOW)["state"] == "unmeasurable"
    assert read_as_of(path, NOW+timedelta(seconds=119, milliseconds=999))["rows"][0]["price"] == 123
    assert read_as_of(path, NOW+timedelta(seconds=120))["rows"] == baseline["coins"]
    later = NOW+timedelta(seconds=40)
    newer = {"constructed_at": later.isoformat(),
             "coins": [{"symbol": "X", "source": "coinbase", "price": 101}]}
    with patch("market_data.crypto_collector.publish.utcnow", return_value=later):
        _publish_snapshot(newer, store, "test")
    assert read_as_of(path, later)["rows"] == newer["coins"]
    assert read_as_of(path, later-timedelta(microseconds=1))["rows"][0]["price"] == 123


def test_ack_failure_leaves_gap_and_identical_retry_recovers(tmp_path, monkeypatch):
    import sqlite3
    from test_crypto_collector import FakeObjectStore
    from market_data.crypto_collector.publish import _publish_snapshot
    from market_data.crypto_collector import spot_receipts

    db = tmp_path / "private" / "spot.sqlite3"
    store = FakeObjectStore()
    candidate = doc("coinbase")
    candidate["feed"] = "crypto"
    monkeypatch.setenv("OPTIONSVIEW_CRYPTO_SPOT_RECEIPTS", str(db))
    baseline = {"constructed_at": candidate["baseline_at"],
                "coins": [{"symbol": "X", "source": "coinbase", "price": 100}]}
    with patch("market_data.crypto_collector.publish.utcnow", return_value=NOW):
        _publish_snapshot(baseline, store, "bucket")
    with patch("market_data.panel_minute.utcnow", return_value=NOW):
        publish(store, "bucket", "crypto", candidate, NOW, crypto_receipts=db)
    second = NOW+timedelta(seconds=30)
    update = doc("coinbase", second)
    update.update(feed="crypto", started_at=(second-timedelta(seconds=1)).isoformat(),
                  completed_at=second.isoformat())
    update["prices"]["X"] = mark("coinbase", 110, second, second)
    with patch("market_data.panel_minute.utcnow", return_value=second), \
            patch.object(spot_receipts, "confirm", side_effect=sqlite3.OperationalError("disk error")), \
            pytest.raises(sqlite3.OperationalError):
        publish(store, "bucket", "crypto", update, second, crypto_receipts=db)
    assert json.loads(store.objects["crypto/prices.json"][0])["prices"]["X"]["price_usd"] == 110
    assert spot_receipts.read_as_of(db, second)["unverified_stream"] == "minute"
    count = len(store.writes)
    retry = second+timedelta(seconds=1)
    with patch("market_data.panel_minute.utcnow", return_value=retry):
        assert publish(store, "bucket", "crypto", update, retry, crypto_receipts=db) == "superseded"
    assert len(store.writes) == count
    assert spot_receipts.read_as_of(db, retry)["rows"][0]["price"] == 110
    assert spot_receipts.read_as_of(db, second)["state"] == "unmeasurable"
    assert spot_receipts.read_as_of(db, second-timedelta(seconds=1))["rows"][0]["price"] == 123


def test_crypto_publication_age_buckets():
    from market_data.panel_minute import crypto_freshness

    receipt = NOW - timedelta(seconds=6)
    prices = {str(age): mark("coinbase", 1, NOW - timedelta(seconds=age), receipt)
              for age in (60, 65, 300, 301)}
    prices["absent"] = mark("coingecko")
    assert prices["65"]["freshness"] == "fresh"
    providers = crypto_freshness(prices, NOW)["providers"]
    assert prices["65"]["freshness"] == "stale"
    assert providers["coinbase"] == {
        "counts": {"fresh": 1, "stale_to_5m": 2, "stale_over_5m": 1},
        "oldest_symbol": "301", "oldest_age_seconds": 301,
    }
    assert providers["coingecko"]["counts"] == {"missing": 1}


def test_two_confirmed_minutes_replay_same_old_trade(tmp_path):
    from market_data.crypto_collector.spot_receipts import prepare, confirm, read_as_of

    db = tmp_path / "spot.sqlite3"
    trade = NOW - timedelta(minutes=52)
    baseline = {"constructed_at": (NOW-timedelta(hours=1)).isoformat(),
                "coins": [{"symbol": "X", "source": "coinbase", "price": 100}]}
    def record(stream, payload, at):
        attempt = prepare(db, stream, json.dumps(payload).encode(), at)
        confirm(db, attempt, at)
    record("hourly", baseline, NOW-timedelta(minutes=5))
    observations = set()
    for minute in (0, 1):
        at = NOW + timedelta(minutes=minute)
        overlay = doc("coinbase", trade)
        overlay.update(feed="crypto", baseline_at=baseline["constructed_at"], completed_at=at.isoformat())
        overlay["prices"]["X"]["received_at"] = at.isoformat()
        record("minute", overlay, at)
        row, = read_as_of(db, at)["rows"]
        assert f"{3120 + 60*minute}s old" in row["minuteLabel"]
        observations.add((row["symbol"], row["source"], row["provider_observed_at"], row["price"]))
    assert observations == {("X", "coinbase", trade.isoformat(), 123)}
