import gzip
import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import oa203_return_scanner as scanner  # noqa: E402
from oa203_returns import (  # noqa: E402
    ContractInfo,
    Observation,
    ReturnPolicy,
    contract_metrics,
    first_to_max,
    open_to_close,
    rank,
    trade_bar_metrics,
    trough_to_peak,
)

T0 = 1_790_343_000_000  # an arbitrary session-time epoch ms
MIN = 60_000


def obs(i, bid, ask, age_s=0, spot=100.0):
    t = T0 + i * 5 * MIN
    return Observation(t=t, bid=bid, ask=ask, bid_ms=t - age_s * 1000, ask_ms=t - age_s * 1000,
                       underlying_price=spot)


def info(symbol="X260928C00100000", option_type="call", strike=100.0, ok_sweeps=None):
    return ContractInfo(symbol=symbol, underlying="X", option_type=option_type, strike=strike,
                        expiration="2026-09-28", chain_ok_sweeps=ok_sweeps or 0)


class ChronologicalReturns(unittest.TestCase):
    def test_first_to_max_ignores_peaks_before_entry(self):
        leg = first_to_max([None, 2.0, 1.0, 3.0], [9.0, 2.0, 1.0, 3.0])
        self.assertEqual((leg.entry_index, leg.exit_index), (1, 3))
        self.assertAlmostEqual(leg.pct, 0.5)

    def test_high_before_low_is_a_loss_not_a_gain(self):
        # An unordered daily high/low ratio would call this +900%.
        leg = first_to_max([10.0, 1.0], [10.0, 1.0])
        self.assertAlmostEqual(leg.pct, -0.9)
        self.assertLess(trough_to_peak([10.0, 1.0], [10.0, 1.0]).pct, 0)

    def test_trough_to_peak_pairs_only_later_exits(self):
        leg = trough_to_peak([5, 1, 4, 0.5, 1.5], [5, 1, 4, 0.5, 1.5])
        self.assertEqual((leg.entry_index, leg.exit_index), (1, 2))
        self.assertAlmostEqual(leg.pct, 3.0)

    def test_single_observation_has_no_return(self):
        self.assertIsNone(first_to_max([1.0], [1.0]))
        self.assertIsNone(open_to_close([1.0], [1.0]))
        self.assertIsNone(trough_to_peak([1.0], [1.0]))

    def test_open_to_close_uses_last_valid(self):
        leg = open_to_close([None, 1.0, 2.0, None], [None, 1.0, 2.0, None])
        self.assertEqual((leg.entry_index, leg.exit_index), (1, 2))


class QuoteBases(unittest.TestCase):
    def test_crossed_and_one_sided_quotes_have_no_mid(self):
        self.assertIsNone(Observation(0, 1.2, 1.0).mid)
        self.assertIsNone(Observation(0, 0.0, 0.05).mid)
        self.assertEqual(Observation(0, 1.0, 1.2).mid, 1.1)

    def test_exec_basis_buys_ask_sells_bid(self):
        path = [obs(0, 1.0, 1.2), obs(1, 2.0, 2.2), obs(2, 1.5, 1.7)]
        row = contract_metrics(info(ok_sweeps=3), path)
        self.assertAlmostEqual(row["exec_first_to_max_entry"], 1.2)
        self.assertAlmostEqual(row["exec_first_to_max_exit"], 2.0)
        self.assertAlmostEqual(row["mid_first_to_max_pct"], 2.1 / 1.1 - 1, places=6)
        self.assertTrue(row["clean"])
        self.assertAlmostEqual(row["mid_first_to_max_abs_change_per_contract"], 100.0)

    def test_flags_are_reported_not_dropped(self):
        path = [obs(0, 0.01, 0.03), obs(1, 0.02, 0.04), obs(2, 0.9, 1.1), obs(3, 0.03, 0.05)]
        row = contract_metrics(info(ok_sweeps=4), path)
        self.assertIn("tiny_entry", row["mid_first_to_max_flags"])
        self.assertIn("exit_isolated_spike", row["mid_first_to_max_flags"])
        self.assertFalse(row["clean"])
        self.assertIsNotNone(row["mid_first_to_max_pct"])

    def test_stale_and_low_coverage(self):
        path = [obs(0, 1.0, 1.2, age_s=4000), obs(1, 1.5, 1.7)]
        row = contract_metrics(info(ok_sweeps=10), path, ReturnPolicy(stale_quote_s=1800))
        self.assertIn("entry_stale", row["mid_first_to_max_flags"])
        self.assertIn("low_coverage", row["contract_flags"])

    def test_strike_vs_spot_at_entry(self):
        row = contract_metrics(info(strike=105.0, ok_sweeps=2), [obs(0, 1, 1.2), obs(1, 2, 2.2)])
        self.assertAlmostEqual(row["strike_vs_spot_pct"], 0.05)


