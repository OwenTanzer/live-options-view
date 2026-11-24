"""Paper Max Pain siblings with confirmed-fill, session-scoped episode resets."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime

from ..client import Book
from ..durability import atomic_json
from ..exits import threshold_price
from ..strategy import Decision, register
from . import max_pain

THRESHOLDS = {"max_pain_mouse_qqq": 0.15, "max_pain_rat_qqq": 0.25}
VERSION = "1.0.0"
MAX_BOARD_AGE_SECONDS = 360


def _timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.timestamp() if dt.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _reading(ctx, last):
    snap = ctx.snapshot
    if snap is None:
        return None, None, None
    observed = _timestamp(snap.timestamp)
    now = ctx.now_et
    if (observed is None or observed < last or not 0 <= now.timestamp() - observed <= MAX_BOARD_AGE_SECONDS
            or snap.expiration != now.date().isoformat()
            or not math.isfinite(snap.underlying_price) or snap.underlying_price <= 0):
        return None, None, None
    try:
        pin, both = max_pain._compute_max_pain(snap.rows)
    except (KeyError, TypeError, ValueError):
        return None, None, None
    if pin is None or not math.isfinite(pin):
        return None, None, None
    reading = (pin, both)
    if both < ctx.params.get("min_strikes_with_oi", max_pain.DEFAULT_MIN_STRIKES_WITH_OI):
        return observed, reading, None
    deviation = (snap.underlying_price - pin) / snap.underlying_price * 100
    if abs(deviation) <= ctx.params.get("pin_threshold_pct", max_pain.DEFAULT_PIN_THRESHOLD_PCT):
        return observed, reading, "neutral"
    return observed, reading, "put" if deviation > 0 else "call"


def _apply_confirmed_sale(state, trades):
    pending = state.pop("pending", None)
    if not pending:
        return
    fill = next((t for t in reversed(trades) if t.get("execution_request_id") == pending["id"]), None)
    if fill is None:  # Runner reconciles any outstanding execution before calling the strategy.
        return
    parsed = Book._normalize(fill)
    if not parsed or parsed[:3] != (pending["symbol"], "sell", pending["quantity"]):
        raise ValueError("Max Pain exit disagrees with reconciled fill")
    filled_at = _timestamp(fill.get("ts"))
    if filled_at is None:
        raise ValueError("Max Pain exit fill has no timestamp")
    state.setdefault("cooldowns", {})[pending["symbol"]] = filled_at + 900
    if pending["block_side"]:
        state["blocked"] = pending["block_side"]
        state["blocked_at"] = filled_at


def _decide(ctx, strategy_id):
    def no(reason):
        return Decision.no_trade(reason, strategy_id, VERSION)

    if ctx.session_phase != "open":
        return no("Market is closed.")
    if ctx.state_dir is None or not ctx.decision_id:
        raise ValueError("Max Pain episode state and decision identity are required")
    username = ctx.account_state["username"]
    path = ctx.state_dir / "max-pain-episodes" / username / (ctx.now_et.date().isoformat() + ".json")
    state = json.loads(path.read_text()) if path.exists() else {}
    before = json.dumps(state, sort_keys=True)

    def finish(decision):
        if json.dumps(state, sort_keys=True) != before:
            atomic_json(path, state)
        return replace(decision, strategy_id=strategy_id, strategy_version=VERSION)

    _apply_confirmed_sale(state, ctx.book.trades)
    now = ctx.now_et.timestamp()
    observed, reading, bias = _reading(ctx, state.get("last_board", 0))
    if observed is not None:
        state["last_board"] = observed
    if (state.get("blocked") and bias is not None and bias != state["blocked"]
            and observed > state["blocked_at"]):
        del state["blocked"], state["blocked_at"]

    quotes = {}
    def executable(symbols):
        missing = [s for s in symbols if s not in quotes]
        if missing:
            fetched = ctx.quotes(missing)
            for sym in missing:
                q = fetched.get(sym)
                quotes[sym] = (q if q and q.is_executable and q.age_seconds >= 0
                               and 0 < q.bid <= q.ask and math.isfinite(q.ask) else None)
        return {s: quotes[s] for s in symbols if quotes[s] is not None}

    positions = list(ctx.book.positions.values())
    if positions:
        pos = positions[0]
        side = max_pain._option_type_from_symbol(pos.symbol)
        if len(positions) != 1 or pos.quantity != 1 or side is None or pos.average_cost <= 0:
            return finish(no("Unexpected position in single-contract Max Pain account."))
        q = executable([pos.symbol]).get(pos.symbol)
        if q is None:
            return finish(no("Waiting for an executable held-contract bid."))
        threshold = THRESHOLDS[strategy_id]
        stop = q.bid <= threshold_price(pos.average_cost, -threshold, 1)
        target = q.bid >= threshold_price(pos.average_cost, threshold, 1)
        if stop or target:
            state["pending"] = {"id": ctx.decision_id, "symbol": pos.symbol, "quantity": 1,
                                "block_side": side if bias is None or bias == side else None}
            trigger = "stop" if stop else "target"
            return finish(Decision("sell", f"Max Pain premium {trigger} at {threshold:.0%}.",
                                   strategy_id, VERSION, pos.symbol, 1,
                                   metadata={"premium_exit": trigger, "fill_basis": pos.average_cost,
                                             "bid": q.bid, "threshold": threshold, "observed_bias": bias}))
    if reading is None:
        return finish(no("No fresh current-session Max Pain board."))
    if not positions and state.get("blocked") and bias == state["blocked"]:
        return finish(no("Awaiting a new directional Max Pain episode."))
    decision = max_pain._decide_core(replace(ctx, quotes=executable), pin_reading=reading)
    if decision.action == "buy" and now < state.get("cooldowns", {}).get(decision.symbol, 0):
        return finish(no("Same contract is inside its 15-minute premium-exit cooldown."))
    return finish(decision)


def _register(strategy_id):
    def decide(ctx):
        return _decide(ctx, strategy_id)
    decide.strategy_id = strategy_id
    decide.strategy_version = VERSION
    return register(decide)


for _strategy_id in THRESHOLDS:
    _register(_strategy_id)

