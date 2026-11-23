"""Offline tests for windows_live: build + publish from local day archives.

Everything here is local and synthetic: day directories are written by
the test in the exact shape ``finalize`` writes them (summary.json,
universe.json, sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz), the
build runs the real producer, and publication runs the real publisher
against a LocalFilesystemBackend. No provider, no R2, no network.
"""

import gzip
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows_live as live
import market_data.options_scanner.windows_publish as pub
from market_data.options_scanner.cli import main as cli_main

DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
BOUNDS = {
    "2026-10-05": (1791207000000, 1791230400000),
    "2026-10-06": (1791293400000, 1791316800000),
    "2026-10-07": (1791379800000, 1791403200000),
    "2026-10-08": (1791466200000, 1791489600000),
    "2026-10-09": (1791552600000, 1791576000000),
}
# (symbol, underlying, type, strike, expiration, root)
CONTRACTS = [
    ("AAA261009C00100000", "AAA", "call", 100, "2026-10-09", "AAA"),
    ("BBB261009P00050000", "BBB", "put", 50, "2026-10-09", "BBB"),
    ("CCC261006C00075000", "CCC", "call", 75, "2026-10-06", "CCC"),
]
# Mid-price per contract per day: AAA rises, BBB falls, CCC lives Oct 5-6 only.
MIDS = {
    "AAA261009C00100000": [0.15, 0.48, 0.50, 0.52, 0.55],
    "BBB261009P00050000": [1.00, 0.90, 0.85, 0.80, 0.75],
    "CCC261006C00075000": [0.25, 0.35],
}
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def _write_day(root: Path, day: str, *, assessment: str = "complete") -> Path:
    """Write one synthetic finalized day directory in finalize() shape."""
    open_ms, close_ms = BOUNDS[day]
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    alive = [c for c in CONTRACTS if DAYS.index(day) < len(MIDS[c[0]])]
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": assessment},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": c[1], "expiration": c[4], "additional_expiration": None}
                     for c in alive],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        # Endpoints must fall inside the 900s open/close tolerances
        # (WindowPolicy open_tolerance_s / close_tolerance_s): first
        # observation within 900s after the open, last within 900s
        # before the close, or the window has no endpoints at all.
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for symbol, underlying, otype, strike, expiry, r in alive:
            mid = MIDS[symbol][DAYS.index(day)]
            rows.append({
                "underlying": underlying, "symbol": symbol, "expiration": expiry,
                "t": t, "sweep": sweep, "bid": round(mid - 0.01, 4),
                "ask": round(mid + 0.01, 4), "bid_ms": t, "ask_ms": t,
                "spot": 100.0, "bid_size": 10, "ask_size": 10,
                "volume": 5, "oi": 100, "root": r, "type": otype,
                "strike": strike, "contract_size": 100,
            })
            chains[underlying] = {"status": "ok", "fetched_ms": t, "rows": 1,
                                  "expiration": expiry}
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row) + "\n").encode())
        (day_dir / "sweeps" / f"sweep_{sweep:04d}.jsonl.gz").write_bytes(buf.getvalue())
        manifest.append({"sweep": sweep, "file": f"sweep_{sweep:04d}.jsonl.gz",
                         "started_ms": open_ms if sweep == 1 else close_ms - 7200000,
                         "ended_ms": open_ms + 7200000 if sweep == 1 else close_ms,
                         "rows": len(rows), "chains": chains, "truncated": False})
    (day_dir / "sweeps" / "manifest.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in manifest), encoding="utf-8")
    return day_dir


def _write_rotation_day(root: Path, day: str, underlyings, mids) -> Path:
    """One synthetic finalized day with one contract per underlying.

    Symbol is <underlying> + 261009C00100000 (root == underlying,
    call, strike 100, expiration 2026-10-09); ``mids`` maps each
    underlying to its mid-price for both sweeps of the day.
    """
    open_ms, close_ms = BOUNDS[day]
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": "complete"},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": u, "expiration": "2026-10-09",
                      "additional_expiration": None} for u in underlyings],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for u in underlyings:
            mid = mids[u]
            rows.append({
                "underlying": u, "symbol": f"{u}261009C00100000",
                "expiration": "2026-10-09",
                "t": t, "sweep": sweep, "bid": round(mid - 0.01, 4),
                "ask": round(mid + 0.01, 4), "bid_ms": t, "ask_ms": t,
                "spot": 100.0, "bid_size": 10, "ask_size": 10,
                "volume": 5, "oi": 100, "root": u, "type": "call",
                "strike": 100, "contract_size": 100,
            })
            chains[u] = {"status": "ok", "fetched_ms": t, "rows": 1,
                         "expiration": "2026-10-09"}
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row) + "\n").encode())
        (day_dir / "sweeps" / f"sweep_{sweep:04d}.jsonl.gz").write_bytes(buf.getvalue())
        manifest.append({"sweep": sweep, "file": f"sweep_{sweep:04d}.jsonl.gz",
                         "started_ms": open_ms if sweep == 1 else close_ms - 7200000,
                         "ended_ms": open_ms + 7200000 if sweep == 1 else close_ms,
                         "rows": len(rows), "chains": chains, "truncated": False})
    (day_dir / "sweeps" / "manifest.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in manifest), encoding="utf-8")
    return day_dir


def _write_overcap_day(root: Path, day: str) -> Path:
    """A day directory whose universe selects 501 underlyings (over the per-day cap)."""
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    open_ms, close_ms = BOUNDS[day]
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": "complete"},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": f"U{i:03d}", "expiration": "2026-10-09",
                      "additional_expiration": None} for i in range(501)],
    }) + "\n", encoding="utf-8")
    return day_dir


