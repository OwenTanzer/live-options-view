"""One-session SLV/DRAM collector, isolated from the existing collector.

No QQQ/SPY/IBIT, price lane, overview, weekly, common health or One Clock
writes. A root-scoped storage guard is applied even to imported helpers.
"""
import os
import time
from datetime import datetime, timezone

from .chain import load_chain
from .config import ET, SNAPSHOT_SECS, log, past_stop
from .feed import DXLinkFeed
from .market import take_snapshot
from .oauth import OAuthTokenManager, tasty_auth, TastyAuthError
from .prices import make_s3
from .quotes import LiveQuoteRegistry, start_live_quote_server, _fresh_spy_spot, _select_spy_strikes
from .state import Counters, SnapshotTracker, UnderlyingSession, restore_state

ROOTS = ("SLV", "DRAM")


class RootStore:
    """Reject every storage operation outside the two exclusively owned roots."""
    def __init__(self, client):
        self.client = client

    @staticmethod
    def check(key):
        if not isinstance(key, str) or ".." in key or not any(
            key.startswith(f"{prefix}/{root.lower()}/")
            for prefix in ("intraday", "settlement") for root in ROOTS
        ):
            raise ValueError("provisional storage boundary refused")

    def put_object(self, **kwargs):
        self.check(kwargs.get("Key"))
        return self.client.put_object(**kwargs)

    def get_object(self, **kwargs):
        self.check(kwargs.get("Key"))
        return self.client.get_object(**kwargs)

    def list_objects_v2(self, **kwargs):
        self.check(kwargs.get("Prefix"))
        return self.client.list_objects_v2(**kwargs)


class RootQuotes(LiveQuoteRegistry):
    def quote_payload(self, symbols):
        import re
        if any(not re.fullmatch(r"(?:SLV|DRAM)\d{6}[CP]\d{8}", s) for s in symbols):
            return {"quotes": [], "requested": len(symbols), "returned": 0,
                    "unavailable": {s: "outside_provisional_roots" for s in symbols},
                    "server_ts": datetime.now(timezone.utc).isoformat()}
        # Never subscribe on demand outside the bounded startup chain.
        with self._lock:
            allowed = set(self._contracts)
        selected = [s for s in symbols if s in allowed]
        result = super().quote_payload(selected)
        result["requested"] = len(symbols)
        result["unavailable"].update({s: "outside_startup_window" for s in symbols if s not in allowed})
        return result


def main():
    today = datetime.now(ET).date()
    if os.getenv("PROVISIONAL_SESSION_DATE") != today.isoformat():
        raise RuntimeError("explicit current session date required; no automatic next-day restart")
    if past_stop() or not os.getenv("LIVE_QUOTE_KEY"):
        raise RuntimeError("session ended or quote key absent")
    if any(os.getenv(f"COLLECT_{root}") != "1" for root in ROOTS):
        raise RuntimeError("both provisional roots must be explicitly enabled")
    # Fail closed on holidays before minting provider credentials.
    from .tiers import _load_calendar
    if today not in _load_calendar():
        raise RuntimeError("not a verified exchange session")
    manager = OAuthTokenManager.from_env()
    auth = tasty_auth(manager)
    chains = {root: load_chain(auth["access_token"], today, root, False)[:2] for root in ROOTS}
    feed = DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions([], list(ROOTS))
    registry = RootQuotes()
    server = start_live_quote_server(registry)
    store = RootStore(make_s3())
    jobs, contracts = [], {}
    started = datetime.now(timezone.utc).isoformat()
    try:
        feed.start()
        if not feed.wait_ready(timeout=30):
            raise RuntimeError("provisional feed did not become ready")
        for root, (listed, expiration) in chains.items():
            deadline = time.monotonic() + 30
            spot, stamp = None, None
            while time.monotonic() < deadline:
                spot, stamp = _fresh_spy_spot(feed.get_state().get(root, {}), datetime.now(timezone.utc))
                if spot is not None:
                    break
                time.sleep(0.5)
            if spot is None:
                raise RuntimeError(f"{root}: no fresh entitled spot")
            selected = _select_spy_strikes(listed, spot)
            if not selected or len(selected) > 67:
                raise RuntimeError(f"{root}: empty or excessive selection")
            us = UnderlyingSession(root)
            restore_state(store, today, us)
            us.subscription_coverage = {"policy": "provisional_fixed_nearest_67",
                "fixed_at_startup": True, "reference_spot": spot,
                "reference_spot_observed_at": stamp, "total_chain_strikes": len(listed),
                "selected_strikes": len(selected), "selected_option_symbols": 2 * len(selected),
                "min_strike": selected[0]["strike"], "max_strike": selected[-1]["strike"],
                "provisional_started_at": started, "partial_session": True}
            for row in selected:
                for side in ("call", "put"):
                    contracts[row[f"{side}_occ"].replace(" ", "")] = {
                        "streamer_symbol": row[f"{side}_sym"], "strike": row["strike"],
                        "type": side, "exp": expiration}
            jobs.append((us, selected, expiration, Counters(), SnapshotTracker()))
        symbols = sorted({v["streamer_symbol"] for v in contracts.values()})
        if len(symbols) > 268:
            raise RuntimeError("provisional contract budget exceeded")
        feed.add_option_subscriptions(symbols)
        registry.set_session(feed, contracts)
        log.info("provisional started=%s roots=%s option_symbols=%d partial_session=true", started, ROOTS, len(symbols))
        reauthenticated = False
        while not past_stop() and datetime.now(ET).date() == today:
            if feed.needs_reauth():
                if reauthenticated:
                    raise TastyAuthError("provisional-reauth-limit")
                reauthenticated = True
                feed.update_token(tasty_auth(manager)["streamer_token"])
            feed.restart_if_dead()
            feed.retry_pending_option_subscriptions()
            if not feed.get_subscription_delivery()["pending_event_pairs"]:
                for us, selected, expiration, counters, tracker in jobs:
                    take_snapshot(store, feed, selected, expiration, "provisional_partial_session",
                                  today, counters, tracker, us)
            time.sleep(SNAPSHOT_SECS)
        registry.clear_session("session_closed")
        log.info("provisional session finalized; no partial-day RVOL baseline promoted")
    except BaseException:
        registry.clear_session("session_failed")
        raise
    finally:
        feed.stop()
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