class TradeBars(unittest.TestCase):
    def test_same_bar_low_and_high_are_not_paired(self):
        bars = [
            {"timestamp": 1, "open": 1.0, "high": 1.1, "low": 0.9, "volume": 5},
            {"timestamp": 2, "open": 1.0, "high": 5.0, "low": 0.1, "volume": 5},
            {"timestamp": 3, "open": 0.12, "high": 0.15, "low": 0.11, "volume": 5},
        ]
        out = trade_bar_metrics(bars)
        # The 0.1 low and 5.0 high share a bar: the trough-to-peak entry must be earlier.
        self.assertAlmostEqual(out["trade_1min_trough_to_peak_entry"], 0.9)
        self.assertAlmostEqual(out["trade_1min_trough_to_peak_exit"], 5.0)
        self.assertAlmostEqual(out["trade_1min_first_to_max_pct"], 4.0)

    def test_no_bars(self):
        self.assertIsNone(trade_bar_metrics([])["trade_1min_first_to_max_pct"])


class Ranking(unittest.TestCase):
    def test_ranks_all_and_clean_within_type_and_keeps_undefined(self):
        rows = [
            {"symbol": "A", "option_type": "call", "mid_first_to_max_pct": 3.0, "clean": False},
            {"symbol": "B", "option_type": "put", "mid_first_to_max_pct": 2.0, "clean": True},
            {"symbol": "C", "option_type": "call", "mid_first_to_max_pct": 1.0, "clean": True},
            {"symbol": "D", "option_type": "call", "mid_first_to_max_pct": None, "clean": False},
        ]
        by = {r["symbol"]: r for r in rank(rows)}
        self.assertEqual([by[s]["rank"] for s in "ABCD"], [1, 2, 3, None])
        self.assertEqual(by["C"]["rank_in_type"], 2)
        self.assertEqual(by["B"]["clean_rank"], 1)
        self.assertEqual(by["C"]["clean_rank_in_type"], 1)
        self.assertIsNone(by["A"]["clean_rank"])
        self.assertEqual(len(by), 4)


OCC = (
    "quantity,underlying,symbol,actype,porc,exchange,actdate\n"
    "100,SPY,1SPY,C,C,CBOE,09/24/2026\n"
    "100,SPY,1SPY,M,C,CBOE,09/24/2026\n"
    "40,SPY,1SPY,C,P,ISE,09/24/2026\n"
    "40,SPY,1SPY,M,P,ISE,09/24/2026\n"
    "30,ABC,1ABC,F,C,CBOE,09/24/2026\n"
    "30,ABC,1ABC,M,C,CBOE,09/24/2026\n"
    "999,OLD,1OLD,C,C,CBOE,09/23/2026\n"
)


class OccVolume(unittest.TestCase):
    def test_halves_two_sided_counts_and_filters_date(self):
        vol = scanner.parse_occ_volume(OCC.encode(), date(2026, 9, 24))
        self.assertEqual(vol["SPY"], {"contracts": 140, "calls": 100, "puts": 40})
        self.assertNotIn("OLD", vol)
        self.assertEqual([u for u, _ in scanner.rank_underlyings(vol)], ["SPY", "ABC"])

    def test_refuses_missing_date_or_error_page(self):
        with self.assertRaises(RuntimeError):
            scanner.parse_occ_volume(OCC.encode(), date(2026, 9, 25))
        with self.assertRaises(RuntimeError):
            scanner.parse_occ_volume(b"Symbol is required.\n", date(2026, 9, 24))


