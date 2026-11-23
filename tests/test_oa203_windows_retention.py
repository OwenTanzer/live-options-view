"""Offline tests for local-output retention / disk headroom / auto-cleanup.

The scanner's persistent /data volume is 5000 MB with 4190 MB used
(~810 MB free, Chej's evidence, not verified here). A 10-session cycle
builds at most 32 windows (6 weekly + 9 overnight + 9 two-session + 8
three-session), each at most 9 MiB of emit members, so the peak is
288 MiB and the hook requires twice that free before building. These
tests prove the pieces that make ongoing publication bounded: the
headroom pre-check fails closed before a build when the emit volume is
too full; the hook removes a successfully published cycle's emit root
and prunes older verified cycles (never a failed/unpublished cycle's
recovery evidence, never the finalized day archives); and the prune
deletes ONLY cycles whose manifest is present, well-formed and complete
and whose every window is verified published or superseded against the
backend -- never source history, never live/published data, never
through a symlink/junction escape. Nothing here touches a provider,
R2, or a collector.
"""

import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows_live as live  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from test_oa203_windows_live import DAYS, _write_day  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
NOW_PLUS_5 = datetime(2026, 10, 9, 21, 5, tzinfo=timezone.utc)
NOW_PLUS_10 = datetime(2026, 10, 9, 21, 10, tzinfo=timezone.utc)


class HeadroomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in DAYS:
            _write_day(self.spool, day)
        self.universe = {"selected": [{"underlying": u, "expiration": "2026-10-09"}
                                       for u in ("AAA", "BBB", "CCC")]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_refuses_when_emit_volume_too_full(self):
        # Fail closed BEFORE building: no emit dir is created at all.
        emit = self.root / "emit"
        with self.assertRaises(ValueError) as ctx:
            live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                               emit, full_universe=True, min_free_bytes=10 ** 18)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertFalse(emit.exists())

    def test_peak_accounting_is_32_windows(self):
        # A 10-session cycle builds at most 32 windows (6 weekly + 9
        # overnight + 9 two-session + 8 three-session); the headroom bound
        # is twice that peak, and there is no hard cycle-count floor: the
        # byte budget alone decides (keep_cycles defaults to 0).
        self.assertEqual(live.MAX_WINDOWS_PER_CYCLE, 32)
        self.assertEqual(live.MAX_WINDOW_EMIT_BYTES, 9 * 1024 * 1024)
        self.assertEqual(live.MAX_CYCLE_EMIT_BYTES, 32 * 9 * 1024 * 1024)
        self.assertEqual(live.MIN_EMIT_FREE_BYTES, 2 * live.MAX_CYCLE_EMIT_BYTES)
        self.assertEqual(live.DEFAULT_KEEP_CYCLES, 0)
        self.assertEqual(live.DEFAULT_MAX_EMIT_BYTES, 192 * 1024 * 1024)

    def test_build_succeeds_with_zero_threshold(self):
        report = live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                                    self.root / "emit", full_universe=True, min_free_bytes=0)
        self.assertEqual(len(report["windows"]), 14)

    def test_run_after_finalize_headroom_fail_closed(self):
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.root / "backend"),
                emit_root=self.root / "emit", now=NOW, min_free_bytes=10 ** 18)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertFalse((self.root / "emit").exists())

    def test_empty_and_missing_spool_fail_closed(self):
        empty = self.root / "empty-spool"
        empty.mkdir()
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(empty, "2026-10-09", self.universe,
                                    backend=pub.LocalFilesystemBackend(self.root / "backend"),
                                    emit_root=self.root / "emit", now=NOW)
        self.assertIn("no finalized day archives", str(ctx.exception))
        with self.assertRaises(FileNotFoundError):
            live.run_after_finalize(self.root / "no-such-spool", "2026-10-09", self.universe,
                                    backend=pub.LocalFilesystemBackend(self.root / "backend"),
                                    emit_root=self.root / "emit", now=NOW)


