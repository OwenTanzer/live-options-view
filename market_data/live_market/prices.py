"""Publish the asynchronous price strip without confusing observation and receipt.

Yahoo's bounded daily fallback intentionally does not claim an intraday time.
Cache/carry-forward retain the entire observation/reference pair and disclose
newer refresh failures. Provider requests and publication cadence are unchanged.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import boto3
import yfinance as yf

import market_data.live_market as _entry
from market_data.overview import INSTRUMENTS, UNIVERSE_VERSION
from .config import (
    COLLECT_SPY, ET, EXTENDED_QUOTE_MAX_AGE_SECS, EXTENDED_QUOTE_MAX_SPREAD_PCT,
    PRICES_SECS, PRICE_TICKERS, R2_BUCKET, STALE_FEED_SECS,
    YF_SYMBOL_MAP, log, past_stop,
)
from .feed import DXLinkFeed
from .price_sessions import EXTENDED_STATUSES
from .quotes import LiveQuoteRegistry, _observed_spy_price_with_basis, _older_observed
from .state import Counters, QQQ_SESSION, _last_prices


def make_s3(*, publication=False):
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
        **({"config": Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1})} if publication else {}),
    )


def _classify_startup(s3, process_start: datetime) -> str:
    try:
        prior = json.loads(s3.get_object(Bucket=R2_BUCKET, Key="intraday/health.json")["Body"].read())
    except Exception:
        return "clean_start"
    if prior.get("collector", {}).get("past_stop", False):
        return "clean_start"
    if prior.get("updated_at"):
        try:
            prior_dt = datetime.fromisoformat(prior["updated_at"].replace("Z", "+00:00"))
            return "recovery_after_crash" if (process_start - prior_dt).total_seconds() < 7200 else "recovery_after_gap"
        except Exception:
            pass
    return "unknown"


def _log_ticker_health(feed: DXLinkFeed):
    state = feed.get_state()
    for label, sym in PRICE_TICKERS.items():
        d = state.get(sym, {})
        price = d.get("last") or ((d["bid"] + d["ask"]) / 2
                                  if d.get("bid") is not None and d.get("ask") is not None else None)
        log.info(f"price ticker {label} ({sym}): {price if price is not None else 'NO DATA'}")


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _positive(value):
    value = _finite(value)
    return value if value is not None and value > 0 else None


def _instrument_id(label):
    # Loaded from the canonical registry (not a second subscription universe).
    return next(row["id"] for row in INSTRUMENTS if row["label"] == label)


YF_FETCH_TIMEOUT_SECS = 20
YF_COOLDOWN_SECS = 300
YF_MIN_INTERVAL_SECS = 300
_yf_cooldown_until: list = [None]
_yf_cache: dict = {"ts": None, "data": None}
_yf_lock = threading.Lock()
_yf_inflight = None
_yf_result = None


def _failure(error, now=None):
    return {"price": None, "last_attempt_at": now or datetime.now(timezone.utc).isoformat(),
            "refresh_error": error}


def fetch_yf_prices(labels=None) -> dict:
    """Get dated, unadjusted daily bars; no fast_info or invented close time.

    The provider's exchange-local date and timezone are retained, including a
    possibly still-forming last bar. 'daily_bar_close' is not 'official_close'.
    The preceding valid bar is a dated reference, not an inferred yesterday.
    """
    selected = {k: v for k, v in YF_SYMBOL_MAP.items() if labels is None or k in labels}
    tickers = yf.Tickers(" ".join(selected.values()))
    def fetch_one(item):
        label, sym = item
        result = {}
        try:
            bars = tickers.tickers[sym].history(period="5d", interval="1d", auto_adjust=False, timeout=10)
            valid = [(stamp, _positive(row.get("Close"))) for stamp, row in bars.iterrows()]
            valid = [(stamp, price) for stamp, price in valid if price is not None]
            if not valid:
                result[label] = _failure("no_dated_daily_observation")
                return label, result[label]
            stamp, price = valid[-1]
            zone = str(stamp.tzinfo) if stamp.tzinfo is not None else None
            # The consumer refuses an artifact whose zone it cannot interpret, so a
            # non-IANA zone string must not enter the public payload.
            if not zone:
                result[label] = _failure("daily_observation_timezone_unknown")
                return label, result[label]
            try:
                ZoneInfo(zone)
            except Exception:
                result[label] = _failure("daily_observation_timezone_unknown")
                return label, result[label]
            now = datetime.now(timezone.utc).isoformat()
            identity = _instrument_id(label)
            reference = None
            if len(valid) > 1:
                prev_stamp, prev = valid[-2]
                reference = {
                    "instrument_id": identity, "value": prev, "source": "yfinance",
                    "source_symbol": sym, "price_basis": "daily_bar_close",
                    "observed_at": None, "observation_time_reason": "daily_bar_has_no_intraday_time",
                    "observation_date": prev_stamp.date().isoformat(),
                    "observation_timezone": str(prev_stamp.tzinfo), "observation_granularity": "day",
                    "session": "daily", "adjustment_basis": "unadjusted", "resolved_contract": None,
                }
            result[label] = {
                "instrument_id": identity, "price": price, "bid": None, "ask": None,
                "volume": None, "prev_close": reference["value"] if reference else None,
                "source": "yfinance", "source_symbol": sym, "price_basis": "daily_bar_close",
                "observed_at": None, "observation_time_reason": "daily_bar_has_no_intraday_time",
                "observation_date": stamp.date().isoformat(),
                "observation_timezone": zone, "observation_granularity": "day", "session": "daily",
                "adjustment_basis": "unadjusted", "resolved_contract": None,
                "collector_received_at": None, "quote_ts": None,
                "acquired_at": now, "last_success_at": now, "last_attempt_at": now,
                "refresh_error": None, "reference": reference,
            }
        except Exception:
            # No raw provider exception text (URLs/credentials) in public artifacts.
            result[label] = _failure("daily_observation_fetch_failed")
        return label, result[label]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(fetch_one, selected.items()))


def fetch_yf_prices_bounded(labels=None) -> dict:
    """One coalesced worker, four requests at once, five-minute refresh budget.

    A timed-out worker retains its eventual result for the next publication.
    Only currently missing instruments are requested, not the whole universe.
    """
    global _yf_inflight, _yf_result
    wanted = set(YF_SYMBOL_MAP) if labels is None else set(labels)
    with _yf_lock:
        now = datetime.now(timezone.utc)
        # Consume completed work before checking the cooldown: timeout must not
        # throw away a later successful response.
        completed = _yf_inflight is not None and not _yf_inflight.is_alive()
        result = _yf_result if completed else None
        if completed:
            _yf_inflight = None
            _yf_result = None
        elif (_yf_cache['ts'] is not None and
                (now - _yf_cache['ts']).total_seconds() < YF_MIN_INTERVAL_SECS):
            return copy.deepcopy(_yf_cache['data'] or {})
        if result is None and _yf_inflight is None:
            requested = wanted.copy()
            def worker():
                global _yf_result
                try:
                    data = fetch_yf_prices(requested)
                    if 'UST2Y' in requested:
                        from .overview_sources import fetch_treasury
                        try:
                            data['UST2Y'] = fetch_treasury()
                        except Exception:
                            data['UST2Y'] = _failure('treasury_daily_fetch_failed')
                    _yf_result = data
                except Exception:
                    _yf_result = {}
            _yf_inflight = threading.Thread(target=worker, name='overview-daily', daemon=True)
            _yf_inflight.start()
            _yf_inflight.join(timeout=YF_FETCH_TIMEOUT_SECS)
            if not _yf_inflight.is_alive():
                result, _yf_result, _yf_inflight = _yf_result, None, None
            error = 'daily_observation_timeout'
        else:
            error = 'daily_observation_fetch_still_running'
        merged = copy.deepcopy(_yf_cache['data'] or {})
        for label in wanted | set(result or {}):
            attempt = (result or {}).get(label, _failure(error, now.isoformat()))
            if _finite(attempt.get('price')) is not None:
                merged[label] = copy.deepcopy(attempt)
            else:
                merged.setdefault(label, {'price': None}).update(
                    last_attempt_at=attempt.get('last_attempt_at', now.isoformat()),
                    refresh_error=attempt.get('refresh_error', error))
        _yf_cache.update(ts=now, data=merged)
        _yf_cooldown_until[0] = now + timedelta(seconds=YF_MIN_INTERVAL_SECS)
        return copy.deepcopy(merged)


def _parse_instant(value):
    """Parse a provider/collector ISO instant; a naive string has no zone and
    is rejected rather than assumed local."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _qualified_extended_quote(raw, now):
    """A two-sided quote that may stand in for an active extended session.

    Requirements are deliberately strict, because a midpoint is not a price
    when one side is a placeholder: both sides present and ordered, both
    collector receipts inside the stale window, and a spread under the
    configured bound. Trades carry provider event time; quotes on this feed
    usually do not, so the provider time is used only when BOTH sides carry
    one and is never inferred from the receipt clock.
    """
    bid, ask = _positive(raw.get("bid")), _positive(raw.get("ask"))
    bid_ts, ask_ts = _parse_instant(raw.get("bid_ts")), _parse_instant(raw.get("ask_ts"))
    if bid is None or ask is None or bid_ts is None or ask_ts is None or ask < bid:
        return None
    try:
        if (now - min(bid_ts, ask_ts)).total_seconds() > EXTENDED_QUOTE_MAX_AGE_SECS:
            return None
    except TypeError:
        return None
    mid = _finite(round(bid / 2 + ask / 2, 4))
    if mid is None or mid <= 0:
        return None
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > EXTENDED_QUOTE_MAX_SPREAD_PCT:
        return None
    observed = None
    bid_observed = _parse_instant(raw.get("bid_observed_at"))
    ask_observed = _parse_instant(raw.get("ask_observed_at"))
    if bid_observed is not None and ask_observed is not None:
        observed = _iso(min(bid_observed, ask_observed))
    return {
        "price": mid, "receipt": _iso(max(bid_ts, ask_ts)), "observed": observed,
        "basis": "bid_ask_midpoint",
        "reason": None if observed else "provider_quote_time_unavailable",
        "session": None,
    }


