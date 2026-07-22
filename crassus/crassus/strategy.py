"""The strategy contract.

A strategy is a plain function from an observation to an intent. It proposes;
it does not execute. UUID creation, HTTP mechanics, retries, reconciliation
and logging all belong to the execution client -- which is what lets a
rules-based function, an ML model and an LLM call be compared later without
touching the execution path.

Fixed by `strategy_output_contract` in MOO-24.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

ACTIONS = frozenset({"no_trade", "buy", "sell"})


@dataclass
class Decision:
    """A strategy's proposed intent for one pass of the loop."""

    action: str
    reason: str
    strategy_id: str
    strategy_version: str
    symbol: str | None = None
    quantity: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}, got {self.action!r}")
        if not self.reason:
            raise ValueError("reason is required -- a no_trade with no reason is unevaluable")

        if self.action == "no_trade":
            if self.symbol is not None or self.quantity is not None:
                raise ValueError("no_trade must not carry a symbol or quantity")
            return

        if not self.symbol:
            raise ValueError(f"{self.action} requires a symbol")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError(f"{self.action} requires a positive integer quantity")

    @property
    def is_trade(self) -> bool:
        return self.action != "no_trade"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "reason": self.reason,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }

    @classmethod
    def no_trade(cls, reason: str, strategy_id: str, strategy_version: str, **kw: Any) -> "Decision":
        return cls(
            action="no_trade",
            reason=reason,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            **kw,
        )


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to see."""

    snapshot: Any  # MarketSnapshot
    account_state: dict[str, Any]  # authoritative /api/me payload
    book: Any  # Book -- positions derived from server trade history
    now_et: Any
    session_phase: str
    quotes: Callable[[list[str]], dict[str, Any]]  # on-demand live quotes
    params: dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    strategy_id: str
    strategy_version: str

    def __call__(self, ctx: StrategyContext) -> Decision: ...


REGISTRY: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    if strategy.strategy_id in REGISTRY:
        raise ValueError(f"Duplicate strategy_id {strategy.strategy_id!r}")
    REGISTRY[strategy.strategy_id] = strategy
    return strategy


def get(strategy_id: str) -> Strategy:
    if strategy_id not in REGISTRY:
        raise KeyError(f"Unknown strategy_id {strategy_id!r}; registered: {sorted(REGISTRY)}")
    return REGISTRY[strategy_id]
