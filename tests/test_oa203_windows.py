import sys
import unittest
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zoneinfo import ZoneInfo


import market_data.options_scanner.windows as w  # noqa: E402
from market_data.options_scanner.returns import ContractInfo, Observation  # noqa: E402

ET = ZoneInfo("America/New_York")
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]


def ms(day, hh, mm):
    return int(datetime.combine(datetime.fromisoformat(day).date(), time(hh, mm), ET).timestamp() * 1000)


def sess(day, close=(16, 0)):
    return w.Session(day, ms(day, 9, 30), ms(day, *close), early_close=close != (16, 0))


def ob(day, hh, mm, bid, ask, size=10):
    t = ms(day, hh, mm)
    return Observation(t=t, bid=bid, ask=ask, bid_ms=t, ask_ms=t, bid_size=size, ask_size=size,
                       underlying_price=100.0)


def info(expiration="2026-10-09", symbol="ABC261009C00100000", underlying="ABC", root="ABC"):
    return ContractInfo(symbol=symbol, underlying=underlying, option_type="call", strike=100.0,
                        expiration=expiration, root=root)


SESSIONS = [sess(d) for d in WEEK]


def by_kind(windows, kind):
    return next(x for x in windows if x.kind == kind)


def week_path():
    return [
        ob("2026-10-05", 9, 35, 1.9, 2.0), ob("2026-10-05", 11, 0, 1.4, 1.5), ob("2026-10-05", 15, 55, 1.7, 1.8),
        ob("2026-10-06", 9, 35, 2.4, 2.5), ob("2026-10-07", 12, 0, 3.4, 3.5),
        ob("2026-10-09", 10, 0, 4.0, 4.1), ob("2026-10-09", 15, 55, 2.9, 3.1),
    ]


class Construction(unittest.TestCase):
    def test_reversed_or_overlapping_intervals_rejected(self):
        with self.assertRaises(ValueError):
            w.Session("2026-10-05", ms("2026-10-05", 16, 0), ms("2026-10-05", 9, 30))
        with self.assertRaises(ValueError):
            w.Session("2026-10-05", ms("2026-10-06", 9, 30), ms("2026-10-06", 16, 0))
        with self.assertRaises(ValueError):
            w.Window("overnight", "bad", (SESSIONS[1], SESSIONS[0]), (SESSIONS[1],), SESSIONS[0])

    def test_weekly_windows_normal_week(self):
        windows, skipped = w.weekly_windows(SESSIONS)
        self.assertEqual(skipped, [])
        self.assertEqual([x.kind for x in windows], ["weekly_cheapest", "weekly_fixed", "weekly_cheapest_best_exit"])
        self.assertEqual(windows[0].window_id, "weekly-2026-W41-cheapest")
        self.assertEqual((windows[0].entry_sessions[0].day, windows[0].exit_session.day), ("2026-10-05", "2026-10-09"))

    def test_friday_and_monday_holidays(self):
        no_fri = [s for s in SESSIONS if s.day != "2026-10-09"]
        self.assertEqual(w.weekly_windows(no_fri)[0][0].exit_session.day, "2026-10-08")
        no_mon = [s for s in SESSIONS if s.day != "2026-10-05"]
        self.assertEqual(w.weekly_windows(no_mon)[0][0].entry_sessions[0].day, "2026-10-06")

    def test_short_week_is_skipped_not_shortened(self):
        windows, skipped = w.weekly_windows([SESSIONS[0]])
        self.assertEqual(windows, [])
        self.assertEqual(skipped[0]["reason"], "too_few_sessions")

    def test_overnight_includes_weekend_gap_by_policy(self):
        days = SESSIONS + [sess("2026-10-12")]
        on = w.overnight_windows(days)
        self.assertEqual(len(on), 5)
        self.assertEqual(on[-1].window_id, "overnight-2026-10-09_2026-10-12")
        self.assertEqual(len(w.overnight_windows(days, w.WindowPolicy(overnight_across_gaps=False))), 4)

    def test_rolling_multi_session(self):
        ms_windows = w.multi_session_windows(SESSIONS)
        self.assertEqual(sum(x.kind == "sessions_2" for x in ms_windows), 4)
        self.assertEqual(sum(x.kind == "sessions_3" for x in ms_windows), 3)
        self.assertEqual(ms_windows[0].window_id, "sessions2-2026-10-05_2026-10-06")

    def test_policy_is_versioned_and_provisional(self):
        d = w.WindowPolicy().as_dict()
        self.assertEqual(d["version"], "oa203-windows-v1")
        self.assertIn("D1-D5_D7_D8_approved", d["decisions_status"])
        # D6/D9/opening tolerance/continuity are Chej engineering-reviewed
        # for inert/offline use only, not Owen release approval.
        self.assertIn("chej_engineering_reviewed_offline_only", d["decisions_status"])
        self.assertEqual(d["close_tolerance_s"], 900)
        self.assertEqual(d["qualified_endpoint_policy"]["max_quote_age_s"], 1800)
        with self.assertRaises(ValueError):
            w.WindowPolicy(early_week_sessions=0)
        with self.assertRaises(ValueError):
            w.WindowPolicy(multi_session_mode="anchored")