def _midpoint_input(raw):
    """Two-sided midpoint when no trade exists at all; strict gates are not
    applied here because there is no other price to fall back to."""
    bid, ask = _positive(raw.get("bid")), _positive(raw.get("ask"))
    if bid is None or ask is None or ask < bid:
        return None
    mid = _finite(round(bid / 2 + ask / 2, 4))
    if mid is None or mid <= 0:
        return None
    bid_ts, ask_ts = _parse_instant(raw.get("bid_ts")), _parse_instant(raw.get("ask_ts"))
    receipt = _iso(min(bid_ts, ask_ts)) if bid_ts is not None and ask_ts is not None else None
    observed = None
    bid_observed = _parse_instant(raw.get("bid_observed_at"))
    ask_observed = _parse_instant(raw.get("ask_observed_at"))
    if bid_observed is not None and ask_observed is not None:
        observed = _iso(min(bid_observed, ask_observed))
    return {
        "price": mid, "receipt": receipt, "observed": observed,
        "basis": "bid_ask_midpoint",
        "reason": None if observed else "provider_event_time_unavailable",
        "session": None,
    }


def _print_selection(raw, *, market_session, session_start, trade_observed):
    """A provider-timed TimeAndSale print for the active extended session.

    Only extended sessions consult prints. The print must carry its own
    provider time inside the session, must not be marked regular-session by the
    provider, and must be newer than the snapshot Trade event; otherwise the
    caller keeps its existing selection. The collector receipt is carried
    separately as the row's quote/receipt time and never becomes the
    observation time.
    """
    if market_session not in EXTENDED_STATUSES:
        return None
    price = _positive(raw.get("ts_price"))
    observed = _parse_instant(raw.get("ts_time"))
    receipt = _parse_instant(raw.get("ts_received_at"))
    if price is None or observed is None or receipt is None:
        return None
    start = _parse_instant(session_start) if session_start else None
    if start is not None and observed < start:
        return None
    if raw.get("ts_extended") is False:
        return None
    if trade_observed is not None and observed <= trade_observed:
        return None
    return {"price": price, "receipt": _iso(receipt), "observed": _iso(observed),
            "basis": "last_trade", "reason": None, "session": "extended"}


