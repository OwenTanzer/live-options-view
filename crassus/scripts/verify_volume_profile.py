#!/usr/bin/env python3
"""Prove the volume_profile_qqq strategy's profile math and decision logic.

Hermetic like verify_trump_whisperer.py and verify_momentum_qqq.py: no
network access, no real `yfinance` call. `build_profile()`/`_detect_regime()`
are exercised directly with hand-built `Bar` series; the strategy's
`_decide_core()` is exercised directly with those same hand-built bars -- it
takes no reader and makes no I/O. `VolumeProfileBarReader`'s cache is
exercised with an injected fake `fetch_fn` so `yfinance` is never imported or
invoked.

    python scripts/verify_volume_profile.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import volume_profile as vp  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def make_snapshot(underlying_price: float, rows: list[dict]) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": underlying_price,
            "rows": rows,
        },
        raw=b"{}",
    )


CALL_ROW = {"OptionSymbol": "QQQ240101C00400000", "Strike": 400.0, "Type": "call", "Bid": 1.0, "Ask": 1.1}
PUT_ROW = {"OptionSymbol": "QQQ240101P00400000", "Strike": 400.0, "Type": "put", "Bid": 1.0, "Ask": 1.1}


def make_ctx(
    *,
    session_phase: str = "open",
    trades: list[dict] | None = None,
    quote_map: dict[str, Quote] | None = None,
    params: dict | None = None,
    rows: list[dict] | None = None,
    underlying_price: float = 400.0,
    now_et: datetime | None = None,
) -> StrategyContext:
    snapshot = make_snapshot(underlying_price, rows if rows is not None else [CALL_ROW, PUT_ROW])
    book = Book(trades or [])
    quote_map = quote_map or {}
    return StrategyContext(
        snapshot=snapshot,
        account_state={},
        book=book,
        now_et=now_et,
        session_phase=session_phase,
        quotes=lambda symbols: {s: quote_map[s] for s in symbols if s in quote_map},
        params=params or {},
    )


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def stale_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:05:00")


BASE = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)


def bar(minute: int, price: float, volume: float) -> vp.Bar:
    """A bar whose high/low/close all sit at `price` (typical_price == price)."""
    return vp.Bar(timestamp=BASE + timedelta(minutes=minute), high=price, low=price, close=price, volume=volume)


def make_peaked_bars() -> list[vp.Bar]:
    """20 minutes of bars with a clear, unambiguous volume peak at 400.00.

    Prices step from 398.50 up to 401.50 in $0.25 increments (bin_width) with
    most volume concentrated right around 400.00-400.25, tapering off toward
    the edges -- a textbook single-peaked profile.
    """
    prices_volumes = [
        (398.50, 50), (398.75, 80), (399.00, 150), (399.25, 300),
        (399.50, 500), (399.75, 800), (400.00, 1200), (400.25, 1000),
        (400.50, 600), (400.75, 350), (401.00, 200), (401.25, 100),
        (401.50, 60),
    ]
    bars = []
    for i, (price, volume) in enumerate(prices_volumes):
        bars.append(bar(i, price, volume))
    return bars


# ---------------------------------------------------------------------------
# build_profile() -- POC / value area math
# ---------------------------------------------------------------------------


def scenario_registered() -> None:
    print("\n1. Registration")
    check("volume_profile_qqq is registered", "volume_profile_qqq" in REGISTRY)
    check(
        "Registered callable carries strategy_id/version",
        getattr(REGISTRY["volume_profile_qqq"], "strategy_id", None) == vp.STRATEGY_ID
        and getattr(REGISTRY["volume_profile_qqq"], "strategy_version", None) == vp.STRATEGY_VERSION,
    )


def scenario_poc_and_value_area() -> None:
    print("\n2. build_profile(): POC and value area from a clear volume peak")
    bars = make_peaked_bars()
    profile = vp.build_profile(bars, bin_width=0.25, value_area_pct=0.68)
    check("profile was built", profile is not None)
    check(
        "POC lands on the bin with the most volume (~400.00-400.25)",
        399.9 < profile.poc < 400.4,
        profile.poc,
    )
    check(
        "value area straddles the POC",
        profile.value_area_low <= profile.poc <= profile.value_area_high,
        (profile.value_area_low, profile.poc, profile.value_area_high),
    )
    total = sum(v for _, v in [
        (398.50, 50), (398.75, 80), (399.00, 150), (399.25, 300),
        (399.50, 500), (399.75, 800), (400.00, 1200), (400.25, 1000),
        (400.50, 600), (400.75, 350), (401.00, 200), (401.25, 100),
        (401.50, 60),
    ])
    check(
        "value area is narrower than the full traded range (68% capture)",
        (profile.value_area_high - profile.value_area_low) < 3.0,
        (profile.value_area_low, profile.value_area_high),
    )
    check("total_volume matches the sum of all bar volumes", profile.total_volume == total, profile.total_volume)


def scenario_build_profile_empty() -> None:
    print("\n3. build_profile(): no bars -> None, not a crash")
    check("returns None for empty bar list", vp.build_profile([]) is None)


# ---------------------------------------------------------------------------
# _decide_core() -- decision logic
# ---------------------------------------------------------------------------


def scenario_market_closed() -> None:
    print("\n4. Market not open declines without touching bars")
    ctx = make_ctx(session_phase="premarket")
    decision = vp._decide_core(ctx, None, None)
    check("no_trade when market isn't open", not decision.is_trade)
    check("reason names the session phase", "premarket" in decision.reason)
    check("metadata still carries the five required fields", set(["poc", "value_area_high", "value_area_low", "current_price", "regime"]) <= set(decision.metadata))


def scenario_fetch_error() -> None:
    print("\n5. Bar fetch failure declines instead of crashing the account")
    ctx = make_ctx(session_phase="open")
    decision = vp._decide_core(ctx, None, "ConnectionError: DNS failure")
    check("no_trade on fetch error", not decision.is_trade)
    check("reason surfaces the underlying error", "ConnectionError" in decision.reason)


def scenario_fetch_error_while_positioned_retains() -> None:
    print("\n6. Fetch failure while holding a position retains it")
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vp._decide_core(ctx, None, "empty response")
    check("action is no_trade, not sell", decision.action == "no_trade", decision.action)
    check("reason cites the missing observation", "unavailable" in decision.reason.lower(), decision.reason)
    check("reason mentions retaining the position", "retaining" in decision.reason.lower(), decision.reason)


def _breakout_bars_above() -> list[vp.Bar]:
    """Peaked profile around 400, then a fresh, sustained break above the VA."""
    bars = make_peaked_bars()
    # Last few minutes: price pushes decisively above the value area.
    n = len(bars)
    bars.append(bar(n, 402.50, 400))
    bars.append(bar(n + 1, 402.75, 400))
    bars.append(bar(n + 2, 403.00, 400))
    return bars


def _breakout_bars_below() -> list[vp.Bar]:
    bars = make_peaked_bars()
    n = len(bars)
    bars.append(bar(n, 397.00, 400))
    bars.append(bar(n + 1, 396.75, 400))
    bars.append(bar(n + 2, 396.50, 400))
    return bars


def scenario_breakout_above_buys_call() -> None:
    print("\n7. Fresh breakout above the value area -> buy one call")
    bars = _breakout_bars_above()
    ctx = make_ctx(session_phase="open", underlying_price=403.00, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = vp._decide_core(ctx, bars, None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("regime metadata is breakout", decision.metadata["regime"] == "breakout", decision.metadata)


def scenario_breakout_below_buys_put() -> None:
    print("\n8. Fresh breakout below the value area -> buy one put")
    bars = _breakout_bars_below()
    ctx = make_ctx(session_phase="open", underlying_price=396.50, quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = vp._decide_core(ctx, bars, None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("regime metadata is breakout", decision.metadata["regime"] == "breakout", decision.metadata)


def _reentry_bars_from_above() -> list[vp.Bar]:
    """Price went above the VA recently, then re-entered -- POC sits below current price.

    Excursion/re-entry bars carry deliberately small volume (10, vs. ~5390
    total across the peaked profile) so they register as price observations
    for regime detection without materially reshaping the POC/value area
    computed from the dominant peaked distribution.
    """
    bars = make_peaked_bars()
    n = len(bars)
    bars.append(bar(n, 401.00, 10))       # excursion above the VA (0.625 high)
    bars.append(bar(n + 1, 400.70, 10))   # still above, re-entering
    bars.append(bar(n + 2, 400.50, 10))   # now inside the VA -- current price
    return bars


def _reentry_bars_from_below() -> list[vp.Bar]:
    """Price went below the VA recently, then re-entered -- POC sits above current price."""
    bars = make_peaked_bars()
    n = len(bars)
    bars.append(bar(n, 399.00, 10))       # excursion below the VA (399.625 low)
    bars.append(bar(n + 1, 399.40, 10))   # still below, re-entering
    bars.append(bar(n + 2, 399.70, 10))   # now inside the VA -- current price
    return bars


def scenario_reentry_from_above_poc_below_buys_put() -> None:
    print("\n9. Re-entry from above (POC below current price) -> buy one put")
    bars = _reentry_bars_from_above()
    current_price = 400.50
    profile = vp.build_profile(bars, bin_width=0.25, value_area_pct=0.68)
    check("sanity: current price is inside the value area", profile.value_area_low <= current_price <= profile.value_area_high, (profile.value_area_low, current_price, profile.value_area_high))
    check("sanity: POC is below the re-entry price for this scenario", profile.poc < current_price, profile.poc)
    ctx = make_ctx(session_phase="open", underlying_price=current_price, quote_map={"QQQ240101P00400000": fresh_quote("QQQ240101P00400000")})
    decision = vp._decide_core(ctx, bars, None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the put", decision.symbol == "QQQ240101P00400000", decision.symbol)
    check("regime metadata is reentry", decision.metadata["regime"] == "reentry", decision.metadata)


def scenario_reentry_from_below_poc_above_buys_call() -> None:
    print("\n10. Re-entry from below (POC above current price) -> buy one call")
    bars = _reentry_bars_from_below()
    current_price = 399.70
    profile = vp.build_profile(bars, bin_width=0.25, value_area_pct=0.68)
    check("sanity: current price is inside the value area", profile.value_area_low <= current_price <= profile.value_area_high, (profile.value_area_low, current_price, profile.value_area_high))
    check("sanity: POC is above the re-entry price for this scenario", profile.poc > current_price, profile.poc)
    ctx = make_ctx(session_phase="open", underlying_price=current_price, quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")})
    decision = vp._decide_core(ctx, bars, None)
    check("action is buy", decision.action == "buy", decision.action)
    check("targets the call", decision.symbol == "QQQ240101C00400000", decision.symbol)
    check("regime metadata is reentry", decision.metadata["regime"] == "reentry", decision.metadata)


def _settled_bars() -> tuple[list[vp.Bar], float]:
    """A peaked profile, then a long quiet tail sitting on the POC.

    `make_peaked_bars()` alone spans price all the way from 398.50 to
    401.50 -- both tails of that spread sit outside the ~399.6-400.6 value
    area, so a `breakout_lookback_minutes`-sized window anchored at the very
    last of those 13 bars would still "see" that natural spread and read as
    a fresh excursion. Appending several more minutes of bars parked right
    at the POC pushes the lookback window's anchor forward until it only
    covers quiet, inside-the-value-area trading -- a genuinely settled
    read, not an artifact of the synthetic peak's own tails.
    """
    bars = make_peaked_bars()
    profile = vp.build_profile(bars, bin_width=0.25, value_area_pct=0.68)
    n = len(bars)
    for i in range(12):
        bars.append(bar(n + i, profile.poc, 20))
    return bars, profile.poc


def scenario_settled_inside_no_signal() -> None:
    print("\n11. Price settled inside the value area with no recent excursion -> no_trade")
    bars, inside_price = _settled_bars()
    ctx = make_ctx(session_phase="open", underlying_price=inside_price)
    decision = vp._decide_core(ctx, bars, None)
    check("no_trade with no fresh crossing", not decision.is_trade, decision.to_dict())
    check("regime metadata is none", decision.metadata["regime"] == "none", decision.metadata)


def scenario_settled_inside_closes_held_position() -> None:
    print("\n12. Price settles back inside with no recent excursion while holding -> close it")
    bars, inside_price = _settled_bars()
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(
        session_phase="open", trades=trades, underlying_price=inside_price,
        quote_map={"QQQ240101C00400000": fresh_quote("QQQ240101C00400000")},
    )
    decision = vp._decide_core(ctx, bars, None)
    check("action is sell, not no_trade", decision.action == "sell", decision.action)
    check("closes the held call", decision.symbol == "QQQ240101C00400000", decision.symbol)


def scenario_already_positioned_holds() -> None:
    print("\n13. Already holding the supported side -- no pyramiding")
    bars = _breakout_bars_above()
    trades = [{"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=403.00)
    decision = vp._decide_core(ctx, bars, None)
    check("no_trade rather than adding a second contract", not decision.is_trade)
    check("reason says already holding", "Already holding" in decision.reason)


def scenario_unexpected_short_stands_down() -> None:
    print("\n14. Unexpected short position -- stand down, don't compound it")
    bars = _breakout_bars_above()
    trades = [{"sym": "QQQ240101C00400000", "side": "sell", "qty": 1, "price": 1.0}]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=403.00)
    decision = vp._decide_core(ctx, bars, None)
    check("no_trade rather than compounding an unexpected short", not decision.is_trade)
    check("reason names the unexpected short", "short" in decision.reason.lower())


def scenario_stale_quote_declines() -> None:
    print("\n15. Breakout signal but stale quote declines rather than risking a 409")
    bars = _breakout_bars_above()
    ctx = make_ctx(session_phase="open", underlying_price=403.00, quote_map={"QQQ240101C00400000": stale_quote("QQQ240101C00400000")})
    decision = vp._decide_core(ctx, bars, None)
    check("no_trade on a stale quote", not decision.is_trade)
    check("reason cites executability", "not executable" in decision.reason)


def scenario_multiple_open_positions_stand_down() -> None:
    print("\n16. More than one open position -- stand down rather than guess")
    bars = _breakout_bars_above()
    trades = [
        {"sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0},
        {"sym": "QQQ240101P00400000", "side": "buy", "qty": 1, "price": 1.0},
    ]
    ctx = make_ctx(session_phase="open", trades=trades, underlying_price=403.00)
    decision = vp._decide_core(ctx, bars, None)
    check("no_trade with more than one open position", not decision.is_trade)
    check("reason flags multiple positions", "more than one" in decision.reason.lower())


# ---------------------------------------------------------------------------
# VolumeProfileBarReader -- cache behavior, no yfinance ever invoked
# ---------------------------------------------------------------------------


def scenario_reader_cache_respected() -> None:
    print("\n17. VolumeProfileBarReader: second read within refresh_interval_s doesn't re-fetch")
    call_count = {"n": 0}

    def fake_fetch(symbol: str) -> list[vp.Bar]:
        call_count["n"] += 1
        return make_peaked_bars()

    reader = vp.VolumeProfileBarReader(symbol="QQQ", refresh_interval_s=300.0, fetch_fn=fake_fetch)
    first = reader.read()
    second = reader.read()
    check("fetch_fn was called exactly once across two reads", call_count["n"] == 1, call_count["n"])
    check("both reads return the same cached bar list", first is second)


def scenario_reader_force_refetches() -> None:
    print("\n18. VolumeProfileBarReader: force=True bypasses the cache")
    call_count = {"n": 0}

    def fake_fetch(symbol: str) -> list[vp.Bar]:
        call_count["n"] += 1
        return make_peaked_bars()

    reader = vp.VolumeProfileBarReader(symbol="QQQ", refresh_interval_s=300.0, fetch_fn=fake_fetch)
    reader.read()
    reader.read(force=True)
    check("fetch_fn was called twice when forced", call_count["n"] == 2, call_count["n"])


def scenario_reader_bounds_a_hanging_fetch() -> None:
    print("\n18b. VolumeProfileBarReader: a fetch_fn that never returns is bounded by fetch_timeout_s")

    def hangs(symbol: str) -> list[vp.Bar]:
        time.sleep(5.0)
        return make_peaked_bars()

    reader = vp.VolumeProfileBarReader(symbol="QQQ", refresh_interval_s=300.0, fetch_fn=hangs, fetch_timeout_s=0.05, cooldown_s=60.0)
    started = time.monotonic()
    try:
        reader.read()
        check("read() raised on timeout", False)
    except RuntimeError as exc:
        elapsed = time.monotonic() - started
        check("read() returned promptly rather than waiting for the hung call", elapsed < 1.0, elapsed)
        check("reason cites the timeout", "timeout" in str(exc).lower(), str(exc))


def scenario_reader_cooldown_after_timeout_skips_refetch() -> None:
    print("\n18c. VolumeProfileBarReader: a subsequent read during cooldown fails fast without calling fetch_fn again")
    calls = {"n": 0}

    def hangs(symbol: str) -> list[vp.Bar]:
        calls["n"] += 1
        time.sleep(5.0)
        return make_peaked_bars()

    reader = vp.VolumeProfileBarReader(symbol="QQQ", refresh_interval_s=0.0, fetch_fn=hangs, fetch_timeout_s=0.05, cooldown_s=60.0)
    try:
        reader.read()
    except RuntimeError:
        pass
    check("first (timed-out) call invoked fetch_fn", calls["n"] == 1, calls["n"])
    try:
        reader.read(force=True)
        check("second read during cooldown raised", False)
    except RuntimeError as exc:
        check("second read during cooldown did not invoke fetch_fn again", calls["n"] == 1, calls["n"])
        check("reason cites the cooldown", "cooldown" in str(exc).lower(), str(exc))


def scenario_decide_stale_bars_treated_as_insufficient_data() -> None:
    print("\n18d. _decide(): bars far older than ctx.now_et are rejected as stale, not traded on")
    bars = make_peaked_bars()
    latest_bar_ts = max(b.timestamp for b in bars)

    class FreshReader:
        def read(self, force: bool = False) -> list[vp.Bar]:
            return bars

    vp._reader = FreshReader()
    # now_et is 6 hours after the latest bar -- e.g. yfinance served the
    # prior session, or the feed has stalled.
    stale_now = latest_bar_ts + timedelta(hours=6)
    decision = vp._decide(make_ctx(session_phase="open", now_et=stale_now))
    check("no_trade on stale bars", not decision.is_trade)
    check("reason cites staleness", "old" in decision.reason.lower() or "stall" in decision.reason.lower(), decision.reason)

    # Same bars, but now_et close to the latest bar -- fresh, should proceed
    # to a real regime read rather than being rejected.
    fresh_now = latest_bar_ts + timedelta(minutes=1)
    decision2 = vp._decide(make_ctx(session_phase="open", now_et=fresh_now))
    check(
        "fresh bars are not rejected as stale (reaches a real regime, not the staleness reason)",
        "old" not in decision2.reason.lower() and "stall" not in decision2.reason.lower(),
        decision2.reason,
    )


def main() -> int:
    for scenario in (
        scenario_registered,
        scenario_poc_and_value_area,
        scenario_build_profile_empty,
        scenario_market_closed,
        scenario_fetch_error,
        scenario_fetch_error_while_positioned_retains,
        scenario_breakout_above_buys_call,
        scenario_breakout_below_buys_put,
        scenario_reentry_from_above_poc_below_buys_put,
        scenario_reentry_from_below_poc_above_buys_call,
        scenario_settled_inside_no_signal,
        scenario_settled_inside_closes_held_position,
        scenario_already_positioned_holds,
        scenario_unexpected_short_stands_down,
        scenario_stale_quote_declines,
        scenario_multiple_open_positions_stand_down,
        scenario_reader_cache_respected,
        scenario_reader_force_refetches,
        scenario_reader_bounds_a_hanging_fetch,
        scenario_reader_cooldown_after_timeout_skips_refetch,
        scenario_decide_stale_bars_treated_as_insufficient_data,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
