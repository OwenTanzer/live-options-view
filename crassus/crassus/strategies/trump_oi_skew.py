"""Trump Whisperer x OI skew: trade only when Trump's mood and the near-money
OI skew agree, on IBIT, SLV (silver) and DRAM (memory). Paper only.

Two existing signals, unchanged:

* Trump mood -- `trump_whisperer`'s reading of fresh Truth Social posts
  (shared `TrumpSentimentReader`, same thresholds: mean VADER compound >= 0.2
  bullish, <= -0.2 bearish, at least 2 fresh posts).
* Near-money OI skew -- the reference strategies' band for the root
  (`oi_skew.reference_band`, the IBIT adaptive window) and the OI skew gates
  (|imbalance| >= 0.3, moved >= 0.1 from this account's own session-start
  anchor, >= 4 band strikes).

Entry: flat, and both lean the same way -> buy one ATM call (both bullish) or
put (both bearish) on the root's collected expiration. Disagreement, or
either side without a reading, is no trade.

Exit: the skew is the market-side reading, so a held position closes when
the skew no longer supports it (neutral, thin band or flipped) or when
Trump's mood flips to the other side. A neutral or quiet Trump feed alone does
not close: fresh posts are sparse, and closing on silence would make almost
every hold a few minutes long. A missing observation (Trump feed error,
stale/duplicate snapshot) retains, never closes. The runner's mandatory
end-of-day flatten still applies.

Every decision records both readings and which one would have fired alone
(`agreement`: both / trump_only / skew_only / disagree / neither), so a
review can compare this account cycle by cycle with TrumpWhisperer and the
root's OI Skew Paper account.

The anchor is this strategy's own (keyed by its strategy id and account), so
it never shares session history with the root's reference skew account.
"""

from __future__ import annotations

import re
from typing import Any

from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..replay import register_state_resetter
from ..strategy import Decision, StrategyContext, register
from ..trump_sentiment import TrumpSentimentSnapshot
from . import trump_whisperer
from .oi_skew import (
    DEFAULT_IMBALANCE_THRESHOLD,
    DEFAULT_MIN_BAND_STRIKES,
    DEFAULT_MIN_CHANGE_FROM_SESSION_START,
    DEFAULT_NEAR_MONEY_PCT,
    _finite_number,
    _open_positions,
    _option_type_from_symbol,
    _reset_state,
    _tracker_for,
    reference_band,
)

STRATEGY_VERSION = "1.0.0"
ROOTS = {
    "trump_whisperer_oi_skew_ibit": "IBIT",
    "trump_whisperer_oi_skew_slv": "SLV",
    "trump_whisperer_oi_skew_dram": "DRAM",
}