def _select_price_input(raw, *, market_session=None, session_start=None, now=None):
    """Select the price for the active session with its exact provenance.

    A last trade is current only when it carries a provider event time inside
    the active session. During premarket/after-hours a trade that predates the
    session is last session's close: a qualified live two-sided quote then
    becomes the current price, with an unknown provider time when the feed
    supplies none. A stale close is retained rather than replaced by a wide or
    half-updated quote.
    """
    now = now or datetime.now(timezone.utc)
    last = _positive(raw.get("last"))
    last_receipt = _parse_instant(raw.get("last_ts"))
    last_observed = _parse_instant(raw.get("last_observed_at"))
    start = _parse_instant(session_start) if session_start else None
    if last is not None:
        trade_current = last_observed is not None and (
            start is None or last_observed >= start)
        if trade_current:
            print_input = _print_selection(
                raw, market_session=market_session, session_start=session_start,
                trade_observed=last_observed)
            if print_input is not None:
                return print_input
            return {"price": last, "receipt": _iso(last_receipt),
                    "observed": _iso(last_observed), "basis": "last_trade",
                    "reason": None, "session": raw.get("last_session")}
        print_input = _print_selection(
            raw, market_session=market_session, session_start=session_start,
            trade_observed=last_observed)
        if print_input is not None:
            return print_input
        if market_session in EXTENDED_STATUSES:
            quote = _qualified_extended_quote(raw, now)
            if quote is not None:
                return quote
        return {"price": last, "receipt": _iso(last_receipt),
                "observed": _iso(last_observed), "basis": "last_trade",
                "reason": None if last_observed else "provider_event_time_unavailable",
                "session": raw.get("last_session")}
    print_input = _print_selection(
        raw, market_session=market_session, session_start=session_start,
        trade_observed=None)
    if print_input is not None:
        return print_input
    return _midpoint_input(raw)


