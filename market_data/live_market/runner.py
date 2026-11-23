"""Session runner: one market session end to end, plus the entry point.

WHY this module now: _run_session (263 lines) is the second-largest unit
in the collector and the orchestrator -- it wires auth, the feed, the
snapshot/price/health loops and the weekly loop into one session. It moved
out early (with DXLinkFeed) so the biggest units landed first; its callees
now live in their final homes in the package (see the responsibility map).
main() moves with it: it is the Railway entry point and its test patch
points (start_live_quote_server, make_s3, wait_for_premarket, _run_session)
resolve through the package entry, as they did in the flat file.
"""

import secrets
import threading
import time
from datetime import date, datetime, timezone

import market_data.live_market as _entry  # entry indirection for main()'s test patch points

from .config import (
    COLLECT_SPY,
    COLLECT_IBIT,
    ET,
    ONE_CLOCK_PUBLICATIONS,
    HEALTH_SECS,
    PRICE_LANE_ENABLED,
    R2_BUCKET,
    PRICES_SECS,
    PRICE_TICKERS,
    SNAPSHOT_SECS,
    STOP_HOUR,
    STOP_MIN,
    WEEKLY_SNAPSHOT_SECS,
    log,
    past_stop,
)
from .feed import DXLinkFeed
from .oauth import OAuthTokenManager, TastyAuthError, tasty_auth
from .tiers import classify_tier
from .chain import load_chain
from .steo import eia_steo_loop
from .health import health_loop, push_health
from .market import take_snapshot
from .publication_queue import PublicationQueue
from .prices import (
    _classify_startup,
    _log_ticker_health,
    make_s3,
    prices_loop,
)
from .price_lane import PriceLane, build_rolling_store
from .quotes import (
    LiveQuoteRegistry,
    _fresh_spy_spot,
    _observed_spy_price,
    _select_spy_strikes,
)
from .state import (
    Counters,
    QQQ_SESSION,
    SnapshotTracker,
    UnderlyingSession,
    UNDERLYING_SESSIONS,
    finalize_rvol_baseline,
    load_rvol_baseline,
    restore_state,
)
from .sessions import weekly_snapshot_loop


def price_writer_plan() -> str:
    """The single decision point for who writes intraday/prices.json (#178).

    main() starts the continuous lane for this plan; _run_session() starts the
    legacy in-session loop only for the other value. Keeping one function means
    the two can never both be active because of duplicated env checks.
    """
    return "continuous_price_lane" if PRICE_LANE_ENABLED else "in_session_legacy"

_publication_queues = {}


def _publication_queue(us, today, counters):
    if not ONE_CLOCK_PUBLICATIONS:
        return None
    if us.symbol not in _publication_queues:
        queue = PublicationQueue(make_s3(publication=True), R2_BUCKET, us.symbol)
        _publication_queues[us.symbol] = queue
        queue.start()
    queue = _publication_queues[us.symbol]
    queue.session = (today.isoformat(), counters)
    return queue


