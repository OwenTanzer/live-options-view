#!/usr/bin/env python3
"""Prove the Black-Scholes tool's math and its use as Newton's diagnostic.

Hermetic like verify_vwap_rvol.py, which this mirrors in style: no network
access, no real snapshot fetch. `black_scholes.py`'s pricing/Greeks/IV-solver
functions are checked against a textbook reference case and internal
consistency properties (put-call parity, IV round-trip, including a
European put whose price sits below its undiscounted intrinsic value);
`bs_edge.py`'s `evaluate_edge_gate` is exercised with hand-built snapshot
rows, including the reviewer-identified asynchronous-input and provider
time-convention reproductions; `momentum_qqq._decide_core()` is exercised
directly with a hand-built `MomentumSignal` plus `bs_edge_diagnostics_enabled`
in `params` -- no tracker, no collector, no I/O.

    python scripts/verify_black_scholes.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.black_scholes import (  # noqa: E402
    MIN_T_YEARS,
    greeks,
    implied_volatility,
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
    print("\n8. implied_volatility(): a price below any sigma's minimum returns None, doesn't crash")
    # deep ITM call, well below even the (near-)undiscounted intrinsic floor
    solved = implied_volatility("call", 10.0, 420.0, 400.0, 0.01, 0.05)
    check("returns None", solved is None)


def scenario_iv_put_round_trips_below_undiscounted_intrinsic() -> None:
    print("\n8b. implied_volatility(): a European put priced below undiscounted intrinsic still round-trips")
    # Reviewer's reproduction: at r=5%, this put's theoretical price (6.8036)
    # sits below undiscounted intrinsic (K-S=10) but above the correct
    # discounted-intrinsic floor (K*e^-rT - S ~= 5.122) -- an earlier
    # version's undiscounted pre-check rejected this as "invalid," when it's
    # exactly the price theoretical_price() itself produces.
    S, K, T, r, true_sigma = 90.0, 100.0, 1.0, 0.05, 0.10
    price = theoretical_price("put", S, K, T, r, true_sigma)
    check("price sits below undiscounted intrinsic (K-S=10)", price < (K - S), price)
    solved = implied_volatility("put", price, S, K, T, r)
    check("round-trips to sigma=0.10 instead of returning None",
          solved is not None and approx(solved, true_sigma, tol=1e-4), f"solved={solved}")


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


def scenario_edge_gate_async_moving_spot_reproduction() -> None:
    print("\n16b. evaluate_edge_gate(): reviewer repro -- a moving spot alone produces a large false edge")
    # PR fixture: 2026-06-10 14:00 ET, call S=K=400, board IV=.20. 45s later
    # the underlying has moved to 400.50 with IV unchanged; a genuinely valid
    # quote at that price (.774804936) reads as +60.4% "edge" purely because
    # evaluate_edge_gate is still comparing against the stale board S=400.
    quote_time = NOW.replace(second=45)
    T = time_to_expiry_years(quote_time, EXPIRATION)
    valid_quote_after_move = theoretical_price("call", 400.50, 400.0, T, 0.05, 0.20)
    check("reproduces the reviewer's quoted midpoint", approx(valid_quote_after_move, 0.774804936, tol=1e-6),
          valid_quote_after_move)
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.20}, 400.0, quote_time, EXPIRATION,
                               valid_quote_after_move, max_edge_pct=0.15)
    check("status ok (data looked usable)", gate.status == "ok", gate.status)
    check("edge_ok False despite the quote being genuinely valid -- stale board S, not a broken quote",
          gate.edge_ok is False, gate.edge_pct)
    check("edge magnitude matches the reviewer's reproduction (~+60.4%)", approx(gate.edge_pct, 0.604, tol=0.01),
          gate.edge_pct)


def scenario_edge_gate_async_changing_iv_reproduction() -> None:
    print("\n16c. evaluate_edge_gate(): reviewer repro -- IV changing between board and quote produces a false edge")
    quote_time = NOW.replace(second=45)  # same 45s-later fixture as the moving-spot repro
    T = time_to_expiry_years(quote_time, EXPIRATION)
    valid_quote_new_iv = theoretical_price("call", 400.0, 400.0, T, 0.05, 0.25)
    check("reproduces the reviewer's quoted midpoint", approx(valid_quote_new_iv, 0.603180761, tol=1e-6),
          valid_quote_new_iv)
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.20}, 400.0, quote_time, EXPIRATION,
                               valid_quote_new_iv, max_edge_pct=0.15)
    check("status ok (data looked usable)", gate.status == "ok", gate.status)
    check("edge_ok False despite the quote being genuinely valid -- stale board IV, not a broken quote",
          gate.edge_ok is False, gate.edge_pct)
    check("edge magnitude matches the reviewer's reproduction (~+24.9%)", approx(gate.edge_pct, 0.249, tol=0.01),
          gate.edge_pct)


def scenario_edge_gate_provider_time_convention_mismatch() -> None:
    print("\n16d. evaluate_edge_gate(): reviewer repro -- dxFeed's frozen 30-minute near-expiry IV convention "
          "disagrees with this module's wall-clock countdown even for simultaneous, self-consistent data")
    near_close = datetime(2026, 6, 10, 15, 40, 0, tzinfo=ET)  # 20 minutes to a 16:00 close
    provider_convention_t = 30.0 / (365.0 * 24.0 * 60.0)  # dxFeed freezes T at 30m near expiry
    provider_price = theoretical_price("call", 400.0, 400.0, provider_convention_t, 0.05, 0.20)
    check("reproduces the reviewer's provider-convention price", approx(provider_price, 0.241690709, tol=1e-6),
          provider_price)
    gate = evaluate_edge_gate({**CALL_ROW, "IV": 0.20}, 400.0, near_close, EXPIRATION,
                               provider_price, max_edge_pct=0.15)
    check("status ok (data looked usable)", gate.status == "ok", gate.status)
    check("edge_ok False despite simultaneous, self-consistent provider data -- a convention mismatch, not a broken quote",
          gate.edge_ok is False, gate.edge_pct)
    check("edge magnitude matches the reviewer's reproduction (~+22.5%)", approx(gate.edge_pct, 0.225, tol=0.01),
          gate.edge_pct)


# ---------------------------------------------------------------------------
# momentum_qqq._decide_core() with bs_edge_diagnostics_enabled
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


def scenario_diagnostics_off_by_default_no_behavior_change() -> None:
    print("\n17. _decide_core(): omitting bs_edge_diagnostics_enabled is identical to no diagnostics (regression guard)")
    row, fair = fair_call_row()
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}
    symbol = blown_out_row["OptionSymbol"]
    ctx = make_ctx(rows=[blown_out_row], quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])})
    decision = mq._decide_core(ctx, bullish_signal())
    check("still buys -- diagnostics never evaluated when the param is absent", decision.action == "buy", decision.to_dict())
    check("no bs_* metadata recorded when disabled", "bs_gate_status" not in decision.metadata, decision.metadata)


def scenario_diagnostics_record_fairly_priced_quote() -> None:
    print("\n18. _decide_core(): bs_edge_diagnostics_enabled records edge_ok=True for a fairly priced quote, still buys")
    row, _fair = fair_call_row()
    symbol = row["OptionSymbol"]
    ctx = make_ctx(
        rows=[row], params={"bs_edge_diagnostics_enabled": True},
        quote_map={symbol: fresh_quote(symbol, row["Bid"], row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("buys -- quote is fairly priced", decision.action == "buy", decision.to_dict())
    check("audit metadata records the bs gate status", decision.metadata.get("bs_gate_status") == "ok", decision.metadata)
    check("audit metadata records edge_ok=True", decision.metadata.get("bs_edge_ok") is True, decision.metadata)


def scenario_diagnostics_never_veto_an_implausibly_mispriced_quote() -> None:
    print("\n19. _decide_core(): bs_edge_diagnostics_enabled never vetoes, even an implausibly mispriced quote")
    row, fair = fair_call_row()
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}
    symbol = blown_out_row["OptionSymbol"]
    ctx = make_ctx(
        rows=[blown_out_row], params={"bs_edge_diagnostics_enabled": True},
        quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("still buys -- diagnostics are informational, not a veto", decision.action == "buy", decision.to_dict())
    check("audit metadata records edge_ok=False for the mispriced quote", decision.metadata.get("bs_edge_ok") is False,
          decision.metadata)


def scenario_diagnostics_never_veto_when_row_has_no_iv() -> None:
    print("\n20. _decide_core(): bs_edge_diagnostics_enabled never vetoes when the row carries no IV")
    row, _fair = fair_call_row()
    no_iv_row = {**row, "IV": None}
    symbol = no_iv_row["OptionSymbol"]
    ctx = make_ctx(
        rows=[no_iv_row], params={"bs_edge_diagnostics_enabled": True},
        quote_map={symbol: fresh_quote(symbol, no_iv_row["Bid"], no_iv_row["Ask"])},
    )
    decision = mq._decide_core(ctx, bullish_signal())
    check("still buys -- missing IV is recorded, not a veto", decision.action == "buy", decision.to_dict())
    check("audit metadata records bs_gate_status=no_iv", decision.metadata.get("bs_gate_status") == "no_iv",
          decision.metadata)


def scenario_diagnostics_never_block_a_close() -> None:
    print("\n21. _decide_core(): bs_edge_diagnostics_enabled never applies to (or blocks) closing a held position")
    row, fair = fair_call_row()
    blown_out_row = {**row, "Bid": round(fair * 5, 4), "Ask": round(fair * 5 + 0.01, 4)}
    symbol = blown_out_row["OptionSymbol"]
    trades = [{"sym": symbol, "side": "buy", "qty": 1, "price": fair}]
    ctx = make_ctx(
        rows=[blown_out_row], trades=trades, params={"bs_edge_diagnostics_enabled": True},
        quote_map={symbol: fresh_quote(symbol, blown_out_row["Bid"], blown_out_row["Ask"])},
    )
    bearish = MomentumSignal(
        lookback_minutes=60.0, current_price=396.0, anchor_price=400.0, return_pct=-0.01,
        sample_count=10, anchor_age_minutes=60.0, status="ok",
    )
    decision = mq._decide_core(ctx, bearish)
    check("action is sell -- diagnostics don't touch the closing leg", decision.action == "sell", decision.to_dict())
    check("closes the actual held call", decision.symbol == symbol)


def scenario_diagnostics_preserve_newton_opens_with_asynchronous_inputs() -> None:
    print("\n22. _decide_core(): known false discrepancies remain metadata, not execution vetoes")
    row, _fair = fair_call_row()
    symbol = row["OptionSymbol"]
    scenarios = (
        ("spot moved", NOW + timedelta(seconds=45), 400.50, 0.20, None),
        ("IV moved", NOW + timedelta(seconds=45), 400.0, 0.25, None),
        ("provider's 30m clock", NOW.replace(hour=15, minute=40), 400.0, 0.20,
         30.0 / (365.0 * 24.0 * 60.0)),
    )
    for name, now, current_spot, current_iv, provider_t in scenarios:
        T = provider_t if provider_t is not None else time_to_expiry_years(now, EXPIRATION)
        mid = theoretical_price("call", current_spot, 400.0, T, 0.05, current_iv)
        stamp = now.isoformat()
        quote = Quote(symbol=symbol, bid=mid - 0.01, ask=mid + 0.01,
                      quote_ts=stamp, server_ts=stamp)
        enabled_ctx = replace(
            make_ctx(rows=[row], quote_map={symbol: quote},
                     params={"bs_edge_diagnostics_enabled": True}),
            now_et=now,
        )
        enabled = mq._decide_core(enabled_ctx, bullish_signal())
        disabled = mq._decide_core(replace(enabled_ctx, params={}), bullish_signal())
        check(f"{name}: Newton opens with diagnostics enabled", enabled.action == "buy", enabled.to_dict())
        check(f"{name}: false discrepancy recorded", enabled.metadata.get("bs_edge_ok") is False,
              enabled.metadata.get("bs_edge_pct"))
        check(f"{name}: execution decision matches diagnostics disabled",
              (enabled.action, enabled.symbol, enabled.quantity, enabled.reason) ==
              (disabled.action, disabled.symbol, disabled.quantity, disabled.reason))


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
        scenario_iv_put_round_trips_below_undiscounted_intrinsic,
        scenario_iv_zero_time_returns_none,
        scenario_time_to_expiry_ordinary_day,
        scenario_time_to_expiry_early_close_shortens_countdown,
        scenario_time_to_expiry_floors_at_min,
        scenario_edge_gate_no_iv,
        scenario_edge_gate_expired,
        scenario_edge_gate_within_band,
        scenario_edge_gate_outside_band,
        scenario_edge_gate_async_moving_spot_reproduction,
        scenario_edge_gate_async_changing_iv_reproduction,
        scenario_edge_gate_provider_time_convention_mismatch,
        scenario_diagnostics_off_by_default_no_behavior_change,
        scenario_diagnostics_record_fairly_priced_quote,
        scenario_diagnostics_never_veto_an_implausibly_mispriced_quote,
        scenario_diagnostics_never_veto_when_row_has_no_iv,
        scenario_diagnostics_never_block_a_close,
        scenario_diagnostics_preserve_newton_opens_with_asynchronous_inputs,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
