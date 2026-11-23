"""Offline tests for windows_combine: full-universe combine + emit.

Everything here is local and synthetic, except the gated real-artifact
test (skipped unless REPLAY_130_CHECKPOINT points at the preserved
#130 checkpoint): the combine rule is verified against the real
14-batch replay, not just fixtures.
"""

import copy
import csv
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows as w  # noqa: E402
import market_data.options_scanner.windows_combine as combine  # noqa: E402
import market_data.options_scanner.windows_outputs as outputs  # noqa: E402
from market_data.options_scanner.returns import ContractInfo  # noqa: E402
from test_oa203_windows import SESSIONS, ob  # noqa: E402

WIN = None  # overnight 2026-10-05 -> 2026-10-06, set in setUpModule
DAYS = ["2026-10-05", "2026-10-06"]


def setUpModule():
    global WIN
    WIN = next(x for x in w.overnight_windows(SESSIONS) if x.window_id == "overnight-2026-10-05_2026-10-06")


def _path(entry_mid, exit_mid):
    """Two observations: close endpoint day 1, open endpoint day 2."""
    return [
        ob("2026-10-05", 15, 55, round(entry_mid - 0.01, 4), round(entry_mid + 0.01, 4)),
        ob("2026-10-06", 9, 35, round(exit_mid - 0.01, 4), round(exit_mid + 0.01, 4)),
    ]


def _rows(specs):
    """Evaluate contracts over WIN. specs: (symbol, underlying, entry_mid, exit_mid)."""
    rows = []
    for symbol, underlying, entry_mid, exit_mid in specs:
        # OCC symbol: root(3) + yymmdd(6) + C/P + strike(8, thousandths).
        ci = ContractInfo(symbol=symbol, underlying=underlying,
                          option_type="put" if symbol[9] == "P" else "call",
                          strike=int(symbol[10:]) / 1000, expiration="2026-10-09",
                          root=symbol[:3])
        rows.append(w.evaluate(WIN, ci, _path(entry_mid, exit_mid), DAYS))
    return rows


def _status():
    return {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in DAYS}


def _sources():
    return [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "a" * 64, "bytes": 123}
            for d in DAYS]


def _coverage(underlyings, batches, scope="full_universe"):
    locks = {(s["key"], s["sha256"]) for s in _sources()}
    return combine.CombineCoverage(frozenset(underlyings), tuple(batches),
                                   frozenset(locks), scope)


def _write_emit(root, specs, *, state_status=None):
    return outputs.write_window(root, WIN, _rows(specs), w.WindowPolicy(),
                                state_status or _status(), _sources(),
                                "2026-10-06T20:00:00Z", {"mode": "test"})


