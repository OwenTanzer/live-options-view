"""SPY SMA(200) double-or-out strategy.

Signal: with 201 completed daily bars, compare the prior close/prior SMA(200)
with the latest close/latest SMA(200). A bullish crossover targets 200% long
SPY; a bearish crossover targets cash. Equality at either comparison creates
no new signal. The strategy holds across sessions and deliberately opts out of
the intraday flatten policy via ``params["overnight_hold"]``.

Execution convention: a signal is computed only from bars whose session date
is before the current ET date, so it is known before the next regular session.
The order is sent only while ``session_phase == "open"`` and only against an
executable live quote from the existing Crassus quote service. No fill is
assumed at the signal close.

The paper engine needs three bounded extensions that this strategy declares:
``instrument_type="share"`` (SPY), an opt-in per-bot margin limit, and an
opt-in overnight-hold flag. See the module docstring in
``crassus/scripts/verify_sma200.py`` for the architecture checks.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..daily_bars import (
    DailyBar, DailyBarError, completed_bars, expected_completed_session,
    fetch_yahoo_daily_bars, get_daily_bars, history_gap,
)
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "spy_sma200_double_or_out"
STRATEGY_VERSION = "1.0.0"
DEFAULT_SYMBOL = "SPY"
DEFAULT_WINDOW = 200
DEFAULT_LEVERAGE = 2.0
DEFAULT_FINANCING_RATE = 0.06
DEFAULT_STATE_DIR = Path(os.environ.get("CRASSUS_DATA_ROOT", ".")) / "state"


@dataclass(frozen=True)
class SmaSignal:
    status: str
    bar_date: str | None = None
    close: float | None = None
    sma: float | None = None
    prior_close: float | None = None
    prior_sma: float | None = None
    sample_count: int = 0
    window: int = DEFAULT_WINDOW

    @property
    def is_bullish_cross(self) -> bool:
        return self.status == "bullish_cross"

    @property
    def is_bearish_cross(self) -> bool:
        return self.status == "bearish_cross"

    @property
    def target(self) -> str:
        if self.is_bullish_cross:
            return "2x_long"
        if self.is_bearish_cross:
            return "cash"
        return "hold"

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def compute_sma_signal(bars: list[DailyBar], *, window: int = DEFAULT_WINDOW) -> SmaSignal:
    """Pure SMA(200) crossover calculation over completed bars.

    Requires ``window + 1`` bars: the current SMA uses the latest ``window``
    closes (including the signal day), and the prior SMA uses the preceding
    ``window`` closes. Returns ``insufficient_history`` rather than guessing.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    ordered = sorted(bars, key=lambda bar: bar.date)
    if len(ordered) < window + 1:
        return SmaSignal("insufficient_history", sample_count=len(ordered), window=window)
    current = ordered[-window:]
    prior = ordered[-(window + 1):-1]
    latest = ordered[-1]
    prior_close = ordered[-2].close
    sma = sum(bar.close for bar in current) / window
    prior_sma = sum(bar.close for bar in prior) / window
    common = dict(
        bar_date=latest.date.isoformat(),
        close=latest.close,
        sma=sma,
        prior_close=prior_close,
        prior_sma=prior_sma,
        sample_count=len(ordered),
        window=window,
    )
    # Equality at either the close/SMA comparison or the prior comparison
    # produces no cross; only strict penetration on the latest close counts.
    if prior_close <= prior_sma and latest.close > sma:
        return SmaSignal("bullish_cross", **common)
    if prior_close >= prior_sma and latest.close < sma:
        return SmaSignal("bearish_cross", **common)
    return SmaSignal("no_cross", **common)


@dataclass
class SmaState:
    activated_on: str
    last_processed_bar_date: str | None = None
    pending: dict[str, Any] | None = None
    regime: str = "cash"
    last_signal: dict[str, Any] = field(default_factory=dict)
    last_reason: str = ""
    updated_at: str | None = None
    adjustment_basis: str = "unknown"

    @classmethod
    def load(cls, path: Path) -> "SmaState | None":
        try:
            raw = json.loads(path.read_text())
            return cls(
                activated_on=str(raw["activated_on"]),
                last_processed_bar_date=raw.get("last_processed_bar_date"),
                pending=raw.get("pending"),
                regime=raw.get("regime", "cash"),
                last_signal=raw.get("last_signal") or {},
                last_reason=raw.get("last_reason", ""),
                updated_at=raw.get("updated_at"),
                adjustment_basis=raw.get("adjustment_basis", "unknown"),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, path)


