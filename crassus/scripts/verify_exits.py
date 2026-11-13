#!/usr/bin/env python3
"""Verify configurable profit-taking and protective exits (#86 / B3).

The fixtures cover the decision table and the lifecycle edges the quest asks
for: inclusive threshold boundaries, long and short holdings, the
liquidation-side price (bid for a long, ask for a short), every held contract in
a multi-position book with stop-over-profit precedence, stale and missing
quotes, the Phelps window's two configurable bypasses, configuration
validation, the re-entry cooldown (including across a restart), the audit
metadata that records trigger/quote/threshold/fill, and the runner wiring that
makes an exit a real submitted order attributed to `exit_policy`.

    python scripts/verify_exits.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

if sys.platform == "win32":  # POSIX directory fsync has no meaning here
    import crassus.audit as _audit
    import crassus.durability as _durability

    _durability.sync_directory = lambda path: None
    _audit.sync_directory = lambda path: None

from crassus import exits  # noqa: E402
from crassus.client import AccountState, Book  # noqa: E402
from crassus.exits import (  # noqa: E402
    ExitConfigError,
    ExitPolicy,
    ExitStateStore,
    evaluate,
    pnl_pct,
    reentry_blocked,
    resolve_policy,
)
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategy import Decision, StrategyContext  # noqa: E402

SYMBOL = "QQQ240101C00400000"
NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _scratch_root() -> Path:
    candidate = Path(os.environ.get("CRASSUS_TEST_TMPDIR") or ROOT.parent / ".test-tmp") / "exits"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tmpdir() -> Path:
    base = _scratch_root()
    for attempt in range(100):
        candidate = base / f"run-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not create a scratch directory under {base}")


def quote(bid: float | None, ask: float | None, *, age_s: float = 5.0) -> Quote:
    server = NOW.replace(second=30)
    quote_ts = server - timedelta(seconds=age_s)
    return Quote(
        symbol=SYMBOL,
        bid=bid,
        ask=ask,
        quote_ts=quote_ts.replace(tzinfo=None).isoformat(),
        server_ts=server.replace(tzinfo=None).isoformat(),
    )


def book(*trades) -> Book:
    return Book(list(trades))


def long_trade(price: float = 1.00, symbol: str = SYMBOL, ts: str = "2024-01-01T10:00:00Z"):
    return {"sym": symbol, "side": "buy", "qty": 1, "price": price, "ts": ts}


def short_trade(price: float = 1.00, symbol: str = SYMBOL, ts: str = "2024-01-01T10:00:00Z"):
    return {"sym": symbol, "side": "sell", "qty": 1, "price": price, "ts": ts}


def ctx(book_obj, quotes, *, params=None, now_et=NOW) -> StrategyContext:
    return StrategyContext(
        snapshot=None,
        account_state={"username": "crassus_test"},
        book=book_obj,
        now_et=now_et,
        session_phase="open",
        quotes=lambda symbols: {s: quotes[s] for s in symbols if s in quotes},
        params=params or {},
        account_alias="Test",
    )


class BoundaryTests(unittest.TestCase):
    def test_profit_boundary_is_inclusive(self):
        policy = ExitPolicy(profit_target_pct=0.50)
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.50, 1.55)}), policy)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.metadata["exit_trigger"], "profit_target")

        below = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.49, 1.54)}), policy)
        self.assertIsNone(below)

    def test_stop_boundary_is_inclusive(self):
        policy = ExitPolicy()
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.50, 0.55)}), policy)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

        above = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.51, 0.56)}), policy)
        self.assertIsNone(above)

    def test_long_liquidates_at_the_bid_not_the_ask(self):
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.50, 3.00)}),
                            ExitPolicy(profit_target_pct=0.50))
        self.assertEqual(decision.action, "sell")
        self.assertEqual(decision.quantity, 1)
        self.assertEqual(decision.metadata["exit_liquidation_price"], 1.50)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_short_liquidates_at_the_ask(self):
        # Short 1.00; buying back at 0.50 is a 50% gain.
        short_book = book(short_trade(1.00))
        decision = evaluate(ctx(short_book, {SYMBOL: quote(0.40, 0.50)}),
                            ExitPolicy(profit_target_pct=0.50))
        self.assertEqual(decision.action, "buy")
        self.assertEqual(decision.quantity, 1)
        self.assertEqual(decision.metadata["exit_liquidation_price"], 0.50)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_short_stop_uses_the_ask(self):
        short_book = book(short_trade(1.00))
        decision = evaluate(ctx(short_book, {SYMBOL: quote(1.40, 1.50)}), ExitPolicy())
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")
        self.assertEqual(decision.metadata["exit_liquidation_price"], 1.50)

    def test_cost_basis_comes_from_the_documented_book_average(self):
        # Two buys at 1.00 and 2.00 -> average 1.50; bid 2.25 is +50%.
        scaled = book(
            {"sym": SYMBOL, "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": SYMBOL, "side": "buy", "qty": 1, "price": 2.00, "ts": "2024-01-01T10:05:00Z"},
        )
        decision = evaluate(ctx(scaled, {SYMBOL: quote(2.25, 2.30)}),
                            ExitPolicy(profit_target_pct=0.50))
        self.assertEqual(decision.quantity, 2)
        self.assertAlmostEqual(decision.metadata["exit_cost_basis"], 1.5)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_pnl_sign_convention(self):
        self.assertAlmostEqual(pnl_pct(1.0, 1.5, 1), 0.5)
        self.assertAlmostEqual(pnl_pct(1.0, 0.5, 1), -0.5)
        self.assertAlmostEqual(pnl_pct(1.0, 0.5, -1), 0.5)
        with self.assertRaises(ValueError):
            pnl_pct(0.0, 1.0, 1)


class PremiumStop35Tests(unittest.TestCase):
    """Skew + Direction step 2: the selected 35% entry-premium stop.

    Full-position exit at the first eligible executable bid at or below 65% of
    the position's OWN entry basis, including after each re-entry.
    """
    POLICY = ExitPolicy(stop_loss_pct=-0.35)

    def test_exactly_65_percent_triggers_for_every_cent_basis(self):
        # Integer ground truth: bid_cents * 100 <= basis_cents * 65.
        misses = []
        for basis_c in range(5, 2001):
            for bid_c in (basis_c * 65 // 100 - 1, basis_c * 65 // 100,
                          basis_c * 65 // 100 + 1):
                if bid_c <= 0:
                    continue
                want = bid_c * 100 <= basis_c * 65
                got = exits._threshold_reached(basis_c / 100, bid_c / 100, 1, -0.35)
                if got != want:
                    misses.append((basis_c, bid_c))
        self.assertEqual(misses, [])

    def test_float_trap_case_now_fires(self):
        # (0.91 - 1.40) / 1.40 == -0.3499999999999999 in binary floats.
        self.assertGreater(pnl_pct(1.40, 0.91, 1), -0.35)
        decision = evaluate(ctx(book(long_trade(1.40)), {SYMBOL: quote(0.91, 0.95)}), self.POLICY)
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")
        self.assertEqual(decision.metadata["exit_threshold_price"], 0.91)

    def test_one_cent_above_the_level_holds(self):
        self.assertIsNone(evaluate(ctx(book(long_trade(1.40)), {SYMBOL: quote(0.92, 0.95)}),
                                   self.POLICY))

    def test_reentry_uses_its_own_entry_basis(self):
        # First lot 2.00 closed at 1.00; re-entry at 1.00. The stop level is
        # 0.65 (65% of 1.00), not 65% of any blend with the closed lot.
        reentered = book(long_trade(2.00, ts="2024-01-01T10:00:00Z"),
                         {"sym": SYMBOL, "side": "sell", "qty": 1, "price": 1.00,
                          "ts": "2024-01-01T10:20:00Z"},
                         long_trade(1.00, ts="2024-01-01T10:40:00Z"))
        self.assertEqual(reentered.position(SYMBOL).average_cost, 1.00)
        fires = evaluate(ctx(reentered, {SYMBOL: quote(0.65, 0.70)}), self.POLICY)
        self.assertEqual(fires.metadata["exit_cost_basis"], 1.0)
        self.assertEqual(fires.metadata["exit_threshold_price"], 0.65)
        self.assertIsNone(evaluate(ctx(reentered, {SYMBOL: quote(0.66, 0.70)}), self.POLICY))
        # And the other way: cheap first lot, dearer re-entry.
        dearer = book(long_trade(1.00, ts="2024-01-01T10:00:00Z"),
                      {"sym": SYMBOL, "side": "sell", "qty": 1, "price": 1.10,
                       "ts": "2024-01-01T10:20:00Z"},
                      long_trade(2.00, ts="2024-01-01T10:40:00Z"))
        self.assertEqual(evaluate(ctx(dearer, {SYMBOL: quote(1.30, 1.40)}), self.POLICY)
                         .metadata["exit_threshold_price"], 1.3)
        self.assertIsNone(evaluate(ctx(dearer, {SYMBOL: quote(1.31, 1.40)}), self.POLICY))

    def test_exit_is_the_full_position(self):
        two = book(long_trade(1.00), long_trade(1.00, ts="2024-01-01T10:01:00Z"))
        decision = evaluate(ctx(two, {SYMBOL: quote(0.60, 0.65)}), self.POLICY)
        self.assertEqual((decision.action, decision.quantity), ("sell", 2))

    def test_overshoot_is_auditable(self):
        decision = evaluate(ctx(book(long_trade(1.00)), {SYMBOL: quote(0.40, 0.45)}), self.POLICY)
        meta = decision.metadata
        self.assertEqual(meta["exit_threshold_price"], 0.65)
        self.assertEqual(meta["exit_liquidation_price"], 0.40)  # 0.25 past the level

    def test_first_eligible_bid_waits_for_an_executable_quote(self):
        held = book(long_trade(1.00))
        stale = evaluate(ctx(held, {SYMBOL: quote(0.60, 0.65, age_s=40.0)}), self.POLICY)
        self.assertEqual(stale.action, "no_trade")
        self.assertEqual(stale.metadata["exit_pending"][0]["reason"], "stale_quote")
        missing = evaluate(ctx(held, {}), self.POLICY)
        self.assertEqual(missing.metadata["exit_pending"][0]["reason"], "missing_liquidation_quote")
        fresh = evaluate(ctx(held, {SYMBOL: quote(0.60, 0.65)}), self.POLICY)
        self.assertEqual(fresh.action, "sell")

    def test_oi_skew_config_is_inert_until_activation(self):
        params = {"exits": {"stop_loss_pct": -0.35}}
        self.assertFalse(resolve_policy("oi_skew_qqq", params, activated=False).enabled)
        live = resolve_policy("oi_skew_qqq", params, activated=True)
        self.assertTrue(live.enabled)
        self.assertEqual((live.stop_loss_pct, live.profit_target_pct), (-0.35, None))

    def test_existing_default_boundaries_unchanged(self):
        # -50% default and +50% target still inclusive exactly at the level.
        self.assertTrue(exits._threshold_reached(1.00, 0.50, 1, -0.50))
        self.assertFalse(exits._threshold_reached(1.00, 0.51, 1, -0.50))
        self.assertTrue(exits._threshold_reached(1.00, 1.50, 1, 0.50))
        self.assertTrue(exits._threshold_reached(1.00, 1.50, -1, -0.50))   # short stop at ask
        self.assertFalse(exits._threshold_reached(1.00, 1.49, -1, -0.50))


class QuoteTests(unittest.TestCase):
    def test_stale_quote_blocks_the_exit_and_holds_the_cycle(self):
        stale = quote(0.40, 0.45, age_s=30.0)
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: stale}), ExitPolicy())
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_pending_symbols"], [SYMBOL])

    def test_missing_quote_blocks_the_exit(self):
        decision = evaluate(ctx(book(long_trade()), {}), ExitPolicy())
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_pending_symbols"], [SYMBOL])

    def test_quote_missing_the_liquidation_side_is_not_usable(self):
        # No bid, so a long cannot be valued even though an ask exists.
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(None, 1.5)}), ExitPolicy())
        self.assertEqual(decision.action, "no_trade")

    def test_quote_error_becomes_a_holding_no_trade(self):
        def broken(_symbols):
            raise RuntimeError("quote service down")

        context = ctx(book(long_trade()), {})
        context.quotes = broken
        decision = evaluate(context, ExitPolicy())
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_quotes_error"], "RuntimeError")

    def test_flat_book_produces_no_exit(self):
        self.assertIsNone(evaluate(ctx(book(), {}), ExitPolicy()))


class PrecedenceTests(unittest.TestCase):
    def test_stop_outranks_profit_in_a_multi_position_book(self):
        winners = book(
            {"sym": "WIN", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "LOSS", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        quotes = {
            "WIN": Quote("WIN", 1.70, 1.75, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
            "LOSS": Quote("LOSS", 0.40, 0.45, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
        }
        decision = evaluate(ctx(winners, quotes), ExitPolicy(profit_target_pct=0.50))
        self.assertEqual(decision.symbol, "LOSS")
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

    def test_largest_loss_wins_between_two_stops(self):
        two = book(
            {"sym": "A", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "B", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        quotes = {
            "A": Quote("A", 0.45, 0.50, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
            "B": Quote("B", 0.20, 0.25, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
        }
        decision = evaluate(ctx(two, quotes), ExitPolicy())
        self.assertEqual(decision.symbol, "B")

    def test_every_held_contract_is_considered_each_cycle(self):
        two = book(
            {"sym": "A", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "B", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        # A has no quote; B is comfortably profitable and must still exit.
        quotes = {"B": Quote("B", 1.60, 1.65, "2024-01-01T12:00:25", "2024-01-01T12:00:30")}
        decision = evaluate(ctx(two, quotes), ExitPolicy(profit_target_pct=0.50))
        self.assertEqual(decision.symbol, "B")
        self.assertEqual(decision.metadata["exit_trigger"], "profit_target")

    def test_phelps_stop_bypasses_the_window_by_default(self):
        params = {"phelps_minutes": 27.5, "exits": {"profit_target_pct": 0.50}}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))  # 5 minutes ago
        decision = evaluate(
            ctx(recent, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "sell")
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

    def test_phelps_profit_waits_for_the_window_by_default(self):
        params = {"phelps_minutes": 27.5, "exits": {"profit_target_pct": 0.50}}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))
        policy = resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True)
        deferred = evaluate(
            ctx(recent, {SYMBOL: quote(1.60, 1.65)}, params=params),
            policy,
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(deferred.action, "no_trade")
        self.assertEqual(deferred.metadata["exit_pending_symbols"], [SYMBOL])

        mature = book(long_trade(ts="2024-01-01T11:00:00Z"))  # 60 minutes ago
        fired = evaluate(
            ctx(mature, {SYMBOL: quote(1.60, 1.65)}, params=params),
            policy,
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(fired.action, "sell")

    def test_phelps_stop_can_be_made_to_respect_the_window(self):
        params = {"phelps_minutes": 27.5, "exits": {"stop_bypasses_phelps": False}}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))
        decision = evaluate(
            ctx(recent, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_pending"][0]["reason"], "phelps_window")

    def test_audit_names_the_precedence_that_was_actually_applied(self):
        # A stop configured to bypass says so, and a profit target configured
        # to bypass says *that* -- the sentence follows the policy rather than
        # asserting the default in both directions.
        params = {"phelps_minutes": 27.5,
                  "exits": {"profit_target_pct": 0.50,
                            "stop_bypasses_phelps": True, "profit_bypasses_phelps": True}}
        policy = resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True)

        stop = evaluate(
            ctx(book(long_trade(ts="2024-01-01T11:55:00Z")), {SYMBOL: quote(0.40, 0.45)},
                params=params),
            policy, strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(stop.metadata["exit_phelps_window"], "bypassed_by_config")
        self.assertIn("stop_bypasses_phelps=true", stop.reason)

        profit = evaluate(
            ctx(book(long_trade(ts="2024-01-01T11:55:00Z")), {SYMBOL: quote(1.60, 1.65)},
                params=params),
            policy, strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(profit.metadata["exit_phelps_window"], "bypassed_by_config")
        self.assertIn("profit_bypasses_phelps=true", profit.reason)

    def test_audit_says_window_elapsed_when_it_really_elapsed(self):
        params = {"phelps_minutes": 27.5}
        mature = book(long_trade(ts="2024-01-01T11:00:00Z"))
        decision = evaluate(
            ctx(mature, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.metadata["exit_phelps_window"], "window_elapsed")
        self.assertIn("already elapsed", decision.reason)

    def test_non_phelps_strategy_records_no_window(self):
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.40, 0.45)}),
                            resolve_policy("reddit_sentiment_qqq", {}, activated=True),
                            strategy_id="reddit_sentiment_qqq")
        self.assertEqual(decision.metadata["exit_phelps_window"], "not_applicable")
        self.assertNotIn("Phelps", decision.reason)

    def test_unknown_entry_time_does_not_hold_a_stop_open(self):
        params = {"phelps_minutes": 27.5}
        untimed = book({"sym": SYMBOL, "side": "buy", "qty": 1, "price": 1.00})
        decision = evaluate(
            ctx(untimed, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "sell")


class PolicyTests(unittest.TestCase):
    def test_covered_strategy_enables_the_layer(self):
        self.assertTrue(resolve_policy("reddit_sentiment_qqq", {}, activated=True).enabled)

    def test_layer_is_inert_without_deployment_activation(self):
        # Neither the covered-strategy default nor an explicit per-account
        # opt-in can bypass the deployment switch: this branch must change no
        # trading behaviour until CRASSUS_EXITS_ENABLED is set.
        self.assertFalse(resolve_policy("reddit_sentiment_qqq", {}).enabled)
        self.assertFalse(
            resolve_policy("reddit_sentiment_qqq", {"exits": {"enabled": True}}).enabled
        )
        decision = evaluate(
            ctx(book(long_trade()), {SYMBOL: quote(0.10, 0.15)}),
            resolve_policy("reddit_sentiment_qqq", {}),
        )
        self.assertIsNone(decision)

    def test_activation_parsing_requires_an_explicit_affirmative(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(
                exits.activation_from_environment({"CRASSUS_EXITS_ENABLED": value}), value
            )
        self.assertFalse(exits.activation_from_environment({}))
        for value in ("0", "false", "no", "off", "maybe", ""):
            self.assertFalse(
                exits.activation_from_environment({"CRASSUS_EXITS_ENABLED": value}), value
            )

    def test_uncovered_strategy_stays_off_unless_it_opts_in(self):
        self.assertFalse(resolve_policy("smoke_atm_roundtrip", {}, activated=True).enabled)
        self.assertFalse(resolve_policy("looking_glass_straddle", {}, activated=True).enabled)
        self.assertTrue(resolve_policy("smoke_atm_roundtrip", {"exits": {"enabled": True}}, activated=True).enabled)

    def test_disabled_policy_never_decides(self):
        policy = resolve_policy("reddit_sentiment_qqq", {"exits": {"enabled": False}}, activated=True)
        self.assertIsNone(evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.10, 0.15)}), policy))

    def test_thresholds_are_configurable(self):
        policy = resolve_policy("reddit_sentiment_qqq", {"exits": {
            "profit_target_pct": 0.25, "stop_loss_pct": -0.15,
        }}, activated=True)
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.84, 0.90)}), policy)
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")
        self.assertEqual(decision.metadata["exit_threshold_pct"], -0.15)

    def test_approved_default_policy_is_bounded_loss_only(self):
        # The default is the policy Owen approved on 2026-11-09: stop -0.50,
        # profit-taking disabled, stop bypasses Phelps, profit respects it,
        # 15-minute cooldown. A change to any of these is a policy change and
        # has to be deliberate, which is what this test makes expensive.
        self.assertIsNone(exits.DEFAULT_PROFIT_TARGET_PCT)
        policy = resolve_policy("reddit_sentiment_qqq", {}, activated=True)
        self.assertTrue(policy.enabled)
        self.assertIsNone(policy.profit_target_pct)
        self.assertEqual(policy.stop_loss_pct, -0.50)
        self.assertTrue(policy.stop_bypasses_phelps)
        self.assertFalse(policy.profit_bypasses_phelps)
        self.assertEqual(policy.reentry_cooldown_minutes, 15.0)

        # Under the default a doubling is not a profit exit, and the stop fires.
        self.assertIsNone(
            evaluate(ctx(book(long_trade()), {SYMBOL: quote(2.40, 2.50)}), policy)
        )
        stopped = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.50, 0.55)}), policy)
        self.assertEqual(stopped.metadata["exit_trigger"], "protective_stop")
        self.assertIsNone(stopped.metadata["exit_policy"]["profit_target_pct"])

    def test_profit_target_can_be_disabled_entirely(self):
        policy = resolve_policy(
            "reddit_sentiment_qqq", {"exits": {"profit_target_pct": None}}, activated=True
        )
        self.assertIsNone(policy.profit_target_pct)
        # A doubling plus is still not a profit exit when the target is null.
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(2.20, 2.30)}), policy)
        self.assertIsNone(decision)
        # The stop is unaffected.
        stopped = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.40, 0.45)}), policy)
        self.assertEqual(stopped.metadata["exit_trigger"], "protective_stop")

    def test_a_hundred_percent_target_is_enabled_not_disabled(self):
        # The distinction the recommendation depends on: +100% is a live
        # target that fires on a doubling; only null disables profit-taking.
        policy = resolve_policy(
            "reddit_sentiment_qqq", {"exits": {"profit_target_pct": 1.0}}, activated=True
        )
        fired = evaluate(ctx(book(long_trade()), {SYMBOL: quote(2.00, 2.10)}), policy)
        self.assertEqual(fired.metadata["exit_trigger"], "profit_target")
        self.assertEqual(fired.metadata["exit_threshold_pct"], 1.0)
        below = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.90, 2.00)}), policy)
        self.assertIsNone(below)

    def test_malformed_config_raises_per_account(self):
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"profit_target_pct": "lots"}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"stop_loss_pct": 0.2}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"profit_target_pct": float("nan")}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"stop_los_pct": -0.5}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": []})

    def test_precedence_switches_require_real_booleans(self):
        # bool("false") is True: a quoted value would silently invert the
        # policy, so strings are rejected rather than coerced.
        for field in ("stop_bypasses_phelps", "profit_bypasses_phelps"):
            for value in ("false", "true", 0, 1, None, "yes"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ExitConfigError):
                        resolve_policy(
                            "reddit_sentiment_qqq",
                            {"exits": {field: value}},
                            activated=True,
                        )
        policy = resolve_policy(
            "reddit_sentiment_qqq",
            {"exits": {"stop_bypasses_phelps": False, "profit_bypasses_phelps": True}},
            activated=True,
        )
        self.assertFalse(policy.stop_bypasses_phelps)
        self.assertTrue(policy.profit_bypasses_phelps)

    def test_audit_metadata_carries_the_whole_decision(self):
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.40, 0.45)}), ExitPolicy())
        metadata = decision.metadata
        for key in ("exit_trigger", "exit_symbol", "exit_held_quantity", "exit_cost_basis",
                    "exit_liquidation_price", "exit_bid", "exit_ask", "exit_quote_age_s",
                    "exit_pnl_pct", "exit_threshold_pct", "exit_policy"):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["exit_symbol"], SYMBOL)
        self.assertEqual(metadata["exit_held_quantity"], 1)
        self.assertEqual(metadata["exit_quote_age_s"], 5.0)


class ReentryTests(unittest.TestCase):
    def policy(self, minutes=15.0):
        return ExitPolicy(reentry_cooldown_minutes=minutes)

    def test_exit_blocks_the_same_contract_until_the_cooldown_ends(self):
        store = ExitStateStore(_tmpdir())
        now = [datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)]
        store._now = lambda: now[0]
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)

        buy = Decision(action="buy", symbol=SYMBOL, quantity=1, reason="signal",
                       strategy_id="reddit_sentiment_qqq", strategy_version="1")
        blocked = reentry_blocked(buy, username="crassus_test", store=store,
                                  policy=self.policy(), book=book())
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.action, "no_trade")
        self.assertEqual(blocked.metadata["blocked_symbol"], SYMBOL)
        self.assertAlmostEqual(blocked.metadata["exit_cooldown_remaining_s"], 900.0, places=0)

        now[0] = now[0] + timedelta(minutes=16)
        self.assertIsNone(reentry_blocked(buy, username="crassus_test", store=store,
                                          policy=self.policy(), book=book()))

    def test_cooldown_survives_a_restart(self):
        directory = _tmpdir()
        store = ExitStateStore(directory)
        store.record("crassus_test", SYMBOL, trigger="profit_target", quantity=1)
        reopened = ExitStateStore(directory)
        self.assertGreater(reopened.cooldown_remaining_s("crassus_test", SYMBOL, 15.0), 0)

    def test_reentry_guard_never_blocks_a_reducing_trade(self):
        store = ExitStateStore(_tmpdir())
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)
        held = book(long_trade())
        sell = Decision(action="sell", symbol=SYMBOL, quantity=1, reason="reduce",
                        strategy_id="reddit_sentiment_qqq", strategy_version="1")
        self.assertIsNone(reentry_blocked(sell, username="crassus_test", store=store,
                                          policy=self.policy(), book=held))

    def test_old_entries_are_pruned(self):
        directory = _tmpdir()
        now = [datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)]
        store = ExitStateStore(directory, now=lambda: now[0])
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)
        now[0] = now[0] + timedelta(hours=30)
        store.record("crassus_test", "OTHER", trigger="profit_target", quantity=1)
        saved = json.loads((directory / "exits.json").read_text())
        self.assertNotIn(SYMBOL, saved["accounts"]["crassus_test"])
        self.assertIn("OTHER", saved["accounts"]["crassus_test"])


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DurableStateTests(unittest.TestCase):
    """The cooldown record must survive a crash, a corrupt file and a full disk."""

    def _policy(self):
        return ExitPolicy(reentry_cooldown_minutes=15.0)

    def _buy(self, symbol=SYMBOL):
        return Decision(action="buy", symbol=symbol, quantity=1, reason="signal",
                        strategy_id="reddit_sentiment_qqq", strategy_version="1")

    def test_a_failed_save_keeps_the_previous_state_and_blocks_new_entries(self):
        directory = _tmpdir()
        store = ExitStateStore(directory)
        store.record("crassus_test", "A", trigger="protective_stop", quantity=1)
        before = (directory / "exits.json").read_text()

        # atomic_json writes a temp file and replaces; a failure before the
        # replace must leave the previous record intact.
        with mock.patch.object(exits, "atomic_json", side_effect=OSError("disk full")):
            store.record("crassus_test", "B", trigger="profit_target", quantity=1)

        self.assertEqual((directory / "exits.json").read_text(), before)
        self.assertFalse(store.trusted)
        self.assertTrue(store.degraded_reason.startswith("state_write_failed"))
        blocked = reentry_blocked(self._buy("NEW"), username="crassus_test", store=store,
                                  policy=self._policy(), book=book())
        self.assertEqual(blocked.action, "no_trade")
        self.assertIn("exit_state_degraded", blocked.metadata)
        # The in-memory record still blocks its own contract.
        self.assertGreater(store.cooldown_remaining_s("crassus_test", "B", 15.0), 0)

        # A later successful save repairs the file and clears the block.
        store.record("crassus_test", "B", trigger="profit_target", quantity=1)
        self.assertTrue(store.trusted)
        self.assertIsNone(reentry_blocked(self._buy("NEW"), username="crassus_test",
                                          store=store, policy=self._policy(), book=book()))

    def test_a_corrupt_state_file_is_rebuilt_from_the_ledger(self):
        directory = _tmpdir()
        ledger_dir = directory / "logs"
        ledger_dir.mkdir()
        state_dir = directory / "state"
        state_dir.mkdir()
        stamp = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
        record = {
            "decision_id": "d-1",
            "decision": {
                "action": "sell",
                "symbol": SYMBOL,
                "quantity": 1,
                "metadata": {
                    "exit_trigger": "protective_stop",
                    "exit_symbol": SYMBOL,
                    "exit_account_username": "crassus_test",
                },
            },
            "timestamp_utc": stamp.isoformat(timespec="seconds"),
        }
        (ledger_dir / "decisions-run1.jsonl").write_text(json.dumps(record) + "\n")
        (state_dir / "exits.json").write_text("{ this is not json")

        store = ExitStateStore(state_dir, ledger_dir=ledger_dir, now=lambda: stamp)
        self.assertTrue(store.trusted, store.degraded_reason)
        self.assertEqual(store.recovered_from, "durable_sources")
        self.assertGreater(store.cooldown_remaining_s("crassus_test", SYMBOL, 15.0), 0)
        self.assertEqual(store.cooldown_remaining_s("crassus_test", "OTHER", 15.0), 0)
        # The rebuilt file is valid again, so the next boot is normal.
        self.assertEqual(json.loads((state_dir / "exits.json").read_text())["schema"],
                         "exit_state.v1")

    def test_a_corrupt_state_file_is_rebuilt_from_an_inflight_intent(self):
        directory = _tmpdir()
        state_dir = directory
        (state_dir / "exits.json").write_text("[]")
        intent = {
            "execution_request_id": "req-9",
            "decision": {
                "action": "sell",
                "symbol": SYMBOL,
                "quantity": 1,
                "metadata": {
                    "exit_trigger": "profit_target",
                    "exit_account_username": "crassus_test",
                },
            },
        }
        (state_dir / "Luigi.inflight.json").write_text(json.dumps(intent))
        store = ExitStateStore(state_dir)
        self.assertTrue(store.trusted, store.degraded_reason)
        self.assertEqual(store.recovered_from, "durable_sources")
        self.assertGreater(store.cooldown_remaining_s("crassus_test", SYMBOL, 15.0), 0)

    def test_unrecoverable_state_blocks_entries_without_blocking_protective_exits(self):
        directory = _tmpdir()
        (directory / "exits.json").write_text("{ broken")
        with mock.patch.object(ExitStateStore, "_recover_from_durable_sources",
                               return_value=({}, False)):
            store = ExitStateStore(directory)
        self.assertFalse(store.trusted)

        # Protective liquidation is unaffected by the guard's distrust.
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.40, 0.45)}),
                            ExitPolicy())
        self.assertEqual(decision.action, "sell")

        blocked = reentry_blocked(self._buy("NEW"), username="crassus_test", store=store,
                                  policy=self._policy(), book=book())
        self.assertEqual(blocked.action, "no_trade")
        self.assertIn("untrusted", blocked.reason)

    def test_reducing_trades_still_pass_while_the_store_is_degraded(self):
        directory = _tmpdir()
        (directory / "exits.json").write_text("{ broken")
        with mock.patch.object(ExitStateStore, "_recover_from_durable_sources",
                               return_value=({}, False)):
            store = ExitStateStore(directory)
        sell = Decision(action="sell", symbol=SYMBOL, quantity=1, reason="reduce",
                        strategy_id="reddit_sentiment_qqq", strategy_version="1")
        self.assertIsNone(reentry_blocked(sell, username="crassus_test", store=store,
                                          policy=self._policy(), book=book(long_trade())))


class RunnerWiringTests(unittest.TestCase):
    """The exit must become a real submitted order, attributed correctly."""

    def setUp(self):
        # Pin the runner's clock mid-session: the mandatory flatten must not be
        # the thing that closes the position in these scenarios.
        import crassus.clock as clock_module

        self._clock = clock_module
        self._original_now_et = clock_module.now_et
        clock_module.now_et = lambda: datetime(
            2024, 1, 2, 12, 0, tzinfo=timezone(timedelta(hours=-5))
        )

    def tearDown(self):
        self._clock.now_et = self._original_now_et

    def _runner(self, directory: Path, *, params=None, trades=None, outcome="filled",
               pending=None):
        from crassus.audit import DecisionLedger, Outcome
        from crassus.runner import Runner

        runner = object.__new__(Runner)
        account = SimpleNamespace(
            alias="Luigi", username="crassus_luigi",
            strategy_id="reddit_sentiment_qqq", params=params or {},
        )
        state = AccountState(username="crassus_luigi", balance_cash=10000.0,
                             trades=list(trades) if trades is not None else [long_trade()])
        runner.accounts = [account]
        runner.dry_run = False
        runner.ledger = DecisionLedger(directory)
        runner.retired = set()
        runner._stop = None
        runner.exit_state = ExitStateStore(directory)
        runner.exits_activated = True
        runner.quotes = SimpleNamespace(
            quotes=lambda symbols: {s: quote(0.40, 0.45) for s in symbols},
            last_retry_note=None,
        )
        runner.sessions = {"Luigi": SimpleNamespace(me=lambda: state,
                                                   sync_metadata=lambda **kw: None)}
        submitted = []
        outcome_class = getattr(Outcome, outcome.upper())

        class _Executor:
            def pending_intent(self):
                return pending

            def submit(self, **kwargs):
                submitted.append(kwargs)
                return SimpleNamespace(outcome_class=outcome_class,
                                       execution_request_id=f"req-{len(submitted)}",
                                       http_status=500 if outcome_class == Outcome.REJECTED else 200,
                                       server_response={}, latency_ms=12,
                                       state_after=state, note=None)

            def finalize(self, request_id):
                return None

        runner.executors = {"Luigi": _Executor()}
        runner._recover_pending = lambda acct: pending is not None
        return runner, account, submitted

    def test_exit_becomes_a_sell_attributed_to_the_exit_policy(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory)
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-01T17:00:00+00:00",
                     "snapshot_time": "2024-01-01T17:00:00+00:00",
                     "expiration": "2024-01-01", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")

        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["side"], "sell")
        self.assertEqual(submitted[0]["symbol"], SYMBOL)
        self.assertEqual(submitted[0]["strategy_id"], "exit_policy")
        self.assertEqual(submitted[0]["decision"]["metadata"]["exit_trigger"], "protective_stop")
        # The cooldown was recorded before submission.
        self.assertGreater(
            runner.exit_state.cooldown_remaining_s("crassus_luigi", SYMBOL, 15.0), 0
        )

    def test_rejected_exit_is_recorded_and_retried_on_the_next_cycle(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory, outcome="rejected")
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(len(submitted), 1)
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        self.assertEqual(json.loads(ledger[-1])["outcome_class"], "rejected")

        # The position is still held and still below its stop: the next cycle
        # must propose the exit again rather than treat the rejection as done.
        runner._run_account(account, snapshot, "open")
        self.assertEqual(len(submitted), 2)
        self.assertNotEqual(submitted[1]["decision_id"], submitted[0]["decision_id"])

    def test_settled_position_produces_no_second_exit(self):
        # Long then sold: the book is flat, so a settled position cannot be
        # exited twice.
        directory = _tmpdir()
        settled = [
            long_trade(),
            {"sym": SYMBOL, "side": "sell", "qty": 1, "price": 0.55,
             "ts": "2024-01-02T11:00:00Z"},
        ]
        runner, account, submitted = self._runner(directory, trades=settled)
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(submitted, [])

    def test_pending_exit_intent_is_reconciled_before_any_new_exit(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(
            directory, pending={"execution_request_id": "req-0", "decision_id": "d-0"}
        )
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        # Recovery handled the in-flight exit; no second exit was proposed and
        # no cooldown entry was written for a trade that has not been re-sent.
        self.assertEqual(submitted, [])
        self.assertEqual(
            runner.exit_state.cooldown_remaining_s("crassus_luigi", SYMBOL, 15.0), 0.0
        )

    def test_reentry_cooldown_blocks_a_real_entry_decision(self):
        # End-to-end through the runner: the account is flat, the strategy
        # proposes a buy, and the cooldown from an earlier exit turns that buy
        # into an audited no_trade rather than an order.
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory, trades=[])
        runner.exit_state.record("crassus_luigi", SYMBOL,
                                 trigger="protective_stop", quantity=1)
        buy = Decision(action="buy", symbol=SYMBOL, quantity=1, reason="signal still long",
                       strategy_id="reddit_sentiment_qqq", strategy_version="1")
        with mock.patch("crassus.runner.get_strategy", return_value=lambda ctx: buy):
            runner._run_account(account, self._snapshot(), "open")

        self.assertEqual(submitted, [])
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        record = json.loads(ledger[-1])
        self.assertEqual(record["outcome_class"], "no_trade")
        self.assertIn("Exit cooldown", record["reason"])
        self.assertEqual(record["decision"]["metadata"]["blocked_symbol"], SYMBOL)

    def _snapshot(self):
        return MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )

    def test_protective_exit_fires_without_a_market_snapshot(self):
        # A scanner outage must not suspend risk control: the exit is decided
        # from the book and a fresh liquidation quote.
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory)
        runner._run_account(account, None, "open")
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["strategy_id"], "exit_policy")

    def test_new_entries_still_wait_for_the_snapshot(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory, trades=[])
        buy = Decision(action="buy", symbol=SYMBOL, quantity=1, reason="signal",
                       strategy_id="reddit_sentiment_qqq", strategy_version="1")
        with mock.patch("crassus.runner.get_strategy", return_value=lambda ctx: buy):
            runner._run_account(account, None, "open")
        self.assertEqual(submitted, [])
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        record = json.loads(ledger[-1])
        self.assertEqual(record["outcome_class"], "runner_error")
        self.assertIn("No market snapshot", record["reason"])

    def test_invalid_exit_config_records_an_error_instead_of_trading(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(
            directory, params={"exits": {"stop_loss_pct": 0.5}}
        )
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-01T17:00:00+00:00",
                     "snapshot_time": "2024-01-01T17:00:00+00:00",
                     "expiration": "2024-01-01", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(submitted, [])
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        record = json.loads(ledger[-1])
        self.assertEqual(record["outcome_class"], "runner_error")
        self.assertIn("Invalid exit configuration", record["reason"])


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (BoundaryTests, PremiumStop35Tests, QuoteTests, PrecedenceTests, PolicyTests,
                 ReentryTests, DurableStateTests, RunnerWiringTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
