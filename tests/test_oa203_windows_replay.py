import sys
import unittest
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data.options_scanner.windows_replay as replay  # noqa: E402
from test_oa203_windows import SESSIONS, ms, sess  # noqa: E402


class ReplayIntegrity(unittest.TestCase):
    def replay_day(self, extra_status="ok", extra_rows=1, include_extra_row=True,
                   extra_role="additional_expiry", nearest_status="ok", top_rows=2,
                   nearest_t=1, extra_t=1, sweep_start=0, sweep_end=2,
                   session_close=2, fetched_ms=None, nearest_fetched_ms=None):
        day = "2026-10-16"
        base = ms(day, 9, 30)
        at = lambda offset: base + offset if offset is not None else None
        prefix = f"oa203/scanner/{day}/"
        nearest = {"underlying": "SPY", "symbol": "SPY261016C00100000",
                   "expiration": day, "sweep": 1, "t": at(nearest_t)}
        extra = {**nearest, "symbol": "SPY261023C00100000", "expiration": "2026-10-23",
                 "collection_role": extra_role, "t": at(extra_t)}
        manifest = [{"sweep": 1, "file": "sweep_0001.jsonl.gz", "truncated": False, "rows": top_rows,
                     "started_ms": at(sweep_start), "ended_ms": at(sweep_end),
                     "chains": {"SPY": {"status": nearest_status, "rows": 1,
                                        "fetched_ms": at(nearest_t if nearest_fetched_ms is None else nearest_fetched_ms)}},
                     "additional_chains": {"SPY": {"2026-10-23": {
                         "status": extra_status, "expiration": "2026-10-23", "rows": extra_rows,
                         "fetched_ms": at(extra_t)}}}}]
        if fetched_ms is not None:
            manifest[0]["additional_chains"]["SPY"]["2026-10-23"]["fetched_ms"] = at(fetched_ms)
        rows = [nearest] + ([extra] if include_extra_row else [])
        artifacts = {
            prefix + "summary.json": json.dumps({"trade_date": day, "final": True,
                                                   "session_open_ms": base, "session_close_ms": at(session_close),
                                                   "assessment": {"status": "complete"}}).encode(),
            prefix + "universe.json": json.dumps({"trade_date": day, "selected": [{
                "underlying": "SPY", "expiration": day,
                "additional_expiration": "2026-10-23"}]}).encode(),
            prefix + "sweeps/manifest.jsonl": (json.dumps(manifest[0]) + "\n").encode(),
            prefix + "sweeps/sweep_0001.jsonl.gz": gzip.compress(
                "".join(json.dumps(r) + "\n" for r in rows).encode()),
        }
        symbols, sampled = defaultdict(list), defaultdict(set)
        with mock.patch.object(replay, "fetch", side_effect=lambda key: artifacts[key]):
            replay.load_session(day, {"SPY"}, symbols, sampled)
        return symbols, sampled

    def test_extra_rows_require_successful_manifest_and_exact_count(self):
        symbols, sampled = self.replay_day()
        self.assertEqual(len(symbols), 2)
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-23")])
        for settings in ({"extra_status": "error"}, {"extra_rows": 2},
                         {"include_extra_row": False}, {"extra_role": "nearest"},
                         {"nearest_status": "error"}, {"top_rows": 3},
                         {"nearest_t": 5000, "nearest_fetched_ms": 1000, "sweep_end": 1000},
                         {"extra_t": None}, {"sweep_start": 3, "sweep_end": 2},
                         {"fetched_ms": 3}, {"fetched_ms": 0}):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                self.replay_day(**settings)

    def test_sweep_bounds_inclusive_session_close_exclusive(self):
        _, sampled = self.replay_day(nearest_t=0, extra_t=2, session_close=3)
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-16")])
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-23")])
        symbols, sampled = self.replay_day(nearest_t=0, extra_t=2, session_close=2)
        self.assertEqual(len(symbols), 1)
        self.assertNotIn("2026-10-16", sampled[("SPY", "2026-10-23")])

    def test_row_after_matched_sweep_end_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "row disagrees with sweep manifest"):
            self.replay_day(nearest_t=5000, nearest_fetched_ms=1000, sweep_end=1000)
        with self.assertRaisesRegex(RuntimeError, "row lacks a successful matching chain fetch"):
            self.replay_day(nearest_t=0, nearest_fetched_ms=1)

    def test_occ_identity_rejects_stitched_metadata(self):
        row = {"expiration": "2026-10-09", "type": "call", "strike": 100.0, "root": "ABC"}
        replay.validate_contract_identity("ABC261009C00100000", row)
        with self.assertRaises(RuntimeError):
            replay.validate_contract_identity("ABC261016C00100000", row)
        with self.assertRaises(RuntimeError):
            replay.validate_contract_identity("ABC261009P00100000", row)

    def test_manifest_cutoff_requires_complete_contiguous_sweeps(self):
        good = [{"sweep": 1, "file": "sweep_0001.jsonl.gz", "chains": {}, "truncated": False,
                 "started_ms": 100, "ended_ms": 1000}]
        summary = {"assessment": {"status": "complete"}}
        self.assertFalse(replay.validate_manifest(good, summary, "2026-10-05"))
        for bad in ([dict(good[0], sweep=2)],
                    [dict(good[0], started_ms=None)], [dict(good[0], ended_ms=99)],
                    good + [dict(good[0], sweep=2, file="sweep_0002.jsonl.gz", started_ms=999)]):
            with self.assertRaises(RuntimeError):
                replay.validate_manifest(bad, summary, "2026-10-05")
        with self.assertRaises(RuntimeError):
            replay.validate_manifest(good, {"assessment": {"status": "partial"}}, "2026-10-05")
        # A truncated NON-final sweep is still a real mid-session gap.
        two = [dict(good[0], truncated=True),
               dict(good[0], sweep=2, file="sweep_0002.jsonl.gz", started_ms=1000, ended_ms=2000)]
        with self.assertRaises(RuntimeError):
            replay.validate_manifest(two, summary, "2026-10-05")
        # A truncated FINAL sweep is the normal end-of-day partial sweep
        # (market close cuts it short); it is accepted and its rows are
        # excluded from window evidence.
        self.assertTrue(replay.validate_manifest(
            [dict(good[0]), dict(good[0], sweep=2, file="sweep_0002.jsonl.gz",
                                  truncated=True, started_ms=1000, ended_ms=2000)],
            summary, "2026-10-05"))

    def test_truncated_final_sweep_rows_excluded(self):
        # Two sweeps; the final one is truncated. Its rows must not enter
        # window evidence, and the exclusion must be recorded.
        day = "2026-10-16"
        base = ms(day, 9, 30)
        at = lambda offset: base + offset
        prefix = f"oa203/scanner/{day}/"
        manifest = [
            {"sweep": 1, "file": "sweep_0001.jsonl.gz", "truncated": False, "rows": 1,
             "started_ms": at(0), "ended_ms": at(1000),
             "chains": {"SPY": {"status": "ok", "rows": 1, "fetched_ms": at(0)}},
             "additional_chains": {}},
            {"sweep": 2, "file": "sweep_0002.jsonl.gz", "truncated": True, "rows": 1,
             "started_ms": at(1000), "ended_ms": at(2000),
             "chains": {"SPY": {"status": "ok", "rows": 1, "fetched_ms": at(1000)}},
             "additional_chains": {}},
        ]
        row1 = {"underlying": "SPY", "symbol": "SPY261016C00100000", "expiration": day,
                "sweep": 1, "t": at(0)}
        row2 = {**row1, "sweep": 2, "t": at(1000)}
        artifacts = {
            prefix + "summary.json": json.dumps({"trade_date": day, "final": True,
                                                   "session_open_ms": base, "session_close_ms": at(2000),
                                                   "assessment": {"status": "complete"}}).encode(),
            prefix + "universe.json": json.dumps({"trade_date": day, "selected": [{
                "underlying": "SPY", "expiration": day}]}).encode(),
            prefix + "sweeps/manifest.jsonl": "".join(json.dumps(m) + "\n" for m in manifest).encode(),
            prefix + "sweeps/sweep_0001.jsonl.gz": gzip.compress((json.dumps(row1) + "\n").encode()),
            prefix + "sweeps/sweep_0002.jsonl.gz": gzip.compress((json.dumps(row2) + "\n").encode()),
        }
        symbols, sampled = defaultdict(list), defaultdict(set)
        with mock.patch.object(replay, "fetch", side_effect=lambda key: artifacts[key]):
            meta = replay.load_session(day, {"SPY"}, symbols, sampled)
        # Only the first (complete) sweep's row is kept.
        self.assertEqual(len(symbols["SPY261016C00100000"]), 1)
        self.assertEqual(symbols["SPY261016C00100000"][0]["sweep"], 1)
        self.assertEqual(meta["excluded_final_sweep"]["sweep"], 2)
        self.assertEqual(meta["excluded_final_sweep"]["reason"], "truncated_final_sweep_excluded")

    def test_calendar_rejects_missing_session_and_wrong_bounds(self):
        self.assertIn((2026, 41), replay.validate_calendar(SESSIONS))
        with self.assertRaises(RuntimeError):
            replay.validate_calendar(SESSIONS[:2] + SESSIONS[3:])
        with self.assertRaises(RuntimeError):
            replay.validate_calendar([sess("2026-10-05", close=(13, 0))] + SESSIONS[1:])
        self.assertNotIn((2026, 41), replay.validate_calendar(SESSIONS[:3]))