def state_path(username: str, state_dir: Path | None = None) -> Path:
    root = Path(state_dir or DEFAULT_STATE_DIR)
    safe = "".join(ch for ch in username if ch.isalnum() or ch in "._-") or "account"
    return root / "sma200" / f"{safe}.json"


def size_long(equity: float, ask: float, *, leverage: float = DEFAULT_LEVERAGE) -> int:
    """Whole-share size for the target notional; never rounds up."""
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (equity, ask)):
        raise ValueError("equity and ask must be finite numbers")
    if equity <= 0 or ask <= 0 or leverage <= 1:
        return 0
    return max(0, int(math.floor((equity * leverage) / ask)))


def _position(ctx: StrategyContext, symbol: str) -> int:
    return int(ctx.book.position(symbol).quantity)


def _other_positions(ctx: StrategyContext, symbol: str) -> dict[str, int]:
    return {name: int(pos.quantity) for name, pos in ctx.book.positions.items()
            if name != symbol and pos.quantity != 0}


def _quote(ctx: StrategyContext, symbol: str):
    try:
        return ctx.quotes([symbol]).get(symbol)
    except Exception:
        return None


def _public_state(state: SmaState, signal: SmaSignal | None, reason: str,
                  *, symbol: str, position: int, equity: float | None = None,
                  ask: float | None = None) -> dict[str, Any]:
    payload = {
        "strategy": STRATEGY_ID,
        "symbol": symbol,
        "window": DEFAULT_WINDOW,
        "signal_date": signal.bar_date if signal else None,
        "close": signal.close if signal else None,
        "sma": signal.sma if signal else None,
        "prior_close": signal.prior_close if signal else None,
        "prior_sma": signal.prior_sma if signal else None,
        "signal": signal.status if signal else "waiting",
        "target": signal.target if signal else "hold",
        "position": "2x_long" if position > 0 else "cash",
        "position_qty": position,
        "equity": equity,
        "ask": ask,
        "pending": bool(state.pending),
        "reason": reason,
        "adjustment_basis": state.adjustment_basis,
        "execution_convention": "next_regular_session_executable_quote",
    }
    return payload