class CombineRowsTests(unittest.TestCase):
    def test_single_attribution_keeps_all(self):
        rows = _rows([("AAA261009C00100000", "AAA", 0.15, 0.48),
                      ("BBB261009P00050000", "BBB", 1.00, 0.90)])
        kept, conflicts, stats = combine.combine_rows(WIN.window_id, [("batch-01", rows)])
        self.assertEqual(len(kept), 2)
        self.assertEqual(conflicts, [])
        self.assertEqual(stats, {"window_id": WIN.window_id, "raw_rows": 2, "distinct_symbols": 2,
                                 "kept": 2, "conflicting_symbols": 0,
                                 "pure_duplicate_groups": 0, "pure_duplicate_rows_removed": 0})

    def test_conflicting_duplicate_excluded_fail_closed(self):
        # Same OCC symbol under two underlyings with DIFFERING evidence.
        a = _rows([("SHR261009C00100000", "AAA", 0.15, 0.48)])
        b = _rows([("SHR261009C00100000", "BBB", 0.90, 0.20)])
        clean = _rows([("CCC261009C00100000", "CCC", 0.50, 0.60)])
        kept, conflicts, stats = combine.combine_rows(
            WIN.window_id, [("batch-01", a + clean), ("batch-02", b)])
        self.assertEqual([r["symbol"] for r in kept], ["CCC261009C00100000"])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["symbol"], "SHR261009C00100000")
        self.assertEqual({x["batch"] for x in conflicts[0]["attributions"]}, {"batch-01", "batch-02"})
        self.assertEqual(stats["conflicting_symbols"], 1)
        self.assertEqual(stats["raw_rows"], 3)
        self.assertEqual(stats["kept"], 1)

    def test_pure_duplicate_deduped(self):
        a = _rows([("SHR261009C00100000", "AAA", 0.15, 0.48)])
        b = _rows([("SHR261009C00100000", "BBB", 0.15, 0.48)])
        kept, conflicts, stats = combine.combine_rows(
            WIN.window_id, [("batch-01", a), ("batch-02", b)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(conflicts, [])
        self.assertEqual(stats["pure_duplicate_groups"], 1)
        self.assertEqual(stats["pure_duplicate_rows_removed"], 1)

    def test_full_universe_rank_cut_after_combining(self):
        # Subset A alone ranks X first; the combined universe ranks Y first.
        a = _rows([("AAA261009C00100000", "AAA", 1.00, 1.50)])
        b = _rows([("BBB261009C00100000", "BBB", 0.15, 0.48)])
        kept, _, _ = combine.combine_rows(WIN.window_id, [("batch-01", a), ("batch-02", b)])
        ranked = w.rank_window(kept, "mid")
        self.assertEqual(ranked[0]["symbol"], "BBB261009C00100000")
        self.assertEqual(ranked[0]["mid_rank"], 1)
        self.assertEqual(ranked[1]["symbol"], "AAA261009C00100000")
        self.assertEqual(ranked[1]["mid_rank"], 2)
        # And the subset ranking alone would have crowned the wrong top.
        self.assertEqual(w.rank_window(a, "mid")[0]["symbol"], "AAA261009C00100000")


class QuoteEvidenceTests(unittest.TestCase):
    def test_quote_stamp_only_difference_is_conflict(self):
        # Chej repro: two rows differing ONLY in qualified.entry_ask_ms.
        a = _rows([("AAA261009C00100000", "AAA", 1, 1.5)])[0]
        b = copy.deepcopy(a)
        b["qualified"]["entry_ask_ms"] = (b["qualified"].get("entry_ask_ms") or 0) + 1000
        kept, conflicts, stats = combine.combine_rows(
            WIN.window_id, [("a", [a]), ("b", [b])])
        self.assertEqual(kept, [])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(stats["conflicting_symbols"], 1)
        by_batch = {x["batch"]: x for x in conflicts[0]["attributions"]}
        self.assertNotEqual(by_batch["a"]["qualified"]["entry_ask_ms"],
                            by_batch["b"]["qualified"]["entry_ask_ms"])
        self.assertEqual(by_batch["a"]["qualified"]["pct"],
                         by_batch["b"]["qualified"]["pct"])

    def test_each_basis_quote_field_difference_is_conflict(self):
        for basis in w.BASES:
            for field in ("entry_ask_ms", "exit_ask_ms", "entry_bid_ms",
                          "exit_bid_ms", "entry_relative_spread", "exit_relative_spread",
                          "entry_ask_size", "entry_session", "abs_change_per_contract"):
                a = _rows([("AAA261009C00100000", "AAA", 1, 1.5)])[0]
                b = copy.deepcopy(a)
                current = b[basis].get(field)
                if isinstance(current, str):
                    b[basis][field] = "2026-10-06"  # the window's other session day
                else:
                    b[basis][field] = (current or 0) + 1
                kept, conflicts, _ = combine.combine_rows(
                    WIN.window_id, [("a", [a]), ("b", [b])])
                self.assertEqual((kept, len(conflicts)), ([], 1), (basis, field))

    def test_flags_only_difference_is_conflict(self):
        # Metadata/flags-only difference is a conflict (EVIDENCE_FIELDS precedent).
        a = _rows([("AAA261009C00100000", "AAA", 1, 1.5)])[0]
        b = copy.deepcopy(a)
        b["flags"] = [*a["flags"], "wide_spread"]
        kept, conflicts, _ = combine.combine_rows(
            WIN.window_id, [("a", [a]), ("b", [b])])
        self.assertEqual(kept, [])
        self.assertEqual(len(conflicts), 1)


class CombineEmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_ranks_globally_and_reports_conflict(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50),
                             ("SHR261009C00100000", "AAA", 0.15, 0.48)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48),
                             ("SHR261009C00100000", "BBB", 0.90, 0.20)])
        report = combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                      coverage=_coverage({"AAA", "BBB"},
                                                         ["batch-01", "batch-02"]))
        self.assertEqual(report["coverage"]["scope"], "full_universe")
        win_report = report["windows"]["overnight/overnight-2026-10-05_2026-10-06"]
        self.assertEqual(win_report["kept"], 2)
        self.assertEqual(win_report["conflicting_symbols"], 1)
        self.assertEqual(win_report["raw_rows"], 4)
        self.assertEqual(report["conflicts"]["symbols"], 1)
        self.assertEqual(report["conflicts"]["rows"], 2)
        out = self.root / "combined" / "oa203" / "windows" / "oa203-windows-v1" / "overnight" / "overnight-2026-10-05_2026-10-06"
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "final")
        self.assertEqual(summary["scope"]["universe_scope"], "full_universe")
        self.assertEqual(summary["contracts_evaluated"], 2)
        with open(out / "rankings.csv", encoding="utf-8", newline="") as f:
            ranked = list(csv.DictReader(f))
        mids = [r for r in ranked if r["basis"] == "mid"]
        self.assertEqual(mids[0]["symbol"], "BBB261009C00100000")
        self.assertEqual(mids[0]["rank"], "1")
        self.assertNotIn("SHR261009C00100000", [r["symbol"] for r in ranked])
        with open(self.root / "combined" / "conflicts.csv", encoding="utf-8", newline="") as f:
            conflicts = list(csv.DictReader(f))
        self.assertEqual(len(conflicts), 2)
        self.assertEqual({c["symbol"] for c in conflicts}, {"SHR261009C00100000"})
        # Paths evidence survives the round trip for kept rows.
        with gzip.open(out / "paths.csv.gz", "rt", encoding="utf-8", newline="") as f:
            paths = {r["symbol"]: r for r in csv.DictReader(f)}
        self.assertEqual(set(paths), {"AAA261009C00100000", "BBB261009C00100000"})
        self.assertEqual(paths["BBB261009C00100000"]["mid_pct"], "2.2")

    def test_paths_round_trip_preserves_all_bases(self):
        emit = self.root / "batch-01"
        _write_emit(emit, [("AAA261009C00100000", "AAA", 0.15, 0.48)])
        win_dir = (emit / "oa203" / "windows" / "oa203-windows-v1" / "overnight"
                   / "overnight-2026-10-05_2026-10-06")
        original = _rows([("AAA261009C00100000", "AAA", 0.15, 0.48)])[0]
        parsed = combine.rows_from_paths((win_dir / "paths.csv.gz").read_bytes(), WIN)
        self.assertEqual(len(parsed), 1)
        for basis in w.BASES:
            self.assertEqual(parsed[0][basis]["pct"], original[basis]["pct"])
            self.assertEqual(parsed[0][basis]["status"], original[basis]["status"])
            self.assertEqual(parsed[0][basis]["entry"], original[basis]["entry"])
            self.assertEqual(parsed[0][basis]["exit"], original[basis]["exit"])

    def test_missing_window_fails(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        # Remove the window from batch B: its emit tree no longer matches.
        import shutil
        shutil.rmtree(emit_b / "oa203" / "windows" / "oa203-windows-v1" / "overnight")
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"}, ["batch-01", "batch-02"]))

    def test_duplicate_batch_root_fails(self):
        emit_a = self.root / "batch-01"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_a], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"}, ["batch-01", "batch-02"]))


class CombineCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _summary(self, out):
        d = (out / "oa203" / "windows" / "oa203-windows-v1" / "overnight"
             / "overnight-2026-10-05_2026-10-06")
        return json.loads((d / "summary.json").read_text(encoding="utf-8"))

    def test_no_coverage_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined")

    def test_single_subset_with_subset_contract_is_partial_not_final(self):
        emit_a = self.root / "batch-01"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        report = combine.combine_emit([emit_a], self.root / "combined",
                                      coverage=_coverage({"AAA"}, ["batch-01"],
                                                         scope="bounded_subset"))
        self.assertEqual(report["coverage"]["scope"], "bounded_subset")
        summary = self._summary(self.root / "combined")
        self.assertEqual(summary["state"], "partial")
        self.assertEqual(summary["scope"]["universe_scope"], "bounded_subset")
        self.assertEqual(summary["state_reasons"], ["bounded_universe_subset"])

    def test_single_subset_with_full_universe_contract_refused(self):
        emit_a = self.root / "batch-01"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        # Contract claims a second batch that never arrives.
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"},
                                                    ["batch-01", "batch-02"]))
        # Contract claims underlyings the single batch does not cover.
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"}, ["batch-01"]))

    def test_missing_batch_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB", "CCC"},
                                                    ["batch-01", "batch-02", "batch-03"]))

    def test_unexpected_batch_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        emit_c = self.root / "batch-03"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        _write_emit(emit_c, [("CCC261009C00100000", "CCC", 0.50, 0.60)])
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b, emit_c], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"},
                                                    ["batch-01", "batch-02"]))

    def test_aliased_duplicate_batch_refused(self):
        import shutil
        emit_a = self.root / "batch-01"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        shutil.copytree(emit_a, self.root / "batch-01-copy")
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, self.root / "batch-01-copy"],
                                 self.root / "combined",
                                 coverage=_coverage({"AAA"},
                                                    ["batch-01", "batch-01-copy"]))

    def test_same_underlying_in_two_batches_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009P00050000", "AAA", 0.15, 0.48)])
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                 coverage=_coverage({"AAA"}, ["batch-01", "batch-02"]))

    def test_first_summary_tampered_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        win_dir = (emit_a / "oa203" / "windows" / "oa203-windows-v1" / "overnight"
                   / "overnight-2026-10-05_2026-10-06")
        summary_path = win_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["policy"]["version"] = "bogus"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"},
                                                    ["batch-01", "batch-02"]))

    def test_first_summary_sessions_tampered_refused(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)])
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)])
        win_dir = (emit_a / "oa203" / "windows" / "oa203-windows-v1" / "overnight"
                   / "overnight-2026-10-05_2026-10-06")
        summary_path = win_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["sessions"][0]["day"] = "2026-10-04"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaises(ValueError):
            combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                 coverage=_coverage({"AAA", "BBB"},
                                                    ["batch-01", "batch-02"]))

    def test_partial_provenance_is_partial_never_final(self):
        emit_a = self.root / "batch-01"
        emit_b = self.root / "batch-02"
        partial_status = {d: {"final": True, "assessment": "partial", "sweeps": 6}
                          for d in DAYS}
        _write_emit(emit_a, [("AAA261009C00100000", "AAA", 1.00, 1.50)],
                    state_status=partial_status)
        _write_emit(emit_b, [("BBB261009C00100000", "BBB", 0.15, 0.48)],
                    state_status=partial_status)
        report = combine.combine_emit([emit_a, emit_b], self.root / "combined",
                                      coverage=_coverage({"AAA", "BBB"},
                                                         ["batch-01", "batch-02"]))
        self.assertEqual(report["coverage"]["scope"], "full_universe")
        summary = self._summary(self.root / "combined")
        self.assertEqual(summary["state"], "partial")
        self.assertIn("session_partial:2026-10-05", summary["state_reasons"])
        self.assertEqual(summary["scope"]["universe_scope"], "full_universe")

    def test_quote_difference_reported_per_basis_in_emit(self):
        rows_a = _rows([("AAA261009C00100000", "AAA", 1.00, 1.50)])
        rows_b = _rows([("AAA261009C00100000", "BBB", 1.00, 1.50)])
        rows_b[0]["qualified"]["entry_ask_ms"] = (
            rows_b[0]["qualified"].get("entry_ask_ms") or 0) + 1000
        outputs.write_window(self.root / "batch-01", WIN, rows_a, w.WindowPolicy(),
                             _status(), _sources(), "2026-10-06T20:00:00Z", {"mode": "test"})
        outputs.write_window(self.root / "batch-02", WIN, rows_b, w.WindowPolicy(),
                             _status(), _sources(), "2026-10-06T20:00:00Z", {"mode": "test"})
        report = combine.combine_emit([self.root / "batch-01", self.root / "batch-02"],
                                      self.root / "combined",
                                      coverage=_coverage({"AAA", "BBB"},
                                                         ["batch-01", "batch-02"]))
        self.assertEqual(report["conflicts"]["symbols"], 1)
        with open(self.root / "combined" / "conflicts.csv", encoding="utf-8",
                  newline="") as f:
            conflicts = list(csv.DictReader(f))
        self.assertEqual(len(conflicts), 2)
        by_batch = {c["batch"]: c for c in conflicts}
        self.assertNotEqual(by_batch["batch-01"]["qualified_entry_ask_ms"],
                            by_batch["batch-02"]["qualified_entry_ask_ms"])
        self.assertEqual(by_batch["batch-01"]["mid_entry_ask_ms"],
                         by_batch["batch-02"]["mid_entry_ask_ms"])
        win_report = report["windows"]["overnight/overnight-2026-10-05_2026-10-06"]
        self.assertEqual(len(win_report["conflicts"][0]["attributions"]), 2)
        self.assertIn("qualified",
                      win_report["conflicts"][0]["attributions"][0])