def _window_ids(report):
    return {(w["kind"], w["window_id"]) for w in report["windows"]}


def _write_shared_symbol_day(root: Path, day: str) -> Path:
    """One day where the same OCC symbol is quoted under two underlyings.

    AAA and DDD both quote AAA261009C00100000, with DIFFERING evidence
    (AAA rises, DDD falls); BBB quotes its own symbol. The shared symbol
    must be excluded from every window (fail-closed), never served under
    either attribution. Same finalize() shape as _write_day.
    """
    open_ms, close_ms = BOUNDS[day]
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    idx = DAYS.index(day)
    specs = [
        ("AAA261009C00100000", "AAA", "call", 100, "AAA", [0.15, 0.48, 0.50, 0.52, 0.55][idx]),
        ("AAA261009C00100000", "DDD", "call", 100, "AAA", [0.90, 0.20, 0.25, 0.30, 0.35][idx]),
        ("BBB261009P00050000", "BBB", "put", 50, "BBB", [1.00, 0.90, 0.85, 0.80, 0.75][idx]),
    ]
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": "complete"},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": u, "expiration": "2026-10-09", "additional_expiration": None}
                     for u in ("AAA", "BBB", "DDD")],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for symbol, underlying, otype, strike, r, mid in specs:
            rows.append({
                "underlying": underlying, "symbol": symbol, "expiration": "2026-10-09",
                "t": t, "sweep": sweep, "bid": round(mid - 0.01, 4),
                "ask": round(mid + 0.01, 4), "bid_ms": t, "ask_ms": t,
                "spot": 100.0, "bid_size": 10, "ask_size": 10,
                "volume": 5, "oi": 100, "root": r, "type": otype,
                "strike": strike, "contract_size": 100,
            })
            chains[underlying] = {"status": "ok", "fetched_ms": t, "rows": 1,
                                  "expiration": "2026-10-09"}
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row) + "\n").encode())
        (day_dir / "sweeps" / f"sweep_{sweep:04d}.jsonl.gz").write_bytes(buf.getvalue())
        manifest.append({"sweep": sweep, "file": f"sweep_{sweep:04d}.jsonl.gz",
                         "started_ms": open_ms if sweep == 1 else close_ms - 7200000,
                         "ended_ms": open_ms + 7200000 if sweep == 1 else close_ms,
                         "rows": len(rows), "chains": chains, "truncated": False})
    (day_dir / "sweeps" / "manifest.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in manifest), encoding="utf-8")
    return day_dir


class LiveBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.days = [_write_day(self.root / "days", d) for d in DAYS]
        self.emit = self.root / "emit"

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_all_kinds_subset_is_partial(self):
        # A bounded-subset build NEVER claims full-universe final: it is
        # served partial with the bounded_universe_subset reason, even
        # when every loaded day is final and complete.
        report = live.build_windows(self.days, {"AAA", "BBB", "CCC"}, self.emit)
        self.assertEqual(len(report["windows"]), 14)
        self.assertEqual(report["universe_scope"], "bounded_subset")
        kinds = {k for k, _ in _window_ids(report)}
        self.assertEqual(kinds, {"weekly_fixed", "weekly_cheapest",
                                 "weekly_cheapest_best_exit", "overnight",
                                 "sessions_2", "sessions_3"})
        by_id = {(w["kind"], w["window_id"]): w for w in report["windows"]}
        self.assertIn(("weekly_cheapest", "weekly-2026-W41-cheapest"), by_id)
        self.assertIn(("overnight", "overnight-2026-10-05_2026-10-06"), by_id)
        self.assertIn(("sessions_3", "sessions3-2026-10-05_2026-10-07"), by_id)
        for w in report["windows"]:
            self.assertIn(w["kind"], kinds)
        # Every emit has the three members; subset summaries are partial.
        for kind, window_id in _window_ids(report):
            d = self.emit / "oa203" / "windows" / "oa203-windows-v1" / kind / window_id
            self.assertTrue((d / "summary.json").is_file(), kind)
            self.assertTrue((d / "rankings.csv").is_file(), kind)
            self.assertTrue((d / "paths.csv.gz").is_file(), kind)
            summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["state"], "partial", window_id)
            self.assertEqual(summary["state_reasons"], ["bounded_universe_subset"], window_id)
            self.assertEqual(summary["scope"]["mode"], "live_archive", window_id)
            self.assertEqual(summary["scope"]["universe_scope"], "bounded_subset", window_id)
            # Rows are window-scoped: a contract counts only if it has
            # in-session observations in this window's own sessions.
            # CCC is sampled Oct 5-6 only, so windows not touching those
            # days evaluate 2 contracts, the rest 3.
            win_days = {s["day"] for s in summary["sessions"]}
            expected = 3 if win_days & {"2026-10-05", "2026-10-06"} else 2
            self.assertEqual(summary["contracts_evaluated"], expected, window_id)
        overnight = by_id[("overnight", "overnight-2026-10-05_2026-10-06")]
        self.assertEqual(overnight["qualified"]["top"][0]["symbol"], "AAA261009C00100000")

    def test_build_full_universe_final(self):
        emit = self.root / "emit-full"
        report = live.build_windows(self.days, {"AAA", "BBB", "CCC"}, emit,
                                    full_universe=True)
        self.assertEqual(report["universe_scope"], "full_universe")
        self.assertEqual(report["contracts_kept"], 37)  # 9 windows touching Oct 5/6 x 3 contracts + 5 windows x 2 (CCC sampled Oct 5-6 only; rows are window-scoped)
        self.assertEqual(report["conflicting_symbols"], 0)
        for kind, window_id in _window_ids(report):
            d = emit / "oa203" / "windows" / "oa203-windows-v1" / kind / window_id
            summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["state"], "final", window_id)
            self.assertEqual(summary["state_reasons"], [], window_id)
            self.assertEqual(summary["scope"]["universe_scope"], "full_universe", window_id)

    def test_shared_symbol_conflict_excluded_from_every_window(self):
        # The same OCC symbol under two underlyings with differing
        # evidence is excluded from every window (fail-closed), never
        # served under either attribution.
        import csv as _csv
        spool = self.root / "shared"
        for day in DAYS:
            _write_shared_symbol_day(spool, day)
        emit = self.root / "emit-shared"
        report = live.build_windows(
            [spool / d for d in DAYS], {"AAA", "BBB", "DDD"}, emit, full_universe=True)
        self.assertEqual(len(report["windows"]), 14)
        self.assertEqual(report["conflicting_symbols"], 1)
        self.assertEqual(len(report["conflicts"]), 14)  # one per window
        self.assertEqual({c["symbol"] for c in report["conflicts"]},
                         {"AAA261009C00100000"})
        self.assertEqual(report["contracts_kept"], 14)  # BBB kept in every window
        for win in report["windows"]:
            rankings = (emit / "oa203" / "windows" / "oa203-windows-v1" / win["kind"]
                        / win["window_id"] / "rankings.csv")
            with open(rankings, newline="", encoding="utf-8") as f:
                symbols = {r["symbol"] for r in _csv.DictReader(f)}
            self.assertNotIn("AAA261009C00100000", symbols)
            self.assertIn("BBB261009P00050000", symbols)

    def test_partial_day_admitted_with_reasons(self):
        _write_day(self.root / "days2", "2026-10-08", assessment="partial")
        days = [self.days[0], self.days[1], self.days[2],
                self.root / "days2" / "2026-10-08", self.days[4]]
        emit = self.root / "emit-partial"
        report = live.build_windows(days, {"AAA", "BBB", "CCC"}, emit,
                                    full_universe=True)
        self.assertEqual(len(report["windows"]), 14)

        def summary_for(kind, window_id):
            d = emit / "oa203" / "windows" / "oa203-windows-v1" / kind / window_id
            return json.loads((d / "summary.json").read_text(encoding="utf-8"))

        # Windows touching the partial day are partial with the reason named.
        s78 = summary_for("overnight", "overnight-2026-10-07_2026-10-08")
        self.assertEqual(s78["state"], "partial")
        self.assertIn("session_partial:2026-10-08", s78["state_reasons"])
        self.assertEqual(s78["contracts_evaluated"], 2)  # CCC has no observations Oct 7-8; rows are window-scoped
        # Windows that do not touch it stay final.
        s56 = summary_for("overnight", "overnight-2026-10-05_2026-10-06")
        self.assertEqual(s56["state"], "final")
        self.assertNotIn("session_partial:2026-10-08", s56["state_reasons"])

    def test_torn_manifest_refused_fail_closed(self):
        _write_day(self.root / "days2", "2026-10-08", assessment="partial")
        bad = [self.days[0], self.days[1], self.days[2],
               self.root / "days2" / "2026-10-08", self.days[4]]
        manifest = bad[3] / "sweeps" / "manifest.jsonl"
        entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        entries[0]["truncated"] = True  # a truncated NON-final sweep: a real mid-session gap
        manifest.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            live.build_windows(bad, {"AAA", "BBB", "CCC"}, self.root / "emit")

    def test_damaged_manifest_refused_fail_closed(self):
        _write_day(self.root / "days2", "2026-10-08", assessment="partial")
        bad = [self.days[0], self.days[1], self.days[2],
               self.root / "days2" / "2026-10-08", self.days[4]]
        manifest = bad[3] / "sweeps" / "manifest.jsonl"
        entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        entries[0]["chains"] = None  # damaged sweep index
        manifest.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            live.build_windows(bad, {"AAA", "BBB", "CCC"}, self.root / "emit")

    def test_bounds(self):
        with self.assertRaises(ValueError):
            live.build_windows(self.days, {f"U{i:02d}" for i in range(51)}, self.emit)
        with self.assertRaises(ValueError):
            live.build_windows(self.days * 3, {"AAA"}, self.emit)
        # Full-universe cap is per day, not on the union: a 501-underlying
        # union over days that select only 3 each succeeds.
        report501 = live.build_windows(
            self.days, {f"U{i:03d}" for i in range(498)} | {"AAA", "BBB", "CCC"},
            self.root / "emit-501", full_universe=True)
        self.assertEqual(len(report501["underlyings"]), 501)
        # 51 underlyings are fine in full-universe mode (the loaded days
        # select only AAA/BBB/CCC, which the set covers).
        report = live.build_windows(self.days, {"AAA", "BBB", "CCC"} | {f"U{i:02d}" for i in range(48)},
                                    self.root / "emit-51", full_universe=True)
        self.assertEqual(len(report["windows"]), 14)

    def test_per_day_cap_still_enforced(self):
        over = _write_overcap_day(self.root / "overcap", "2026-10-05")
        with self.assertRaises(ValueError):
            live.build_windows([over], {f"U{i:03d}" for i in range(501)},
                               self.root / "emit-overcap", full_universe=True)

    def test_rotation_500_plus_1_union_succeeds(self):
        # Blocker (3): 500 underlyings day 1 + 1 new underlying day 2 is a
        # 501-union and must NOT fail — the 500 cap is per day.
        import csv as _csv
        day1 = _write_rotation_day(
            self.root / "rot", "2026-10-05",
            [f"U{i:03d}" for i in range(500)],
            {f"U{i:03d}": (1.00 if i == 0 else 0.15 if i == 1 else 0.50)
             for i in range(500)})
        day2 = _write_rotation_day(
            self.root / "rot", "2026-10-06", ["NEW1", "U000", "U001"],
            {"NEW1": 0.20, "U000": 1.50, "U001": 0.48})
        union = {f"U{i:03d}" for i in range(500)} | {"NEW1"}
        report = live.build_windows([day1, day2], union, self.root / "emit-rot",
                                    full_universe=True)
        self.assertEqual(len(report["underlyings"]), 501)
        self.assertEqual(report["universe_scope"], "full_universe")
        self.assertEqual(len(report["windows"]), 2)  # overnight + sessions_2; no complete week
        # Ranking is over the combined 501-universe: U001 (+220%) tops U000 (+50%).
        rankings = (self.root / "emit-rot" / "oa203" / "windows" / "oa203-windows-v1"
                    / "overnight" / "overnight-2026-10-05_2026-10-06" / "rankings.csv")
        with open(rankings, newline="", encoding="utf-8") as f:
            mids = [r for r in _csv.DictReader(f) if r["basis"] == "mid"]
        self.assertEqual(mids[0]["symbol"], "U001261009C00100000")
        self.assertEqual(mids[0]["rank"], "1")
        self.assertEqual(mids[1]["symbol"], "U000261009C00100000")
        # The rotated-in underlying has no entry-day observation, so it is
        # excluded from the overnight window, not invented.
        self.assertNotIn("NEW1261009C00100000", {r["symbol"] for r in mids})
        summary = json.loads((rankings.parent / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "final")
        self.assertEqual(summary["scope"]["universe_scope"], "full_universe")

    def test_publish_all_local_then_idempotent(self):
        live.build_windows(self.days, {"AAA", "BBB", "CCC"}, self.emit)
        backend = pub.LocalFilesystemBackend(self.root / "backend")
        results = live.publish_windows(self.emit, backend, now=NOW)
        self.assertEqual(len(results), 14)
        self.assertTrue(all(r["status"] == "published" for r in results))
        for r in results:
            pointer = json.loads(
                (self.root / "backend" / r["prefix"] / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(pointer["revision"], r["revision"])
            self.assertEqual(pointer["state"], "partial")
        again = live.publish_windows(self.emit, backend, now=NOW)
        self.assertTrue(all(r["status"] == "already_published" for r in again))
        # The published subset windows are partial, never final.
        summary = json.loads((self.root / "backend" / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-05_2026-10-06"
                              / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "partial")
        self.assertEqual(summary["state_reasons"], ["bounded_universe_subset"])

    def test_cli_build_and_publish(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_main(["windows-build", "--dir", str(self.days[0]), "--dir", str(self.days[1]),
                           "--dir", str(self.days[2]), "--dir", str(self.days[3]),
                           "--dir", str(self.days[4]),
                           "--underlyings", "AAA", "BBB", "CCC",
                           "--emit-root", str(self.emit)])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(len(report["windows"]), 14)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            rc2 = cli_main(["windows-publish", "--emit-root", str(self.emit),
                            "--out", str(self.root / "backend")])
        self.assertEqual(rc2, 0)
        results = json.loads(out2.getvalue())
        self.assertEqual(len(results), 14)
        self.assertTrue(all(r["status"] == "published" for r in results))


if __name__ == "__main__":
    unittest.main()
