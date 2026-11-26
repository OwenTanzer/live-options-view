"""Regular Max Pain with Regular Newton confirmation on entry only."""
from dataclasses import replace

from ..replay import register_state_resetter
from ..strategy import Decision, StrategyContext, register
from . import max_pain, momentum_qqq

STRATEGY_ID = "max_pain_newton_qqq"
VERSION = "1.0.0"


def decide(ctx: StrategyContext) -> Decision:
    # Both accounts use the same QQQ observation history. Also warm the series
    # while holding positions and on quiet pin boards for standalone replay.
    signal, source_error = momentum_qqq.observe_signal(ctx)
    direction = momentum_qqq.supported_side(signal, ctx.params or {})
    parent = max_pain._decide_core(ctx)
    metadata = {
        **(parent.metadata or {}),
        "newton_direction": direction,
        "newton_return": signal.return_pct if signal else None,
        "newton_status": source_error or (signal.status if signal else "unavailable"),
    }
    if parent.action == "buy":
        pin_side = max_pain._option_type_from_symbol(parent.symbol)
        metadata["pin_direction"] = pin_side
        if direction is None or direction != pin_side:
            return Decision.no_trade(
                f"Max Pain favors {pin_side}; Newton {direction or 'unavailable/neutral'} does not confirm.",
                STRATEGY_ID, VERSION, metadata=metadata,
            )
    return replace(parent, strategy_id=STRATEGY_ID, strategy_version=VERSION, metadata=metadata)


decide.strategy_id = STRATEGY_ID
decide.strategy_version = VERSION
register_state_resetter(STRATEGY_ID, momentum_qqq._reset_state)
max_pain_newton_qqq = register(decide)
