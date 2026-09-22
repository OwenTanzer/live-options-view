#!/usr/bin/env python3
"""Prove the Black-Scholes tool's math and its use as Newton's optional gate.

Hermetic like verify_vwap_rvol.py, which this mirrors in style: no network
access, no real snapshot fetch. `black_scholes.py`'s pricing/Greeks/IV-solver
functions are checked against a textbook reference case and internal
consistency properties (put-call parity, IV round-trip); `bs_edge.py`'s
`evaluate_edge_gate` is exercised with hand-built snapshot rows;
`momentum_qqq._decide_core()` is exercised directly with a hand-built
`MomentumSignal` plus `bs_edge_confirmation_required` in `params` -- no
tracker, no collector, no I/O.

    python scripts/verify_black_scholes.py
"""

from __future__ import annotations

import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.black_scholes import (  # noqa: E402
    MIN_T_YEARS,
    greeks,
    implied_volatility,
    intrinsic_value,
    theoretical_price,
    time_to_expiry_years,
)
from crassus.bs_edge import evaluate_edge_gate  # noqa: E402
from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.momentum import MomentumSignal  # noqa: E402
from crassus.strategies import momentum_qqq as mq  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

ET = ZoneInfo("America/New_York")

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def approx(a: float, b: float, tol: float = 1e-3) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# theoretical_price() -- textbook reference case
# ---------------------------------------------------------------------------
# S=100, K=100, T=1y, r=5%, sigma=20% -- Hull's canonical worked example.