class OutputSchema(unittest.TestCase):
    """The emitted window/run/contract schema is basis-specific and stable.

    Status and returns are per-basis (mid/exec/qualified), not a single
    global verdict: a row can be eligible on mid/exec while excluded on
    qualified (e.g. a wide spread), and the top-ranked contract differs
    by basis. The replay CSV carries the qualified basis's values; the
    per-basis eligible counts and top lists live in replay_summary.json;
    paths.csv.gz carries every basis's columns. These tests pin that
    schema so #131 can consume it without re-deriving windows.
    """

    def test_ranked_csv_schema_is_qualified_basis_and_retains_all_rows(self):
        import csv
        import io
        import market_data.options_scanner.windows as w
        from market_data.options_scanner.returns import ContractInfo, Observation
        from test_oa203_windows import WEEK, week_path, info, by_kind
        windows, _ = w.weekly_windows(SESSIONS)
        win = by_kind(windows, "weekly_cheapest")
        # One contract eligible on mid/exec but excluded on qualified
        # (wide spread), one eligible on all three.
        rows = []
        for symbol, bid, ask in (("AAA261009C00100000", 1.0, 5.0), ("BBB261009C00100000", 1.0, 1.1)):
            c = info(symbol=symbol, underlying=symbol[:3], root=symbol[:3])
            path = [Observation(t=o.t, bid=bid, ask=ask, bid_ms=o.t, ask_ms=o.t,
                                bid_size=10, ask_size=10, underlying_price=100.0) for o in week_path()]
            rows.append(w.evaluate(win, c, path, WEEK))
        ranked = w.rank_window(rows, "qualified")
        buf = io.StringIO()
        # write_ranked_csv writes to a path; use a temp file.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.csv"
            replay.write_ranked_csv(p, ranked, "qualified")
            with open(p, newline="") as src:
                got = list(csv.DictReader(src))
        self.assertEqual(list(got[0].keys()), replay.RANKED_CSV_COLUMNS)
        self.assertEqual(len(got), 2)  # every row retained, eligible or excluded
        by_symbol = {r["symbol"]: r for r in got}
        # The wide-spread contract is excluded on the qualified basis.
        self.assertEqual(by_symbol["AAA261009C00100000"]["status"], "excluded")
        # The tight contract is eligible on the qualified basis.
        self.assertEqual(by_symbol["BBB261009C00100000"]["status"], "eligible")
        # But the wide-spread contract IS eligible on mid/exec (basis-specific).
        mid = w.rank_window(rows, "mid")
        self.assertTrue(any(r["mid"]["status"] == "eligible" for r in mid if r["symbol"] == "AAA261009C00100000"))
        # The qualified rank is a rank only for qualified-eligible rows.
        self.assertEqual(by_symbol["BBB261009C00100000"]["rank"], "1")
        self.assertEqual(by_symbol["AAA261009C00100000"]["rank"], "")

    def test_summary_schema_is_per_basis(self):
        # The replay summary carries a per-basis eligible count and top
        # list for every basis; the counts can differ (qualified is the
        # strictest). Assert the structure, not a fixture value.
        import market_data.options_scanner.windows as w
        for basis in w.BASES:
            self.assertIn(basis, w.BASES)
        # A rank_window result carries a per-basis rank key.
        self.assertEqual(sorted(f"{b}_rank" for b in w.BASES),
                         ["exec_rank", "mid_rank", "qualified_rank"])

    def test_conflicting_duplicates_fail_closed_with_both_records(self):
        # A symbol attributed to two underlyings with DIFFERING evidence
        # (e.g. different missing_session reasons) is a conflict, not a
        # duplicate: it is excluded from the combined ranked output (fail
        # closed), both provenance/coverage records are preserved in
        # conflicts.csv, and no canonical identity is asserted from root
        # spelling. A symbol whose rows are identical in evidence is a
        # pure duplicate and is deduped to one row.
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[1]
        cols = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
                "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]
        win = "overnight-2026-10-05_2026-10-06"
        # Two planned batches: batch-01 has PSKY, batch-02 has SKYD.
        # The same SKYD-rooted symbol appears in both, with different
        # missing_session reasons (conflicting coverage evidence).
        def row(symbol, underlying, reasons):
            return {"rank": "", "symbol": symbol, "underlying": underlying, "option_type": "put",
                    "strike": "4.0", "expiration": "2026-10-09", "pct": "", "entry": "", "exit": "",
                    "abs_change_per_contract": "", "entry_session": "", "exit_session": "",
                    "entry_ms": "", "exit_ms": "", "flags": "", "status": "excluded", "reasons": reasons}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            batches_dir = tmp / "batches"
            (batches_dir / "batch-01").mkdir(parents=True)
            (batches_dir / "batch-02").mkdir(parents=True)
            planned = [["PSKY"], ["SKYD"]]
            (tmp / "batches.json").write_text(json.dumps(planned))
            (tmp / "union.json").write_text(json.dumps(["PSKY", "SKYD"]))
            lock = {"sources": [{"key": "oa203/scanner/2026-10-05/summary.json", "sha256": "a" * 64, "bytes": 1}]}
            for i, (under, reasons) in enumerate(
                    [("PSKY", "missing_session:2026-10-07"), ("SKYD", "missing_session:2026-10-06")], 1):
                d = batches_dir / f"batch-{i:02d}"
                (d / "replay_summary.json").write_text(json.dumps(
                    {"contracts": 1, "underlyings": [under]}))
                (d / "source_lock.json").write_text(json.dumps(lock))
                # The contract appears in every window (one row per window),
                # as in a real replay.
                for w in [win, "overnight-2026-10-06_2026-10-07", "sessions2-2026-10-05_2026-10-06",
                          "sessions2-2026-10-06_2026-10-07", "sessions3-2026-10-05_2026-10-07"]:
                    with open(d / f"{w}.csv", "w", newline="") as f:
                        wr = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
                        wr.writeheader()
                        wr.writerow(row("SKYD261009P00004000", under, reasons))
            out = tmp / "combined"
            r = subprocess.run([sys.executable, str(root / "combine-full-universe.py"),
                                "--batches-dir", str(batches_dir), "--out", str(out),
                                "--planned-batches", str(tmp / "batches.json"),
                                "--planned-union", str(tmp / "union.json")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            # The conflicting symbol is excluded from the combined CSV.
            with open(out / f"{win}.csv", newline="") as f:
                got = list(csv.DictReader(f))
            self.assertEqual(got, [])
            # Both records are preserved in the conflict report, in every
            # window (2 rows per window x 5 windows).
            with open(out / "conflicts.csv", newline="") as f:
                conf = list(csv.DictReader(f))
            self.assertEqual(len(conf), 10)
            self.assertEqual({c["underlying"] for c in conf}, {"PSKY", "SKYD"})
            self.assertEqual({c["reasons"] for c in conf},
                             {"missing_session:2026-10-07", "missing_session:2026-10-06"})
            summary = json.loads((out / "full_universe_summary.json").read_text())
            self.assertEqual(summary["windows"][win]["conflicting_symbols"], 1)
            self.assertEqual(summary["conflicts"]["symbols"], 1)
            self.assertEqual(summary["conflicts"]["rows"], 10)

    def test_identical_duplicate_dedups_and_metadata_or_flags_conflict(self):
        # (a) Two byte-identical evidence rows for one symbol dedup to a
        # single row in the combined output (no conflict, exit 0) — the
        # reproducer for the line-179 accounting defect. Three identical
        # rows remove len(group)-1 = 2 rows. (b) Rows identical except for
        # a metadata field (strike) or a quality field (flags) are a
        # conflict, not a pure duplicate: excluded fail-closed, both
        # records preserved. Differing identity/quality evidence is never
        # silently selected.
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[1]
        cols = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
                "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]
        wins = ["overnight-2026-10-05_2026-10-06", "overnight-2026-10-06_2026-10-07",
                "sessions2-2026-10-05_2026-10-06", "sessions2-2026-10-06_2026-10-07",
                "sessions3-2026-10-05_2026-10-07"]
        win = wins[0]

        def base_row(symbol, underlying, **over):
            r = {"rank": "", "symbol": symbol, "underlying": underlying, "option_type": "put",
                 "strike": "4.0", "expiration": "2026-10-09", "pct": "", "entry": "", "exit": "",
                 "abs_change_per_contract": "", "entry_session": "", "exit_session": "",
                 "entry_ms": "", "exit_ms": "", "flags": "", "status": "excluded",
                 "reasons": "missing_session:2026-10-05"}
            r.update(over)
            return r

        def run_case(rows_by_batch, contracts_per_batch):
            # rows_by_batch: list of (underlying, [row, ...]) per batch.
            # Each batch has the same rows in every window. Returns
            # (returncode, stderr, combined rows per window, conflict rows,
            # summary) — read inside the temp dir before it is removed.
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                batches_dir = tmp / "batches"
                planned = [[u] for u, _ in rows_by_batch]
                (tmp / "batches.json").write_text(json.dumps(planned))
                (tmp / "union.json").write_text(json.dumps([u for u, _ in rows_by_batch]))
                lock = {"sources": [{"key": "k", "sha256": "a" * 64, "bytes": 1}]}
                for i, (under, rows) in enumerate(rows_by_batch, 1):
                    d = batches_dir / f"batch-{i:02d}"
                    d.mkdir(parents=True)
                    (d / "replay_summary.json").write_text(json.dumps(
                        {"contracts": contracts_per_batch, "underlyings": [under]}))
                    (d / "source_lock.json").write_text(json.dumps(lock))
                    for w in wins:
                        with open(d / f"{w}.csv", "w", newline="") as f:
                            wr = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
                            wr.writeheader()
                            for r in rows:
                                wr.writerow(r)
                out = tmp / "combined"
                r = subprocess.run([sys.executable, str(root / "combine-full-universe.py"),
                                    "--batches-dir", str(batches_dir), "--out", str(out),
                                    "--planned-batches", str(tmp / "batches.json"),
                                    "--planned-union", str(tmp / "union.json")],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    return r.returncode, r.stderr, None, None, None
                combined = {}
                for w in wins:
                    with open(out / f"{w}.csv", newline="") as f:
                        combined[w] = list(csv.DictReader(f))
                with open(out / "conflicts.csv", newline="") as f:
                    conf = list(csv.DictReader(f))
                summary = json.loads((out / "full_universe_summary.json").read_text())
                return r.returncode, r.stderr, combined, conf, summary

        # (a) Two byte-identical rows (only the underlying/batch differs):
        # dedup to one row, no conflict, exit 0.
        rc, err, combined, conf, summary = run_case(
            [("AA", [base_row("AA261009P00004000", "AA")]),
             ("BB", [base_row("AA261009P00004000", "BB")])], 1)
        self.assertEqual(rc, 0, err)
        got = combined[win]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["symbol"], "AA261009P00004000")
        self.assertEqual(conf, [])
        self.assertEqual(summary["windows"][win]["pure_duplicate_groups"], 1)
        self.assertEqual(summary["windows"][win]["pure_duplicate_rows_removed"], 1)
        self.assertEqual(summary["windows"][win]["conflicting_symbols"], 0)

        # (a2) Three identical rows: dedup to one, 2 rows removed.
        rc, err, combined, conf, summary = run_case(
            [("AA", [base_row("AA261009P00004000", "AA")]),
             ("BB", [base_row("AA261009P00004000", "BB")]),
             ("CC", [base_row("AA261009P00004000", "CC")])], 1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(combined[win]), 1)
        self.assertEqual(summary["windows"][win]["pure_duplicate_groups"], 1)
        self.assertEqual(summary["windows"][win]["pure_duplicate_rows_removed"], 2)

        # (b) Metadata-only difference (strike): a conflict, excluded.
        rc, err, combined, conf, summary = run_case(
            [("AA", [base_row("AA261009P00004000", "AA")]),
             ("BB", [base_row("AA261009P00004000", "BB", strike="5.0")])], 1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(combined[win], [])
        conf_win = [c for c in conf if c["window"] == win]
        self.assertEqual(len(conf_win), 2)
        self.assertEqual({c["strike"] for c in conf_win}, {"4.0", "5.0"})
        # The conflict receipt preserves the complete record.
        self.assertIn("abs_change_per_contract", conf_win[0])
        self.assertEqual(summary["windows"][win]["conflicting_symbols"], 1)

        # (b2) Flags-only difference: also a conflict, excluded.
        rc, err, combined, conf, summary = run_case(
            [("AA", [base_row("AA261009P00004000", "AA")]),
             ("BB", [base_row("AA261009P00004000", "BB", flags="exit_on_expiration_day")])], 1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(combined[win], [])
        conf_win = [c for c in conf if c["window"] == win]
        self.assertEqual(len(conf_win), 2)
        self.assertEqual({c["flags"] for c in conf_win}, {"", "exit_on_expiration_day"})

    def test_missing_or_inconsistent_batch_fails(self):
        # A missing batch, an unexpected batch, a non-disjoint union, or
        # an inconsistent source lock FAILS the combine instead of
        # producing a partial result.
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[1]
        cols = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
                "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]
        win = "overnight-2026-10-05_2026-10-06"
        wins = [win, "overnight-2026-10-06_2026-10-07", "sessions2-2026-10-05_2026-10-06",
                "sessions2-2026-10-06_2026-10-07", "sessions3-2026-10-05_2026-10-07"]
        def make_batch(d, under, lock_sha="a" * 64):
            d.mkdir(parents=True, exist_ok=True)
            (d / "replay_summary.json").write_text(json.dumps({"contracts": 0, "underlyings": [under]}))
            (d / "source_lock.json").write_text(json.dumps(
                {"sources": [{"key": "k", "sha256": lock_sha, "bytes": 1}]}))
            for w in wins:
                with open(d / f"{w}.csv", "w", newline="") as f:
                    wr = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
                    wr.writeheader()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "batches.json").write_text(json.dumps([["AA"], ["BB"]]))
            (tmp / "union.json").write_text(json.dumps(["AA", "BB"]))
            # Missing batch-02: only batch-01 present.
            batches_dir = tmp / "batches"
            make_batch(batches_dir / "batch-01", "AA")
            r = subprocess.run([sys.executable, str(root / "combine-full-universe.py"),
                                "--batches-dir", str(batches_dir), "--out", str(tmp / "out"),
                                "--planned-batches", str(tmp / "batches.json"),
                                "--planned-union", str(tmp / "union.json")],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing batch", r.stderr)
            # Inconsistent source lock: batch-02 has a different hash.
            make_batch(batches_dir / "batch-02", "BB", lock_sha="b" * 64)
            r = subprocess.run([sys.executable, str(root / "combine-full-universe.py"),
                                "--batches-dir", str(batches_dir), "--out", str(tmp / "out"),
                                "--planned-batches", str(tmp / "batches.json"),
                                "--planned-union", str(tmp / "union.json")],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("source lock differs", r.stderr)

    def test_cache_reuses_blob_and_verifies_hash(self):
        # A blob already in --cache-dir is reused (no network fetch) and
        # its SHA-256 is still recorded; a cached blob whose hash disagrees
        # with the lock is rejected, not trusted.
        import tempfile
        key = "oa203/scanner/2026-10-05/summary.json"
        body = b'{"assessment": {"status": "complete"}}'
        digest = __import__("hashlib").sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            dest = cache / key
            dest.parent.mkdir(parents=True)
            dest.write_bytes(body)
            replay.CACHE_DIR = cache
            replay.SOURCES.clear()
            replay.EXPECTED_HASHES = None
            self.assertEqual(replay.fetch(key), body)
            self.assertTrue(replay.SOURCES[-1].get("cached"))
            self.assertEqual(replay.SOURCES[-1]["sha256"], digest)
            # A lock that disagrees with the cached blob is rejected.
            replay.EXPECTED_HASHES = {key: "0" * 64}
            with self.assertRaises(RuntimeError):
                replay.fetch(key)
            replay.CACHE_DIR = None
            replay.EXPECTED_HASHES = None
            replay.SOURCES.clear()


if __name__ == "__main__":
    unittest.main()