def skew_reading(ctx: StrategyContext, strategy_id: str, root: str) -> dict[str, Any]:
    """Observe this snapshot on the strategy's own anchor and classify it.

    `lean` is "call", "put", "neutral" (a current read that supports
    neither side), "stale" (no new observation) or "unavailable" (board,
    spot or band unusable)."""
    snapshot = ctx.snapshot
    if snapshot is None:
        return {"lean": "unavailable", "why": "No market snapshot."}
    pattern = rf"{root}\d{{6}}[CP]\d{{8}}"
    symbols = [row.get("OptionSymbol") for row in snapshot.rows] + list(_open_positions(ctx))
    if not symbols or any(not re.fullmatch(pattern, str(s)) for s in symbols):
        return {"lean": "unavailable", "why": f"Missing or mismatched {root} contract board or holding."}
    spot = snapshot.underlying_price
    if not _finite_number(spot) or spot <= 0:
        return {"lean": "unavailable", "why": "Missing valid spot."}
    params = dict(ctx.params or {})
    tracker = _tracker_for(ctx, params.get("near_money_pct", DEFAULT_NEAR_MONEY_PCT), strategy_id)
    (call_oi, put_oi, strikes), params = reference_band(snapshot, root, params)
    threshold = params.get("imbalance_threshold", DEFAULT_IMBALANCE_THRESHOLD)
    min_change = params.get("min_change_from_session_start", DEFAULT_MIN_CHANGE_FROM_SESSION_START)
    min_strikes = params.get("min_band_strikes", DEFAULT_MIN_BAND_STRIKES)
    total = call_oi + put_oi
    imbalance = (call_oi - put_oi) / total if total > 0 else None
    timestamp = getattr(snapshot, "timestamp", None)
    recorded = bool(imbalance is not None and timestamp and tracker.observe(timestamp, imbalance))
    start = tracker.session_start_imbalance
    delta = imbalance - start if imbalance is not None and start is not None else None
    reading = {
        "imbalance_ratio": imbalance,
        "session_start_imbalance": start,
        "delta_from_session_start": delta,
        "band_strikes": strikes,
        "near_money_call_oi": call_oi,
        "near_money_put_oi": put_oi,
        "near_money_pct": params.get("near_money_pct"),
        "anchor_observed_at": tracker.session_start_observed_at,
    }
    if imbalance is not None and not recorded:
        return {**reading, "lean": "stale", "why": "Snapshot is not a new observation."}
    if imbalance is None or strikes < min_strikes:
        return {**reading, "lean": "unavailable",
                "why": f"Near-money band has {strikes} strike(s) with OI (need {min_strikes})."}
    if abs(imbalance) < threshold:
        return {**reading, "lean": "neutral", "why": f"Imbalance {imbalance:.3f} is below {threshold}."}
    if delta is None or abs(delta) < min_change:
        return {**reading, "lean": "neutral",
                "why": f"Imbalance moved {delta} from session start (need {min_change})."}
    return {**reading, "lean": "call" if imbalance > 0 else "put",
            "why": f"Near-money skew {imbalance:.3f}, moved {delta:.3f} from session start."}


def trump_reading(ctx: StrategyContext, snapshot: TrumpSentimentSnapshot | None,
                  fetch_error: str | None) -> dict[str, Any]:
    """`lean` is "call", "put", "neutral" or "unavailable" (feed error)."""
    if fetch_error is not None:
        return {"lean": "unavailable", "why": f"Trump sentiment unavailable: {fetch_error}"}
    if snapshot is None:
        return {"lean": "neutral", "why": "No Trump sentiment snapshot."}
    params = ctx.params or {}
    min_sample = params.get("min_sample_size", trump_whisperer.DEFAULT_MIN_SAMPLE_SIZE)
    bullish = params.get("bullish_threshold", trump_whisperer.DEFAULT_BULLISH_THRESHOLD)
    bearish = params.get("bearish_threshold", trump_whisperer.DEFAULT_BEARISH_THRESHOLD)
    reading = {
        "sample_size": snapshot.sample_size,
        "mean_compound": snapshot.mean_compound,
        "latest_post_at": snapshot.latest_post_at,
    }
    mean = snapshot.mean_compound
    if snapshot.sample_size < min_sample:
        return {**reading, "lean": "neutral",
                "why": f"Only {snapshot.sample_size} fresh Trump post(s); need {min_sample}."}
    if mean is None or bearish < mean < bullish:
        return {**reading, "lean": "neutral", "why": f"Trump sentiment is neutral (mean_compound={mean})."}
    return {**reading, "lean": "call" if mean >= bullish else "put",
            "why": f"Trump sentiment mean_compound={mean:.3f} over {snapshot.sample_size} posts."}


def agreement(trump_lean: str, skew_lean: str) -> str:
    directional = ("call", "put")
    t, s = trump_lean in directional, skew_lean in directional
    if t and s:
        return "both" if trump_lean == skew_lean else "disagree"
    return "trump_only" if t else "skew_only" if s else "neither"