def scenario_textbook_call_price() -> None:
    print("\n1. theoretical_price(): textbook call matches Hull's reference value")
    price = theoretical_price("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    check("call ~= 10.4506", approx(price, 10.4506, tol=1e-3), f"got {price:.4f}")


def scenario_textbook_put_price() -> None:
    print("\n2. theoretical_price(): textbook put matches Hull's reference value")
    price = theoretical_price("put", 100.0, 100.0, 1.0, 0.05, 0.2)
    check("put ~= 5.5735", approx(price, 5.5735, tol=1e-3), f"got {price:.4f}")


def scenario_put_call_parity() -> None:
    print("\n3. theoretical_price(): put-call parity holds (C - P = S - K*e^-rT)")
    S, K, T, r, sigma = 412.5, 400.0, 0.05, 0.045, 0.35
    call = theoretical_price("call", S, K, T, r, sigma)
    put = theoretical_price("put", S, K, T, r, sigma)
    import math
    parity_rhs = S - K * math.exp(-r * T)
    check("C - P == S - K*e^-rT", approx(call - put, parity_rhs, tol=1e-6),
          f"C-P={call - put:.6f}, rhs={parity_rhs:.6f}")


def scenario_t_le_zero_falls_back_to_intrinsic() -> None:
    print("\n4. theoretical_price(): T <= 0 returns intrinsic value, not a blown-up extrapolation")
    call_itm = theoretical_price("call", 405.0, 400.0, 0.0, 0.05, 0.2)
    call_otm = theoretical_price("call", 395.0, 400.0, 0.0, 0.05, 0.2)
    check("ITM call at T=0 is intrinsic", call_itm == 5.0, call_itm)
    check("OTM call at T=0 is worthless", call_otm == 0.0, call_otm)


# ---------------------------------------------------------------------------
# greeks() -- sanity bounds
# ---------------------------------------------------------------------------


def scenario_greeks_bounds() -> None:
    print("\n5. greeks(): delta bounds, positive gamma/vega, negative theta near ATM")
    g_call = greeks("call", 400.0, 400.0, 30.0 / 365.0, 0.05, 0.25)
    g_put = greeks("put", 400.0, 400.0, 30.0 / 365.0, 0.05, 0.25)
    check("call delta in (0, 1)", 0.0 < g_call.delta < 1.0, g_call.delta)
    check("put delta in (-1, 0)", -1.0 < g_put.delta < 0.0, g_put.delta)
    check("call delta - put delta ~= 1 (parity)", approx(g_call.delta - g_put.delta, 1.0, tol=1e-6))
    check("gamma positive", g_call.gamma > 0.0, g_call.gamma)
    check("gamma identical for call/put at same strike (parity)", approx(g_call.gamma, g_put.gamma, tol=1e-9))
    check("vega positive", g_call.vega > 0.0, g_call.vega)
    check("call theta negative (near-ATM time decay)", g_call.theta < 0.0, g_call.theta)


def scenario_greeks_undefined_at_expiry_raises() -> None:
    print("\n6. greeks(): T <= 0 raises rather than fabricating a Greeks read on a dead option")
    try:
        greeks("call", 400.0, 400.0, 0.0, 0.05, 0.25)
        check("raises ValueError at T=0", False)
    except ValueError:
        check("raises ValueError at T=0", True)


# ---------------------------------------------------------------------------
# implied_volatility() -- round-trip and edge cases
# ---------------------------------------------------------------------------


def scenario_iv_round_trip() -> None:
    print("\n7. implied_volatility(): round-trips a price generated at a known sigma")
    for true_sigma in (0.10, 0.25, 0.60, 1.20):
        S, K, T, r = 400.0, 400.0, 45.0 / (365.0 * 24.0 * 60.0), 0.05
        price = theoretical_price("call", S, K, T, r, true_sigma)
        solved = implied_volatility("call", price, S, K, T, r)
        check(f"sigma={true_sigma} round-trips", solved is not None and approx(solved, true_sigma, tol=1e-4),
              f"solved={solved}")


def scenario_iv_below_intrinsic_returns_none() -> None:
    print("\n8. implied_volatility(): a price below intrinsic value returns None, doesn't crash")
    # deep ITM call, intrinsic = 20; quoting it at 10 is not a valid option price
    solved = implied_volatility("call", 10.0, 420.0, 400.0, 0.01, 0.05)
    check("returns None", solved is None)


def scenario_iv_zero_time_returns_none() -> None:
    print("\n9. implied_volatility(): T <= 0 returns None rather than dividing by zero")
    solved = implied_volatility("call", 5.0, 400.0, 400.0, 0.0, 0.05)
    check("returns None", solved is None)


# ---------------------------------------------------------------------------
# time_to_expiry_years()
# ---------------------------------------------------------------------------


def scenario_time_to_expiry_ordinary_day() -> None:
    print("\n10. time_to_expiry_years(): 0DTE at 3pm ET on an ordinary day is under an hour left")
    now = datetime(2026, 6, 10, 15, 0, 0, tzinfo=ET)  # Wednesday, ordinary session
    T = time_to_expiry_years(now, date(2026, 6, 10))
    hours_left = T * 365.0 * 24.0
    check("about 1 hour left (16:00 close)", approx(hours_left, 1.0, tol=0.01), f"{hours_left:.4f}h")


def scenario_time_to_expiry_early_close_shortens_countdown() -> None:
    print("\n11. time_to_expiry_years(): an early-close expiration day shortens T vs an ordinary 16:00 close")
    from datetime import time as _time
    from crassus.exchange_calendar import session_close
    day_after_thanksgiving = date(2026, 11, 27)  # published NYSE early close
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=ET)
    close = session_close(day_after_thanksgiving)
    check("calendar reports an early close before 16:00", close.hour < 16, close)
    T_default = time_to_expiry_years(now, day_after_thanksgiving)  # uses the real 13:00 close
    T_hypothetical_16 = time_to_expiry_years(now, day_after_thanksgiving, close_time=_time(16, 0))
    check("default (real early close) leaves less time than a hypothetical 16:00 close",
          T_default < T_hypothetical_16, f"default={T_default:.6f} hypothetical_16={T_hypothetical_16:.6f}")


def scenario_time_to_expiry_floors_at_min() -> None:
    print("\n12. time_to_expiry_years(): past the close, clamps to MIN_T_YEARS instead of going negative")
    now = datetime(2026, 6, 10, 20, 0, 0, tzinfo=ET)  # well after a 16:00 close
    T = time_to_expiry_years(now, date(2026, 6, 10))
    check("clamped to MIN_T_YEARS", T == MIN_T_YEARS, T)


# ---------------------------------------------------------------------------
# bs_edge.evaluate_edge_gate()
# ---------------------------------------------------------------------------