class PruneTests(unittest.TestCase):
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
        # Three real cycles (Oct 7, 8, 9), each publishing to the same
        # backend at an advancing wall clock, so the Oct 7 cycle's windows
        # are superseded by the Oct 9 publications. Built with build +
        # manifest + publish directly, NOT through the hook: the hook
        # removes a successfully published cycle's emit root, so going
        # through it would leave nothing to prune.
        self.backend = pub.LocalFilesystemBackend(self.backend_root)
        for day, now in (("2026-10-07", NOW), ("2026-10-08", NOW_PLUS_5),
                         ("2026-10-09", NOW_PLUS_10)):
            self._make_cycle(day, now)

    def _make_cycle(self, day, now, *, emit_root=None, backend=None):
        """Build + manifest + publish one cycle, keeping its emit root."""
        emit_root = self.emit_root if emit_root is None else emit_root
        backend = self.backend if backend is None else backend
        emit = emit_root / day
        report = live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                                    emit, full_universe=True, generated_at=now, min_free_bytes=0)
        window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
        (emit / "cycle.json").write_text(json.dumps({
            "cycle": day, "day": day, "days": DAYS,
            "underlyings": ["AAA", "BBB", "CCC"], "universe_scope": "full_universe",
            "generated_at": report["generated_at"],
            "windows": [[k, w_id] for k, w_id in window_ids],
        }) + "\n", encoding="utf-8")
        live.publish_windows(emit, backend, window_ids=set(window_ids), now=now)
        return report

    def tearDown(self):
        self.tmp.cleanup()

    def _cycle(self, day):
        return self.emit_root / day

    def test_prune_dry_run_lists_but_deletes_nothing(self):
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=2, max_emit_bytes=0, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["pruned"], ["2026-10-07"])
        self.assertEqual(report["kept"], ["2026-10-08", "2026-10-09"])
        self.assertTrue(self._cycle("2026-10-07").exists())
        for day in DAYS:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_prune_deletes_only_verified_old_cycle(self):
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=2, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], ["2026-10-07"])
        self.assertFalse(self._cycle("2026-10-07").exists())
        self.assertTrue(self._cycle("2026-10-08").exists())
        self.assertTrue(self._cycle("2026-10-09").exists())
        # Day archives untouched; published pointers untouched.
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)
        pointer = json.loads((self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-08_2026-10-09"
                              / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(pointer["revision"].startswith("rev_"))

    def test_prune_keep_cycles_is_recency_preference_not_floor(self):
        # keep_cycles protects only the NEWEST N cycles even when the byte
        # budget is 0; it is NOT a hard floor. A floor of 10 cycle dirs
        # cannot fit the headroom at full-universe scale, so the byte
        # budget alone decides and the report shows the over-budget state
        # instead of silently exceeding it.
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=1, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], ["2026-10-07", "2026-10-08"])
        self.assertEqual(report["kept"], ["2026-10-09"])
        self.assertTrue(report["bytes_after"] > report["max_emit_bytes"])
        self.assertFalse(self._cycle("2026-10-07").exists())
        self.assertFalse(self._cycle("2026-10-08").exists())
        self.assertTrue(self._cycle("2026-10-09").exists())

    def test_prune_skips_unpublished_cycle(self):
        # A cycle built but never published: its emit is the only copy.
        # Verified against a FRESH backend (no pointers at all), so every
        # window in it is unpublished.
        empty_backend = pub.LocalFilesystemBackend(self.root / "backend-empty")
        unpublished = self.root / "emit-unpub" / "2026-10-09"
        # build_windows writes the oa203 tree directly under the emit root,
        # so the cycle dir needs its cycle.json manifest for the prune to see it.
        # The manifest lists EVERY window the cycle built: a manifest that
        # omits a window is not evidence about that window, so the skip
        # below is genuinely about publication, not about an incomplete list.
        report = live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                                    unpublished, full_universe=True, min_free_bytes=0)
        window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
        (unpublished / "cycle.json").write_text(json.dumps({
            "cycle": "2026-10-09", "day": "2026-10-09", "days": DAYS,
            "underlyings": ["AAA", "BBB", "CCC"], "universe_scope": "full_universe",
            "generated_at": report["generated_at"],
            "windows": [[k, w_id] for k, w_id in window_ids],
        }) + "\n", encoding="utf-8")
        report = live.prune_windows_emit(self.root / "emit-unpub", empty_backend,
                                         keep_cycles=0, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], [])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("not published", report["skipped"][0]["reason"])
        self.assertTrue(unpublished.exists())

    def test_prune_never_touches_non_cycle_entries(self):
        stray_dir = self.emit_root / "notes"
        stray_dir.mkdir()
        (stray_dir / "cycle.json").write_text("{}\n", encoding="utf-8")
        (self.emit_root / "stray.txt").write_text("keep me\n", encoding="utf-8")
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=0, max_emit_bytes=0, dry_run=False)
        self.assertTrue(stray_dir.exists())
        self.assertTrue((self.emit_root / "stray.txt").is_file())
        self.assertNotIn("notes", report["pruned"])
        self.assertNotIn("stray.txt", report["pruned"])


