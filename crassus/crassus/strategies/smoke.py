"""The P1 smoke strategy: deterministic, trivial, and deliberately not clever.

Its only job is to exercise the whole path -- observe, decide, execute,
reconcile, record -- end to end. It buys one ATM contract when flat and sells
that same contract when long, so consecutive cycles alternate sides and the
position closes itself rather than accumulating.

Do not read anything into its P&L. It is an instrument for testing the
pipeline, not a hypothesis about the market.
"""

from __future__ import annotations

from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "smoke_atm_roundtrip"
STRATEGY_VERSION = "1.0.0"


def _decide(ctx: StrategyContext) -> Decision:
    def no(reason: str, **meta) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or None,
        )

    if ctx.session_phase != "open":
        # Outside regular hours the collector's quotes age out, and the Worker
        # rejects anything older than ~15s. Declining here keeps the attempt
        # out of the rate-limit budget instead of buying a guaranteed 409.
        return no(
            f"Market is {ctx.session_phase}; execution quotes will be stale.",
            session_phase=ctx.session_phase,
        )

    row = ctx.snapshot.atm("call")
    if not row:
        return no("No quoted call in the snapshot to trade.")

    symbol = row["OptionSymbol"]
    position = ctx.book.position(symbol)

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
        )

    common = dict(
        symbol=symbol,
        quantity=1,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            "strike": row["Strike"],
            "underlying_price": ctx.snapshot.underlying_price,
            "bid": quote.bid,
            "ask": quote.ask,
            "quote_age_seconds": quote.age_seconds,
            "held_quantity": position.quantity,
        },
    )

    if position.quantity > 0:
        return Decision(
            action="sell",
            reason=f"Holding {position.quantity} {symbol}; closing one to complete the round trip.",
            **common,
        )

    if position.quantity < 0:
        # Not reachable from this strategy's own actions, but the book is
        # rebuilt from server history that may include anything.
        return no(
            f"Unexpected short position in {symbol}; standing down rather than compounding it.",
            symbol=symbol,
            held_quantity=position.quantity,
        )

    return Decision(
        action="buy",
        reason=f"Flat in {symbol}; opening one contract to exercise the execution path.",
        **common,
    )


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
smoke_atm_roundtrip = register(_decide)
