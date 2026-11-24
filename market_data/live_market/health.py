"""Health telemetry: the intraday/health.json lifecycle feed.

WHY this module: health.json is the collector's own pulse -- counters,
feed staleness, SPY collection state -- published every 15 seconds for
operators, not for the market-data path. It reads the quote helpers and
the session state, and nothing else.
"""

import json
import time
from datetime import date, datetime, timezone
from typing import Optional

from .config import (
    COLLECT_SPY,
    HEALTH_SECS,
    PRICE_TICKERS,
    R2_BUCKET,
    STALE_FEED_SECS,
    log,
    past_stop,
)
from .feed import DXLinkFeed
from .quotes import _fresh_spy_spot, _observed_spy_price, _spy_subscription_coverage
from .state import Counters, SnapshotTracker

# -- health.json upload (every 15s) ------------------------------------------

def push_health(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date,
                spy_context: Optional[dict] = None):
    fh   = feed.get_health()
    ctr  = counters.get()
    trk  = tracker.get()
    now  = datetime.now(timezone.utc)

    last_event = fh["last_feed_event_time"]
    feed_stale = (last_event is None or (now - last_event).total_seconds() > STALE_FEED_SECS)

    state     = feed.get_state()
    no_data   = [label for label, sym in PRICE_TICKERS.items()
                 if sym and state.get(sym, {}).get("last") is None and state.get(sym, {}).get("bid") is None]
    with_data = sum(bool(s) for s in PRICE_TICKERS.values()) - len(no_data)

    spy_context = spy_context or {"status": "unavailable", "reason": "no_spy_session"}
    spy_state = state.get("SPY", {}) if COLLECT_SPY else {}
    spy_price, spy_ts = _observed_spy_price(spy_state)
    spy_age = None
    if spy_ts:
        try:
            spy_age = (now - datetime.fromisoformat(spy_ts)).total_seconds()
        except ValueError:
            pass
    spy_spot_status = ("unavailable" if spy_price is None or spy_age is None else
                       "live" if 0 <= spy_age <= STALE_FEED_SECS else "stale")
    spy_counters = spy_context.get("counters")
    spy_tracker = spy_context.get("tracker")
    spy_health = {
        "collection_status": spy_context.get("status"),
        "reason": spy_context.get("reason"),
        "spot": {"price": spy_price, "observed_at": spy_ts, "status": spy_spot_status},
        "subscription_coverage": _spy_subscription_coverage(
            spy_context.get("subscription_coverage"), _fresh_spy_spot(spy_state, now)[0]),
        "subscription_delivery": feed.get_subscription_delivery(),
        "last_snapshot_upload_time": spy_counters.get()["last_snapshot_time"] if spy_counters else None,
        "cadence": spy_tracker.get() if spy_tracker else None,
    }

    payload = json.dumps({
        "run_id":             run_id,
        "trade_date":         today.isoformat(),
        "process_start_time": process_start.isoformat(),
        "updated_at":         now.isoformat(),
        "classification":     classification,
        "collector": {
            "past_stop":      past_stop(),
            "loop_alive":     True,
            "last_loop_time": now.isoformat(),
        },
        "feed": {
            "connected":            fh["connected"],
            "authorized":           fh["authorized"],
            "channel_open":         fh["channel_open"],
            "reconnect_count":      fh["reconnect_count"],
            "last_feed_event_time": last_event.isoformat() if last_event else None,
            "feed_stale":           feed_stale,
            "last_error":           fh["last_error"],
            "last_close_code":      fh["last_close_code"],
        },
        "uploads": {
            "prices_success_count":      ctr["prices_ok"],
            "snapshot_success_count":    ctr["snapshot_ok"],
            "csv_success_count":         ctr["csv_ok"],
            "failure_count":             ctr["failures"],
            "last_price_upload_time":    ctr["last_price_time"],
            "last_snapshot_upload_time": ctr["last_snapshot_time"],
        },
        "cadence": trk,
        "symbols": {
            "expected_price_symbols":  sum(bool(s) for s in PRICE_TICKERS.values()),
            "price_symbols_with_data": with_data,
            "no_data_symbols":         no_data,
        },
        "spy": spy_health,
    }, default=str)

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/health.json",
            Body=payload.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.error(f"health.json upload failed: {e}")
        counters.inc_failure()


def health_loop(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date,
                spy_context: Optional[dict] = None):
    while not past_stop():
        try:
            push_health(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context)
        except Exception as e:
            log.error(f"health loop error: {e}")
        time.sleep(HEALTH_SECS)
    log.info("health loop stopped")



__all__ = [
    "push_health",
    "health_loop",
]
