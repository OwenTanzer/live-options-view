"""Opening OI path forecasts for four isolated QQQ long/cash paper accounts."""
import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta
from functools import lru_cache, partial
from pathlib import Path
from urllib.parse import urlsplit

import requests

from ..clock import ET
from ..config import BASE_URL
from ..exchange_calendar import preceding_session, session_close
from ..market import QuoteRateLimited
from ..oi_associator import ARCHITECTURES, forecast
from ..strategy import Decision, register

IDS = {"oi_associator_" + name: name for name in ARCHITECTURES}
VERSION = "1.0.0"
MODEL = Path(__file__).resolve().parents[1] / "oi_associator_v1.json"
ENTRY = time(9, 35)
EXIT = time(15, 30)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("unqualified timestamp")
    return result.astimezone(ET)


@lru_cache(maxsize=1)
def load_bundle():
    raw = MODEL.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=4)
def opening(day):
    """Query an immutable as-of boundary, shared across the four accounts."""
    prior = preceding_session(day)
    if not prior:
        raise ValueError("verified previous exchange session unavailable")
    base = urlsplit(BASE_URL)
    if base.scheme != "https" and not (base.scheme == "http" and base.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("opening feed requires a trusted HTTPS origin")
    cutoff = int(datetime.combine(day, ENTRY, ET).timestamp() * 1000)
    response = requests.get(BASE_URL.rstrip("/") + "/api/one-clock/opening-oi",
                            params={"session": day.isoformat(), "cutoff": cutoff},
                            timeout=5, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("opening reference HTTP " + str(response.status_code))
    packet = response.json()
    cal = packet["calibration"]
    opened = int(datetime.combine(day, time(9, 30), ET).timestamp() * 1000)
    evidence = cal["openingEvidence"]
    if (packet["session"] != day.isoformat() or cal["settledSession"] != prior[0].date().isoformat()
            or cal["openingAt"] != opened or not opened <= packet["capturedAt"] <= packet["availableAt"] <= cutoff
            or not isinstance(packet["etag"], str) or not packet["etag"]
            or not re.fullmatch(r"intraday/" + day.strftime("%Y%m%d") + r"/opening-oi/v1/[0-9]{4}\.json", evidence)
            or not (cal["tier"] in {"0DTE_Weekly","0DTE_Regular","0DTE_Monthly"})):
        raise ValueError("opening reference identity or availability mismatch")
    spot = cal["openingSpot"]
    if not finite(spot) or spot <= 0:
        raise ValueError("invalid opening spot")
    legs = [{"strike": k["strike"], "call": k.get("callOI"), "put": k.get("putOI")} for k in packet["levels"]]
    return spot, legs, {"opening_evidence": evidence, "opening_etag": packet["etag"],
                        "available_at_ms": packet["availableAt"], "cutoff_ms": cutoff,
                        "settled_session": cal["settledSession"], "opening_spot": spot}


@lru_cache(maxsize=16)
def frozen_path(strategy_id, day):
    bundle, digest = load_bundle()
    spot, legs, source = opening(day)
    prediction = forecast(bundle, IDS[strategy_id], day.isoformat(), spot, legs)
    return {**source, "forecast_bps": list(prediction), "model_sha256": digest,
            "trained_through": bundle["trained_through"]}


def account_position(ctx):
    """Only authoritative, reconciled single-share fills can open/close this isolated book."""
    history = ctx.book.trades
    age = (ctx.now_et - stamp(ctx.account_state["fetched_at"])).total_seconds()
    if (not 0 <= age <= 30 or not isinstance(history, list) or ctx.account_state["trade_count"] != len(history)
            or ctx.account_state.get("account_closed") or ctx.account_state.get("trade_history_present") is False):
        raise ValueError("account snapshot stale, closed or incomplete")
    holding = bought_today = False
    last = None
    for trade in history:
        when = stamp(trade["ts"])
        if (trade["sym"] != "QQQ" or trade.get("instrument_type") != "share" or trade["qty"] != 1
                or trade["side"] != ("sell" if holding else "buy") or not finite(trade["price"])
                or trade["price"] <= 0 or when > ctx.now_et or (last and when < last)):
            raise ValueError("unreconciled one-share fill history")
        last = when
        holding = trade["side"] == "buy"
        bought_today |= holding and when.date() == ctx.now_et.date()
    expected = {"QQQ": 1} if holding else {}
    if {k: v.quantity for k, v in ctx.book.positions.items()} != expected:
        raise ValueError("position disagrees with fill history")
    return holding, bought_today


def order(ctx, strategy_id, side, reason, meta, *, quote=None, deadline=None):
    if quote is None:
        quote = ctx.quotes(["QQQ"]).get("QQQ")
    try:
        fresh = (quote is not None and quote.symbol == "QQQ" and quote.instrument_class == "equity" and quote.is_executable
                 and 0 <= (stamp(quote.server_ts) - stamp(quote.ask_ts if side == "buy" else quote.bid_ts)).total_seconds() <= 15
                 and 0 <= quote.age_seconds <= 15 and finite(quote.ask) and finite(quote.bid)
                 and 0 < quote.bid <= quote.ask and abs((ctx.now_et - stamp(quote.server_ts)).total_seconds()) <= 15)
        if fresh and deadline is not None:
            fresh = stamp(quote.server_ts) < deadline
    except (TypeError, ValueError, AttributeError):
        fresh = False
    if not fresh:
        return Decision.no_trade("Fresh executable QQQ share quote unavailable", strategy_id, VERSION, metadata=meta)
    limit = quote.ask if side == "buy" else quote.bid
    # The Worker requires penny limits. Round a buy down and a sell up so
    # even a fractional quote cannot relax the admitted cash or minimum bid.
    limit = (math.floor(limit*100 + 1e-8) if side == "buy" else math.ceil(limit*100 - 1e-8)) / 100
    if side == "buy" and (not finite(ctx.account_state.get("balance_cash")) or ctx.account_state["balance_cash"] < quote.ask):
        return Decision.no_trade("Available cash cannot cover one QQQ share at ask", strategy_id, VERSION, metadata=meta)
    if limit <= 0:
        return Decision.no_trade("QQQ quote cannot provide a positive penny limit", strategy_id, VERSION, metadata=meta)
    return Decision(side, reason, strategy_id, VERSION, symbol="QQQ", quantity=1, instrument_type="share",
                    metadata={**meta, "quote_time": quote.quote_ts}, limit_price=limit)


def checked_flatten(ctx, strategy_id, quote):
    try:
        if account_position(ctx)[0] and quote is not None:
            return order(ctx, strategy_id, "sell", "Mandatory QQQ share flatten", {}, quote=quote)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return Decision.no_trade("Awaiting reconciled QQQ share position and quote for mandatory flatten", strategy_id, VERSION)


def decide(ctx, *, strategy_id):
    def skip(reason, meta=None):
        return Decision.no_trade(reason, strategy_id, VERSION, metadata=meta)
    if ctx.session_phase != "open":
        return skip("Outside regular session")
    now = ctx.now_et.astimezone(ET)
    day = now.date()
    try:
        holding, bought_today = account_position(ctx)
        if holding:
            if not bought_today or now.time() >= EXIT:
                return order(ctx, strategy_id, "sell", "Past-due QQQ share exit" if not bought_today else "15:30 QQQ share exit", {})
            return skip("Holding one QQQ share until exit")
        if bought_today:
            return skip("Account already purchased QQQ this session")
        grace = ctx.params.get("entry_grace_seconds", 300)
        if type(grace) is not int or not 1 <= grace <= 300:
            return skip("Invalid entry grace, expected 1–300 seconds")
        start = datetime.combine(day, ENTRY, ET)
        deadline = start + timedelta(seconds=grace)
        if not start <= now < deadline or session_close(day) <= EXIT:
            return skip("Outside regular full-session entry window")
        evidence = frozen_path(strategy_id, day)
        if evidence["forecast_bps"][-1] <= 2:
            return skip("Forecast at or below two-basis-point cash hurdle", evidence)
        return order(ctx, strategy_id, "buy", "Opening OI forecast exceeds two basis points",
                     evidence, deadline=deadline)
    except QuoteRateLimited:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, OSError, AttributeError, requests.RequestException) as exc:
        return skip("Opening OI input or account unavailable: " + str(exc))


for name in IDS:
    strategy = partial(decide, strategy_id=name)
    strategy.strategy_id = name
    strategy.strategy_version = VERSION
    strategy.requires_snapshot = False
    register(strategy)