def _spy_selection(raw):
    """SPY keeps its accepted midpoint-first selector, with event provenance."""
    price, receipt, basis = _observed_spy_price_with_basis(raw)
    observed = None
    if basis == "bid_ask_midpoint":
        observed = _older_observed(
            ("bid", raw.get("bid_observed_at")), ("ask", raw.get("ask_observed_at")))
    elif basis == "last_trade":
        observed = _iso(_parse_instant(raw.get("last_observed_at")))
    reason = None
    if price is not None and not observed:
        reason = ("provider_quote_time_unavailable" if basis == "bid_ask_midpoint"
                  else "provider_event_time_unavailable")
    return {"price": price, "receipt": receipt, "observed": observed, "basis": basis,
            "reason": reason, "session": None}


def _dx_row(label, sym, raw, selection=None, *, market_session=None,
            session_start=None, now=None):
    if selection is None:
        selection = _select_price_input(raw, market_session=market_session,
                                        session_start=session_start, now=now)
    price = selection.get("price") if selection else None
    receipt = selection.get("receipt") if selection else None
    observed = selection.get("observed") if selection else None
    basis = selection.get("basis") if selection else None
    reason = selection.get("reason") if selection else None
    instrument_session = selection.get("session") if selection else None
    from .overview_sources import RESOLVED_CONTRACTS
    contract = RESOLVED_CONTRACTS.get(label)
    identity = _instrument_id(label)
    prev = _finite(raw.get("prev_close"))
    reference = None
    if prev is not None:
        reference = {
            "instrument_id": identity, "value": prev, "source": "dxlink", "source_symbol": sym,
            "price_basis": "previous_close", "observed_at": None,
            "observation_time_reason": "summary_has_no_observation_timestamp",
            "observation_date": raw.get("prev_close_date"), "observation_timezone": None,
            # Day ID retains its provider date, but Summary supplies no zone.
            "observation_granularity": None,
            "session": None, "adjustment_basis": "unknown", "resolved_contract": contract,
        }
    return {
        "instrument_id": identity, "price": price, "bid": _positive(raw.get("bid")),
        "ask": _positive(raw.get("ask")),
        "prev_close": prev, "volume": raw.get("volume"),
        "source": "dxlink" if price is not None else None,
        "source_symbol": sym if price is not None else None, "price_basis": basis,
        "quote_ts": receipt, "collector_received_at": receipt,
        "observed_at": observed if price is not None else None,
        "observation_time_reason": (reason if price is not None else None),
        "observation_date": None, "observation_timezone": "UTC" if observed else None,
        "observation_granularity": "event" if observed else None,
        "session": instrument_session,
        "adjustment_basis": "unknown", "resolved_contract": contract,
        "acquired_at": receipt, "last_success_at": receipt,
        "last_attempt_at": receipt, "refresh_error": None, "reference": reference,
    }


