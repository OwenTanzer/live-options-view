"""Order-flow skew for QQQ: the OI Skew idea read from live signed prints.

`oi_skew_qqq` leans with near-money call-vs-put open interest. Open interest
is settled once a day, so every intraday reading of it describes yesterday's
positioning. This sibling reads the same near-money band from the
collector's live order-flow projection instead (`crassus/order_flow.py`,
republished every ~3 s), so the reading reflects trades from the last few
minutes.

Reading, per runner cycle:

* Window: the change in each contract's session totals over the last
  `window_minutes` (default 15). The strategy keeps its own history of
  projection readings per account; until it holds a reading at least that old
  (after a start or restart) it has no window and makes no call.
* Band: contracts whose strike is within `near_money_pct` (default 2%) of the
  snapshot's underlying price, same as `oi_skew_qqq`.
* Pressure: only quote-signed volume with a prevailing quote at most 5 s old
  (`q1` + `q5`) counts. Bullish = call buys + put sells; bearish = put buys +
  call sells. imbalance = (bullish - bearish) / (bullish + bearish), in
  [-1, 1].
* Coverage gates (no call, and a held position closes): fewer than
  `min_window_contracts` signed contracts in the band; low-confidence plus
  unsigned volume above `max_low_confidence_share` of band volume; fewer
  than `min_band_strikes` strikes with any flow.
* Missing observation (retain, never close): fewer than `min_band_strikes`
  band strikes in the collector's universe at all (its contract list is fixed
  at the open, so a large move can leave the band uncaptured), a fetch or shape error, a
  pre-label collector, a projection older than `max_feed_age_s`, a feed gap
  overlapping the window, a missing or stale spot, no window yet, or a
  projection that is not newer than the last one this account recorded.

Entry and exit match `oi_skew_qqq`: flat with |imbalance| >= threshold opens
one ATM call (bullish) or put (bearish); a held position closes when the
reading no longer supports it; one position at a time; the runner's
end-of-day flatten applies. No session-start drift gate: a window of fresh
prints has no inherited baseline to filter out, which is the reason that
gate exists in `oi_skew_qqq`.

Be honest about what this is: an inferred aggressor side is not a known
opening or closing trade, and a buy at the ask can be a short closing. The
2026-11-23 audit measured how often the q1/q5 labels agree with the
provider's own trade-time bid/ask (76-89%), which is consistency, not ground
truth. Paper only, and inactive until a replay supports it.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any

from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..order_flow import FlowSnapshot, OrderFlowError, OrderFlowReader
from ..replay import register_state_resetter
from ..strategy import Decision, StrategyContext, register
from .oi_skew import _account_key, _finite_number, _open_positions, _option_type_from_symbol

STRATEGY_ID = "flow_skew_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULTS = dict(
    near_money_pct=0.02,
    window_minutes=15,
    imbalance_threshold=0.10,
    min_window_contracts=2000,
    max_low_confidence_share=0.30,
    min_band_strikes=4,
    max_feed_age_s=120,
    max_spot_age_s=360,
)
# Readings kept beyond the window, so a slightly late cycle still finds a base.
_HISTORY_SLACK_MS = 10 * 60_000


class FlowWindowTracker:
    """One account's recent projection readings for the current session day."""

    def __init__(self) -> None:
        self.day: str | None = None
        self.readings: list[tuple[int, FlowSnapshot]] = []

    def observe(self, flow: FlowSnapshot, keep_ms: int) -> bool:
        if flow.day != self.day:
            self.day, self.readings = flow.day, []
        if self.readings and flow.last_received_ms <= self.readings[-1][0]:
            return False
        self.readings.append((flow.last_received_ms, flow))
        floor = flow.last_received_ms - keep_ms
        while len(self.readings) > 2 and self.readings[1][0] <= floor:
            self.readings.pop(0)
        return True

    def base(self, at_or_before_ms: int) -> FlowSnapshot | None:
        best = None
        for t, flow in self.readings:
            if t <= at_or_before_ms:
                best = flow
        return best


_trackers: dict[tuple[str, str], FlowWindowTracker] = {}
_lock = threading.Lock()


def _tracker(ctx: StrategyContext) -> FlowWindowTracker:
    key = (STRATEGY_ID, _account_key(ctx))
    with _lock:
        return _trackers.setdefault(key, FlowWindowTracker())