class Weekly(unittest.TestCase):
    def setUp(self):
        self.windows, _ = w.weekly_windows(SESSIONS)

    def run_kind(self, kind, path=None, **kw):
        return w.evaluate(by_kind(self.windows, kind), kw.pop("info", info()), path or week_path(),
                          kw.pop("sampled", WEEK), kw.pop("policy", w.WindowPolicy()))

    def test_cheapest_entry_to_friday_close(self):
        r = self.run_kind("weekly_cheapest")
        self.assertEqual(r["exec"]["status"], "eligible")
        self.assertEqual((r["exec"]["entry"], r["exec"]["exit"]), (1.5, 2.9))
        self.assertAlmostEqual(r["exec"]["pct"], 2.9 / 1.5 - 1, places=6)
        self.assertAlmostEqual(r["exec"]["abs_change_per_contract"], 140.0)
        self.assertAlmostEqual(r["mid"]["entry"], 1.45)
        self.assertAlmostEqual(r["mid"]["exit"], 3.0)
        self.assertEqual(r["exec"]["exit_lead_before_close_s"], 300)
        self.assertTrue(r["hindsight"])
        self.assertIn("exit_on_expiration_day", r["flags"])

    def test_fixed_and_best_exit_variants_stay_separate(self):
        fixed = self.run_kind("weekly_fixed")
        self.assertEqual(fixed["exec"]["entry"], 2.0)
        self.assertFalse(fixed["hindsight"])
        best = self.run_kind("weekly_cheapest_best_exit")
        self.assertEqual(best["exec"]["exit"], 4.0)
        self.assertGreater(best["exec"]["pct"], self.run_kind("weekly_cheapest")["exec"]["pct"])
        future = ob("2026-10-09", 14, 0, 99.0, 100.0)
        future = replace(future, bid_ms=future.t + 60_000)
        guarded = self.run_kind("weekly_cheapest_best_exit", path=week_path() + [future])
        self.assertEqual(guarded["exec"]["exit"], 4.0)
        self.assertEqual(guarded["mid"]["exit"], 4.05)

    def test_no_look_ahead_on_entry(self):
        base = self.run_kind("weekly_cheapest")
        later = week_path() + [ob("2026-10-07", 10, 0, 0.4, 0.5), ob("2026-10-09", 14, 0, 9.0, 9.1)]
        again = self.run_kind("weekly_cheapest", path=later)
        self.assertEqual(again["exec"]["entry_ms"], base["exec"]["entry_ms"])
        self.assertEqual(again["exec"]["entry"], base["exec"]["entry"])

    def test_qualified_entry_skips_wide_spread(self):
        path = week_path() + [ob("2026-10-05", 12, 0, 0.5, 1.0)]  # cheaper ask, 67% spread
        r = self.run_kind("weekly_cheapest", path=path)
        self.assertEqual(r["exec"]["entry"], 1.0)
        self.assertEqual(r["qualified"]["entry"], 1.5)

    def test_expires_before_exit_and_missing_session(self):
        r = self.run_kind("weekly_cheapest", info=info(expiration="2026-10-07"))
        self.assertEqual(r["exec"]["status"], "excluded")
        self.assertIn("expires_before_exit", r["exec"]["reasons"])
        r = self.run_kind("weekly_cheapest", sampled=[d for d in WEEK if d != "2026-10-07"])
        self.assertIn("missing_session:2026-10-07", r["mid"]["reasons"])
        self.assertIsNone(r["mid"]["pct"])

    def test_expiration_day_rules(self):
        spx = info(symbol="SPX261009C06600000", underlying="SPX", root="SPX")
        self.assertIn("am_settled_on_exit_day", self.run_kind("weekly_cheapest", info=spx)["contract_exclusions"])
        spxw = info(symbol="SPXW261009C06600000", underlying="SPX", root="SPXW")
        self.assertEqual(self.run_kind("weekly_cheapest", info=spxw)["exec"]["status"], "eligible")
        strict = w.WindowPolicy(allow_expiration_day_exit=False)
        self.assertIn("exit_on_expiration_day_disallowed", self.run_kind("weekly_cheapest", policy=strict)["contract_exclusions"])
        later = info(expiration="2026-10-16", symbol="ABC261016C00100000")
        self.assertEqual(self.run_kind("weekly_cheapest", info=later)["flags"], [])

    def test_d8_friday_to_monday_same_contract_continuity(self):
        # D8: one additional expiry for SPY/QQQ only. The Oct 23 contract
        # sampled as the additional chain on Friday Oct 16 is the SAME
        # contract sampled as the nearest chain on Monday Oct 19, so a
        # Friday->Monday overnight window can follow it. Within the
        # approved two-chain limit: nearest + one additional, nothing more.
        fri = sess("2026-10-16")
        mon = sess("2026-10-19")
        win = w.overnight_windows([fri, mon])[0]
        self.assertEqual(win.window_id, "overnight-2026-10-16_2026-10-19")
        contract = info(expiration="2026-10-23", symbol="SPY261023C00100000",
                        underlying="SPY", root="SPY")
        path = [ob("2026-10-16", 15, 55, 2.0, 2.1), ob("2026-10-19", 9, 35, 2.4, 2.5)]
        row = w.evaluate(win, contract, path, ["2026-10-16", "2026-10-19"])
        self.assertEqual(row["exec"]["status"], "eligible")
        self.assertEqual(row["exec"]["entry_session"], "2026-10-16")
        self.assertEqual(row["exec"]["exit_session"], "2026-10-19")
        # If the Oct 23 chain had NOT been sampled on Monday (no second
        # chain retained), the hold cannot be fabricated.
        row = w.evaluate(win, contract, path, ["2026-10-16"])
        self.assertEqual(row["exec"]["status"], "excluded")
        self.assertIn("missing_session:2026-10-19", row["exec"]["reasons"])
        # A contract expiring on the Friday itself cannot cross the weekend.
        expiring = info(expiration="2026-10-16", symbol="SPY261016C00100000",
                        underlying="SPY", root="SPY")
        row = w.evaluate(win, expiring, path, ["2026-10-16", "2026-10-19"])
        self.assertIn("expires_before_exit", row["exec"]["reasons"])

    def test_adjusted_deliverable_fails_closed(self):
        # An adjusted root (corporate action, e.g. XYZ1) means the economic
        # deliverable is unknown from the quote alone. It cannot become an
        # ordinary continuity-qualified result: it is EXCLUDED, not merely
        # flagged. Raw observations are preserved in the row (the contract
        # still appears with its exclusion reason), never deleted.
        adjusted = info(symbol="XYZ1261009C00100000", underlying="XYZ", root="XYZ1")
        row = self.run_kind("weekly_cheapest", info=adjusted)
        self.assertIn("adjusted_deliverable_unknown", row["contract_exclusions"])
        for basis in w.BASES:
            self.assertEqual(row[basis]["status"], "excluded")
            self.assertIn("adjusted_deliverable_unknown", row[basis]["reasons"])
        self.assertIn("adjusted_root_check_deliverable", row["flags"])
        # A standard (unadjusted) root with the same shape is unaffected.
        normal = info(symbol="XYZ261009C00100000", underlying="XYZ", root="XYZ")
        row = self.run_kind("weekly_cheapest", info=normal)
        self.assertNotIn("adjusted_deliverable_unknown", row["contract_exclusions"])
        self.assertEqual(row["exec"]["status"], "eligible")

    def test_close_endpoint_tolerance_and_no_interpolation(self):
        path = [o for o in week_path() if o.t != ms("2026-10-09", 15, 55)]  # last Fri obs is 10:00
        r = self.run_kind("weekly_cheapest", path=path)
        self.assertIn("close_endpoint_missing", r["exec"]["reasons"])
        crossed = week_path()[:-1] + [ob("2026-10-09", 15, 52, 2.8, 3.0), ob("2026-10-09", 15, 57, 3.2, 3.0)]
        r = self.run_kind("weekly_cheapest", path=crossed)
        self.assertEqual(r["exec"]["exit"], 2.8)  # skips the crossed 15:57 quote, never blends

    def test_15_minute_close_latest_qualifying_and_quote_side_age(self):
        path = week_path()[:-1] + [ob("2026-10-09", 15, 46, 2.0, 2.1),
                                  ob("2026-10-09", 15, 48, 2.4, 2.5),
                                  ob("2026-10-09", 15, 57, 3.0, 2.9)]
        row = self.run_kind("weekly_cheapest", path=path)
        self.assertEqual(row["qualified"]["exit"], 2.4)
        self.assertEqual(row["qualified"]["exit_lead_before_close_s"], 720)
        self.assertEqual(row["qualified"]["exit_bid_quote_age_s"], 0)
        self.assertIn("close_endpoint_missing", self.run_kind(
            "weekly_cheapest", path=path, policy=w.WindowPolicy(close_tolerance_s=600))["qualified"]["reasons"])
        fresh = ob("2026-10-09", 15, 48, 2.4, 2.5)
        stale = replace(fresh, bid_ms=fresh.t - 1801_000)
        row = self.run_kind("weekly_cheapest", path=week_path()[:-1] + [stale])
        self.assertIn("close_endpoint_missing", row["qualified"]["reasons"])

    def test_future_or_missing_quote_times_never_enter_any_basis(self):
        clean_entry = ob("2026-10-05", 9, 35, 1.9, 2.0)
        clean_exit = ob("2026-10-09", 15, 55, 2.9, 3.0)
        variants = (replace(clean_entry, bid_ms=clean_entry.t + 60_000),
                    replace(clean_entry, ask_ms=clean_entry.t + 60_000),
                    replace(clean_entry, bid_ms=None), replace(clean_entry, ask_ms=None))
        for bad in variants:
            with self.subTest(bid_ms=bad.bid_ms, ask_ms=bad.ask_ms):
                r = self.run_kind("weekly_cheapest", path=[bad, clean_exit])
                for basis in w.BASES:
                    self.assertEqual(r[basis]["status"], "excluded")
                    self.assertTrue(any("future" in why or "missing" in why for why in r[basis]["reasons"]))
        for bad in (replace(clean_exit, bid_ms=clean_exit.t + 60_000),
                    replace(clean_exit, ask_ms=clean_exit.t + 60_000),
                    replace(clean_exit, bid_ms=None), replace(clean_exit, ask_ms=None)):
            r = self.run_kind("weekly_fixed", path=[clean_entry, bad])
            for basis in w.BASES:
                self.assertEqual(r[basis]["status"], "excluded")

    def test_stale_is_distinct_from_impossible_future_evidence(self):
        fresh = ob("2026-10-09", 15, 55, 2.9, 3.0)
        stale = replace(fresh, bid_ms=fresh.t - 1801_000)
        r = self.run_kind("weekly_fixed", path=[ob("2026-10-05", 9, 35, 1.9, 2.0), stale])
        self.assertEqual(r["mid"]["status"], "eligible")
        self.assertEqual(r["exec"]["status"], "eligible")
        self.assertEqual(r["qualified"]["status"], "excluded")
        self.assertIn("exit_bid_timestamp_stale", r["qualified"]["reasons"])

    def test_missing_observation_time_cannot_form_an_endpoint(self):
        missing = replace(ob("2026-10-05", 9, 35, 1.9, 2.0), t=None)
        row = self.run_kind("weekly_fixed", path=[missing, ob("2026-10-09", 15, 55, 2.9, 3.0)])
        for basis in w.BASES:
            self.assertIn("observation_time_invalid", row[basis]["reasons"])

    def test_delayed_first_quote_makes_fixed_entry_missing(self):
        path = [o for o in week_path() if o.t != ms("2026-10-05", 9, 35)]
        r = self.run_kind("weekly_fixed", path=path)
        self.assertIn("open_endpoint_missing", r["exec"]["reasons"])


