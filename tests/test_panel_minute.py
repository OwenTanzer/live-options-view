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
