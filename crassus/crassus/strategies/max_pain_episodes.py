"""Paper Max Pain siblings with confirmed-fill, session-scoped episode resets."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime

from ..client import Book
from ..durability import atomic_json
from ..exits import threshold_price
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, register
from . import max_pain

THRESHOLDS = {"max_pain_mouse_qqq": 0.15, "max_pain_rat_qqq": 0.25}
CHEAPER_PARENTS = {
    "max_pain_mouse_cheaper_qqq": "max_pain_mouse_qqq",
    "max_pain_rat_cheaper_qqq": "max_pain_rat_qqq",
}
THRESHOLDS.update({sibling: THRESHOLDS[parent] for sibling, parent in CHEAPER_PARENTS.items()})
VERSION = "1.0.0"
MAX_BOARD_AGE_SECONDS = 360


def _timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.timestamp() if dt.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _usable_row(row, expiry, occ_prefix):
    if not isinstance(row, dict) or row.get("Type") not in ("call", "put"):
        return False
    symbol = row.get("OptionSymbol")
    prefix = occ_prefix + ("C" if row["Type"] == "call" else "P")
    if not isinstance(symbol, str) or not symbol.startswith(prefix):
        return False
    digits = symbol[len(prefix) :]
    strike = row.get("Strike")
    oi = row.get("OpenInterest")
    return (
        len(digits) == 8
        and digits.isascii()
        and digits.isdigit()
        and _number(strike)
        and strike > 0
        and strike == int(digits) / 1000
        and row.get("TradeDate", expiry) == expiry
        and row.get("Expiration", expiry) == expiry
        and (oi is None or _number(oi) and oi >= 0)
    )


def _side_quote(q, *, held):
    if q is None or not (_number(q.bid) and _number(q.ask) and 0 < q.bid <= q.ask):
        return None
    stamp = q.bid_ts if held else q.ask_ts
    observed, server = _timestamp(stamp), _timestamp(q.server_ts)
    if (
        observed is None
        or server is None
        or not 0 <= server - observed <= EXECUTION_QUOTE_MAX_AGE_S
    ):
        return None
    # The baseline's final executable check must use the same side clock.
    return replace(q, quote_ts=stamp)


def _reading(ctx, last):
    snap = ctx.snapshot
    if snap is None:
        return None, None, None
    observed = _timestamp(snap.timestamp)
    now = ctx.now_et
    expiry = now.date().isoformat()
    occ_prefix = f"QQQ{now:%y%m%d}"
    if (
        observed is None
        or observed < last
        or not 0 <= now.timestamp() - observed <= MAX_BOARD_AGE_SECONDS
        or snap.expiration != expiry
        or not _number(snap.underlying_price)
        or snap.underlying_price <= 0
        or not isinstance(snap.rows, list)
        or not all(_usable_row(row, expiry, occ_prefix) for row in snap.rows)
    ):
        return None, None, None
    pin, both = max_pain._compute_max_pain(snap.rows)
    if pin is None or not math.isfinite(pin):
        return None, None, None
    reading = (pin, both)
    if both < ctx.params.get(
        "min_strikes_with_oi", max_pain.DEFAULT_MIN_STRIKES_WITH_OI
    ):
        return observed, reading, None
    deviation = (snap.underlying_price - pin) / snap.underlying_price * 100
    if abs(deviation) <= ctx.params.get(
        "pin_threshold_pct", max_pain.DEFAULT_PIN_THRESHOLD_PCT
    ):
        return observed, reading, "neutral"
    return observed, reading, "put" if deviation > 0 else "call"


def _valid_state(state):
    if not isinstance(state, dict) or set(state) - {
        "pending",
        "cooldowns",
        "last_board",
        "blocked",
        "blocked_at",
    }:
        return False
    times = state.get("cooldowns", {})
    if not isinstance(times, dict) or not all(
        _number(t) and t >= 0 for t in times.values()
    ):
        return False
    if not _number(state.get("last_board", 0)) or state.get("last_board", 0) < 0:
        return False
    if ("blocked" in state) != ("blocked_at" in state):
        return False
    if "blocked" in state and (
        state["blocked"] not in ("call", "put") or not _number(state["blocked_at"])
    ):
        return False
    if "pending" not in state:
        return True
    p = state["pending"]
    return (
        isinstance(p, dict)
        and set(p) == {"id", "symbol", "quantity", "block_side"}
        and isinstance(p["id"], str)
        and bool(p["id"])
        and isinstance(p["symbol"], str)
        and p["quantity"] == 1
        and p["block_side"] in (None, "call", "put")
    )


def _apply_confirmed_sale(state, trades):
    pending = state.pop("pending", None)
    if not pending:
        return
    fill = next(
        (t for t in reversed(trades) if t.get("execution_request_id") == pending["id"]),
        None,
    )
    if (
        fill is None
    ):  # Runner reconciles any outstanding execution before calling the strategy.
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


def _cheaper_entry(ctx, strategy_id, parent, executable):
    """Compare the parent contract with exactly one outward strike on this board."""
    direction = 1 if parent["Type"] == "call" else -1
    outward = (
        row for row in ctx.snapshot.rows
        if row["Type"] == parent["Type"] and direction * (row["Strike"] - parent["Strike"]) > 0
    )
    child = min(outward, key=lambda row: direction * row["Strike"], default=None)
    symbols = [parent["OptionSymbol"]]
    if child:
        symbols.append(child["OptionSymbol"])
    live = executable(symbols)
    parent_quote = live.get(symbols[0])
    child_quote = live.get(child["OptionSymbol"]) if child else None
    meta = {
        "parent_symbol": symbols[0],
        "parent_strike": parent["Strike"],
        "parent_ask": parent_quote.ask if parent_quote else None,
        "parent_ask_ts": parent_quote.ask_ts if parent_quote else None,
        "candidate_symbol": child["OptionSymbol"] if child else None,
        "candidate_strike": child["Strike"] if child else None,
        "candidate_ask": child_quote.ask if child_quote else None,
        "candidate_ask_ts": child_quote.ask_ts if child_quote else None,
    }
    if not parent_quote or not child_quote:
        reason = "Waiting for executable asks on the parent and its adjacent outward strike."
    elif child_quote.ask >= parent_quote.ask:
        reason = "Adjacent outward strike is not cheaper than the parent."
    elif abs(child_quote.ask * 100 - round(child_quote.ask * 100)) > 1e-7:
        reason = "Adjacent ask cannot be submitted as a cent-priced paper limit."
    else:
        return Decision(
            "buy", "Adjacent outward strike has a cheaper executable ask.", strategy_id, VERSION,
            child["OptionSymbol"], 1, limit_price=round(child_quote.ask, 2),
            metadata={**meta, "strike": child["Strike"], "bid": child_quote.bid,
                      "ask": child_quote.ask, "quote_age_seconds": child_quote.age_seconds},
        )
    return Decision.no_trade(reason, strategy_id, VERSION, metadata=meta)


def _decide(ctx, strategy_id):
    def no(reason):
        return Decision.no_trade(reason, strategy_id, VERSION)

    if ctx.session_phase != "open":
        return no("Market is closed.")
    if ctx.state_dir is None or not ctx.decision_id:
        raise ValueError("Max Pain episode state and decision identity are required")
    username = ctx.account_state["username"]
    path = (
        ctx.state_dir
        / "max-pain-episodes"
        / username
        / (ctx.now_et.date().isoformat() + ".json")
    )
    degraded = False
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
        if not _valid_state(state):
            raise ValueError("Invalid Max Pain episode state")
        before = json.dumps(state, sort_keys=True)
        _apply_confirmed_sale(state, ctx.book.trades)
    except (ValueError, OSError):
        # Preserve the unreadable evidence and freeze entries for this session.
        # The reconciled book and executable bid still suffice to reduce exposure.
        state, before, degraded = {}, "", True

    def finish(decision):
        if not degraded and json.dumps(state, sort_keys=True) != before:
            atomic_json(path, state)
        return replace(decision, strategy_id=strategy_id, strategy_version=VERSION)

    if degraded and ctx.book.is_flat:
        return no("Episode state unavailable; new entries disabled for this session.")
    now = ctx.now_et.timestamp()
    observed, reading, bias = _reading(ctx, state.get("last_board", 0))
    if observed is not None:
        state["last_board"] = observed
    if (
        state.get("blocked")
        and bias is not None
        and bias != state["blocked"]
        and observed > state["blocked_at"]
    ):
        del state["blocked"], state["blocked_at"]

    positions = list(ctx.book.positions.values())
    quotes = {}

    def executable(symbols):
        missing = [s for s in symbols if s not in quotes]
        if missing:
            fetched = ctx.quotes(missing)
            for sym in missing:
                quotes[sym] = _side_quote(fetched.get(sym), held=bool(positions))
        return {s: quotes[s] for s in symbols if quotes[s] is not None}

    if positions:
        pos = positions[0]
        side = max_pain._option_type_from_symbol(pos.symbol)
        if (
            len(positions) != 1
            or pos.quantity != 1
            or side is None
            or pos.average_cost <= 0
        ):
            return finish(
                no("Unexpected position in single-contract Max Pain account.")
            )
        q = executable([pos.symbol]).get(pos.symbol)
        if q is None:
            return finish(no("Waiting for an executable held-contract bid."))
        threshold = THRESHOLDS[strategy_id]
        stop = q.bid <= threshold_price(pos.average_cost, -threshold, 1)
        target = q.bid >= threshold_price(pos.average_cost, threshold, 1)
        if stop or target:
            state["pending"] = {
                "id": ctx.decision_id,
                "symbol": pos.symbol,
                "quantity": 1,
                "block_side": side if bias is None or bias == side else None,
            }
            trigger = "stop" if stop else "target"
            return finish(
                Decision(
                    "sell",
                    f"Max Pain premium {trigger} at {threshold:.0%}.",
                    strategy_id,
                    VERSION,
                    pos.symbol,
                    1,
                    metadata={
                        "premium_exit": trigger,
                        "fill_basis": pos.average_cost,
                        "bid": q.bid,
                        "threshold": threshold,
                        "observed_bias": bias,
                    },
                )
            )
    if reading is None:
        return finish(no("No fresh current-session Max Pain board."))
    if not positions and state.get("blocked"):
        return finish(no("Awaiting a new directional Max Pain episode."))
    snapshot = ctx.snapshot
    if not positions:
        # The frozen replay ranks ATM candidates among uncrossed, two-sided
        # board quotes. Their OI still belongs to the full pin calculation.
        eligible = [
            row
            for row in snapshot.rows
            if _number(row.get("Bid"))
            and _number(row.get("Ask"))
            and 0 < row["Bid"] <= row["Ask"]
        ]
        snapshot = replace(snapshot, rows=eligible)
    comparison = None
    if strategy_id in CHEAPER_PARENTS and not positions and bias in ("call", "put"):
        parent = snapshot.atm(bias)
        if parent:
            comparison = _cheaper_entry(ctx, strategy_id, parent, executable)
    decision = max_pain._decide_core(
        replace(ctx, snapshot=snapshot, quotes=executable), pin_reading=reading
    )
    if comparison is not None and (decision.action == "buy" or comparison.action == "no_trade"):
        decision = replace(comparison, metadata={**(decision.metadata or {}), **comparison.metadata})
    if decision.action == "buy" and now < state.get("cooldowns", {}).get(
        decision.symbol, 0
    ):
        return finish(
            Decision.no_trade("Same contract is inside its 15-minute premium-exit cooldown.",
                              strategy_id, VERSION, metadata=decision.metadata if comparison else None)
        )
    return finish(decision)


def _register(strategy_id):
    def decide(ctx):
        return _decide(ctx, strategy_id)

    decide.strategy_id = strategy_id
    decide.strategy_version = VERSION
    return register(decide)


for _strategy_id in THRESHOLDS:
    _register(_strategy_id)
