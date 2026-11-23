"""Offline retry tests for the F2 window products (retry fix).

A finalize retry, or a next-day cycle overlapping the same windows,
must NOT revise unchanged window products. The released publisher /
consumer contract is unchanged (rev_<sha256(summary.json)> and member
digests); the producer writes stable canonical per-window summary
bytes, so an unchanged window rebuilds byte-identically and the
publisher returns already_published with no fresh revision, no
re-upload, and no attempt token consumed. Genuine source changes still
revise exactly the windows they touch. Nothing here touches a
provider, R2, or a collector.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows_live as live  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
import test_oa203_windows_live as live_tests  # noqa: E402
from test_oa203_windows_live import BOUNDS, DAYS, _write_day, _write_rotation_day  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
NOW_PLUS_5 = datetime(2026, 10, 9, 21, 5, tzinfo=timezone.utc)
NOW_PLUS_10 = datetime(2026, 10, 9, 21, 10, tzinfo=timezone.utc)

# Monday 2026-10-12 (Columbus Day, an XNYS trading day), 13:30-20:00 UTC.
BOUNDS["2026-10-12"] = (1791811800000, 1791835200000)

OCT12_UNIVERSE = {"selected": [{"underlying": u, "expiration": "2026-10-09"}
                               for u in ("AAA", "BBB", "DDD")]}


def _attempts(backend_root):
    """{window_id: latest-attempt.json bytes} for every published window."""
    out = {}
    base = Path(backend_root) / "oa203" / "windows" / "oa203-windows-v1"
    for kind_dir in sorted(base.iterdir()):
        for win_dir in sorted(kind_dir.iterdir()):
            ledger = win_dir / "latest-attempt.json"
            if ledger.is_file():
                out[win_dir.name] = ledger.read_bytes()
    return out


def _touching(report, day):
    """Window ids whose summary sessions include ``day`` (from the build report).

    The hook deletes a successfully published cycle's emit root, so the
    emit tree is gone by the time a test reads it; the build report in
    the return value carries the same window/session data.
    """
    return {w["window_id"] for w in report["build"]["windows"] if day in w["sessions"]}


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in DAYS:
            _write_day(self.spool, day)
        self.universe = {"selected": [{"underlying": u, "expiration": "2026-10-09"}
                                       for u in ("AAA", "BBB", "CCC")]}
        self.backend_root = self.root / "backend"
        self.emit_root = self.root / "emit"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, day, universe, now):
        return live.run_after_finalize(
            self.spool, day, universe,
            backend=pub.LocalFilesystemBackend(self.backend_root),
            emit_root=self.emit_root, now=now)

    def _revisions(self, report):
        return {r["window_id"]: r["revision"] for r in report["publish"]}

    def test_same_cycle_retry_advancing_wall_clock(self):
        # Chej's repro: same unchanged finalized Oct 9 inputs, same
        # backend/emit root, first NOW then NOW+5min. Before the fix all
        # 14 windows received new revisions; after it, zero.
        first = self._run("2026-10-09", self.universe, NOW)
        self.assertEqual(len(first["publish"]), 14)
        self.assertTrue(all(r["status"] == "published" for r in first["publish"]))
        attempts_before = _attempts(self.backend_root)
        again = self._run("2026-10-09", self.universe, NOW_PLUS_5)
        self.assertEqual(len(again["publish"]), 14)
        self.assertTrue(all(r["status"] == "already_published" for r in again["publish"]),
                        [r for r in again["publish"] if r["status"] != "already_published"])
        self.assertEqual(self._revisions(again), self._revisions(first))
        # No attempt token consumed by the idempotent retry: the attempt
        # ledgers are byte-identical.
        self.assertEqual(_attempts(self.backend_root), attempts_before)
        # And a third run at a still later clock is equally idempotent.
        third = self._run("2026-10-09", self.universe, NOW_PLUS_10)
        self.assertTrue(all(r["status"] == "already_published" for r in third["publish"]))
        self.assertEqual(self._revisions(third), self._revisions(first))

    def test_next_day_overlapping_windows(self):
        first = self._run("2026-10-09", self.universe, NOW)
        first_revs = self._revisions(first)
        # New day Oct 12: AAA/BBB continue, plus a NEW underlying DDD
        # with rows. DDD has no observations in any pre-Oct-12 window,
        # so it must not leak rows or scope into them.
        _write_rotation_day(self.spool, "2026-10-12", ["AAA", "BBB", "DDD"],
                            {"AAA": 0.60, "BBB": 0.70, "DDD": 0.40})
        nxt = self._run("2026-10-12", OCT12_UNIVERSE, NOW_PLUS_5)
        self.assertEqual(len(nxt["publish"]), 17)
        by_id = {r["window_id"]: r for r in nxt["publish"]}
        new_ids = {"overnight-2026-10-09_2026-10-12",
                   "sessions2-2026-10-09_2026-10-12",
                   "sessions3-2026-10-08_2026-10-12"}
        self.assertEqual({r["window_id"] for r in nxt["publish"]
                          if r["status"] == "published"}, new_ids)
        for window_id, rev in first_revs.items():
            self.assertEqual(by_id[window_id]["status"], "already_published", window_id)
            self.assertEqual(by_id[window_id]["revision"], rev, window_id)

    def test_genuine_change_revises_only_touched_windows(self):
        first = self._run("2026-10-09", self.universe, NOW)
        first_revs = self._revisions(first)
        # Change only day 2026-10-09's AAA mid (0.55 -> 0.90) and retry
        # at a later wall clock: exactly the 6 windows whose sessions
        # include 2026-10-09 revise; the other 8 stay already_published.
        import shutil
        mids = live_tests.MIDS["AAA261009C00100000"]
        old = mids[4]
        mids[4] = 0.90
        try:
            shutil.rmtree(self.spool / "2026-10-09")
            _write_day(self.spool, "2026-10-09")
        finally:
            mids[4] = old
        again = self._run("2026-10-09", self.universe, NOW_PLUS_5)
        by_id = {r["window_id"]: r for r in again["publish"]}
        self.assertEqual(len(by_id), 14)
        touching = _touching(again, "2026-10-09")
        self.assertEqual(len(touching), 6)
        for window_id, r in by_id.items():
            if window_id in touching:
                self.assertEqual(r["status"], "published", window_id)
                self.assertNotEqual(r["revision"], first_revs[window_id], window_id)
                # The new revision is within the 3-attempt budget: its
                # first attempt is attempt 1.
                ledger = json.loads((self.backend_root / "oa203" / "windows"
                                     / "oa203-windows-v1" / r["kind"] / window_id
                                     / "latest-attempt.json").read_text(encoding="utf-8"))
                self.assertEqual(ledger["attempt"], 1, window_id)
                self.assertEqual(ledger["revision"], r["revision"], window_id)
            else:
                self.assertEqual(r["status"], "already_published", window_id)
                self.assertEqual(r["revision"], first_revs[window_id], window_id)

    def test_unrelated_rotation_does_not_revise_untouched_windows(self):
        first = self._run("2026-10-09", self.universe, NOW)
        first_revs = self._revisions(first)
        # Rotate the Oct 5 universe: add DDD to its selected list, with
        # NO rows. Windows whose sessions exclude Oct 5 must stay
        # already_published; windows touching Oct 5 revise only because
        # their declared selected universe changed.
        universe_path = self.spool / "2026-10-05" / "universe.json"
        universe = json.loads(universe_path.read_text(encoding="utf-8"))
        universe["selected"].append({"underlying": "DDD", "expiration": "2026-10-09",
                                     "additional_expiration": None})
        universe_path.write_text(json.dumps(universe) + "\n", encoding="utf-8")
        again = self._run("2026-10-09", self.universe, NOW_PLUS_5)
        by_id = {r["window_id"]: r for r in again["publish"]}
        touching = _touching(again, "2026-10-05")
        self.assertEqual(len(touching), 6)
        for window_id, r in by_id.items():
            if window_id in touching:
                self.assertEqual(r["status"], "published", window_id)
            else:
                self.assertEqual(r["status"], "already_published", window_id)
                self.assertEqual(r["revision"], first_revs[window_id], window_id)


if __name__ == "__main__":
    unittest.main()