def decide_core(ctx: StrategyContext, strategy_id: str, trump_snapshot: TrumpSentimentSnapshot | None,
                trump_error: str | None) -> Decision:
    root = ROOTS[strategy_id]
    # Observe first, in every phase: the pre-open reading is the session anchor.
    skew = skew_reading(ctx, strategy_id, root)
    trump = trump_reading(ctx, trump_snapshot, trump_error)
    meta: dict[str, Any] = {
        "root": root,
        "skew": skew,
        "trump": trump,
        "agreement": agreement(trump["lean"], skew["lean"]),
    }

    def no(reason: str, **extra: Any) -> Decision:
        return Decision.no_trade(reason, strategy_id, STRATEGY_VERSION, metadata={**meta, **extra})

    def trade(action: str, symbol: str, quantity: int, reason: str, **extra: Any) -> Decision:
        quote = ctx.quotes([symbol]).get(symbol)
        if quote is None:
            return no(f"No live quote returned for {symbol}.", symbol=symbol)
        if not quote.is_executable:
            return no(f"Live quote for {symbol} is not executable "
                      f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
                      symbol=symbol, bid=quote.bid, ask=quote.ask)
        return Decision(action=action, symbol=symbol, quantity=quantity, reason=reason,
                        strategy_id=strategy_id, strategy_version=STRATEGY_VERSION,
                        metadata={**meta, **extra, "bid": quote.bid, "ask": quote.ask,
                                  "quote_age_seconds": quote.age_seconds})

    if ctx.session_phase != "open":
        return no(f"Market is {ctx.session_phase}; execution quotes will be stale.")
    positions = _open_positions(ctx)
    if len(positions) > 1:
        return no("Holding more than one open option position; standing down.",
                  open_positions={s: p.quantity for s, p in positions.items()})
    if positions:
        held, position = next(iter(positions.items()))
        held_type = _option_type_from_symbol(held)
        if position.quantity < 0 or held_type is None:
            return no(f"Unexpected holding {held} ({position.quantity}); standing down.", symbol=held)
        if skew["lean"] == "stale" or trump["lean"] == "unavailable":
            return no(f"Missing observation ({skew['why'] if skew['lean'] == 'stale' else trump['why']}); "
                      f"retaining the held {held_type}.", symbol=held)
        opposite = "put" if held_type == "call" else "call"
        if skew["lean"] != held_type:
            return trade("sell", held, position.quantity,
                         f"OI skew no longer supports the held {held_type} ({skew['why']}); closing it.",
                         closing_symbol=held)
        if trump["lean"] == opposite:
            return trade("sell", held, position.quantity,
                         f"Trump mood flipped to {opposite}s ({trump['why']}); closing the held {held_type}.",
                         closing_symbol=held)
        return no(f"Holding {held}; skew still supports it and Trump has not flipped.", symbol=held)
    if meta["agreement"] != "both":
        return no(f"No agreement ({meta['agreement']}): Trump {trump['lean']} -- {trump['why']} "
                  f"OI skew {skew['lean']} -- {skew['why']}")
    side = skew["lean"]
    row = ctx.snapshot.atm(side)
    if not row:
        return no(f"No quoted {side} in the {root} snapshot to trade.")
    mood = "bullish" if side == "call" else "bearish"
    return trade("buy", row["OptionSymbol"], 1,
                 f"Trump mood and {root} OI skew are both {mood}; opening one {side}.",
                 strike=row["Strike"], underlying_price=ctx.snapshot.underlying_price)


def _register(strategy_id: str) -> None:
    def decide(ctx: StrategyContext) -> Decision:
        if ctx.session_phase != "open":
            return decide_core(ctx, strategy_id, None, None)
        try:
            snapshot = trump_whisperer._reader.read()
        except Exception as exc:  # TrumpFetchError, network failure, etc.
            return decide_core(ctx, strategy_id, None, str(exc))
        return decide_core(ctx, strategy_id, snapshot, None)

    decide.strategy_id = strategy_id
    decide.strategy_version = STRATEGY_VERSION
    register(decide)
    register_state_resetter(strategy_id, lambda: _reset_state(strategy_id))


for _strategy_id in ROOTS:
    _register(_strategy_id)