def decide(ctx: StrategyContext, *, fetcher=None, state_dir: Path | None = None) -> Decision:
    """Evaluate the strategy for one runner cycle."""
    def no(reason: str, *, meta: dict[str, Any] | None = None) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or {},
        )

    params = ctx.params or {}
    symbol = str(params.get("symbol", DEFAULT_SYMBOL)).upper()
    leverage = float(params.get("leverage", DEFAULT_LEVERAGE))
    if leverage <= 1 or leverage > 4:
        return no(f"Invalid leverage {leverage!r}; expected >1 and <=4.")

    username = str((ctx.account_state or {}).get("username") or "account")
    path = state_path(username, state_dir)
    state = SmaState.load(path)
    position = _position(ctx, symbol)
    others = _other_positions(ctx, symbol)
    if others:
        # This is a single-underlying strategy; an unrelated book means the
        # account was repurposed, so stand down rather than sell someone else's
        # symbols.
        return no("Account holds unrelated positions; standing down.",
                  meta={"strategy_state": _public_state(
                      state or SmaState(activated_on=ctx.now_et.date().isoformat()),
                      None, "unrelated_position", symbol=symbol, position=position)})

    try:
        bars, cache_status, basis = get_daily_bars(
            symbol,
            state_root=state_dir or DEFAULT_STATE_DIR,
            now=ctx.now_et,
            fetcher=fetcher or fetch_yahoo_daily_bars,
            cache_hours=float(params.get("bar_cache_hours", 6.0)),
        )
    except DailyBarError as exc:
        return no(f"Daily bars unavailable: {exc}",
                  meta={"strategy_state": _public_state(
                      state or SmaState(activated_on=ctx.now_et.date().isoformat(),
                                        adjustment_basis="unknown"),
                      None, "daily_bars_unavailable", symbol=symbol, position=position)})

    if state is not None:
        state.adjustment_basis = basis
    if basis != "adjusted_close":
        reason = (f"Daily series is {basis}; this strategy requires one complete "
                  f"adjusted-close series. No trade.")
        return no(reason, meta={"strategy_state": _public_state(
            state or SmaState(activated_on=ctx.now_et.date().isoformat(),
                              adjustment_basis=basis),
            None, "adjustment_basis_unavailable", symbol=symbol, position=position)})

    from ..daily_bars import completed_bars
    completed = completed_bars(bars, ctx.now_et)
    expected = expected_completed_session(ctx.now_et)
    eligible = [bar for bar in completed if bar.date <= expected]
    if not eligible or eligible[-1].date != expected:
        latest_text = eligible[-1].date.isoformat() if eligible else "none"
        reason = (f"Daily history is stale: latest bar {latest_text}, expected completed "
                  f"session {expected}. No trade.")
        if state is not None and state.pending:
            state.pending = None
            state.last_reason = reason
            state.save(path)
        return no(reason, meta={"strategy_state": _public_state(
            state or SmaState(activated_on=ctx.now_et.date().isoformat(),
                              adjustment_basis=basis),
            None, "stale_daily_history", symbol=symbol, position=position)})
    if len(eligible) < DEFAULT_WINDOW + 1:
        return no(
            f"Insufficient completed daily bars ({len(eligible)}); need {DEFAULT_WINDOW + 1}.",
            meta={"strategy_state": _public_state(
                state or SmaState(activated_on=ctx.now_et.date().isoformat(),
                                  adjustment_basis=basis),
                None, "insufficient_history", symbol=symbol, position=position)},
        )
    window = eligible[-DEFAULT_WINDOW - 1:]
    gap = history_gap(window)
    if gap:
        reason = f"Daily history gap ({gap}); no trade."
        if state is not None and state.pending:
            state.pending = None
            state.last_reason = reason
            state.save(path)
        return no(reason, meta={"strategy_state": _public_state(
            state or SmaState(activated_on=ctx.now_et.date().isoformat(),
                              adjustment_basis=basis),
            None, "gap_in_daily_history", symbol=symbol, position=position)})
    completed = eligible
    latest = completed[-1]

    # A pending signal is only executable while its bar is the latest completed
    # session. If a newer session exists, the old target is stale; cancel it
    # and evaluate the new bar rather than trading a missed day.
    if state is not None and state.pending and state.pending.get("bar_date") != expected.isoformat():
        stale_bar = state.pending.get("bar_date")
        state.pending = None
        state.last_reason = f"Outdated pending signal {stale_bar} canceled; awaiting the latest session."
        state.save(path)

    if state is None:
        # Fresh account: mark today's latest completed bar as the baseline and
        # wait for the next crossover. It must not backfill a historical cross.
        state = SmaState(
            activated_on=ctx.now_et.date().isoformat(),
            last_processed_bar_date=latest.date.isoformat(),
            regime="2x_long" if position > 0 else "cash",
            last_signal={"status": "fresh_account_baseline",
                         "bar_date": latest.date.isoformat()},
            last_reason="Fresh account warmed up; waiting for a new crossover.",
            updated_at=ctx.now_et.isoformat(),
            adjustment_basis=basis,
        )
        state.save(path)
        return no(state.last_reason, meta={"strategy_state": _public_state(
            state, None, state.last_reason, symbol=symbol, position=position)})

    # If a fill landed, reconcile the persisted regime before considering a
    # new bar; the server book is authoritative across restarts.
    if position > 0 and state.regime != "2x_long":
        state.regime = "2x_long"
    elif position == 0 and state.pending is None and state.regime == "2x_long":
        state.regime = "cash"

    signal = compute_sma_signal(completed, window=DEFAULT_WINDOW)

    if state.pending is None and latest.date.isoformat() > (state.last_processed_bar_date or ""):
        if signal.status == "no_cross":
            state.last_processed_bar_date = latest.date.isoformat()
            state.last_signal = signal.as_metadata()
            state.last_reason = "No new crossover on the latest completed bar; holding."
            state.updated_at = ctx.now_et.isoformat()
            state.save(path)
            return no(state.last_reason, meta={"strategy_state": _public_state(
                state, signal, state.last_reason, symbol=symbol, position=position)})
        if signal.is_bullish_cross or signal.is_bearish_cross:
            state.pending = {
                "bar_date": signal.bar_date,
                "target": signal.target,
                "signal": signal.as_metadata(),
                "created_at": ctx.now_et.isoformat(),
            }
            state.last_signal = signal.as_metadata()
            state.last_reason = f"New {signal.status} on {signal.bar_date}; awaiting next regular-session quote."
            state.updated_at = ctx.now_et.isoformat()
            state.save(path)

    if state.pending is None:
        reason = "No new completed bar since the last processed signal; holding."
        return no(reason, meta={"strategy_state": _public_state(
            state, signal, reason, symbol=symbol, position=position)})

    pending = state.pending
    bar_date = pending.get("bar_date")
    target = pending.get("target")
    if bar_date and bar_date <= (state.last_processed_bar_date or ""):
        state.pending = None
        state.save(path)
        return no("Stale pending signal cleared.", meta={"strategy_state": _public_state(
            state, signal, "stale_pending_cleared", symbol=symbol, position=position)})

    if ctx.session_phase != "open":
        reason = f"Signal {bar_date} is waiting for the next regular session; market is {ctx.session_phase}."
        return no(reason, meta={"strategy_state": _public_state(
            state, signal, reason, symbol=symbol, position=position)})

    quote = _quote(ctx, symbol)
    if quote is None:
        reason = f"No live quote returned for {symbol}; retrying next cycle."
        return no(reason, meta={"strategy_state": _public_state(
            state, signal, reason, symbol=symbol, position=position)})
    if not getattr(quote, "is_executable", False):
        reason = (f"Live quote for {symbol} is not executable "
                  f"(age={getattr(quote, 'age_seconds', None)}s); waiting.")
        return no(reason, meta={"strategy_state": _public_state(
            state, signal, reason, symbol=symbol, position=position)})

    if target == "2x_long":
        if position > 0:
            state.last_processed_bar_date = bar_date
            state.pending = None
            state.regime = "2x_long"
            state.last_reason = f"Bullish signal {bar_date} already represented by {position} shares."
            state.save(path)
            return no(state.last_reason, meta={"strategy_state": _public_state(
                state, signal, state.last_reason, symbol=symbol, position=position)})
        equity = float((ctx.account_state or {}).get("balance_cash") or 0.0)
        qty = size_long(equity, float(quote.ask), leverage=leverage)
        if qty < 1:
            reason = f"Entry equity ${equity:.2f} cannot buy one share at ask {quote.ask}."
            return no(reason, meta={"strategy_state": _public_state(
                state, signal, reason, symbol=symbol, position=position, equity=equity,
                ask=float(quote.ask))})
        state.last_reason = (f"Bullish crossover {bar_date}: buying {qty} {symbol} shares at "
                             f"the next regular-session ask {quote.ask:.2f} for "
                             f"{leverage:.1f}x entry equity.")
        state.updated_at = ctx.now_et.isoformat()
        state.save(path)
        return Decision(
            action="buy", symbol=symbol, quantity=qty,
            instrument_type="share",
            reason=state.last_reason,
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={
                "strategy_state": _public_state(
                    state, signal, state.last_reason, symbol=symbol, position=position,
                    equity=equity, ask=float(quote.ask)),
                "target_exposure": leverage,
                "bar_date": bar_date,
                "quote_ask": float(quote.ask),
                "quote_age_seconds": getattr(quote, "age_seconds", None),
                "financing_rate_annual": float(params.get("financing_rate", DEFAULT_FINANCING_RATE)),
            },
        )

    if target == "cash":
        if position == 0:
            state.last_processed_bar_date = bar_date
            state.pending = None
            state.regime = "cash"
            state.last_reason = f"Bearish signal {bar_date} already represented by cash."
            state.save(path)
            return no(state.last_reason, meta={"strategy_state": _public_state(
                state, signal, state.last_reason, symbol=symbol, position=position)})
        state.last_reason = (f"Bearish crossover {bar_date}: selling {position} {symbol} shares "
                             f"at the next regular-session bid {quote.bid:.2f}.")
        state.updated_at = ctx.now_et.isoformat()
        state.save(path)
        return Decision(
            action="sell", symbol=symbol, quantity=position,
            instrument_type="share",
            reason=state.last_reason,
            strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
            metadata={
                "strategy_state": _public_state(
                    state, signal, state.last_reason, symbol=symbol, position=position,
                    ask=float(quote.bid)),
                "bar_date": bar_date,
                "quote_bid": float(quote.bid),
                "quote_age_seconds": getattr(quote, "age_seconds", None),
            },
        )

    return no(f"Unrecognized pending target {target!r}; standing down.",
              meta={"strategy_state": _public_state(
                  state, signal, "unrecognized_target", symbol=symbol, position=position)})


_decide = decide
_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
# The daily strategy reads its own daily bars and only needs a live quote at
# execution time; an intraday chain snapshot is not part of its signal.
_decide.requires_snapshot = False
spy_sma200_double_or_out = register(_decide)
