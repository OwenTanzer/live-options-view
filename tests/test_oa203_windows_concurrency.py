#!/usr/bin/env python3
"""Same-window concurrency + global reservation races for the F2 publisher.

All interleavings are deterministic one-shot hooks in a local backend
(no threads, no sleeps): a hook fires at one exact publisher step and runs
a second publish (or a crashed publisher's partial write) inline, so the
interleaving is reproduced exactly on every run.

Findings pinned here (see docs/130-window-publisher.md):

- Two publishers, SAME window, SAME revision: safe. Both reserve different
  attempt tokens, write identical bytes, and the pointer is identical.
- Two publishers, SAME window, DIFFERENT revisions: a mixed flat tree CAN
  be produced (the pointer race: B completes fully between A's member
  writes and A's pointer write). The publisher cannot detect it; the
  consumer's pointer check refuses it (market_mcp/window.test.js).
- A crashed publisher's late flat-paths write CAN leave a paths-only mix
  (summary/rankings match the pointer, paths do not). The consumer's
  detail paths-pin refuses it.
- Two publishers, DIFFERENT windows, racing a nearly-full global budget:
  the old check-then-reserve let both pass (over-commit). The fix reserves
  first, then re-verifies against a fresh inventory and releases the token
  when the cap is exceeded, so at most one reservation lands past the cap.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data.options_scanner.windows as w  # noqa: E402
import market_data.options_scanner.windows_outputs as outputs  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from test_oa203_windows import SESSIONS, info, ob  # noqa: E402
from test_oa203_windows_publish import (FakeBackend, NOW, WINDOW_ID, KIND,  # noqa: E402
                                        read_json, sha, write_fixture, xlu_info)

DAYS = ["2026-10-05", "2026-10-06"]
PREFIX = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"


class OneShotHook:
    """Fires `action` once, the first time `match(key)` is true."""

    def __init__(self, match, action):
        self.match = match
        self.action = action
        self.armed = True

    def __call__(self, key):
        if self.armed and self.match(key):
            self.armed = False
            self.action()
            return True
        return False


class HookedBackend(pub.LocalFilesystemBackend):
    """Local backend with one-shot hooks on put / put_if_absent."""

    def __init__(self, root):
        super().__init__(root)
        self.put_hook = None
        self.reserve_hook = None

    def put(self, key, data, content_type):
        if self.put_hook is not None:
            self.put_hook(key)
        return super().put(key, data, content_type)

    def put_if_absent(self, key, data, content_type):
        if self.reserve_hook is not None:
            self.reserve_hook(key)
        return super().put_if_absent(key, data, content_type)


def overnight_rows_variant(xlu_exit_bid=0.60, xlu_exit_ask=0.61):
    """Overnight rows with a different XLU exit: a second revision of the
    same window, same shape (same counts/shortlist) but different values."""
    win = w.overnight_windows(SESSIONS[:2])[0]
    specs = [
        (xlu_info(), [ob("2026-10-05", 15, 55, 0.14, 0.15),
                      ob("2026-10-06", 9, 35, xlu_exit_bid, xlu_exit_ask)]),
        (info(), [ob("2026-10-05", 15, 55, 1.7, 1.8), ob("2026-10-06", 9, 35, 2.4, 2.5)]),
        (info(symbol="BBB261009C00100000", underlying="BBB", root="BBB"),
         [ob("2026-10-05", 15, 55, 1.9, 2.0), ob("2026-10-06", 9, 35, 1.0, 1.1)]),
        (info(symbol="CCC261005C00100000", underlying="CCC", root="CCC", expiration="2026-10-05"),
         [ob("2026-10-05", 15, 55, 1.7, 1.8), ob("2026-10-06", 9, 35, 2.4, 2.5)]),
    ]
    return win, [w.evaluate(win, ci, path, DAYS) for ci, path in specs]


def write_variant(root: Path, exit_bid=0.60, exit_ask=0.61) -> Path:
    """A second revision of the same window: same counts, different rows,
    and a later generated_at so the summary (and therefore the revision)
    differs. Note the revision is summary-content-addressed: rows alone do
    not change it, which is why generated_at must differ here."""
    win, rows = overnight_rows_variant(exit_bid, exit_ask)
    status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in DAYS}
    sources = [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "a" * 64, "bytes": 123}
               for d in DAYS]
    return outputs.write_window(root, win, rows, w.WindowPolicy(), status, sources,
                                "2026-10-06T21:00:00Z",
                                {"mode": "test", "underlyings": ["XLU", "ABC", "BBB", "CCC"]})


def flat_digests(backend, prefix=PREFIX):
    return {name: sha(backend.get(f"{prefix}/{name}")) for name in pub.WINDOW_MEMBERS}


class SameWindowConcurrency(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.emit1 = self.tmp / "emit1"
        self.dir1 = write_fixture(self.emit1)
        self.rev1 = "rev_" + sha((self.dir1 / "summary.json").read_bytes())
        self.emit2 = self.tmp / "emit2"
        self.dir2 = write_variant(self.emit2)
        self.rev2 = "rev_" + sha((self.dir2 / "summary.json").read_bytes())
        self.assertNotEqual(self.rev1, self.rev2)
        # Same shape: the mix is only dangerous because the counts agree.
        s1 = json.loads((self.dir1 / "summary.json").read_text(encoding="utf-8"))
        s2 = json.loads((self.dir2 / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s1["counts"], s2["counts"])
        self.store = self.tmp / "store"
        self.backend = HookedBackend(self.store)

    def tearDown(self):
        self._td.cleanup()

    def test_flat_members_publish_summary_last(self):
        order = []
        orig_put = self.backend.put

        def recording_put(key, data, content_type):
            order.append(key)
            return orig_put(key, data, content_type)

        self.backend.put = recording_put
        result = pub.publish_window(self.emit1, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        flat = [k for k in order if k.startswith(PREFIX + "/")
                and "/" not in k[len(PREFIX) + 1:]]
        self.assertEqual(flat, [f"{PREFIX}/paths.csv.gz", f"{PREFIX}/rankings.csv",
                                f"{PREFIX}/summary.json", f"{PREFIX}/latest.json",
                                f"{PREFIX}/latest-attempt.json"])

    def test_same_revision_overlap_is_safe(self):
        nested = []
        self.backend.put_hook = OneShotHook(
            lambda k: k == f"{PREFIX}/latest.json",
            lambda: nested.append(pub.publish_window(
                self.emit1, KIND, WINDOW_ID, self.backend, now=NOW)))
        first = pub.publish_window(self.emit1, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["status"], "published")
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["revision"], nested[0]["revision"])
        pointer = read_json(self.backend, f"{PREFIX}/latest.json")
        self.assertEqual(pointer["revision"], self.rev1)
        self.assertEqual(pointer["members"], first["members"])
        self.assertEqual(flat_digests(self.backend), first["members"])
        tokens = [k for k in self.backend.list_keys(PREFIX + "/")
                  if "/budget/" in k["key"]]
        self.assertEqual(len(tokens), 2)  # one attempt token per publisher

    def test_pointer_race_produces_detectable_mixed_flat_tree(self):
        """B completes fully between A's member writes and A's pointer write:
        flat members end up rev2, the pointer claims rev1. The publisher
        cannot detect this; the consumer's pointer check must refuse it."""
        nested = []
        self.backend.put_hook = OneShotHook(
            lambda k: k == f"{PREFIX}/latest.json",
            lambda: nested.append(pub.publish_window(
                self.emit2, KIND, WINDOW_ID, self.backend, now=NOW)))
        first = pub.publish_window(self.emit1, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(nested[0]["status"], "published")
        self.assertEqual(first["status"], "published")
        pointer = read_json(self.backend, f"{PREFIX}/latest.json")
        self.assertEqual(pointer["revision"], self.rev1)
        flat = flat_digests(self.backend)
        # The mix: every flat member is rev2 while the pointer claims rev1.
        self.assertEqual(flat, {"summary.json": sha((self.dir2 / "summary.json").read_bytes()),
                                "rankings.csv": sha((self.dir2 / "rankings.csv").read_bytes()),
                                "paths.csv.gz": sha((self.dir2 / "paths.csv.gz").read_bytes())})
        self.assertNotEqual(flat, pointer["members"])

    def test_crashed_publisher_late_paths_write_leaves_paths_only_mix(self):
        """B writes only its first flat member (paths) and crashes; that
        write lands between A's paths and rankings writes. Summary and
        rankings match the pointer, paths do not: only the detail paths-pin
        catches this mix."""
        rev2_paths = (self.dir2 / "paths.csv.gz").read_bytes()

        def crash_after_paths():
            self.backend.put(f"{PREFIX}/paths.csv.gz", rev2_paths,
                             pub._content_type("paths.csv.gz"))

        self.backend.put_hook = OneShotHook(
            lambda k: k == f"{PREFIX}/rankings.csv", lambda: crash_after_paths())
        result = pub.publish_window(self.emit1, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        pointer = read_json(self.backend, f"{PREFIX}/latest.json")
        flat = flat_digests(self.backend)
        self.assertEqual(flat["summary.json"], pointer["members"]["summary.json"])
        self.assertEqual(flat["rankings.csv"], pointer["members"]["rankings.csv"])
        self.assertNotEqual(flat["paths.csv.gz"], pointer["members"]["paths.csv.gz"])
        self.assertEqual(flat["paths.csv.gz"], sha(rev2_paths))


class GlobalBudgetRace(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.emit_a = self.tmp / "emit-a"
        self.dir_a = write_fixture(self.emit_a)
        # A second, different window for the racing publisher.
        self.emit_c = self.tmp / "emit-c"
        win_c = w.overnight_windows(SESSIONS[2:4])[0]
        rows_c = [w.evaluate(win_c, ci, path, ["2026-10-07", "2026-10-08"])
                  for ci, path in [
                      (xlu_info(), [ob("2026-10-07", 15, 55, 0.14, 0.15),
                                    ob("2026-10-08", 9, 35, 0.48, 0.49)]),
                      (info(), [ob("2026-10-07", 15, 55, 1.7, 1.8),
                                ob("2026-10-08", 9, 35, 2.4, 2.5)]),
                      (info(symbol="BBB261009C00100000", underlying="BBB", root="BBB"),
                       [ob("2026-10-07", 15, 55, 1.9, 2.0),
                        ob("2026-10-08", 9, 35, 1.0, 1.1)]),
                      (info(symbol="CCC261005C00100000", underlying="CCC", root="CCC",
                            expiration="2026-10-05"),
                       [ob("2026-10-07", 15, 55, 1.7, 1.8),
                        ob("2026-10-08", 9, 35, 2.4, 2.5)])]]
        status_c = {d: {"final": True, "assessment": "complete", "sweeps": 6}
                    for d in ["2026-10-07", "2026-10-08"]}
        sources_c = [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "a" * 64, "bytes": 123}
                     for d in ["2026-10-07", "2026-10-08"]]
        self.dir_c = outputs.write_window(self.emit_c, win_c, rows_c, w.WindowPolicy(),
                                          status_c, sources_c, "2026-10-08T20:00:00Z",
                                          {"mode": "test",
                                           "underlyings": ["XLU", "ABC", "BBB", "CCC"]})
        self.store = self.tmp / "store"
        self.backend = HookedBackend(self.store)

    def tearDown(self):
        self._td.cleanup()

    def test_two_publishers_racing_a_full_budget_only_one_reserves(self):
        # One 20 MiB reservation fits; two do not.
        budget = pub.WindowBudget(max_bytes=21 * 1024 * 1024)
        nested = []
        self.backend.reserve_hook = OneShotHook(
            lambda k: True,
            lambda: nested.append(pub.publish_window(
                self.emit_c, "overnight", "overnight-2026-10-07_2026-10-08",
                self.backend, budget=budget, now=NOW)))
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            pub.publish_window(self.emit_a, KIND, WINDOW_ID, self.backend,
                               budget=budget, now=NOW)
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["status"], "published")
        # The failed publisher released its token; exactly one reservation
        # (the winner's) remains, so the cap is not over-committed.
        tokens = [o for o in self.backend.list_keys("oa203/windows/") if "/budget/" in o["key"]]
        self.assertEqual(len(tokens), 1)
        self.assertIn(nested[0]["reservation"], [t["key"] for t in tokens])
        self.assertNotIn(f"{PREFIX}/latest.json",
                         [o["key"] for o in self.backend.list_keys(PREFIX + "/")])


class RevisionCounting(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "emit"
        self.dir = write_fixture(self.root)
        self.backend = FakeBackend()

    def tearDown(self):
        self._td.cleanup()

    def _seed_revisions(self, n, prefix=PREFIX):
        for i in range(n):
            self.backend.put(f"{prefix}/rev_{i:064x}/summary.json", b"{}",
                             "application/json")

    def test_same_revision_retries_do_not_consume_revision_slots(self):
        # 15 published revisions + one attempt token for the revision being
        # published: distinct revisions stay 16, so the publish is allowed
        # and reserves the next attempt slot for the same revision.
        self._seed_revisions(15)
        revision = "rev_" + sha((self.dir / "summary.json").read_bytes())
        self.backend.put(f"{PREFIX}/budget/{revision}-1.json", b"{}",
                         "application/json")
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["revision"], revision)
        self.assertEqual(result["attempt"], 2)

    def test_foreign_pending_revision_counts_against_the_cap(self):
        # 15 published revisions + one pending (token-only) foreign revision
        # = 16 distinct revisions; a 17th is refused.
        self._seed_revisions(15)
        self.backend.put(f"{PREFIX}/budget/rev_{'f' * 64}-1.json", b"{}",
                         "application/json")
        with self.assertRaisesRegex(ValueError, "revision budget exhausted"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)


if __name__ == "__main__":
    unittest.main()