def reset_state() -> None:
    with _lock:
        _trackers.clear()


def _params(ctx: StrategyContext) -> dict[str, Any]:
    return {**DEFAULTS, **(ctx.params or {})}


def _spot(ctx: StrategyContext, max_age_s: float) -> float | None:
    snapshot = ctx.snapshot
    spot = getattr(snapshot, "underlying_price", None)
    if not _finite_number(spot) or spot <= 0:
        return None
    try:
        observed = datetime.fromisoformat(str(snapshot.timestamp).replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if observed.tzinfo is None:
        return None
    if ctx.now_et is not None and not 0 <= (ctx.now_et - observed).total_seconds() <= max_age_s:
        return None
    return float(spot)


def window_reading(now: FlowSnapshot, base: FlowSnapshot, spot: float, params: dict[str, Any]) -> dict[str, Any]:
    """Pure: band pressure over (base, now]. `lean` is call / put / neutral / thin, or
    unavailable when the band falls outside the collector's universe."""
    pct = params["near_money_pct"]
    bull = bear = low = unsigned = 0
    strikes, captured = set(), set()
    for symbol, c in now.contracts.items():
        if abs(c.strike - spot) / spot > pct:
            continue
        captured.add(c.strike)
        b = base.contracts.get(symbol)
        d_buy = c.high_buy - (b.high_buy if b else 0)
        d_sell = c.high_sell - (b.high_sell if b else 0)
        d_low = c.low - (b.low if b else 0)
        d_u = c.unsigned - (b.unsigned if b else 0)
        if min(d_buy, d_sell, d_low, d_u) < 0:  # totals only grow within a session
            return {"lean": "unavailable", "why": f"Session totals for {symbol} went backwards."}
        if d_buy + d_sell + d_low + d_u:
            strikes.add(c.strike)
        if c.type == "call":
            bull, bear = bull + d_buy, bear + d_sell
        else:
            bull, bear = bull + d_sell, bear + d_buy
        low, unsigned = low + d_low, unsigned + d_u
    signed = bull + bear
    total = signed + low + unsigned
    share = (low + unsigned) / total if total else None
    imbalance = (bull - bear) / signed if signed else None
    reading = dict(bullish_contracts=bull, bearish_contracts=bear, low_confidence_contracts=low,
                   unsigned_contracts=unsigned, low_confidence_share=share, imbalance=imbalance,
                   band_strikes=len(strikes), window_start_ms=base.last_received_ms,
                   window_end_ms=now.last_received_ms)
    if len(captured) < params["min_band_strikes"]:
        # The collector's contract universe is fixed at the open; a large move can
        # carry spot past it. That is missing coverage, not a quiet market.
        return {**reading, "lean": "unavailable",
                "why": f"Only {len(captured)} near-money strike(s) are in the collector's universe "
                       f"(need {params['min_band_strikes']}); spot has moved outside the captured strikes."}
    if len(strikes) < params["min_band_strikes"]:
        return {**reading, "lean": "thin",
                "why": f"{len(strikes)} near-money strike(s) traded in the window (need {params['min_band_strikes']})."}
    if signed < params["min_window_contracts"]:
        return {**reading, "lean": "thin",
                "why": f"{signed} confidently signed contracts in the window (need {params['min_window_contracts']})."}
    if share > params["max_low_confidence_share"]:
        return {**reading, "lean": "thin",
                "why": f"{share:.0%} of band volume is low-confidence or unsigned "
                       f"(limit {params['max_low_confidence_share']:.0%})."}
    if abs(imbalance) < params["imbalance_threshold"]:
        return {**reading, "lean": "neutral",
                "why": f"Flow imbalance {imbalance:+.3f} is inside +/-{params['imbalance_threshold']}."}
    return {**reading, "lean": "call" if imbalance > 0 else "put",
            "why": f"Near-money flow imbalance {imbalance:+.3f} over {signed} signed contracts."}


def flow_reading(ctx: StrategyContext, flow: FlowSnapshot | None, error: str | None,
                 tracker: FlowWindowTracker) -> dict[str, Any]:
    """Observe the projection on this account's tracker and classify the window."""
    params = _params(ctx)
    if error is not None or flow is None:
        return {"lean": "unavailable", "why": f"Order flow unavailable: {error or 'no projection'}."}
    window_ms = int(params["window_minutes"] * 60_000)
    now_ms = int(ctx.now_et.timestamp() * 1000) if ctx.now_et is not None else flow.last_received_ms
    age_s = (now_ms - flow.last_received_ms) / 1000
    if not 0 <= age_s <= params["max_feed_age_s"] + 5:
        return {"lean": "unavailable", "why": f"Order-flow projection is {age_s:.0f}s old "
                                              f"(limit {params['max_feed_age_s']}s); stale feed, not a quiet market."}
    if not tracker.observe(flow, window_ms + _HISTORY_SLACK_MS):
        return {"lean": "stale", "why": "Order-flow projection is not newer than the last recorded reading."}
    spot = _spot(ctx, params["max_spot_age_s"])
    if spot is None:
        return {"lean": "unavailable", "why": "Missing or stale underlying spot."}
    base = tracker.base(flow.last_received_ms - window_ms)
    if base is None:
        return {"lean": "unavailable",
                "why": f"No reading {params['window_minutes']} min old yet (warming up after start)."}
    for start, end in flow.gaps:
        if start < flow.last_received_ms and (end is None or end > base.last_received_ms):
            return {"lean": "unavailable", "why": "A feed gap overlaps the window; the window is incomplete."}
    return {**window_reading(flow, base, spot, params), "spot": spot, "params": params}


def decide_core(ctx: StrategyContext, flow: FlowSnapshot | None, error: str | None,
                tracker: FlowWindowTracker | None = None) -> Decision:
    # Observe first, in every phase, so the window history is ready at the open.
    reading = flow_reading(ctx, flow, error, tracker or _tracker(ctx))
    meta: dict[str, Any] = {"flow": reading}

    def no(reason: str, **extra: Any) -> Decision:
        return Decision.no_trade(reason, STRATEGY_ID, STRATEGY_VERSION, metadata={**meta, **extra})

    def trade(action: str, symbol: str, reason: str, quantity: int = 1, **extra: Any) -> Decision:
        quote = ctx.quotes([symbol]).get(symbol)
        if quote is None:
            return no(f"No live quote returned for {symbol}.", symbol=symbol)
        if not quote.is_executable:
            return no(f"Live quote for {symbol} is not executable "
                      f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
                      symbol=symbol, bid=quote.bid, ask=quote.ask)
        return Decision(action=action, symbol=symbol, quantity=quantity, reason=reason,
                        strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                        metadata={**meta, **extra, "bid": quote.bid, "ask": quote.ask,
                                  "quote_age_seconds": quote.age_seconds})

    if ctx.session_phase != "open":
        return no(f"Market is {ctx.session_phase}; execution quotes will be stale.")
    positions = _open_positions(ctx)
    if len(positions) > 1:
        return no("Holding more than one open option position; standing down.",
                  open_positions={s: p.quantity for s, p in positions.items()})
    lean = reading["lean"]
    if positions:
        held, position = next(iter(positions.items()))
        held_type = _option_type_from_symbol(held)
        if position.quantity < 0 or held_type is None:
            return no(f"Unexpected holding {held} ({position.quantity}); standing down.", symbol=held)
        if lean in ("stale", "unavailable"):
            return no(f"Missing observation ({reading['why']}); retaining the held {held_type}.", symbol=held)
        if lean != held_type:
            return trade("sell", held, f"Order flow no longer supports the held {held_type} "
                                       f"({reading['why']}); closing it.", position.quantity, closing_symbol=held)
        return no(f"Holding {held}; order flow still supports it ({reading['why']})", symbol=held)
    if lean not in ("call", "put"):
        return no(f"No flow signal ({lean}): {reading['why']}")
    row = ctx.snapshot.atm(lean)
    if not row:
        return no(f"No quoted {lean} in the QQQ snapshot to trade.")
    mood = "bullish" if lean == "call" else "bearish"
    return trade("buy", row["OptionSymbol"], f"Near-money order flow is {mood}; opening one {lean}. {reading['why']}",
                 strike=row["Strike"], underlying_price=ctx.snapshot.underlying_price)


_reader = OrderFlowReader()


def _decide(ctx: StrategyContext) -> Decision:
    try:
        flow = _reader.read()
    except OrderFlowError as exc:
        return decide_core(ctx, None, str(exc))
    return decide_core(ctx, flow, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
flow_skew_qqq = register(_decide)
register_state_resetter(STRATEGY_ID, reset_state)