def _attach_lane_observations(prices, lane_context, rolling_store, now):
    """Additive (#178) session and reference fields; the legacy contract is untouched."""
    from .price_history import change_percent

    sessions = lane_context.get("sessions") or {}
    instruments = {row["label"]: row for row in INSTRUMENTS}
    for label, row in prices.items():
        instrument = instruments.get(label) or {}
        session = sessions.get(label)
        if session:
            row["market_session"] = session
        asset_class = instrument.get("asset_class")
        if asset_class == "crypto":
            reference = None
            if rolling_store is not None:
                if (row.get("price") is not None and row.get("source") == "dxlink"
                        and not row.get("stale")):
                    rolling_store.observe(label, row["price"],
                                          row.get("observed_at") or row.get("quote_ts"))
                reference = rolling_store.reference(
                    label, now, instrument_id=row.get("instrument_id"),
                    source_symbol=row.get("source_symbol") or instrument.get("dxlink_symbol"))
            row["rolling_reference"] = reference
            row["rolling_change_pct"] = change_percent(row.get("price"), reference) if reference else None
            row["change_basis"] = "rolling_24h" if reference else "unavailable"
        elif instrument.get("profile") == "FU":
            reference = row.get("reference") or {}
            current_contract = row.get("resolved_contract") or {}
            same_contract = bool(current_contract) and (
                reference.get("resolved_contract") == current_contract)
            row["rolling_reference"] = None
            row["rolling_change_pct"] = None
            row["change_basis"] = ("prior_day_close_same_contract" if same_contract
                                   else "contract_reference_unverified")
        elif row.get("source") in ("dxlink", "last-known") and instrument.get("asset_class") in (
                "equity", "etf", "index"):
            row["rolling_reference"] = None
            row["rolling_change_pct"] = None
            row["change_basis"] = "previous_close_unverified"
        else:
            row["rolling_reference"] = None
            row["rolling_change_pct"] = None
            row["change_basis"] = "dated_or_unknown"
    return prices


def _change(row):
    reference = row.get("reference")
    # Never combine a fallback's value with another identity/source's Summary.
    if (reference and reference.get("instrument_id") == row.get("instrument_id")
            and reference.get("source_symbol") == row.get("source_symbol")
            and reference.get("source") == row.get("origin_source", row.get("source"))):
        prev = _finite(reference.get("value"))
    else:
        prev = None
    # The compact strip has never shown a change for the daily-fallback tiles.
    # Keep that display contract unchanged: the dated pair travels in
    # `reference`, where the overview consumer reports it with its real basis.
    if row.get("origin_source", row.get("source")) != "dxlink":
        row["prev_close"] = None
        row["chg_pct"] = None
        return
    row["prev_close"] = prev
    row["chg_pct"] = _finite(round((row["price"] - prev) / prev * 100, 2)) if row.get("price") is not None and prev is not None and prev > 0 else None


