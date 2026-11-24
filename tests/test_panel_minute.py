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


def test_crypto_as_of_tracks_confirmed_publications(tmp_path):
    from market_data.crypto_collector.spot_receipts import read_as_of, retain
    path = tmp_path / "private" / "spot.sqlite3"
    client = Mock()
    old = doc("coinbase")
    old.update(feed="crypto", started_at=(NOW-timedelta(minutes=1)).isoformat())
    def prior():
        return {"Body": BytesIO(json.dumps(old).encode()), "ETag": "prior"}
    client.get_object.side_effect = lambda **kwargs: prior()
    candidate = doc("coinbase", NOW-timedelta(minutes=2))
    candidate["feed"] = "crypto"
    confirmed = NOW+timedelta(seconds=2)
    with patch("market_data.panel_minute.utcnow", return_value=confirmed):
        assert publish(client, "bucket", "crypto", candidate, NOW, crypto_receipts=path) == "published"
    body = client.put_object.call_args.kwargs["Body"]
    retain(path, body, confirmed+timedelta(minutes=2))
    assert read_as_of(path, NOW) is None
    result = read_as_of(path, confirmed)
    assert result["snapshot"] == json.loads(body)
    assert result["snapshot"]["prices"]["X"]["provider_observed_at"] == NOW.isoformat()
    later = confirmed+timedelta(minutes=1)
    candidate["prices"]["X"]["price_usd"] = 234
    with patch("market_data.panel_minute.utcnow", return_value=later):
        assert publish(client, "bucket", "crypto", candidate, NOW, crypto_receipts=path) == "published"
    assert read_as_of(path, later)["snapshot"]["prices"]["X"]["price_usd"] == 234
    assert read_as_of(path, confirmed) == result
    client.put_object.side_effect = RuntimeError("conditional write failed")
    with pytest.raises(RuntimeError):
        publish(client, "bucket", "crypto", candidate, NOW, crypto_receipts=tmp_path / "failed.db")
    assert not (tmp_path / "failed.db").exists()