CALL_ROW = {"OptionSymbol": "QQQ260610C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
NOW = datetime(2026, 6, 10, 14, 0, 0, tzinfo=ET)  # 2 hours before a 16:00 close
EXPIRATION = date(2026, 6, 10)


def scenario_edge_gate_no_iv() -> None:
    print("\n13. evaluate_edge_gate(): missing/zero row IV -> no_iv, nothing fabricated")
    for bad_iv in (None, 0.0, -0.1):
        gate = evaluate_edge_gate({**CALL_ROW, "IV": bad_iv}, 400.0, NOW, EXPIRATION, 1.05, max_edge_pct=0.15)
        check(f"IV={bad_iv} -> status no_iv", gate.status == "no_iv", gate.status)
        check(f"IV={bad_iv} -> edge_ok is None", gate.edge_ok is None)


def scenario_edge_gate_expired() -> None:
    print("\n14. evaluate_edge_gate(): T collapsed to the expiry floor -> expired")
    past_close = datetime(2026, 6, 10, 20, 0, 0, tzinfo=ET)
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.2}, 400.0, past_close, EXPIRATION, 1.05, max_edge_pct=0.15)
    check("status is expired", gate.status == "expired", gate.status)
    check("edge_ok is None", gate.edge_ok is None)


def scenario_edge_gate_within_band() -> None:
    print("\n15. evaluate_edge_gate(): quote priced at the theoretical value is within any positive band")
    T = time_to_expiry_years(NOW, EXPIRATION)
    fair = theoretical_price("call", 400.0, 400.0, T, 0.05, 0.20)
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.20}, 400.0, NOW, EXPIRATION, fair, max_edge_pct=0.15)
    check("status ok", gate.status == "ok", gate.status)
    check("edge_ok True at the fair value itself", gate.edge_ok is True, gate.edge_pct)
    check("edge_pct ~= 0", approx(gate.edge_pct, 0.0, tol=1e-6), gate.edge_pct)


def scenario_edge_gate_outside_band() -> None:
    print("\n16. evaluate_edge_gate(): a quote far from theoretical value trips the band")
    T = time_to_expiry_years(NOW, EXPIRATION)
    fair = theoretical_price("call", 400.0, 400.0, T, 0.05, 0.20)
    blown_out = fair * 3.0  # 200% above fair value
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.20}, 400.0, NOW, EXPIRATION, blown_out, max_edge_pct=0.15)
    check("status ok (data was usable)", gate.status == "ok", gate.status)
    check("edge_ok False -- outside the band", gate.edge_ok is False, gate.edge_pct)


# ---------------------------------------------------------------------------
# momentum_qqq._decide_core() with bs_edge_confirmation_required
# ---------------------------------------------------------------------------


def make_snapshot(rows: list[dict], underlying_price: float = 400.0) -> MarketSnapshot:
    payload = {
        "timestamp": "2026-06-10T18:00:00+00:00",
        "snapshot_time": "2026-06-10T18:00:00+00:00",
        "expiration": "2026-06-10",
        "underlying_price": underlying_price,
        "rows": rows,
    }
    return MarketSnapshot.from_payload(url="test://snapshot", payload=payload, raw=b"{}")


def make_ctx(*, rows: list[dict], trades: list[dict] | None = None,
             quote_map: dict[str, Quote] | None = None, params: dict | None = None) -> StrategyContext:
    snapshot = make_snapshot(rows)
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot, account_state={}, book=book, now_et=NOW, session_phase="open",
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str, bid: float, ask: float) -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts="2026-06-10T18:00:00", server_ts="2026-06-10T18:00:05")


def bullish_signal() -> MomentumSignal:
    return MomentumSignal(
        lookback_minutes=60.0, current_price=404.0, anchor_price=400.0, return_pct=0.01,
        sample_count=10, anchor_age_minutes=60.0, status="ok",
    )


def fair_call_row() -> tuple[dict, float]:
    T = time_to_expiry_years(NOW, EXPIRATION)
    fair = theoretical_price("call", 400.0, 400.0, T, 0.05, 0.20)
    row = {"OptionSymbol": "QQQ260610C00400000", "Strike": 400.0, "Type": "call",
           "Bid": round(fair - 0.01, 4), "Ask": round(fair + 0.01, 4), "IV": 0.20}
    return row, fair