def push_prices(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None, *,
                lane_context: Optional[dict] = None,
                rolling_store=None,
                now_utc: Optional[datetime] = None):
    state, fh = feed.get_state(), feed.get_health()
    ts_utc = now_utc or datetime.now(timezone.utc)
    last_event = fh["last_feed_event_time"]
    feed_stale = last_event is None or (ts_utc - last_event).total_seconds() > STALE_FEED_SECS
    sessions = (lane_context or {}).get("sessions") or {}
    session_starts = (lane_context or {}).get("session_starts") or {}
    prices = {}
    for label, sym in PRICE_TICKERS.items():
        raw = state.get(sym, {}) if sym else {}
        prices[label] = _dx_row(label, sym, raw, market_session=sessions.get(label),
                                session_start=session_starts.get(label), now=ts_utc)
    missing = [label for label, row in prices.items() if row["price"] is None]
    yf_data = _entry.fetch_yf_prices_bounded(missing) if missing else {}
    for label in missing:
        row = prices[label]
        attempt = yf_data.get(label)
        # Explicit map gate excludes JPY spot and any unapproved fallback.
        if ((label in YF_SYMBOL_MAP or label == "UST2Y") and isinstance(attempt, dict)
                and _positive(attempt.get("price")) is not None
                and attempt.get("instrument_id") == row["instrument_id"]
                and attempt.get("source_symbol") == ("BC_2YEAR" if label == "UST2Y" else YF_SYMBOL_MAP[label])):
            prices[label] = copy.deepcopy(attempt)
            if label == "QQQ":
                QQQ_SESSION.last_spot[:] = [attempt["price"], None]
            continue
        remembered = _last_prices.get(label)
        if remembered is not None:
            # Entire pair, including original acquisition and provider times.
            carried = {**row, **copy.deepcopy(remembered)}
            carried.update(source="last-known", origin_source=remembered.get("origin_source", remembered.get("source")), stale=True)
            # Older restart seeds have only a value: keep unknown provenance
            # unknown, and never inherit a partial current DX Summary reference.
            if "instrument_id" not in remembered:
                carried.update(reference=None, source_symbol=remembered.get("source_symbol"),
                               collector_received_at=remembered.get("quote_ts"),
                               acquired_at=None, last_success_at=None)
            prices[label] = carried
        prices[label].update(
            last_attempt_at=attempt.get("last_attempt_at", ts_utc.isoformat()) if isinstance(attempt, dict) else ts_utc.isoformat(),
            refresh_error=attempt.get("refresh_error") or "observation_unavailable" if isinstance(attempt, dict) else "observation_unavailable",
        )
    # SPY retains its receipt-based price selector, with event provenance added.
    raw_spy = state.get("SPY", {}) if COLLECT_SPY else {}
    prices["SPY"] = _dx_row("SPY", "SPY", raw_spy, _spy_selection(raw_spy),
                            market_session=sessions.get("SPY"), now=ts_utc)
    for label, row in prices.items():
        _change(row)
        if row["price"] is not None and label != "SPY":
            # SPY is not a legacy macro CSV column or last-known tile.
            _last_prices[label] = copy.deepcopy(row)
    if lane_context is not None:
        _attach_lane_observations(prices, lane_context, rolling_store, ts_utc)
    if quote_registry is not None:
        quote_registry.update_ticker_fallbacks(prices, ts_utc.isoformat())
    payload = {
        "schema_version": 2, "universe_version": UNIVERSE_VERSION, "timestamp": ts_utc.isoformat(),
        "snapshot_time": ts_utc.astimezone(ET).strftime("%H:%M ET"),
        "feed_stale": feed_stale, "prices": prices,
    }
    if lane_context is not None:
        payload["price_lane"] = {
            "writer_id": lane_context.get("writer_id"),
            **copy.deepcopy(lane_context.get("summary") or {}),
            "sessions_by_label": dict(lane_context.get("sessions") or {}),
        }
    payload = json.dumps(payload, default=str, allow_nan=False)
    try:
        s3.put_object(Bucket=R2_BUCKET, Key="intraday/prices.json", Body=payload.encode(),
                      ContentType="application/json", CacheControl="no-cache, max-age=0")
        counters.inc_prices(ts_utc.isoformat())
    except Exception:
        counters.inc_failure()
        raise


def prices_loop(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    while not past_stop():
        try:
            push_prices(s3, feed, counters, quote_registry)
        except Exception as e:
            log.error(f"prices.json error: {e}")
        time.sleep(PRICES_SECS)
    log.info("prices loop stopped")


__all__ = [
    "make_s3", "_classify_startup", "_log_ticker_health", "YF_FETCH_TIMEOUT_SECS",
    "YF_COOLDOWN_SECS", "YF_MIN_INTERVAL_SECS", "_yf_cooldown_until", "_yf_cache",
    "fetch_yf_prices", "fetch_yf_prices_bounded", "push_prices", "prices_loop",
]