def next_capture_delay(start, now, interval=SNAPSHOT_SECS):
    # If acquisition itself crossed a boundary, take the current bucket
    # immediately. Never manufacture an observation for a skipped bucket.
    return max(0.0, (int(start) // interval + 1) * interval - now)


def _run_session(token_manager: OAuthTokenManager, quote_registry: LiveQuoteRegistry):
    run_id        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    process_start = datetime.now(timezone.utc)
    log.info(f"session start  run_id={run_id}")

    s3             = make_s3()
    classification = _classify_startup(s3, process_start)
    log.info(f"startup classification: {classification}")

    today   = datetime.now(ET).date()
    restore_state(s3, today, QQQ_SESSION)
    QQQ_SESSION.rvol_baseline = load_rvol_baseline(s3, QQQ_SESSION)
    QQQ_SESSION.rvol_today = {}

    auth    = tasty_auth(token_manager)
    from .overview_sources import resolve_futures
    resolve_futures(auth["access_token"])
    tier    = classify_tier(today)
    log.info(f"session date={today}  tier={tier}")

    strikes, exp_date, weekly_strikes, weekly_exp_date = load_chain(
        auth["access_token"], today
    )

    # Extra 0DTE chains (#121). Each is optional: a failure here disables
    # only that underlying for this session and never touches QQQ.
    extra_chains: list[tuple[UnderlyingSession, list[dict], str]] = []
    pending_chains = []
    extra_contexts = {}
    spy_context = {"status": "disabled" if not COLLECT_SPY else "unavailable",
                   "reason": "COLLECT_SPY=0" if not COLLECT_SPY else "chain_not_loaded"}
    for sym, us in UNDERLYING_SESSIONS.items():
        if us is QQQ_SESSION or (sym == "SPY" and not COLLECT_SPY) or (sym == "IBIT" and not COLLECT_IBIT):
            continue
        try:
            us_strikes, us_exp, _, _ = load_chain(auth["access_token"], today, ticker=sym, include_weekly=False)
        except Exception as e:
            log.error(f"{sym} chain load failed ({e}) -- {sym} collection disabled this session")
            if sym == "SPY":
                spy_context.update(status="missing_today_expiration" if "SPY 0DTE unavailable" in str(e) else "chain_load_failed",
                                   reason=str(e))
            continue
        restore_state(s3, today, us)
        us.rvol_baseline = load_rvol_baseline(s3, us)
        us.rvol_today = {}
        us.subscription_coverage = None
        pending_chains.append((us, us_strikes, us_exp))
        extra_contexts[sym] = spy_context if sym == "SPY" else {}
        extra_contexts[sym].update(status="awaiting_spot", reason="waiting_for_fresh_spot")

    option_syms = []
    contracts = {}
    for s in strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])
        if s["call_occ"]:
            contracts[s["call_occ"].replace(" ", "")] = {
                "streamer_symbol": s["call_sym"], "strike": s["strike"],
                "type": "call", "exp": exp_date,
            }
        if s["put_occ"]:
            contracts[s["put_occ"].replace(" ", "")] = {
                "streamer_symbol": s["put_sym"], "strike": s["strike"],
                "type": "put", "exp": exp_date,
            }

    # Weekly contracts share the same DXLink connection but are intentionally
    # not added to the live quote registry/UI contract map.
    for s in weekly_strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])
    for us, us_strikes, us_exp in extra_chains:
        for s in us_strikes:
            for side in ("call", "put"):
                option_syms.append(s[f"{side}_sym"])
                if s[f"{side}_occ"]:
                    contracts[s[f"{side}_occ"].replace(" ", "")] = {
                        "streamer_symbol": s[f"{side}_sym"], "strike": s["strike"],
                        "type": side, "exp": us_exp,
                    }
    option_syms = list(dict.fromkeys(option_syms))

    # Extra underlyings need their own spot/volume stream, but are not
    # added to PRICE_TICKERS (the price strip and its health counts).
    price_syms = list(dict.fromkeys(
        [s for s in PRICE_TICKERS.values() if s] + [us.symbol for us, _, _ in extra_chains]
        + [us.symbol for us, _, _ in pending_chains]
    ))
    log.info(f"subscribing to {len(option_syms)} option symbols + {len(price_syms)} price tickers")
    for label, sym in PRICE_TICKERS.items():
        log.info(f"  price ticker  {label:<10} -> {sym}")

    feed = DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions(option_syms, price_syms)
    quote_registry.set_session(feed, contracts)
    feed.start()

    if not feed.wait_ready(timeout=30):
        log.warning("DXLink channel not open after 30s -- proceeding anyway")

    log.info("waiting for first option data event (up to 15s)...")
    if feed.wait_first_data(timeout=15):
        log.info("option data flowing -- proceeding to snapshot")
    else:
        log.warning("no feed data within 15s -- proceeding anyway")

    for us, us_strikes, us_exp in pending_chains:
        context = extra_contexts[us.symbol]
        deadline = time.monotonic() + 15
        spot, spot_ts = None, None
        while time.monotonic() < deadline:
            spot, spot_ts = _fresh_spy_spot(feed.get_state().get(us.symbol, {}), datetime.now(timezone.utc))
            if spot is not None:
                break
            time.sleep(0.5)
        if spot is None:
            observed, observed_ts = _observed_spy_price(feed.get_state().get(us.symbol, {}))
            context.update(status="stale_startup_spot" if observed_ts else "spot_unavailable",
                               reason="fresh_spy_spot_not_observed_within_15s")
            log.warning("SPY options unavailable: no fresh timed startup spot within 15s")
        else:
            selected = _select_spy_strikes(us_strikes, spot)
            if not selected:
                context.update(status="empty_chain", reason="no_valid_spy_strikes")
                log.warning("SPY options unavailable: no valid strikes in today's chain")
            else:
                coverage = {
                    "policy": "fixed_startup_nearest_67_strikes",
                    "fixed_at_startup": True,
                    "reference_spot": spot,
                    "reference_spot_observed_at": spot_ts,
                    "total_chain_strikes": len(us_strikes),
                    "selected_strikes": len(selected),
                    "selected_option_symbols": 2 * len(selected),
                    "min_strike": selected[0]["strike"],
                    "max_strike": selected[-1]["strike"],
                }
                us.subscription_coverage = coverage
                context["subscription_coverage"] = coverage
                extra_chains.append((us, selected, us_exp))
                for row in selected:
                    for side in ("call", "put"):
                        occ = row[f"{side}_occ"]
                        if occ:
                            contracts[occ.replace(" ", "")] = {
                                "streamer_symbol": row[f"{side}_sym"],
                                "strike": row["strike"], "type": side, "exp": us_exp,
                            }
                quote_registry.set_session(feed, contracts)
                symbols = list(dict.fromkeys(row[f"{side}_sym"] for row in selected for side in ("call", "put")))
                added = feed.add_option_subscriptions(symbols)
                pending = feed.get_subscription_delivery()["pending_event_pairs"]
                context.update(status="subscription_pending" if pending else "collecting",
                                   reason="subscription_delivery_incomplete" if pending else None)
                log.info(f"SPY fixed startup window: {len(us_strikes)} chain strikes, "
                         f"{len(selected)} selected strikes, {len(symbols)} option symbols, "
                         f"{added} added event/symbol pairs; spot={spot} at {spot_ts}")

    _log_ticker_health(feed)

    counters = Counters()
    tracker  = SnapshotTracker()
    # Separate counters/trackers so extra underlyings can never mask a missed
    # QQQ snapshot in health.json (which stays QQQ-only).
    extra_trackers = {us.symbol: (Counters(), SnapshotTracker()) for us, _, _ in extra_chains}
    if "SPY" in extra_trackers:
        spy_context["counters"], spy_context["tracker"] = extra_trackers["SPY"]

    prices_thread = None
    if price_writer_plan() == "in_session_legacy":
        # Legacy rollback path: the chain session remains the price writer.
        prices_thread = threading.Thread(
            target=prices_loop, args=(s3, feed, counters, quote_registry), daemon=True)
        prices_thread.start()
        log.info(f"prices thread started (every {PRICES_SECS}s, legacy in-session writer)")
    else:
        # The continuous price lane owns intraday/prices.json (#178). Starting a
        # second publisher here would race it, so this thread is deliberately
        # absent.
        log.info("price publication owned by the continuous price lane (options=0 subscriptions there)")

    health_thread = threading.Thread(
        target=health_loop,
        args=(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context),
        daemon=True,
    )
    health_thread.start()
    log.info(f"health thread started (every {HEALTH_SECS}s)")

    if weekly_strikes:
        weekly_thread = threading.Thread(
            target=weekly_snapshot_loop,
            args=(s3, feed, weekly_strikes, weekly_exp_date, today),
            daemon=True,
        )
        weekly_thread.start()
        log.info(
            f"weekly snapshot thread started (expiration {weekly_exp_date}, "
            f"every {WEEKLY_SNAPSHOT_SECS // 60}m during 09:30-16:00 ET)"
        )

    log.info(f"snapshot loop started (every {SNAPSHOT_SECS}s, stop {STOP_HOUR:02d}:{STOP_MIN:02d} ET)")

    queues = {QQQ_SESSION.symbol: _publication_queue(QQQ_SESSION, today, counters)}
    for us, _, _ in extra_chains:
        queues[us.symbol] = _publication_queue(us, today, extra_trackers[us.symbol][0])

    reauth_attempted = False
    while not past_stop():
        if feed.needs_reauth():
            if reauth_attempted:
                feed.stop()
                quote_registry.clear_session(reason="session_failed")
                raise TastyAuthError(
                    "streamer-reauth-circuit",
                    detail="one-reauth-chain-already-used-this-session",
                )
            reauth_attempted = True
            log.warning("DXLink auth failed 3+ times -- one streamer reauth attempt permitted")
            try:
                new_auth = tasty_auth(token_manager)
                feed.update_token(new_auth["streamer_token"])
                log.info("streamer token refreshed")
            except TastyAuthError:
                feed.stop()
                quote_registry.clear_session(reason="session_failed")
                raise
        feed.restart_if_dead()

        for context in extra_contexts.values():
            if context.get("status") == "subscription_pending":
                feed.retry_pending_option_subscriptions()
                if not feed.get_subscription_delivery()["pending_event_pairs"]:
                    context.update(status="collecting", reason=None)

        captured_state = feed.get_state()
        capture = (datetime.now(timezone.utc), captured_state)
        tracker.check_missed(capture[0])
        try:
            take_snapshot(s3, feed, strikes, exp_date, tier, today, counters, tracker,
                          publication_queue=queues[QQQ_SESSION.symbol], capture=capture)
        except Exception as e:
            log.error(f"snapshot error: {e}")
        for us, us_strikes, us_exp in extra_chains:
            if extra_contexts.get(us.symbol, {}).get("status") != "collecting":
                continue
            us_counters, us_tracker = extra_trackers[us.symbol]
            us_tracker.check_missed(capture[0])
            try:
                take_snapshot(s3, feed, us_strikes, us_exp, tier, today, us_counters, us_tracker, us,
                              publication_queue=queues[us.symbol], capture=capture)
            except Exception as e:
                log.error(f"{us.symbol} snapshot error: {e}")
        time.sleep(next_capture_delay(capture[0].timestamp(), time.time()))

    trk = tracker.get()
    ctr = counters.get()
    log.info(
        f"session complete  run_id={run_id}  "
        f"snapshots={trk['snapshot_sequence']}  "
        f"missed={trk['missed_snapshot_count']}  "
        f"failures={ctr['failures']}"
    )

    # Write final health.json with past_stop=True so next startup classifies as clean_start
    try:
        push_health(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context)
    except Exception:
        pass

    # Fold today's RVOL bucket readings into the cross-day baseline. Done here
    # (once, at confirmed session end) rather than incrementally, so a
    # mid-session crash can never leave the baseline holding a partial day.
    finalize_rvol_baseline(s3, today, QQQ_SESSION)
    for us, _, _ in extra_chains:
        finalize_rvol_baseline(s3, today, us)

    feed.stop()
    # Deliberate end of a trading session: the only path a client may show as a
    # closed lane. Every failure path below records session_failed instead.
    quote_registry.clear_session(reason="session_closed")