class FakeClient:
    def __init__(self, expirations=None, chains=None, fail=()):
        self.expirations_map = expirations or {}
        self.chains_map = chains or {}
        self.fail = set(fail)
        self.stats = Counter()
        self.sweep = 0

    def expirations(self, symbol):
        if symbol in self.fail:
            raise RuntimeError("boom")
        return self.expirations_map.get(symbol, [])

    def quotes(self, symbols):
        return {s: {"symbol": s, "bid": 99.9, "ask": 100.1, "bid_date": T0, "ask_date": T0}
                for s in symbols}

    def chain(self, symbol, expiration):
        if symbol in self.fail:
            raise RuntimeError("chain down")
        return self.chains_map[symbol](self.sweep)

    def timesales(self, symbol, day):
        return [{"timestamp": 1, "open": 1.0, "high": 1.5, "low": 0.9, "volume": 3},
                {"timestamp": 2, "open": 1.4, "high": 3.0, "low": 1.3, "volume": 3}]


class UniverseSelection(unittest.TestCase):
    def test_fills_from_later_ranks_and_records_skips(self):
        ranked = [(s, {"contracts": 10 - i, "calls": 0, "puts": 0}) for i, s in enumerate("ABCDE")]
        client = FakeClient(expirations={"A": ["2026-09-25", "2026-09-28"], "C": ["2026-09-24"],
                                         "D": ["2026-10-02"], "E": ["2026-09-26"]}, fail={"B"})
        u = scanner.select_universe(client, ranked, date(2026, 9, 25), target=3)
        self.assertEqual([r["underlying"] for r in u["selected"]], ["A", "D", "E"])
        self.assertEqual(u["selected"][0]["expiration"], "2026-09-25")  # same-day counts
        self.assertEqual(u["skip_counts"], {"provider_error": 1, "no_unexpired_expiration": 1})
        self.assertEqual(u["shortfall"], 0)

    def test_shortfall_when_candidates_run_out(self):
        client = FakeClient(expirations={"A": ["2026-09-28"]})
        u = scanner.select_universe(client, [("A", {}), ("B", {})], date(2026, 9, 25), target=5)
        self.assertEqual(u["shortfall"], 4)
        self.assertEqual(u["skip_counts"], {"no_listed_expirations": 1})


