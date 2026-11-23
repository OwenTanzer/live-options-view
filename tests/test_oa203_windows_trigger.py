"""Offline tests for the live window trigger (Task 1).

The hook is disabled by default: flag off means finalize runs exactly
as before. Flag on (in tests, with a fake hook / local backend) means
build + publish runs for the finalized day. Nothing here touches a
provider, R2, or a collector.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.session as session  # noqa: E402
import market_data.options_scanner.windows_live as live  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from market_data.options_scanner.archive import DayArchive  # noqa: E402
from market_data.options_scanner.runtime import Config  # noqa: E402
from test_oa203_windows_live import DAYS, _write_day  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
NOW_PLUS_5 = datetime(2026, 10, 9, 21, 5, tzinfo=timezone.utc)


class _FakeArchive:
    def __init__(self, spool, day):
        self.dir = spool / day
        self.day = date.fromisoformat(day)
        self.written = {}

    def write_json(self, name, data):
        self.written[name] = data
        return self.dir / name

    def push(self, name):
        return True


class TriggerGateTests(unittest.TestCase):
    def test_flag_off_is_noop_and_calls_nothing(self):
        calls = []
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(session.WINDOWS_LIVE_ENV, None)
            result = session.maybe_run_windows_after_finalize(
                _FakeArchive(Path("/tmp"), "2026-10-09"),
                {"selected": []}, None, hook=lambda *a: calls.append(a))
        self.assertEqual(result, {"enabled": False})
        self.assertEqual(calls, [])

    def test_flag_values_other_than_1_are_off(self):
        for value in ("0", "true", "yes", ""):
            with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: value}):
                self.assertFalse(session.windows_live_enabled(), value)
                self.assertFalse(live.windows_live_enabled(), value)

    def test_flag_on_calls_hook_and_records_result(self):
        calls = []
        archive = _FakeArchive(Path("/tmp"), "2026-10-09")

        def hook(spool_dir, day, universe):
            calls.append((str(spool_dir), day.isoformat(), universe))
            return {"enabled": True, "day": day.isoformat(), "ok": True}

        with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: "1"}):
            result = session.maybe_run_windows_after_finalize(
                archive, {"selected": [{"underlying": "AAA"}]}, None, hook=hook)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["ok"])
        self.assertEqual(archive.written["windows_live.json"], result)

    def test_hook_failure_never_unfinalizes(self):
        def boom(*a):
            raise RuntimeError("boom")

        with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: "1"}):
            result = session.maybe_run_windows_after_finalize(
                _FakeArchive(Path("/tmp"), "2026-10-09"),
                {"selected": []}, None, hook=boom)
        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("RuntimeError", result["error"])


class FinalizeHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _finalize(self, *, hook=None, env_on=False):
        day = date(2026, 10, 5)
        archive = DayArchive(self.root, day, "oa203/scanner", upload=False)
        archive.dir.mkdir(parents=True, exist_ok=True)
        (archive.dir / "universe.json").write_text(
            '{"trade_date": "2026-10-05", "selected": []}', encoding="utf-8")
        client = SimpleNamespace(stats={})
        cfg = Config.from_env()
        env = {session.WINDOWS_LIVE_ENV: "1"} if env_on else {}
        with mock.patch.dict(os.environ, env):
            if not env_on:
                os.environ.pop(session.WINDOWS_LIVE_ENV, None)
            return session.finalize(archive, client, cfg, {"selected": []},
                                    None, None, backfill_enabled=False,
                                    windows_hook=hook)

    def test_finalize_flag_off_unchanged(self):
        with mock.patch.object(session, "maybe_run_windows_after_finalize",
                               return_value={"enabled": False}) as spy:
            summary = self._finalize()
        self.assertTrue(summary["final"])
        self.assertEqual(summary["windows_live"], {"enabled": False})
        spy.assert_called_once()

    def test_finalize_flag_on_records_hook_result(self):
        calls = []

        def hook(spool_dir, day, universe):
            calls.append(day.isoformat())
            return {"enabled": True, "day": day.isoformat()}

        summary = self._finalize(hook=hook, env_on=True)
        self.assertTrue(summary["final"])
        self.assertEqual(summary["windows_live"], {"enabled": True, "day": "2026-10-05"})
        self.assertEqual(calls, ["2026-10-05"])


class RunAfterFinalizeTests(unittest.TestCase):
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

    def test_end_to_end_build_and_publish_local(self):
        backend_root = self.root / "backend"
        report = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["cycle"], "2026-10-09")
        self.assertEqual(report["day"], "2026-10-09")
        self.assertEqual(report["days"], DAYS)
        self.assertEqual(report["build"]["universe_scope"], "full_universe")
        self.assertEqual(len(report["build"]["windows"]), 14)
        self.assertTrue(all(r["status"] == "published" for r in report["publish"]))
        self.assertEqual(len(report["publish"]), 14)
        # A successfully published cycle leaves no accumulated emit behind.
        self.assertTrue(report["cleanup"]["removed"])
        self.assertFalse(Path(report["emit_root"]).exists())
        first_revisions = {r["window_id"]: r["revision"] for r in report["publish"]}
        # The cycle manifest records exactly which windows this cycle built.
        # (The manifest is returned in the report: a successfully published
        # cycle's emit root is removed by the hook, so cycle.json is gone.)
        manifest = report["manifest"]
        self.assertEqual(manifest["cycle"], "2026-10-09")
        self.assertEqual(manifest["days"], DAYS)
        self.assertEqual(len(manifest["windows"]), 14)
        # Re-running the same cycle with unchanged inputs is idempotent
        # even when the wall clock advances: each window's summary bytes
        # are a function of that window's own inputs only (stable
        # per-window generated_at from its exit-session close, plus
        # window-scoped scope and rows), so the rebuild is byte-identical
        # and the publisher returns already_published with the same
        # revisions -- no fresh revisions, no re-upload.
        again = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW_PLUS_5)
        self.assertTrue(all(r["status"] == "already_published" for r in again["publish"]))
        self.assertEqual(len(again["publish"]), 14)
        self.assertEqual({r["window_id"]: r["revision"] for r in again["publish"]},
                         first_revisions)

    def test_changed_window_new_revision_only_for_that_window(self):
        import shutil
        import test_oa203_windows_live as live_tests
        backend_root = self.root / "backend"
        report = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW)
        first = {r["window_id"]: r["revision"] for r in report["publish"]}
        # Change only day 2026-10-09's AAA mid (0.55 -> 0.90) and rebuild that day.
        mids = live_tests.MIDS["AAA261009C00100000"]
        old = mids[4]
        mids[4] = 0.90
        try:
            shutil.rmtree(self.spool / "2026-10-09")
            _write_day(self.spool, "2026-10-09")
        finally:
            mids[4] = old
        again = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW)
        by_id = {r["window_id"]: r for r in again["publish"]}
        self.assertEqual(len(by_id), 14)
        # Windows whose sessions include 2026-10-09: 3 overnight/sessions
        # kinds touching the day + the 3 weekly kinds spanning the week.
        # (Build-report sessions are day strings; the emit tree is gone
        # after a successful publish, so read the report, not the disk.)
        touching = {w["window_id"] for w in again["build"]["windows"]
                    if "2026-10-09" in w["sessions"]}
        self.assertEqual(len(touching), 6)
        for window_id, r in by_id.items():
            if window_id in touching:
                self.assertEqual(r["status"], "published", window_id)
                self.assertNotEqual(r["revision"], first[window_id], window_id)
            else:
                self.assertEqual(r["status"], "already_published", window_id)
                self.assertEqual(r["revision"], first[window_id], window_id)

    def test_stale_window_planted_in_cycle_root_not_published(self):
        import shutil
        # Build (not the hook: the hook removes the emit root after a
        # successful publish, so plant the stale window in the build's
        # own emit tree and publish it explicitly).
        emit = self.root / "emit-stale"
        report = live.build_windows([self.spool / d for d in DAYS],
                                    {"AAA", "BBB", "CCC"}, emit)
        window_ids = {(w["kind"], w["window_id"]) for w in report["windows"]}
        self.assertEqual(len(window_ids), 14)
        # Plant a valid-looking stale window inside the emit root: a copy
        # of a real window re-labelled to an older window id.
        base = emit / "oa203" / "windows" / "oa203-windows-v1"
        src = base / "overnight" / "overnight-2026-10-05_2026-10-06"
        dst = base / "overnight" / "overnight-2026-10-01_2026-10-02"
        shutil.copytree(src, dst)
        summary_path = dst / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["window_id"] = "overnight-2026-10-01_2026-10-02"
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        backend_root = self.root / "backend"
        results = live.publish_windows(
            emit, pub.LocalFilesystemBackend(backend_root),
            window_ids=window_ids, now=NOW)
        self.assertEqual(len(results), 14)
        self.assertNotIn("overnight-2026-10-01_2026-10-02",
                         {r["window_id"] for r in results})
        self.assertFalse((backend_root / "oa203" / "windows" / "oa203-windows-v1"
                          / "overnight" / "overnight-2026-10-01_2026-10-02").exists())

    def test_publish_windows_explicit_list_refuses_missing(self):
        emit = self.root / "emit-list"
        live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"}, emit)
        backend = pub.LocalFilesystemBackend(self.root / "backend-list")
        with self.assertRaises(ValueError):
            live.publish_windows(emit, backend, window_ids={
                ("overnight", "overnight-2026-10-05_2026-10-06"),
                ("overnight", "overnight-2099-01-01_2099-01-02")}, now=NOW)
        # Nothing was published: the missing-window check runs before any upload.
        self.assertFalse((self.root / "backend-list").exists())

    def test_finalized_day_dirs_includes_partial_day(self):
        import shutil
        shutil.rmtree(self.spool / "2026-10-08")
        _write_day(self.spool, "2026-10-08", assessment="partial")
        dirs = live.finalized_day_dirs(self.spool, through_day=date(2026, 10, 9))
        self.assertEqual([d.name for d in dirs], DAYS)
        # A non-final day is skipped, never silently built.
        (self.spool / "2026-10-11").mkdir()
        (self.spool / "2026-10-11" / "summary.json").write_text(
            '{"final": false}', encoding="utf-8")
        dirs = live.finalized_day_dirs(self.spool, through_day=date(2026, 10, 11))
        self.assertNotIn("2026-10-11", [d.name for d in dirs])


if __name__ == "__main__":
    unittest.main()
