#!/usr/bin/env python3
"""No-optimization replay/evaluation for the SPY SMA(200) strategy.

Signals use completed daily closes only. A crossover observed at bar i is
executed at bar i+1's adjusted open, never at the signal close. The replay
includes a fixed 5 bp slippage and 1 bp commission per side and 6% annual
financing on negative cash; parameters are fixed, not tuned.

The fixture is a Yahoo Finance SPY daily chart snapshot stored locally, so the
result is reproducible offline. It is a research/evaluation receipt, not a
deployed-trading claim.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.daily_bars import DailyBar  # noqa: E402
from crassus.strategies.sma200 import compute_sma_signal  # noqa: E402

START_EQUITY = 10_000.0
SLIPPAGE_BPS = 5.0
COMMISSION_BPS = 1.0
FINANCING_RATE = 0.06
SPLIT_DATE = date(2020, 1, 1)


@dataclass
class ReplayState:
    cash: float = START_EQUITY
    shares: int = 0
    pending: str | None = None
    pending_bar: str | None = None
    last_equity: float = START_EQUITY
    trades: int = 0
    traded_notional: float = 0.0
    financing_paid: float = 0.0


def load_fixture(path: Path) -> list[DailyBar]:
    payload = json.loads(path.read_text())
    rows = payload["rows"]
    bars = []
    for row in rows:
        close = float(row["adjclose"])
        raw_close = float(row["close"])
        raw_open = float(row["open"])
        factor = close / raw_close if raw_close else 1.0
        bars.append(DailyBar(
            date.fromisoformat(row["date"]), close, raw_close, "yahoo_fixture",
            raw_open, raw_open * factor,
        ))
    return sorted(bars, key=lambda bar: bar.date)


def _costs(notional: float) -> float:
    return notional * (SLIPPAGE_BPS + COMMISSION_BPS) / 10_000.0


def _apply_financing(state: ReplayState, days: float = 1.0) -> None:
    if state.cash >= 0:
        return
    charge = (-state.cash) * FINANCING_RATE * days / 365.0
    state.cash -= charge
    state.financing_paid += charge


def run_replay(bars: list[DailyBar]) -> dict[str, Any]:
    state = ReplayState()
    equity_curve: list[tuple[date, float]] = []
    daily_returns: list[float] = []
    trades: list[dict[str, Any]] = []

    for index, bar in enumerate(bars):
        # Execute the prior signal at this bar's open, never at its close.
        if state.pending is not None and bar.adjusted_open and index > 0:
            price = float(bar.adjusted_open)
            if state.pending == "2x_long" and state.shares == 0:
                qty = int(math.floor((state.cash * 2.0) / (price * (1 + SLIPPAGE_BPS / 10_000.0))))
                if qty > 0:
                    notional = qty * price
                    total = notional + _costs(notional)
                    state.cash -= total
                    state.shares += qty
                    state.trades += 1
                    state.traded_notional += notional
                    trades.append({"date": bar.date.isoformat(), "side": "buy", "qty": qty,
                                   "price": round(price, 4), "notional": round(notional, 2),
                                   "cost": round(_costs(notional), 2), "reason": state.pending_bar})
            elif state.pending == "cash" and state.shares > 0:
                notional = state.shares * price
                state.cash += notional - _costs(notional)
                state.trades += 1
                state.traded_notional += notional
                trades.append({"date": bar.date.isoformat(), "side": "sell", "qty": state.shares,
                               "price": round(price, 4), "notional": round(notional, 2),
                               "cost": round(_costs(notional), 2), "reason": state.pending_bar})
                state.shares = 0
            state.pending = None
            state.pending_bar = None

        _apply_financing(state)
        equity = state.cash + state.shares * bar.close
        if equity_curve:
            prior = equity_curve[-1][1]
            if prior > 0:
                daily_returns.append(equity / prior - 1.0)
        equity_curve.append((bar.date, equity))

        signal = compute_sma_signal(bars[:index + 1])
        if signal.is_bullish_cross and state.shares == 0:
            state.pending = "2x_long"
            state.pending_bar = signal.bar_date
        elif signal.is_bearish_cross and state.shares > 0:
            state.pending = "cash"
            state.pending_bar = signal.bar_date

    return {
        "bars": len(bars),
        "start": bars[0].date.isoformat(),
        "end": bars[-1].date.isoformat(),
        "state": state,
        "equity_curve": equity_curve,
        "daily_returns": daily_returns,
        "trades": trades,
    }


def max_drawdown(curve: list[tuple[date, float]]) -> float:
    peak = -math.inf
    worst = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return worst


def metrics(curve: list[tuple[date, float]], returns: list[float],
            turnover: float, start_equity: float | None = None) -> dict[str, Any]:
    if not curve:
        return {}
    base = float(start_equity if start_equity is not None else curve[0][1])
    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1 / 365.25)
    final = curve[-1][1]
    annual = (final / base) ** (1 / years) - 1 if final > 0 and base > 0 else -1.0
    if returns:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        vol = math.sqrt(variance) * math.sqrt(252)
        sharpe = (mean * 252) / vol if vol > 0 else 0.0
    else:
        vol = 0.0
        sharpe = 0.0
    return {
        "start": curve[0][0].isoformat(),
        "end": curve[-1][0].isoformat(),
        "final_equity": round(final, 2),
        "total_return": round(final / base - 1, 6) if base > 0 else None,
        "annualized_return": round(annual, 6),
        "annualized_volatility": round(vol, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_drawdown(curve), 6),
        "turnover": round(turnover / max(base, 1), 4),
    }


def buy_hold(bars: list[DailyBar], start_index: int = 0,
             end_index: int | None = None) -> tuple[list[tuple[date, float]], list[float], float]:
    end_index = len(bars) if end_index is None else end_index
    period = bars[start_index:end_index]
    if not period:
        return [], [], 0.0
    first = period[0]
    price = float(first.adjusted_open or first.close)
    qty = int(math.floor((START_EQUITY * 0.999) / price))
    cash = START_EQUITY - qty * price - _costs(qty * price)
    curve = []
    returns = []
    prev = None
    for bar in period:
        equity = cash + qty * bar.close
        if prev is not None and prev > 0:
            returns.append(equity / prev - 1)
        curve.append((bar.date, equity))
        prev = equity
    return curve, returns, qty * price


def slice_result(result: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    curve = [(d, e) for d, e in result["equity_curve"] if start <= d < end]
    # Daily returns are reconstructed from the equity curve, avoiding an
    # off-by-one boundary from the original list.
    returns = []
    for i in range(1, len(curve)):
        prev = curve[i - 1][1]
        if prev > 0:
            returns.append(curve[i][1] / prev - 1)
    traded = sum(t["notional"] for t in result["trades"]
                 if start <= date.fromisoformat(t["date"]) < end)
    return {
        "metrics": metrics(curve, returns, traded),
        "equity_curve": curve,
        "trades": [t for t in result["trades"] if start <= date.fromisoformat(t["date"]) < end],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="crassus/scripts/fixtures/spy_daily_20y.json")
    parser.add_argument("--out", default="crassus/scripts/fixtures/sma200_replay.json")
    args = parser.parse_args()

    bars = load_fixture(Path(args.fixture))
    strategy = run_replay(bars)
    split_index = next((i for i, b in enumerate(bars) if b.date >= SPLIT_DATE), len(bars))
    in_sample = slice_result(strategy, bars[0].date, SPLIT_DATE)
    out_sample = slice_result(strategy, SPLIT_DATE, bars[-1].date.replace(year=bars[-1].date.year + 1))
    bh_all, bh_returns, bh_notional = buy_hold(bars)
    bh_in_curve, bh_in_returns, bh_in_notional = buy_hold(bars, 0, split_index)
    bh_out_curve, bh_out_returns, bh_out_notional = buy_hold(bars, split_index)

    def bh_metrics(curve, returns, turnover):
        return metrics(curve, returns, turnover)

    for trade in strategy["trades"]:
        if trade.get("reason"):
            assert trade["date"] > trade["reason"], "a fill cannot precede its signal bar"

    payload = {
        "strategy": "spy_sma200_double_or_out",
        "convention": {
            "signal": "adjusted daily closes; SMA(200) includes signal day",
            "execution": "next session adjusted open; no retrospective signal-close fill",
            "costs": {"slippage_bps": SLIPPAGE_BPS, "commission_bps": COMMISSION_BPS,
                      "financing_rate_annual": FINANCING_RATE},
            "split": {"in_sample": f"{bars[0].date}..2019-12-31",
                      "out_of_sample": f"2020-01-01..{bars[-1].date}"},
        },
        "full": {
            "strategy": metrics(strategy["equity_curve"], strategy["daily_returns"],
                                strategy["state"].traded_notional),
            "buy_and_hold": bh_metrics(bh_all, bh_returns, bh_notional),
        },
        "in_sample": {
            "strategy": in_sample["metrics"],
            "buy_and_hold": bh_metrics(bh_in_curve, bh_in_returns, bh_in_notional),
        },
        "out_of_sample": {
            "strategy": out_sample["metrics"],
            "buy_and_hold": bh_metrics(bh_out_curve, bh_out_returns, bh_out_notional),
        },
        "trades": strategy["trades"],
        "financing_paid": round(strategy["state"].financing_paid, 2),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({k: payload[k] for k in ("full", "in_sample", "out_of_sample", "financing_paid")},
                     indent=2))
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
