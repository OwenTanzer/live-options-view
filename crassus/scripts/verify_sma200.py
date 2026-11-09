#!/usr/bin/env python3
"""Deterministic B1 checks for the SPY SMA(200) double-or-out strategy.

Run from the repository root:
    python crassus/scripts/verify_sma200.py

The script uses hand-built daily bars and a fake quote/book context; it never
contacts a provider, the Worker or an account. Replay metrics are checked by
``replay_sma200.py`` against the fixed fixture.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.daily_bars import (
    DailyBar, DailyBarError, completed_bars, expected_completed_session,
    history_gap, is_trading_day, parse_yahoo_chart, previous_trading_day,
)  # noqa: E402
from crassus.market import Quote  # noqa: E402
from crassus.strategies import sma200  # noqa: E402
from crassus.strategy import StrategyContext, get  # noqa: E402

ET = ZoneInfo("America/New_York")
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def bars_from(closes: list[float], start: date = date(2025, 1, 2)) -> list[DailyBar]:
    out = []
    day = start
    for value in closes:
        while not is_trading_day(day):
            day += timedelta(days=1)
        out.append(DailyBar(day, float(value), float(value), "yahoo_chart_adjclose", float(value), float(value)))
        day += timedelta(days=1)
    return out


def next_trading_day(day: date) -> date:
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def now_after(day: date, hour: int = 10) -> datetime:
    return datetime.combine(next_trading_day(day), datetime.min.time().replace(hour=hour), ET)


def flat_then(last: float) -> list[DailyBar]:
    # 200 baseline closes at 100, then one signal-day close.
    return bars_from([100.0] * 200 + [last])


def baseline_bars() -> list[DailyBar]:
    return bars_from([100.0] * 201)


def bull_after_baseline() -> list[DailyBar]:
    return bars_from([100.0] * 201 + [101.0])


def bear_after_bull() -> list[DailyBar]:
    return bars_from([100.0] * 201 + [101.0, 101.2, 99.0])


class Ctx:
    def __init__(self, *, position: int = 0, cash: float = 10_000.0,
                 session: str = "open", now: datetime | None = None,
                 params: dict | None = None):
        self.now_et = now or datetime(2025, 10, 13, 10, 0, tzinfo=ET)
        self.session_phase = session
        self.account_state = {"username": "yahoo_chart_adjclose", "balance_cash": cash}
        self.book = Book([{
            "sym": "SPY", "side": "buy", "qty": position, "price": 500.0,
            "instrument_type": "share", "multiplier": 1,
            "execution_request_id": "yahoo_chart_adjclose",
        }] if position else [])
        self.params = params or {"symbol": "SPY", "leverage": 2.0, "bar_cache_hours": 0}
        self.snapshot = None
        self._quote = Quote("SPY", 499.9, 500.1,
                            "2026-01-05T14:59:59+00:00", "2026-01-05T15:00:00+00:00")

    def quotes(self, symbols):
        return {symbol: self._quote for symbol in symbols}


def with_bars(ctx: Ctx, bars: list[DailyBar], state_dir: Path):
    return sma200.decide(ctx, fetcher=lambda _symbol, range_="20y": bars, state_dir=state_dir)


def main() -> int:
    print("\n1. Signal math")
    bull = sma200.compute_sma_signal(flat_then(101.0))
    check("bullish crossover", bull.status == "bullish_cross", bull.status)
    check("current SMA includes signal day", abs(bull.sma - 100.005) < 1e-9, str(bull.sma))
    check("prior SMA excludes signal day", abs(bull.prior_sma - 100.0) < 1e-9, str(bull.prior_sma))
    check("prior close is the 200th baseline close", bull.prior_close == 100.0)
    bear = sma200.compute_sma_signal(flat_then(99.0))
    check("bearish crossover", bear.status == "bearish_cross", bear.status)
    eq = sma200.compute_sma_signal(flat_then(100.0))
    check("equality is no cross", eq.status == "no_cross", eq.status)
    short = sma200.compute_sma_signal(flat_then(101.0)[:200])
    check("200 bars are insufficient", short.status == "insufficient_history", short.status)

    print("\n2. Completed-bar boundary")
    today = date(2026, 1, 5)
    sample = bars_from([100.0] * 200 + [101.0], start=date(2025, 1, 2))
    sample_today = DailyBar(today, 101.0, 101.0, "yahoo_chart_adjclose", 101.0, 101.0)
    complete = completed_bars(sample + [sample_today], datetime(2026, 1, 5, 16, 30, tzinfo=ET))
    check("today's bar is excluded even after the close", complete[-1].date == sample[-1].date,
          str(complete[-1].date))
    payload = {
        "chart": {"result": [{
            "timestamp": [1767225600, 1767312000],
            "indicators": {
                "quote": [{"open": [100.0, 102.0], "close": [100.0, 104.0]}],
                "adjclose": [{"adjclose": [50.0, 52.0]}],
            },
        }]},
    }
    parsed = parse_yahoo_chart(payload)
    check("adjusted close is the signal series", parsed[-1].close == 52.0, str(parsed[-1].close))
    check("adjusted open scales raw open by the adjustment factor",
          abs(parsed[-1].adjusted_open - 51.0) < 1e-9, str(parsed[-1].adjusted_open))
    mixed = {
        "chart": {"result": [{
            "timestamp": [1767225600, 1767312000],
            "indicators": {
                "quote": [{"open": [100.0, 100.0], "close": [100.0, 100.0]}],
                "adjclose": [{"adjclose": [50.0, None]}],
            },
        }]},
    }
    try:
        parse_yahoo_chart(mixed)
        mixed_rejected = False
    except DailyBarError:
        mixed_rejected = True
    check("mixed adjusted/raw series is rejected", mixed_rejected)
    invalid = {
        "chart": {"result": [{
            "timestamp": [1767225600, 1767312000],
            "indicators": {
                "quote": [{"open": [100.0, 100.0], "close": [100.0, 100.0]}],
                "adjclose": [{"adjclose": [0.0, float("nan")]}],
            },
        }]},
    }
    try:
        parse_yahoo_chart(invalid)
        invalid_rejected = False
    except DailyBarError:
        invalid_rejected = True
    check("non-positive/non-finite-only series is rejected", invalid_rejected)
    check("Saturday maps to Friday's completed session",
          previous_trading_day(date(2025, 10, 18)) == date(2025, 10, 17))
    check("Sunday maps to Friday's completed session",
          expected_completed_session(datetime(2025, 10, 19, 12, 0, tzinfo=ET)) == date(2025, 10, 17))
    check("New Year's Day maps to the prior session",
          previous_trading_day(date(2026, 1, 1)) == date(2025, 12, 31))

    print("\n3. Sizing")
    check("2x whole-share sizing floors", sma200.size_long(10_000, 500) == 40,
          str(sma200.size_long(10_000, 500)))
    check("no entry with zero equity", sma200.size_long(0, 500) == 0)
    check("leverage must exceed 1", sma200.size_long(10_000, 500, leverage=1.0) == 0)

    print("\n4. Fresh account waits, then buys the next crossover")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ctx = Ctx(position=0, now=now_after(baseline_bars()[-1].date))
        first = with_bars(ctx, baseline_bars(), root)
        check("fresh account is a no_trade", not first.is_trade, first.action)
        check("fresh account records baseline", "Fresh account" in first.reason, first.reason)
        state = sma200.SmaState.load(sma200.state_path("yahoo_chart_adjclose", root))
        check("baseline persisted", state is not None and state.last_processed_bar_date is not None,
              str(state))
        # Add a new completed bull bar and evaluate the next open.
        bull_bars = bull_after_baseline()
        ctx2 = Ctx(position=0, now=now_after(bull_bars[-1].date))
        second = with_bars(ctx2, bull_bars, root)
        check("bullish signal is a buy", second.is_trade and second.action == "buy", second.action)
        check("share instrument is declared", second.instrument_type == "share", str(second.instrument_type))
        check("size is 2x entry equity", second.quantity == 39, str(second.quantity))
        check("decision carries signal state", isinstance(second.metadata.get("strategy_state"), dict))

    print("\n5. Restart/idempotency and bearish exit")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ctx = Ctx(position=0, now=now_after(baseline_bars()[-1].date))
        with_bars(ctx, baseline_bars(), root)
        # Simulate the fill by creating a position, then replay the same bar.
        ctx_pos = Ctx(position=40, now=now_after(bull_after_baseline()[-1].date, 10))
        # State currently has only the baseline; a pending signal from the
        # previous call is not present in this fresh directory, so create the
        # canonical pending state through a real call.
        ctx_signal = Ctx(position=0, now=now_after(bull_after_baseline()[-1].date))
        with_bars(ctx_signal, bull_after_baseline(), root)
        again = with_bars(ctx_pos, bull_after_baseline(), root)
        check("same signal after fill does not place another order", not again.is_trade, again.action)
        state = sma200.SmaState.load(sma200.state_path("yahoo_chart_adjclose", root))
        check("processed bar persisted after fill", state is not None and
              state.last_processed_bar_date == bull_after_baseline()[-1].date.isoformat(),
              str(state.last_processed_bar_date if state else None))

        hold_bars = bars_from([100.0] * 201 + [101.0, 101.2])
        ctx_hold = Ctx(position=40, now=now_after(hold_bars[-1].date))
        held = with_bars(ctx_hold, hold_bars, root)
        check("unchanged regime holds the position across a new bar", not held.is_trade, held.action)

        bear_bars = bear_after_bull()
        ctx_bear = Ctx(position=40, now=now_after(bear_bars[-1].date))
        sell = with_bars(ctx_bear, bear_bars, root)
        check("bearish signal sells the held position", sell.is_trade and sell.action == "sell", sell.action)
        check("sell quantity equals the held position", sell.quantity == 40, str(sell.quantity))

    print("\n6. Missing/stale executable quote waits without losing the pending signal")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with_bars(Ctx(position=0, now=now_after(baseline_bars()[-1].date)), baseline_bars(), root)
        no_quote = Ctx(position=0, now=now_after(bull_after_baseline()[-1].date))
        no_quote.quotes = lambda symbols: {}
        waiting = with_bars(no_quote, bull_after_baseline(), root)
        check("missing quote is a no_trade", not waiting.is_trade, waiting.action)
        check("waiting reason names the quote", "No live quote" in waiting.reason, waiting.reason)
        state = sma200.SmaState.load(sma200.state_path("yahoo_chart_adjclose", root))
        check("pending signal survives a quote miss", state is not None and state.pending is not None,
              str(state.pending if state else None))

    print("\n7. Stale, gap and outdated-pending fail closed")
    stale_root = tempfile.TemporaryDirectory()
    try:
        stale = with_bars(Ctx(position=0, now=datetime(2026, 1, 6, 10, 0, tzinfo=ET)),
                          baseline_bars(), Path(stale_root.name))
        check("fresh fetch cannot make old bars current",
              not stale.is_trade and "stale" in stale.reason.lower(), stale.reason)
    finally:
        stale_root.cleanup()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        full = bars_from([100.0] * 202)
        gapped = full[:100] + full[101:]
        gap_reason = history_gap(gapped[-201:])
        check("missing session is detected", gap_reason is not None, str(gap_reason))
        gap_call = with_bars(Ctx(position=0, now=now_after(gapped[-1].date)), gapped, root)
        check("gap fails closed without an order", not gap_call.is_trade and "gap" in gap_call.reason.lower(),
              gap_call.reason)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        baseline = baseline_bars()
        state = sma200.SmaState(
            activated_on=now_after(baseline[-1].date).date().isoformat(),
            last_processed_bar_date=baseline[-1].date.isoformat(),
            pending={"bar_date": baseline[-1].date.isoformat(), "target": "2x_long"},
            regime="cash",
        )
        state.save(sma200.state_path("yahoo_chart_adjclose", root))
        bull = bull_after_baseline()
        expected = expected_completed_session(now_after(bull[-1].date))
        call = with_bars(Ctx(position=0, now=now_after(bull[-1].date)), bull, root)
        updated = sma200.SmaState.load(sma200.state_path("yahoo_chart_adjclose", root))
        check("outdated pending signal is replaced by the latest session",
              call.is_trade and updated is not None and updated.pending is not None and
              updated.pending.get("bar_date") == expected.isoformat(),
              f"{call.action} {updated.pending if updated else None}")

    print("\n8. Raw feed cannot masquerade as adjusted")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        raw_bars = [replace(bar, source="yahoo_chart_raw_close") for bar in baseline_bars()]
        raw = with_bars(Ctx(position=0, now=now_after(raw_bars[-1].date)), raw_bars, root)
        check("all-raw feed is a no_trade", not raw.is_trade, raw.action)
        check("all-raw metadata names raw basis",
              raw.metadata["strategy_state"]["adjustment_basis"] == "raw_close",
              str(raw.metadata["strategy_state"]["adjustment_basis"]))
        cache_file = root / "sma200" / "SPY_daily.json"
        check("cache stores the actual basis", json.loads(cache_file.read_text())["adjustment_basis"] == "raw_close")
        cached_raw = with_bars(Ctx(position=0, now=now_after(raw_bars[-1].date)), raw_bars, root)
        check("cached raw series stays no_trade", not cached_raw.is_trade, cached_raw.action)

        unknown_file = root / "sma200" / "SPY_daily.json"
        payload = json.loads(unknown_file.read_text())
        payload["adjustment_basis"] = "unknown"
        unknown_file.write_text(json.dumps(payload))
        unknown = with_bars(Ctx(position=0, now=now_after(raw_bars[-1].date)), raw_bars, root)
        check("unknown cached basis fails closed", not unknown.is_trade and
              "adjustment basis" in unknown.reason.lower(), unknown.reason)

        payload["adjustment_basis"] = "adjusted_close"  # metadata lies about raw bars
        unknown_file.write_text(json.dumps(payload))
        mixed_cache = with_bars(Ctx(position=0, now=now_after(raw_bars[-1].date)), raw_bars, root)
        check("cached basis mismatch fails closed", not mixed_cache.is_trade and
              "consistent" in mixed_cache.reason.lower(), mixed_cache.reason)

    print("\n9. Overnight-hold opt-in and flatten isolation")
    from crassus.flatten import maybe_flatten
    ctx = Ctx(position=40, now=datetime(2026, 1, 5, 15, 50, tzinfo=ET))
    check("default flatten window still closes", maybe_flatten(ctx, {}) is not None)
    opted = maybe_flatten(ctx, {"overnight_hold": True})
    check("overnight_hold suppresses flatten", opted is None, str(opted))

    print("\n10. Registry")
    check("strategy is registered", get(sma200.STRATEGY_ID) is sma200.spy_sma200_double_or_out)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