def announce_scheduler_wait(quote_registry: LiveQuoteRegistry, blocked_session_date: date | None) -> None:
    """Label the runner's deliberate wait for the next session.

    The registry is built uninitialized and main() then blocks in
    wait_for_premarket, so a cold restart after hours would report
    "uninitialized" until the next premarket -- an unexplained outage instead of
    the closed lane the collector is actually in. Two guards keep that honest:
    a wait that follows a blocked session is recovery from an auth or startup
    fault, so the registry keeps session_failed and the client keeps backing
    off; and a registry that already carries a known state (an ended session,
    an open one) is left alone rather than overwritten.
    """
    if blocked_session_date is not None:
        return
    if quote_registry.session_state() == "uninitialized":
        quote_registry.await_next_session()


def main():
    try:
        token_manager = OAuthTokenManager.from_env()
    except TastyAuthError as exc:
        log.critical(f"{exc}; collector remains fail-closed")
        return

    quote_registry = LiveQuoteRegistry()
    _entry.start_live_quote_server(quote_registry)

    # Continuous price lane (#178). It owns its own authenticated DXLink
    # connection and the prices.json write, independent of the option-chain
    # session below. PRICE_LANE_ENABLED=0 restores the legacy path in
    # _run_session; the two are mutually exclusive.
    price_lane = None
    if price_writer_plan() == "continuous_price_lane":
        lane_s3 = _entry.make_s3()
        price_lane = PriceLane(
            token_manager, quote_registry, lane_s3,
            rolling_store=build_rolling_store(lane_s3),
        )
        price_lane.start()

    # Independent of the QQQ market session (unlike prices_loop/health_loop,
    # started fresh inside every _run_session): EIA STEO has nothing to do
    # with market hours, so this runs continuously off its own s3 client for
    # the life of the process. No-ops immediately if EIA_API_KEY is unset.
    steo_thread = threading.Thread(target=eia_steo_loop, args=(_entry.make_s3(),), daemon=True)
    steo_thread.start()

    blocked_session_date: date | None = None
    while True:
        # A cold start after hours leaves the registry uninitialized for the
        # whole wait, which would show as an unexplained outage all evening.
        # Label the deliberate wait -- but never overwrite a failure: a wait
        # that follows a blocked session is recovery from an auth or startup
        # fault and must keep reporting session_failed.
        announce_scheduler_wait(quote_registry, blocked_session_date)
        _entry.wait_for_premarket(blocked_session_date)
        try:
            _entry._run_session(token_manager, quote_registry)
            # The session ran and ended deliberately; the startup circuit from
            # an earlier failure no longer applies.
            blocked_session_date = None
        except TastyAuthError as exc:
            quote_registry.clear_session(reason="session_failed")
            blocked_session_date = datetime.now(ET).date()
            log.critical(
                f"{exc}; auth circuit open for exchange session {blocked_session_date}"
            )
        except Exception as e:
            quote_registry.clear_session(reason="session_failed")
            blocked_session_date = datetime.now(ET).date()
            log.error(
                f"session failed: {e}; startup circuit open for exchange session "
                f"{blocked_session_date}",
                exc_info=True,
            )

__all__ = ["_run_session", "main"]
