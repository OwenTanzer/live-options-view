"""Continuous price lane: the single writer of ``intraday/prices.json``.

WHY this module: the price strip must survive outside the QQQ/SPY option-chain
session. The chain lifecycle is gated to 06:00-16:15 ET because it authenticates
for option chains; prices need 04:00-20:00 ET equities, overnight futures and
weekend crypto. This lane owns one authenticated DXLink connection with price
subscriptions only, publishes on the existing cadence and never touches option
symbols, scanners, Tradier or trading.

Failure is bounded: auth and feed retries share an exponential backoff, a
per-window auth-cycle budget stops a credential loop, and the lane records its
state in a separate health object instead of impersonating the chain session.
When ``PRICE_LANE_ENABLED=0`` the legacy in-session ``prices_loop`` remains the
writer, so rollback is one environment change and never two writers.
"""

import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import market_data.live_market as _entry
from market_data.overview import INSTRUMENTS
from .config import (
    COLLECT_SPY, ET, PRICES_SECS, PRICE_TICKERS, PRICE_LANE_HEALTH_KEY,
    PRICE_LANE_HEALTH_SECS, PRICE_LANE_MAX_AUTH_CYCLES,
    PRICE_LANE_MAX_BACKOFF_SECS, PRICE_LANE_MIN_BACKOFF_SECS,
    PRICE_LANE_CONTRACT_REFRESH_SECS, PRICE_LANE_RETRY_WINDOW_SECS,
    PRICE_LANE_TIMESALE,
    CONTRACT_RESOLVE_RETRY_MIN_SECS, CONTRACT_RESOLVE_RETRY_MAX_SECS, R2_BUCKET,
    log,
)
from .feed import DXLinkFeed
from .oauth import TastyAuthError, tasty_auth
from .price_history import RollingReferenceStore
from .price_sessions import (
    ExchangeCalendarUnavailable, build_equity_calendar, lane_summary,
    lane_summary_payload, session_for, session_start_for,
)
from .overview_sources import futures_snapshot, resolution_status

CRYPTO_LABELS = tuple(row["label"] for row in INSTRUMENTS if row["asset_class"] == "crypto")
INSTRUMENTS_BY_LABEL = {row["label"]: row for row in INSTRUMENTS}


def new_writer_id() -> str:
    """Identify the publishing process in artifacts and health records."""
    return f"{socket.gethostname()}:{os.getpid()}:price-lane"


def _default_resolver():
    from .overview_sources import resolve_futures
    return resolve_futures


class LaneBackoff:
    """Exponential backoff with a hard cap; success resets the ladder."""

    def __init__(self, min_secs=PRICE_LANE_MIN_BACKOFF_SECS,
                 max_secs=PRICE_LANE_MAX_BACKOFF_SECS):
        self.min_secs = min_secs
        self.max_secs = max_secs
        self.failures = 0

    def fail(self) -> float:
        self.failures += 1
        return min(self.max_secs, self.min_secs * (2 ** (self.failures - 1)))

    def success(self):
        self.failures = 0

    @property
    def delay(self) -> float:
        if not self.failures:
            return 0.0
        return min(self.max_secs, self.min_secs * (2 ** (self.failures - 1)))