class PruneValidationTests(unittest.TestCase):
    """Exact, fail-closed prune evidence (Repair 3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in DAYS:
            _write_day(self.spool, day)
        self.backend_root = self.root / "backend"
        self.backend = pub.LocalFilesystemBackend(self.backend_root)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_cycle(self, day, now, *, emit_root, backend=None):
        backend = self.backend if backend is None else backend
        emit = emit_root / day
        report = live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                                    emit, full_universe=True, generated_at=now, min_free_bytes=0)
        window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
        (emit / "cycle.json").write_text(json.dumps({
            "cycle": day, "day": day, "days": DAYS,
            "underlyings": ["AAA", "BBB", "CCC"], "universe_scope": "full_universe",
            "generated_at": report["generated_at"],
            "windows": [[k, w_id] for k, w_id in window_ids],
        }) + "\n", encoding="utf-8")
        live.publish_windows(emit, backend, window_ids=set(window_ids), now=now)
        return report

    def test_prune_superseded_cycle_prunable_only_with_verified_members(self):
        # Cycle A (Oct 7) publishes rev R1. Change the Oct 7 day, rebuild
        # the same cycle: windows touching Oct 7 get new revisions R2 and
        # the pointer moves to R2 with a later published_at. Cycle A is
        # then superseded -- but only because R2's members verify in the
        # backend, which this test checks directly for one window.
        emit_a = self.root / "emit-a"
        emit_b = self.root / "emit-b"
        self._make_cycle("2026-10-07", NOW, emit_root=emit_a)
        import test_oa203_windows_live as lt
        old_mid = lt.MIDS["AAA261009C00100000"][2]
        try:
            lt.MIDS["AAA261009C00100000"][2] = old_mid + 1.0
            shutil.rmtree(self.spool / "2026-10-07")
            _write_day(self.spool, "2026-10-07")
            self._make_cycle("2026-10-07", NOW_PLUS_10, emit_root=emit_b)
        finally:
            lt.MIDS["AAA261009C00100000"][2] = old_mid
            shutil.rmtree(self.spool / "2026-10-07")
            _write_day(self.spool, "2026-10-07")
        pointer = json.loads((self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-06_2026-10-07"
                              / "latest.json").read_text(encoding="utf-8"))
        local_summary = (emit_a / "2026-10-07" / "oa203" / "windows" / "oa203-windows-v1"
                         / "overnight" / "overnight-2026-10-06_2026-10-07"
                         / "summary.json").read_bytes()
        self.assertNotEqual(pointer["revision"],
                            "rev_" + hashlib.sha256(local_summary).hexdigest())
        self.assertGreater(pointer["published_at"], NOW.isoformat())
        ok, reason = live._cycle_prunable(emit_a / "2026-10-07", self.backend)
        self.assertTrue(ok, reason)

    def test_prune_newer_unverified_pointer_does_not_qualify(self):
        emit = self.root / "emit"
        self._make_cycle("2026-10-09", NOW, emit_root=emit)
        cycle = emit / "2026-10-09"
        # Overwrite one window's pointer with a newer revision whose
        # members do not exist in the backend: a newer pointer alone is
        # not proof of durable acceptance.
        win_dir = (self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                   / "overnight" / "overnight-2026-10-05_2026-10-06")
        pointer = json.loads((win_dir / "latest.json").read_text(encoding="utf-8"))
        pointer["revision"] = "rev_" + "f" * 64
        pointer["members"] = {m: "a" * 64 for m in pub.WINDOW_MEMBERS}
        pointer["published_at"] = NOW_PLUS_10.isoformat()
        (win_dir / "latest.json").write_text(json.dumps(pointer) + "\n", encoding="utf-8")
        ok, reason = live._cycle_prunable(cycle, self.backend)
        self.assertFalse(ok)
        self.assertIn("not verified", reason)
        report = live.prune_windows_emit(emit, self.backend, keep_cycles=0,
                                         max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], [])
        self.assertTrue(cycle.exists())

    def test_prune_member_digest_mismatch_does_not_qualify(self):
        emit = self.root / "emit"
        self._make_cycle("2026-10-09", NOW, emit_root=emit)
        cycle = emit / "2026-10-09"
        rankings = (cycle / "oa203" / "windows" / "oa203-windows-v1"
                    / "overnight" / "overnight-2026-10-05_2026-10-06" / "rankings.csv")
        with rankings.open("ab") as fh:
            fh.write(b"# tampered\n")
        ok, reason = live._cycle_prunable(cycle, self.backend)
        self.assertFalse(ok)
        self.assertIn("differ", reason)

    def test_prune_manifest_variants_qualify_nothing(self):
        emit = self.root / "emit"
        self._make_cycle("2026-10-09", NOW, emit_root=emit)
        cycle = emit / "2026-10-09"
        good = json.loads((cycle / "cycle.json").read_text(encoding="utf-8"))
        variants = {
            "windows missing": {k: v for k, v in good.items() if k != "windows"},
            "windows empty": {**good, "windows": []},
            "malformed entry": {**good, "windows": [["bogus_kind", "x"]]},
            "generated_at missing": {k: v for k, v in good.items() if k != "generated_at"},
            "cycle/day mismatch": {**good, "cycle": "2026-10-08"},
            "window omitted": {**good, "windows": good["windows"][:-1]},
        }
        for name, manifest in variants.items():
            variant_root = self.root / ("emit-" + name.replace(" ", "-"))
            variant_cycle = variant_root / "2026-10-09"
            shutil.copytree(cycle, variant_cycle)
            (variant_cycle / "cycle.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8")
            report = live.prune_windows_emit(variant_root, self.backend, keep_cycles=0,
                                             max_emit_bytes=0, dry_run=False)
            self.assertEqual(report["pruned"], [], name)
            self.assertEqual(len(report["skipped"]), 1, name)
            self.assertTrue(variant_cycle.exists(), name)
        # A missing manifest is not a cycle at all: nothing is listed,
        # nothing is pruned.
        variant_root = self.root / "emit-no-manifest"
        variant_cycle = variant_root / "2026-10-09"
        shutil.copytree(cycle, variant_cycle)
        (variant_cycle / "cycle.json").unlink()
        report = live.prune_windows_emit(variant_root, self.backend, keep_cycles=0,
                                         max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], [])
        self.assertEqual(report["cycles_before"], 0)
        self.assertTrue(variant_cycle.exists())

    def test_prune_refuses_symlink_escape(self):
        emit = self.root / "emit"
        self._make_cycle("2026-10-09", NOW, emit_root=emit)
        cycle = emit / "2026-10-09"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "cycle.json").write_text("{}\n", encoding="utf-8")
        try:
            os.symlink(outside, emit / "2026-10-10", target_is_directory=True)
            os.symlink(self.spool / "2026-10-09" / "summary.json",
                       cycle / "escape-link")
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        report = live.prune_windows_emit(emit, self.backend, keep_cycles=0,
                                         max_emit_bytes=0, dry_run=False)
        # The linked child is reported as skipped and never followed; the
        # cycle holding a link inside it is refused; the targets survive.
        self.assertNotIn("2026-10-10", report["pruned"])
        self.assertTrue(any(s["cycle"] == "2026-10-10" for s in report["skipped"]))
        self.assertNotIn("2026-10-09", report["pruned"])
        self.assertTrue(cycle.exists())
        self.assertTrue(outside.exists())
        self.assertTrue((self.spool / "2026-10-09" / "summary.json").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_prune_refuses_junction_escape(self):
        # Windows junctions (mklink /J) are directory links that
        # os.path.is_symlink does not see; _is_link must catch them.
        import subprocess
        emit = self.root / "emit-j"
        self._make_cycle("2026-10-09", NOW, emit_root=emit)
        outside = self.root / "outside-j"
        outside.mkdir()
        (outside / "cycle.json").write_text("{}\n", encoding="utf-8")
        proc = subprocess.run(["cmd", "/c", "mklink", "/J",
                               str(emit / "2026-10-10"), str(outside)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            self.skipTest("junctions unavailable on this host: " + proc.stderr.strip())
        self.assertTrue(live._is_link(emit / "2026-10-10"))
        report = live.prune_windows_emit(emit, self.backend, keep_cycles=0,
                                         max_emit_bytes=0, dry_run=False)
        # The junction child is never followed; the real cycle is pruned.
        self.assertNotIn("2026-10-10", report["pruned"])
        self.assertEqual(report["pruned"], ["2026-10-09"])
        self.assertTrue(outside.exists())

    def test_prune_refuses_spool(self):
        with self.assertRaises(ValueError) as ctx:
            live.prune_windows_emit(self.spool, self.backend, dry_run=False)
        self.assertIn("day archives", str(ctx.exception))
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)


class AutoCleanupTests(unittest.TestCase):
    """Automatic derived-output cleanup inside run_after_finalize (Repair 2)."""

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

    def test_run_after_finalize_removes_own_emit_after_publish(self):
        report = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(self.backend_root),
            emit_root=self.emit_root, now=NOW)
        self.assertTrue(report["enabled"])
        self.assertEqual(len(report["publish"]), 14)
        self.assertTrue(report["cleanup"]["removed"])
        self.assertIsNone(report["cleanup"]["error"])
        self.assertFalse(Path(report["emit_root"]).exists())
        # The manifest is returned because cycle.json is gone with the dir.
        self.assertEqual(report["manifest"]["cycle"], "2026-10-09")
        self.assertEqual(len(report["manifest"]["windows"]), 14)
        # Published products and source archives survive.
        pointer = json.loads((self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-05_2026-10-06"
                              / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(pointer["revision"].startswith("rev_"))
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)

    def test_run_after_finalize_keeps_emit_on_publish_failure(self):
        class FailingBackend(pub.LocalFilesystemBackend):
            def put(self, key, data, content_type=None):
                raise RuntimeError("simulated publish failure")

        with self.assertRaises(RuntimeError):
            live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=FailingBackend(self.backend_root),
                emit_root=self.emit_root, now=NOW)
        # The failed cycle's emit is kept as recovery evidence.
        emit = self.emit_root / "2026-10-09"
        self.assertTrue(emit.is_dir())
        self.assertTrue((emit / "cycle.json").is_file())
        self.assertTrue((emit / "oa203" / "windows" / "oa203-windows-v1"
                         / "overnight" / "overnight-2026-10-05_2026-10-06"
                         / "summary.json").is_file())
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)

    @mock.patch.object(pub, "r2_backend")
    def test_run_after_finalize_prunes_older_verified_cycles(self, default_backend):
        # An older verified cycle (Oct 7, built from its own three days,
        # published directly so its emit survives into the hook run) is
        # pruned by the hook after the new Oct 9 publish.
        older_emit = self.emit_root / "2026-10-07"
        report = live.build_windows([self.spool / d for d in DAYS[:3]], {"AAA", "BBB", "CCC"},
                                    older_emit, full_universe=True, generated_at=NOW,
                                    min_free_bytes=0)
        window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
        (older_emit / "cycle.json").write_text(json.dumps({
            "cycle": "2026-10-07", "day": "2026-10-07", "days": DAYS[:3],
            "underlyings": ["AAA", "BBB", "CCC"], "universe_scope": "full_universe",
            "generated_at": report["generated_at"],
            "windows": [[k, w_id] for k, w_id in window_ids],
        }) + "\n", encoding="utf-8")
        live.publish_windows(older_emit, pub.LocalFilesystemBackend(self.backend_root),
                             window_ids=set(window_ids), now=NOW)
        default_backend.return_value = pub.LocalFilesystemBackend(self.backend_root)
        hook = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            emit_root=self.emit_root, now=NOW_PLUS_10)
        default_backend.assert_called_once_with()
        self.assertTrue(hook["cleanup"]["removed"])
        self.assertEqual(hook["cleanup"]["older_prune"]["pruned"], ["2026-10-07"])
        self.assertFalse(older_emit.exists())
        self.assertFalse(Path(hook["emit_root"]).exists())

    def test_low_headroom_cycle_that_published_still_cleans_up(self):
        # Headroom drops after publication, not while the disk-backed
        # builder still requires scratch. Cleanup must not need headroom.
        calls = {"n": 0}
        published = {"done": False}
        original_publish = live.publish_windows

        def publish_then_fill(*args, **kwargs):
            result = original_publish(*args, **kwargs)
            published["done"] = True
            return result

        def fake_disk_usage(path):
            calls["n"] += 1
            free = 0 if published["done"] else live.MIN_EMIT_FREE_BYTES
            return SimpleNamespace(total=5_000_000_000,
                                   used=5_000_000_000 - free, free=free)

        with mock.patch.object(live.shutil, "disk_usage", fake_disk_usage), \
                mock.patch.object(live, "publish_windows", publish_then_fill):
            report = live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.backend_root),
                emit_root=self.emit_root, now=NOW)
        self.assertGreaterEqual(calls["n"], 1)
        self.assertTrue(report["cleanup"]["removed"])
        self.assertFalse(Path(report["emit_root"]).exists())

    def test_run_after_finalize_refuses_one_byte_below_bound(self):
        def fake_disk_usage(path):
            free = live.MIN_EMIT_FREE_BYTES - 1
            return SimpleNamespace(total=5_000_000_000,
                                   used=5_000_000_000 - free, free=free)

        with mock.patch.object(live.shutil, "disk_usage", fake_disk_usage):
            with self.assertRaises(ValueError) as ctx:
                live.run_after_finalize(
                    self.spool, "2026-10-09", self.universe,
                    backend=pub.LocalFilesystemBackend(self.backend_root),
                    emit_root=self.emit_root, now=NOW)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertFalse(self.emit_root.exists())


TEN_DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
            "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
TEN_BOUNDS = {}
for _d in TEN_DAYS:
    _n = (_date.fromisoformat(_d) - _date(2026, 10, 5)).days
    TEN_BOUNDS[_d] = (1791207000000 + 86400000 * _n, 1791230400000 + 86400000 * _n)
TEN_CONTRACTS = [
    ("AAA261016C00100000", "AAA", "call", 100, "2026-10-16", "AAA"),
    ("BBB261016P00200000", "BBB", "put", 200, "2026-10-16", "BBB"),
]
TEN_MIDS = {
    "AAA261016C00100000": [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19],
    "BBB261016P00200000": [0.20, 0.19, 0.18, 0.17, 0.16, 0.15, 0.14, 0.13, 0.12, 0.11],
}


def _write_ten_day(root: Path, day: str) -> Path:
    """One synthetic finalized day for the 10-session fixture (mirrors _write_day)."""
    open_ms, close_ms = TEN_BOUNDS[day]
    idx = TEN_DAYS.index(day)
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": "complete"},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": c[1], "expiration": c[4], "additional_expiration": None}
                     for c in TEN_CONTRACTS],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for symbol, underlying, otype, strike, expiry, r in TEN_CONTRACTS:
            mid = TEN_MIDS[symbol][idx]
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


class TenSessionTests(unittest.TestCase):
    """10-session scale: 32 windows, peak accounting (Repairs 1-2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in TEN_DAYS:
            _write_ten_day(self.spool, day)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ten_sessions_build_32_windows_within_peak(self):
        emit = self.root / "emit"
        report = live.build_windows([self.spool / d for d in TEN_DAYS], {"AAA", "BBB"},
                                    emit, full_universe=True, min_free_bytes=0)
        pairs = [("2026-10-05", "2026-10-06"), ("2026-10-06", "2026-10-07"),
                 ("2026-10-07", "2026-10-08"), ("2026-10-08", "2026-10-09"),
                 ("2026-10-09", "2026-10-12"), ("2026-10-12", "2026-10-13"),
                 ("2026-10-13", "2026-10-14"), ("2026-10-14", "2026-10-15"),
                 ("2026-10-15", "2026-10-16")]
        triples = [("2026-10-05", "2026-10-07"), ("2026-10-06", "2026-10-08"),
                   ("2026-10-07", "2026-10-09"), ("2026-10-08", "2026-10-12"),
                   ("2026-10-09", "2026-10-13"), ("2026-10-12", "2026-10-14"),
                   ("2026-10-13", "2026-10-15"), ("2026-10-14", "2026-10-16")]
        expected = {("weekly_cheapest", "weekly-2026-W41-cheapest"),
                    ("weekly_cheapest", "weekly-2026-W42-cheapest"),
                    ("weekly_fixed", "weekly-2026-W41-fixed"),
                    ("weekly_fixed", "weekly-2026-W42-fixed"),
                    ("weekly_cheapest_best_exit", "weekly-2026-W41-cheapest_best_exit"),
                    ("weekly_cheapest_best_exit", "weekly-2026-W42-cheapest_best_exit")}
        expected |= {("overnight", f"overnight-{a}_{b}") for a, b in pairs}
        expected |= {("sessions_2", f"sessions2-{a}_{b}") for a, b in pairs}
        expected |= {("sessions_3", f"sessions3-{a}_{b}") for a, b in triples}
        got = {(w["kind"], w["window_id"]) for w in report["windows"]}
        self.assertEqual(len(got), 32)
        self.assertEqual(got, expected)
        total = sum(p.stat().st_size for p in emit.rglob("*") if p.is_file())
        self.assertLessEqual(total, live.MAX_CYCLE_EMIT_BYTES)
        self.assertEqual(live.MIN_EMIT_FREE_BYTES, 2 * live.MAX_CYCLE_EMIT_BYTES)


class ScratchEmitTests(unittest.TestCase):
    """Default emit lives on ephemeral scratch, not the archive volume.

    The 576 MiB pre-check now applies to the scratch filesystem; the hook
    writes nothing to /data except the small windows_live.json record in
    the day archive. Scratch is removed on success AND on failure (the
    publisher's attempt ledger plus the deterministic byte-identical
    rebuild are the recovery evidence); an explicit emit_root keeps the
    old behaviour, including keeping the emit on publish failure.
    """

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
        self.scratch_root = self.root / "scratch"
        self.scratch_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_emit_is_scratch_not_spool(self):
        with mock.patch.dict(os.environ, {"OA203_WINDOWS_SCRATCH_DIR": str(self.scratch_root)}):
            report = live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.backend_root), now=NOW)
        self.assertEqual(report["emit_location"], "scratch")
        self.assertEqual(Path(report["scratch_root"]), self.scratch_root.resolve())
        self.assertEqual(len(report["publish"]), 14)
        self.assertTrue(report["cleanup"]["removed"])
        # No windows_emit ever created in the spool; scratch is empty again.
        self.assertFalse((self.spool / "windows_emit").exists())
        self.assertEqual(list(self.scratch_root.iterdir()), [])
        self.assertFalse(Path(report["emit_root"]).exists())
        # Published products and source archives survive.
        pointer = json.loads((self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-05_2026-10-06"
                              / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(pointer["revision"].startswith("rev_"))
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)

    def test_default_scratch_root_is_system_temp(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("OA203_WINDOWS_SCRATCH_DIR", None)
            report = live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.backend_root), now=NOW)
        self.assertEqual(report["emit_location"], "scratch")
        self.assertEqual(Path(report["scratch_root"]), Path(tempfile.gettempdir()).resolve())
        self.assertEqual(len(report["publish"]), 14)

    def test_scratch_inside_spool_refused(self):
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.backend_root),
                scratch_dir=self.spool, now=NOW)
        self.assertIn("inside the spool", str(ctx.exception))
        with mock.patch.dict(os.environ,
                            {"OA203_WINDOWS_SCRATCH_DIR": str(self.spool / "windows_emit")}):
            with self.assertRaises(ValueError) as ctx:
                live.run_after_finalize(
                    self.spool, "2026-10-09", self.universe,
                    backend=pub.LocalFilesystemBackend(self.backend_root), now=NOW)
        self.assertIn("inside the spool", str(ctx.exception))
        self.assertFalse((self.spool / "windows_emit").exists())

    def test_scratch_missing_root_refused(self):
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.backend_root),
                scratch_dir=self.root / "no-such-dir", now=NOW)
        self.assertIn("not an existing directory", str(ctx.exception))

    def test_scratch_removed_on_publish_failure(self):
        class FailingBackend(pub.LocalFilesystemBackend):
            def put(self, key, data, content_type=None):
                raise RuntimeError("simulated publish failure")

        with mock.patch.dict(os.environ, {"OA203_WINDOWS_SCRATCH_DIR": str(self.scratch_root)}):
            with self.assertRaises(RuntimeError):
                live.run_after_finalize(
                    self.spool, "2026-10-09", self.universe,
                    backend=FailingBackend(self.backend_root), now=NOW)
        # The scratch temp dir is gone even though the publish failed (the
        # backend attempt ledger plus the deterministic byte-identical
        # rebuild are the recovery evidence, per the docstring).
        self.assertEqual(list(self.scratch_root.iterdir()), [])
        self.assertFalse((self.spool / "windows_emit").exists())
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)

    def test_scratch_headroom_checked_on_scratch_filesystem(self):
        def fake_disk_usage(path):
            # Plenty free nowhere: the check must probe the scratch
            # filesystem and name scratch in the failure, not the spool's.
            return SimpleNamespace(total=5_000_000_000,
                                   used=5_000_000_000,
                                   free=live.MIN_EMIT_FREE_BYTES - 1)

        with mock.patch.dict(os.environ, {"OA203_WINDOWS_SCRATCH_DIR": str(self.scratch_root)}):
            with mock.patch.object(live.shutil, "disk_usage", fake_disk_usage):
                with self.assertRaises(ValueError) as ctx:
                    live.run_after_finalize(
                        self.spool, "2026-10-09", self.universe,
                        backend=pub.LocalFilesystemBackend(self.backend_root), now=NOW)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertIn("scratch", str(ctx.exception))
        self.assertFalse((self.spool / "windows_emit").exists())
        self.assertEqual(list(self.scratch_root.iterdir()), [])

    def test_build_windows_scratch_flag_names_scratch(self):
        emit = self.root / "emit"
        with self.assertRaises(ValueError) as ctx:
            live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                               emit, full_universe=True, min_free_bytes=10 ** 18,
                               scratch_emit=True)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertIn("scratch", str(ctx.exception))
        self.assertFalse(emit.exists())

    def test_explicit_emit_root_keeps_old_location(self):
        emit_root = self.root / "emit"
        report = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(self.backend_root),
            emit_root=emit_root, now=NOW)
        self.assertEqual(report["emit_location"], "explicit")
        self.assertIsNone(report["scratch_root"])
        self.assertTrue(report["cleanup"]["removed"])
        self.assertFalse(Path(report["emit_root"]).exists())
        self.assertFalse((self.spool / "windows_emit").exists())



if __name__ == "__main__":
    unittest.main()