class FakeResponse:
    def __init__(self, status, available=100, expiry=0.0):
        self.status_code = status
        self.headers = {"X-Ratelimit-Available": str(available),
                        "X-Ratelimit-Expiry": str(int(expiry * 1000))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return {"expirations": {"date": "2026-09-28"}}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def request(self, *args, **kwargs):
        return self.responses.pop(0)


class RateLimiter(unittest.TestCase):
    def make(self, responses, max_rpm=100):
        clock = {"t": 1000.0}
        slept = []

        def sleep(s):
            slept.append(s)
            clock["t"] += s
        client = scanner.Tradier("tok", max_rpm=max_rpm, reserve=10, session=FakeSession(responses),
                                 clock=lambda: clock["t"], sleep=sleep)
        return client, clock, slept

    def test_pauses_when_token_budget_is_low(self):
        client, clock, slept = self.make([FakeResponse(200, available=5, expiry=1030.0),
                                          FakeResponse(200)])
        self.assertEqual(client.expirations("SPY"), ["2026-09-28"])
        client.expirations("SPY")
        self.assertGreaterEqual(clock["t"], 1030.0)

    def test_retries_429_after_waiting(self):
        client, clock, slept = self.make([FakeResponse(429), FakeResponse(200)])
        self.assertEqual(client.expirations("SPY"), ["2026-09-28"])
        self.assertEqual(client.stats["rate_limited"], 1)
        self.assertGreaterEqual(clock["t"], 1060.0)

    def test_local_cap_spreads_requests(self):
        client, clock, slept = self.make([FakeResponse(200)] * 3, max_rpm=2)
        for _ in range(3):
            client.expirations("SPY")
        self.assertGreaterEqual(clock["t"], 1060.0)


def chain_for(symbol, mids):
    def make(sweep):
        out = []
        for kind, mid in (("call", mids[sweep - 1]), ("put", 1.0)):
            out.append({"symbol": f"{symbol}260928{kind[0].upper()}00100000", "root_symbol": symbol,
                        "option_type": kind, "strike": 100.0, "expiration_date": "2026-09-28",
                        "bid": mid - 0.05, "ask": mid + 0.05, "bid_date": T0, "ask_date": T0,
                        "volume": 10, "open_interest": 50, "contract_size": 100})
        return out
    return make


class EndToEnd(unittest.TestCase):
    def test_sample_archive_build_backfill_and_assess(self):
        universe = {"shortfall": 0, "selected": [
            {"underlying": "AAA", "expiration": "2026-09-28"},
            {"underlying": "BBB", "expiration": "2026-09-28"},
            {"underlying": "CCC", "expiration": "2026-09-28"}]}
        client = FakeClient(chains={"AAA": chain_for("AAA", [1.0, 3.0, 2.0]),
                                    "BBB": chain_for("BBB", [1.0, 1.2, 1.1])}, fail={"CCC"})
        with tempfile.TemporaryDirectory() as tmp:
            archive = scanner.DayArchive(Path(tmp), date(2026, 9, 25), "oa203/scanner", upload=False)
            for sweep in (1, 2, 3):
                client.sweep = sweep
                name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
                entry = scanner.run_sweep(client, universe["selected"], sweep, archive.path(name),
                                          block_size=2, workers=2)
                archive.append_manifest(entry)
            manifest = archive.manifest()
            self.assertEqual(manifest[0]["chains"]["CCC"]["status"], "error")
            self.assertEqual(manifest[0]["status_counts"], {"ok": 2, "error": 1})

            cfg = scanner.Config(upload=False, backfill_top=1, workers=1)
            summary = scanner.finalize(archive, client, cfg, universe, None, None)
            rows = scanner.build_contract_rows(archive.dir, cfg.policy)
            top = next(r for r in rows if r["rank"] == 1)
            self.assertEqual(top["symbol"], "AAA260928C00100000")
            self.assertAlmostEqual(top["mid_first_to_max_pct"], 2.0)
            self.assertAlmostEqual(top["trade_1min_first_to_max_pct"], 2.0)
            self.assertEqual(top["backfill_status"], "ok")
            self.assertEqual(len(rows), 4)  # non-winners are kept
            self.assertEqual(summary["assessment"]["status"], "partial")
            self.assertTrue(any(r.startswith("chain_success_rate") for r in summary["assessment"]["reasons"]))
            with gzip.open(archive.path("contracts.csv.gz"), "rt") as src:
                self.assertEqual(len(src.read().strip().splitlines()), 5)
            detail = scanner.inspect_contract(archive.dir, "AAA260928C00100000", cfg.policy)
            self.assertEqual([p["mid"] for p in detail["path"]], [1.0, 3.0, 2.0])
            text = scanner.readout([archive.dir], top=10, clean_only=False)
            self.assertIn("| AAA |", text)


class Assessment(unittest.TestCase):
    def entry(self, start_min, end_min, ok=500, total=500):
        chains = {f"S{i}": {"status": "ok" if i < ok else "error"} for i in range(total)}
        return {"started_ms": T0 + start_min * MIN, "ended_ms": T0 + end_min * MIN,
                "truncated": False, "status_counts": {"ok": ok, "error": total - ok}, "chains": chains}

    def test_complete_session(self):
        manifest = [self.entry(i * 5, i * 5 + 5) for i in range(78)]
        out = scanner.assess_session({"shortfall": 0}, manifest, T0, T0 + 390 * MIN, [],
                                     {"errors": 0}, {})
        self.assertEqual(out["status"], "complete", out["reasons"])

    def test_gap_and_late_stop_are_partial(self):
        manifest = [self.entry(0, 5), self.entry(5, 10), self.entry(60, 65)]
        out = scanner.assess_session({"shortfall": 3}, manifest, T0, T0 + 390 * MIN, [], None, {})
        self.assertEqual(out["status"], "partial")
        joined = " ".join(out["reasons"])
        for reason in ("universe_shortfall:3", "sweep_gaps", "stopped_before_close", "backfill_not_run"):
            self.assertIn(reason, joined)


if __name__ == "__main__":
    unittest.main()