class PriceLane:
    """One long-lived price publisher, independent of the chain session."""

    def __init__(self, token_manager, quote_registry=None, s3=None, *,
                 writer_id: str | None = None,
                 clock=None, sleep=None,
                 auth=tasty_auth, feed_factory=DXLinkFeed,
                 resolve=None, publish=None,
                 calendar_builder=build_equity_calendar,
                 rolling_store=None,
                 max_auth_cycles: int = PRICE_LANE_MAX_AUTH_CYCLES,
                 retry_window_secs: int = PRICE_LANE_RETRY_WINDOW_SECS,
                 cadence_secs: float = PRICES_SECS):
        self._token_manager = token_manager
        self._quote_registry = quote_registry
        self._btc = None
        if os.environ.get("BTC_OPTIONS_ENABLED") == "1" and quote_registry is not None:
            from .btc_options import BtcOptions
            self._btc = BtcOptions()
            quote_registry.btc = self._btc
        self._s3 = s3
        self._writer_id = writer_id or new_writer_id()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self._auth = auth
        self._feed_factory = feed_factory
        self._resolve = resolve if resolve is not None else _default_resolver()
        self._publish = publish if publish is not None else _entry.push_prices
        self._calendar_builder = calendar_builder
        self._rolling = rolling_store
        self._max_auth_cycles = max_auth_cycles
        self._retry_window = timedelta(seconds=retry_window_secs)
        self._cadence = cadence_secs
        self._contract_refresh_secs = PRICE_LANE_CONTRACT_REFRESH_SECS
        self._auth_payload: Optional[dict] = None
        self._contracts: dict[str, Optional[str]] = {}
        self._contracts_checked_at: Optional[datetime] = None
        self._contracts_et_date = None
        self._contract_reconciliations = 0
        self._contract_gap_since: Optional[datetime] = None
        self._contract_retry_at: Optional[datetime] = None
        self._contract_gap_reason: Optional[str] = None
        self._contract_gaps = 0
        self._access_token_at: Optional[datetime] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._feed: Optional[DXLinkFeed] = None
        self._auth_window_start: Optional[datetime] = None
        self._auth_cycles = 0
        self._calendar = None
        self._calendar_day = None
        self._backoff = LaneBackoff()
        self._state = "starting"
        self._last_error: Optional[str] = None
        self._last_cycle_at: Optional[datetime] = None
        self._last_success_at: Optional[datetime] = None
        self._consecutive_failures = 0
        self._next_retry_at: Optional[datetime] = None
        self._published_rows = 0
        self._summary_payload: dict = {}
        self._last_health_at: Optional[datetime] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self.run, name="price-lane", daemon=True)
        self._thread.start()
        log.info(f"price lane started  writer_id={self._writer_id}  cadence={self._cadence}s")
        return self._thread

    def stop(self):
        self._stop.set()
        if self._rolling is not None:
            try:
                self._rolling.persist(force=True)
            except Exception:
                pass
        feed = self._feed
        self._feed = None
        if feed is not None:
            try:
                feed.stop()
            except Exception:
                pass
        log.info("price lane stop requested")

    def run(self):
        while not self._stop.is_set():
            delay = self.cycle()
            if self._stop.is_set():
                break
            self._sleep(delay)
        self._state = "stopped"
        log.info("price lane stopped")

    # -- one cycle -----------------------------------------------------------
    def cycle(self) -> float:
        """Authenticate/connect if needed, publish once, and return idle delay."""
        now = self._clock()  # production clock is UTC; keep it for history/publication
        now_et = now.astimezone(ET)
        self._last_cycle_at = now
        try:
            calendar = self._calendar_for(now_et)
        except ExchangeCalendarUnavailable as exc:
            # Prices cannot be labelled confidently without trading days. Keep
            # the lane alive (crypto is still continuous) but withhold the
            # equity session labels rather than guessing.
            calendar = None
            self._last_error = f"exchange_calendar_unavailable:{exc}"
        summary = lane_summary(now_et, calendar)
        self._summary_payload = lane_summary_payload(now_et, calendar)
        try:
            self._ensure_feed()
            self._reconcile_contracts(now, now_et)
            self._keep_feed_alive()
            if self._btc is not None:
                try:
                    self._btc.schedule_refresh(self._feed, self._resolution_token(now), now)
                except Exception:
                    self._btc.status = "subscription_unavailable"
            published = self._publish_once(now, now_et, calendar, summary)
            self._published_rows = published
            if self._rolling is not None:
                # Throttled inside the store; this is the restart-persistence
                # path for the bounded crypto history.
                self._rolling.persist()
            self._mark_success(now)
            return self._cadence
        except TastyAuthError as exc:
            return self._fail(f"tasty_auth:{exc.phase}", now, auth_failure=True)
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}", now)

    def _publish_once(self, now: datetime, now_et: datetime, calendar, summary) -> int:
        if self._feed is None:
            raise RuntimeError("price_lane_feed_unavailable")
        sessions = {}
        session_starts = {}
        for label in list(PRICE_TICKERS) + ["SPY"]:
            instrument = INSTRUMENTS_BY_LABEL.get(label)
            if instrument is None:
                continue
            sessions[label] = session_for(instrument, now_et, calendar).status
            start = session_start_for(instrument, now_et, calendar)
            if start is not None:
                session_starts[label] = start.astimezone(timezone.utc).isoformat()
        context = {
            "writer_id": self._writer_id,
            "summary": self._summary_payload,
            "sessions": sessions,
            "session_starts": session_starts,
            "mode": summary["mode"],
        }
        counters = getattr(self, "_counters", None)
        if counters is None:
            counters = _entry.Counters()
            self._counters = counters
        self._publish(self._s3, self._feed, counters, self._quote_registry,
                      lane_context=context, rolling_store=self._rolling, now_utc=now)
        return len(PRICE_TICKERS)

    # -- connection ----------------------------------------------------------
    def _calendar_for(self, now_et: datetime):
        if self._calendar is not None and self._calendar_day == now_et.date():
            return self._calendar
        calendar = self._calendar_builder(now_et)
        self._calendar = calendar
        self._calendar_day = now_et.date()
        return calendar

    def _price_symbols(self) -> list[str]:
        symbols = [symbol for symbol in PRICE_TICKERS.values() if symbol]
        if COLLECT_SPY:
            symbols.append("SPY")
        return list(dict.fromkeys(symbols))

    def _timesale_symbols(self) -> list[str]:
        """US equity/ETF prints only: futures and crypto already carry time."""
        if not PRICE_LANE_TIMESALE:
            return []
        symbols = []
        for label, symbol in PRICE_TICKERS.items():
            if not symbol:
                continue
            instrument = INSTRUMENTS_BY_LABEL.get(label) or {}
            if instrument.get("asset_class") in ("equity", "etf"):
                symbols.append(symbol)
        if COLLECT_SPY:
            symbols.append("SPY")
        return list(dict.fromkeys(symbols))

    def _auth_budget_allows(self, now: datetime) -> bool:
        if self._auth_window_start is None or now - self._auth_window_start > self._retry_window:
            self._auth_window_start = now
            self._auth_cycles = 0
        return self._auth_cycles < self._max_auth_cycles

    def _ensure_feed(self):
        if self._feed is not None and not self._feed.needs_reauth():
            return
        if self._feed is not None:
            self._feed.stop()
            self._feed = None
        now = self._clock()
        if not self._auth_budget_allows(now):
            raise TastyAuthError("lane-budget", detail="auth_cycle_budget_exhausted")
        self._state = "authenticating"
        self._auth_cycles += 1
        auth = self._auth(self._token_manager)
        self._auth_payload = dict(auth)
        self._access_token_at = now
        try:
            self._resolve(auth["access_token"])
        except Exception as exc:
            # Contract resolution is best-effort; unresolved futures stay
            # explicitly unavailable rather than subscribing to a guess.
            log.warning(f"price lane futures resolution unavailable: {type(exc).__name__}")
        self._record_resolution_outcome(now)
        symbols = self._price_symbols()
        feed = self._feed_factory(auth["streamer_url"], auth["streamer_token"])
        feed.set_subscriptions([], symbols, self._timesale_symbols())  # Option symbols are never part of this lane.
        feed.start()
        feed.wait_ready(timeout=30)
        self._feed = feed
        self._contracts = futures_snapshot()
        self._contracts_checked_at = now
        self._contracts_et_date = now.astimezone(ET).date()
        self._state = "streaming"
        log.info(f"price lane feed ready  symbols={len(symbols)}  options=0")

    def _resolution_token(self, now: datetime) -> Optional[str]:
        """An access token for contract resolution, refreshed when it has aged.

        The lane authenticates once and can run for days; a REST access token
        issued at startup is long expired by the six-hour reconciliation. Live
        November 9 evidence: resolution succeeded after startup and then failed
        at 17:54Z and again at 23:54Z while `auth_cycles_this_window` stayed 1.
        Refreshing is budgeted like any other auth attempt.
        """
        aged = (self._access_token_at is None
                or (now - self._access_token_at).total_seconds() >= 600)
        if not aged or not self._auth_budget_allows(now):
            return (self._auth_payload or {}).get("access_token")
        try:
            self._auth_cycles += 1
            auth = self._auth(self._token_manager)
        except Exception as exc:
            log.warning(f"price lane resolution token refresh failed: {type(exc).__name__}")
            return (self._auth_payload or {}).get("access_token")
        # Persist the renewed credentials for every later REST attempt. The live
        # feed keeps the streamer token it connected with; the refreshed payload
        # serves resolution now and any later rebuild. Without this, the next
        # attempt 61 seconds later reused the expired token.
        merged = dict(self._auth_payload or {})
        merged.update(auth)
        self._auth_payload = merged
        self._access_token_at = now
        return auth["access_token"]

    def _record_resolution_outcome(self, now: datetime) -> None:
        """Bounded retry when discovery is degraded, even if the map survived."""
        status = resolution_status()
        if status["state"] == "ok":
            self._contract_retry_at = None
            self._contract_gap_since = None
            self._contract_gap_reason = None
            self._contract_gaps = 0
            return
        if status["state"] not in ("degraded", "partial"):
            return
        if self._contract_retry_at is None or now >= self._contract_retry_at:
            self._contract_retry_at = now + timedelta(seconds=self._contract_retry_delay())
        self._contract_gap_since = self._contract_gap_since or now
        self._contract_gap_reason = f"resolution_{status['state']}"

    def _contract_retry_delay(self) -> float:
        """Bounded retry after an incomplete resolution (not the 6h cadence)."""
        self._contract_gaps += 1
        return min(CONTRACT_RESOLVE_RETRY_MAX_SECS,
                   CONTRACT_RESOLVE_RETRY_MIN_SECS * (2 ** (self._contract_gaps - 1)))

    def _reconcile_contracts(self, now: datetime, now_et: datetime) -> None:
        """Keep the lane's subscriptions on the same contracts the rows name.

        A healthy feed is not evidence that the active month still matches the
        shared mapping: the chain session can resolve a new month, and a daily
        roll changes it without any disconnect. Re-resolve on a bounded cadence
        and rebuild this lane's own feed when the mapping changes.

        A failed or partial resolution is NOT a roll. Preserving the last good
        mapping keeps a healthy contract subscribed and retries on a bounded
        cadence; the November 9 failure instead rebuilt onto a null snapshot,
        which silently unsubscribed every futures symbol until the next day.
        """
        if self._feed is None:
            return
        due = (
            self._contracts_checked_at is None
            or self._contracts_et_date != now_et.date()
            or (now - self._contracts_checked_at).total_seconds() >= self._contract_refresh_secs
        )
        if self._contract_retry_at is not None and now >= self._contract_retry_at:
            due = True
        if due and self._auth_payload is not None:
            try:
                token = self._resolution_token(now)
                if token:
                    self._resolve(token, force=True)
            except Exception as exc:
                log.warning(f"price lane contract refresh failed: {type(exc).__name__}")
            self._contracts_checked_at = now
            self._contracts_et_date = now_et.date()
            self._record_resolution_outcome(now)
        snapshot = futures_snapshot()
        previous = dict(self._contracts or {})
        if snapshot == previous:
            # A preserved map looks unchanged; only the resolver's own recovery
            # may clear a retry deadline. Clearing it here on a degraded
            # resolution was the review blocker: the deadline vanished while the
            # provider was still failing.
            if (previous and all(previous.values())
                    and resolution_status()["state"] == "ok"):
                self._contract_gaps = 0
                self._contract_retry_at = None
                self._contract_gap_since = None
                self._contract_gap_reason = None
            return
        lost = [label for label, sym in previous.items() if sym and not snapshot.get(label)]
        changed = [label for label, sym in snapshot.items()
                   if sym and sym != previous.get(label)]
        if lost and not changed:
            # Resolution gap, not a roll: keep the current subscriptions and
            # retry soon instead of unsubscribing a healthy contract.
            if self._contract_retry_at is None or now >= self._contract_retry_at:
                self._contract_retry_at = now + timedelta(seconds=self._contract_retry_delay())
            self._contract_gap_since = self._contract_gap_since or now
            self._contract_gap_reason = "resolution_incomplete_kept_last_known"
            log.warning(
                f"price lane contract resolution incomplete; keeping "
                f"{sum(1 for value in previous.values() if value)} known contracts")
            return
        # A changed snapshot is provider-directed and actionable even while a
        # resolution retry is pending; only a failed replacement feed suppresses
        # another attempt.
        if (self._contract_retry_at is not None and now < self._contract_retry_at
                and self._contract_gap_reason == "subscription_rebuild_failed"):
            return
        if self._rebuild_feed(snapshot):
            self._contracts = dict(snapshot)
            self._contract_reconciliations += 1
            self._record_resolution_outcome(now)
        else:
            # Never connect-storm: a failed replacement backs off like any
            # other lane failure, and the old feed keeps streaming meanwhile.
            self._contract_gap_since = self._contract_gap_since or now
            self._contract_retry_at = now + timedelta(seconds=self._contract_retry_delay())
            self._contract_gap_reason = "subscription_rebuild_failed"

    def _rebuild_feed(self, snapshot: dict) -> bool:
        """Replace only this lane's connection; chain subscriptions are elsewhere.

        The replacement feed is started before the old one is stopped, so a
        failed rebuild cannot leave the lane with no subscriptions at all.
        """
        if self._auth_payload is None:
            return False
        symbols = self._price_symbols()
        new_feed = None
        try:
            new_feed = self._feed_factory(self._auth_payload["streamer_url"],
                                          self._auth_payload["streamer_token"])
            new_feed.set_subscriptions([], symbols, self._timesale_symbols())
            new_feed.start()
            if not new_feed.wait_ready(timeout=30):
                raise RuntimeError("price_lane_feed_not_ready")
        except Exception as exc:
            log.warning(f"price lane subscription rebuild failed: {type(exc).__name__}")
            if new_feed is not None:
                try:
                    new_feed.stop()
                except Exception:
                    pass
            return False
        old = self._feed
        self._feed = new_feed
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        log.info(f"price lane subscriptions reconciled  symbols={len(symbols)}  options=0")
        return True

    def _keep_feed_alive(self):
        feed = self._feed
        if feed is None:
            return
        feed.restart_if_dead()
        if feed.needs_reauth():
            raise TastyAuthError("streamer-reauth", detail="price_lane_feed_auth_failed")

    # -- health and failure --------------------------------------------------
    def _mark_success(self, now: datetime):
        self._backoff.success()
        self._state = "streaming"
        self._last_error = None
        self._last_success_at = now
        self._consecutive_failures = 0
        self._next_retry_at = None
        self._write_health(now)

    def _fail(self, reason: str, now: datetime, auth_failure: bool = False) -> float:
        delay = self._backoff.fail()
        self._consecutive_failures += 1
        self._last_error = reason
        self._state = "backing_off"
        self._next_retry_at = now + timedelta(seconds=delay)
        if auth_failure:
            feed = self._feed
            self._feed = None
            if feed is not None:
                try:
                    feed.stop()
                except Exception:
                    pass
        log.error(f"price lane cycle failed ({reason}); retry in {int(delay)}s")
        self._write_health(now, force=True)
        return delay

    def health(self) -> dict:
        feed_health = self._feed.get_health() if self._feed is not None else None
        return {
            "state": self._state,
            "writer_id": self._writer_id,
            "last_cycle_at": _iso(self._last_cycle_at),
            "last_success_at": _iso(self._last_success_at),
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "next_retry_at": _iso(self._next_retry_at),
            "published_rows": self._published_rows,
            "cadence_seconds": self._cadence,
            "auth_cycles_this_window": self._auth_cycles,
            "auth_cycle_budget": self._max_auth_cycles,
            "contracts_checked_at": _iso(self._contracts_checked_at),
            "contract_reconciliations": self._contract_reconciliations,
            "contract_gap_since": _iso(self._contract_gap_since),
            "contract_retry_at": _iso(self._contract_retry_at),
            "contract_gap_reason": self._contract_gap_reason,
            "contracts": dict(self._contracts),
            "contract_resolution": resolution_status(),
            "feed": feed_health,
            "lane": self._summary_payload,
            "rolling_references": (self._rolling.status() if self._rolling is not None else None),
            "option_symbols_subscribed": 0,
        }

    def _write_health(self, now: datetime, force: bool = False):
        if self._s3 is None:
            return
        if (not force and self._last_health_at is not None
                and (now - self._last_health_at).total_seconds() < PRICE_LANE_HEALTH_SECS):
            return
        try:
            import json
            payload = json.dumps({**self.health(), "updated_at": _iso(now)},
                                 default=str, allow_nan=False)
            self._s3.put_object(Bucket=R2_BUCKET, Key=PRICE_LANE_HEALTH_KEY,
                                Body=payload.encode(), ContentType="application/json",
                                CacheControl="no-cache, max-age=0")
            self._last_health_at = now
        except Exception as exc:
            log.warning(f"price lane health write failed: {type(exc).__name__}")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def build_rolling_store(s3) -> RollingReferenceStore:
    store = RollingReferenceStore(s3)
    store.load()
    return store


__all__ = [
    "CRYPTO_LABELS", "PriceLane", "LaneBackoff", "new_writer_id",
    "build_rolling_store", "_iso",
]