class OvernightAndSessions(unittest.TestCase):
    def test_overnight_rejects_future_entry_and_exit_sides(self):
        win = w.overnight_windows(SESSIONS)[0]
        entry = ob("2026-10-05", 15, 55, 1.0, 1.1)
        exit_ = ob("2026-10-06", 9, 35, 1.3, 1.4)
        for path in ([replace(entry, ask_ms=entry.t + 60_000), exit_],
                     [entry, replace(exit_, bid_ms=exit_.t + 60_000)]):
            row = w.evaluate(win, info(), path, WEEK)
            for basis in w.BASES:
                self.assertEqual(row[basis]["status"], "excluded")

    def test_overnight_close_to_open(self):
        win = w.overnight_windows(SESSIONS)[0]
        r = w.evaluate(win, info(), week_path(), WEEK)
        self.assertEqual((r["exec"]["entry"], r["exec"]["exit"]), (1.8, 2.4))
        self.assertEqual(r["exec"]["entry_session"], "2026-10-05")
        self.assertEqual(r["exec"]["entry_delay_from_boundary_s"], (ms("2026-10-05", 15, 55) - ms("2026-10-05", 9, 30)) / 1000)
        late = [o for o in week_path() if o.t != ms("2026-10-06", 9, 35)] + [ob("2026-10-06", 9, 50, 2.4, 2.5)]
        self.assertIn("open_endpoint_missing", w.evaluate(win, info(), late, WEEK)["exec"]["reasons"])

    def test_early_close_endpoint(self):
        days = [sess("2026-11-25"), sess("2026-11-27", close=(13, 0))]
        win = w.overnight_windows(days)[0]
        path = [ob("2026-11-25", 15, 58, 1.0, 1.1), ob("2026-11-27", 9, 40, 1.3, 1.4)]
        self.assertEqual(w.evaluate(win, info(expiration="2026-11-27"), path, ["2026-11-25", "2026-11-27"])["exec"]["status"], "eligible")
        win2 = w.multi_session_windows(days)[0]
        path2 = [ob("2026-11-25", 9, 35, 1.0, 1.1), ob("2026-11-27", 12, 55, 1.5, 1.6)]
        r = w.evaluate(win2, info(expiration="2026-11-27"), path2, ["2026-11-25", "2026-11-27"])
        self.assertEqual(r["exec"]["exit_lead_before_close_s"], 300)
        self.assertEqual(r["early_closes"], ["2026-11-27"])

    def test_dst_boundary_uses_each_sessions_own_open(self):
        days = [sess("2026-10-30"), sess("2026-11-02")]
        self.assertEqual(days[1].open_ms - days[0].open_ms, (3 * 24 + 1) * 3600 * 1000)
        win = w.overnight_windows(days)[0]
        path = [ob("2026-10-30", 15, 56, 1.0, 1.1), ob("2026-11-02", 9, 31, 1.2, 1.3)]
        r = w.evaluate(win, info(expiration="2026-11-06"), path, ["2026-10-30", "2026-11-02"])
        self.assertEqual(r["exec"]["status"], "eligible")

    def test_three_session_open_to_close(self):
        win = next(x for x in w.multi_session_windows(SESSIONS) if x.window_id == "sessions3-2026-10-05_2026-10-07")
        path = week_path() + [ob("2026-10-07", 15, 58, 3.3, 3.4)]
        r = w.evaluate(win, info(), path, WEEK)
        self.assertEqual((r["exec"]["entry"], r["exec"]["exit"]), (2.0, 3.3))


class Ranking(unittest.TestCase):
    def test_full_universe_keeps_negative_and_excluded(self):
        win = w.weekly_windows(SESSIONS)[0][0]
        up = w.evaluate(win, info(symbol="AAA261009C00100000"), week_path(), WEEK)
        down_path = week_path()[:-1] + [ob("2026-10-09", 15, 55, 0.9, 1.0)]
        down = w.evaluate(win, info(symbol="BBB261009C00100000"), down_path, WEEK)
        gone = w.evaluate(win, info(symbol="CCC261009C00100000", expiration="2026-10-06"), week_path(), WEEK)
        ranked = w.rank_window([gone, down, up], "exec")
        self.assertEqual([r["symbol"] for r in ranked], ["AAA261009C00100000", "BBB261009C00100000", "CCC261009C00100000"])
        self.assertEqual([r["exec_rank"] for r in ranked], [1, 2, None])
        self.assertLess(ranked[1]["exec"]["pct"], 0)
        self.assertIn("expires_before_exit", ranked[2]["exec"]["reasons"])


if __name__ == "__main__":
    unittest.main()