@unittest.skipUnless(os.environ.get("REPLAY_130_CHECKPOINT"),
                     "needs REPLAY_130_CHECKPOINT (preserved #130 checkpoint)")
class RealArtifactsTests(unittest.TestCase):
    """Verify the combine rule against the real preserved 14-batch replay."""

    WINDOWS = ["overnight-2026-10-05_2026-10-06", "overnight-2026-10-06_2026-10-07",
               "sessions2-2026-10-05_2026-10-06", "sessions2-2026-10-06_2026-10-07",
               "sessions3-2026-10-05_2026-10-07"]

    def test_combined_counts_reproduce_checkpoint(self):
        ckpt = Path(os.environ["REPLAY_130_CHECKPOINT"])
        batches = ckpt / "replay-130-full"
        combined = ckpt / "replay-130-full-combined"
        summary = json.loads((combined / "full_universe_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["batches"], 14)
        self.assertEqual(summary["expected_total_contracts"], 71818)
        for win in self.WINDOWS:
            rows = []
            for d in sorted(p for p in batches.iterdir() if p.is_dir() and p.name.startswith("batch-")):
                with open(d / f"{win}.csv", newline="", encoding="utf-8") as f:
                    rows.extend((d.name, r) for r in csv.DictReader(f))
            by_batch = {}
            for name, r in rows:
                by_batch.setdefault(name, []).append(r)
            kept, conflicts, stats = combine.combine_csv_rows(
                win, [(name, rs) for name, rs in sorted(by_batch.items())])
            self.assertEqual(stats["raw_rows"], 71818, win)
            self.assertEqual(stats["kept"], 71710, win)
            self.assertEqual(stats["conflicting_symbols"], 54, win)
            self.assertEqual(stats["pure_duplicate_groups"], 0, win)
            self.assertEqual(summary["windows"][win]["contracts"], 71710, win)
            self.assertEqual(summary["windows"][win]["conflicting_symbols"], 54, win)
            # The combined top is the full-universe top, not the batch top.
            elig = sorted((r for r in kept if r["status"] == "eligible"),
                          key=lambda r: (-float(r["pct"]), r["symbol"]))
            self.assertEqual(elig[0]["symbol"], summary["windows"][win]["top"][0]["symbol"], win)
        with open(combined / "conflicts.csv", newline="", encoding="utf-8") as f:
            self.assertEqual(sum(1 for _ in csv.DictReader(f)), 540)

    def test_batch01_top_is_not_the_full_universe_top(self):
        ckpt = Path(os.environ["REPLAY_130_CHECKPOINT"])
        batches = ckpt / "replay-130-full"
        combined = ckpt / "replay-130-full-combined"
        summary = json.loads((combined / "full_universe_summary.json").read_text(encoding="utf-8"))
        win = "overnight-2026-10-05_2026-10-06"
        with open(batches / "batch-01" / f"{win}.csv", newline="", encoding="utf-8") as f:
            batch_rows = [r for r in csv.DictReader(f) if r["status"] == "eligible"]
        batch_top = sorted(batch_rows, key=lambda r: (-float(r["pct"]), r["symbol"]))[0]
        full_top = summary["windows"][win]["top"][0]
        self.assertNotEqual(batch_top["symbol"], full_top["symbol"])
        self.assertEqual(batch_top["symbol"], "ALAB261009C00410000")
        self.assertEqual(full_top["symbol"], "CEG261009C00280000")


if __name__ == "__main__":
    unittest.main()