def scenario_gate_off_by_default_no_behavior_change() -> None:
    print("\n17. _decide_core(): omitting bs_edge_confirmation_required is identical to no gate (regression guard)")
    row, fair = fair_call_row()
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}  # would fail the gate if enabled
    symbol = blown_out_row["OptionSymbol"]
    ctx = make_ctx(rows=[blown_out_row], quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])})
    decision = mq._decide_core(ctx, bullish_signal())
    check("still buys -- gate never evaluated when the param is absent", decision.action == "buy", decision.to_dict())


def scenario_gate_allows_fairly_priced_quote() -> None:
    print("\n18. _decide_core(): bs_edge_confirmation_required allows a buy priced at theoretical value")
    row, _fair = fair_call_row()
    symbol = row["OptionSymbol"]
    ctx = make_ctx(
        rows=[row], params={"bs_edge_confirmation_required": True},
        quote_map={symbol: fresh_quote(symbol, row["Bid"], row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("buys -- quote is fairly priced", decision.action == "buy", decision.to_dict())
    check("audit metadata records the bs gate status", decision.metadata.get("bs_gate_status") == "ok", decision.metadata)


def scenario_gate_vetoes_mispriced_quote() -> None:
    print("\n19. _decide_core(): bs_edge_confirmation_required vetoes an implausibly mispriced quote")
    row, fair = fair_call_row()
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}
    symbol = blown_out_row["OptionSymbol"]
    ctx = make_ctx(
        rows=[blown_out_row], params={"bs_edge_confirmation_required": True},
        quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade -- quote is 5x theoretical value", not decision.is_trade, decision.to_dict())
    check("reason cites Black-Scholes", "black-scholes" in decision.reason.lower(), decision.reason)


def scenario_gate_declines_when_row_has_no_iv() -> None:
    print("\n20. _decide_core(): bs_edge_confirmation_required declines when the row carries no IV")
    row, _fair = fair_call_row()
    no_iv_row = {**row, "IV": None}
    symbol = no_iv_row["OptionSymbol"]
    ctx = make_ctx(
        rows=[no_iv_row], params={"bs_edge_confirmation_required": True},
        quote_map={symbol: fresh_quote(symbol, no_iv_row["Bid"], no_iv_row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("no_trade -- no IV to confirm against", not decision.is_trade, decision.to_dict())
    check("reason cites unavailable confirmation", "unavailable" in decision.reason.lower(), decision.reason)


def scenario_gate_never_blocks_a_close() -> None:
    print("\n21. _decide_core(): bs_edge_confirmation_required never vetoes closing a held position")
    row, fair = fair_call_row()
    # Quote is wildly mispriced by BS standards, but this is the *held*
    # symbol on a reversed (bearish) signal -- the gate only guards opens.
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}
    symbol = blown_out_row["OptionSymbol"]
    trades = [{"sym": symbol, "side": "buy", "qty": 1, "price": fair}]
    ctx = make_ctx(
        rows=[blown_out_row], trades=trades, params={"bs_edge_confirmation_required": True},
        quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])},
    )
    bearish = MomentumSignal(
        lookback_minutes=60.0, current_price=396.0, anchor_price=400.0, return_pct=-0.01,
        sample_count=10, anchor_age_minutes=60.0, status="ok",
    )
    decision = mq._decide_core(ctx, bearish)
    check("action is sell -- gate does not block closing a held position", decision.action == "sell", decision.to_dict())
    check("closes the actual held call", decision.symbol == symbol)


def main() -> int:
    for scenario in (
        scenario_textbook_call_price,
        scenario_textbook_put_price,
        scenario_put_call_parity,
        scenario_t_le_zero_falls_back_to_intrinsic,
        scenario_greeks_bounds,
        scenario_greeks_undefined_at_expiry_raises,
        scenario_iv_round_trip,
        scenario_iv_below_intrinsic_returns_none,
        scenario_iv_zero_time_returns_none,
        scenario_time_to_expiry_ordinary_day,
        scenario_time_to_expiry_early_close_shortens_countdown,
        scenario_time_to_expiry_floors_at_min,
        scenario_edge_gate_no_iv,
        scenario_edge_gate_expired,
        scenario_edge_gate_within_band,
        scenario_edge_gate_outside_band,
        scenario_gate_off_by_default_no_behavior_change,
        scenario_gate_allows_fairly_priced_quote,
        scenario_gate_vetoes_mispriced_quote,
        scenario_gate_declines_when_row_has_no_iv,
        scenario_gate_never_blocks_a_close,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
