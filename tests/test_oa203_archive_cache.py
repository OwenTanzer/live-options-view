"""Offline tests for the bounded local archive-cache lifecycle.

The scanner's finalized day archives mirror to R2 and accumulate on the
local volume with no bound. This lifecycle prunes ONLY older redundant
local copies, and only after FRESH exact remote verification at prune
time: every local artifact is re-read from the remote backend and its
SHA-256 compared to the local bytes, and the DayArchive journal's
recorded digest must agree. A stale journal entry alone proves nothing
(the journal's own upload check is length-only). Any verification
failure means nothing is deleted in that run. The newest 10 finalized
sessions, unfinalized days, pending-upload days, recovery-marked days
and any day holding a symlink/junction are protected, never candidates.
No R2 object is ever deleted or overwritten (asserted with a recording
backend). Deletion requires OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1 AND
--confirm; otherwise the report is dry-run evidence only. A pruned day
restores byte-identical from R2 with restore_day(). Nothing here
touches a provider, R2, or a collector.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.archive_cache as cache  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from market_data.options_scanner import cli  # noqa: E402
from market_data.options_scanner.archive import (  # noqa: E402
    UPLOAD_JOURNAL,
    UPLOAD_RECEIPT,
    DayArchive,
)

PREFIX = "oa203/scanner"
VERIFIED_AT = "2026-10-09T21:00:00+00:00"
DAYS12 = [f"2026-10-{d:02d}" for d in range(5, 17)]  # 2026-10-05 .. 2026-10-16


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(payload) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


class RecordingBackend(pub.LocalFilesystemBackend):
    """Local backend that records delete calls (the lifecycle must never call it)."""

    def __init__(self, root):
        super().__init__(root)
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)
        return super().delete(key)


def _write_cache_day(spool, backend_root, day, *, final=True, with_live=True,
                     with_inprogress=False, pending=False, recovery=False):
    """Write one synthetic finalized day + its remote mirror.

    Local files: summary.json, universe.json, sweeps/manifest.jsonl,
    sweeps/sweep_0001.jsonl.gz, optionally windows_live.json and
    inprogress/ members. The local journal lists every artifact with the
    exact local digest; the remote backend holds identical bytes under
    oa203/scanner/<day>/, plus a remote journal that is stale for
    windows_live.json (pushed after the journal, the real production
    shape). The receipt matches the local journal bytes. With
    pending=True the local universe.json is corrupted after journaling
    (pending_uploads() non-empty). With recovery=True an
    interruptions.jsonl marker is written. Returns the day dir.
    """
    day_dir = spool / day
    (day_dir / "sweeps").mkdir(parents=True)
    artifacts = {}
    artifacts["summary.json"] = _json_bytes({
        "final": final, "trade_date": day, "session_open_ms": 1791207000000,
        "session_close_ms": 1791230400000, "assessment": {"status": "complete"},
    })
    artifacts["universe.json"] = _json_bytes({
        "trade_date": day,
        "selected": [{"underlying": "AAA", "expiration": "2026-10-09"}],
    })
    artifacts["sweeps/manifest.jsonl"] = (
        json.dumps({"sweep": 1, "file": "sweep_0001.jsonl.gz", "rows": 1}) + "\n").encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(b'{"underlying": "AAA", "symbol": "AAA261009C00100000"}\n')
    artifacts["sweeps/sweep_0001.jsonl.gz"] = buf.getvalue()
    if with_live:
        artifacts["windows_live.json"] = _json_bytes(
            {"enabled": True, "cycle": day, "day": day})
    inprogress = {}
    if with_inprogress:
        inprogress["inprogress/sweep_0001.json"] = _json_bytes(
            {"sweep": 1, "revision": "rev_" + "a" * 64})
        rev = "b" * 64
        for name in ("summary.json", "leaderboard.csv", "qualified_ask_bid_v1.csv",
                     "manifest.jsonl"):
            inprogress[f"inprogress/sweep_0001/rev_{rev}/{name}"] = (
                f"{name} for {day}\n".encode())
        inprogress["inprogress/upload-attempts.json"] = _json_bytes(
            {"schema_version": 1, "trade_date": day, "attempts": {"1": 2}})
    for name, data in artifacts.items():
        dest = day_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    for name, data in inprogress.items():
        dest = day_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    if recovery:
        (day_dir / "interruptions.jsonl").write_text(
            json.dumps({"at": VERIFIED_AT, "reason": "test"}) + "\n", encoding="utf-8")
    journal = {name: {"sha256": _sha(data), "bytes": len(data),
                      "key": f"{PREFIX}/{day}/{name}", "verified_at": VERIFIED_AT}
               for name, data in artifacts.items()}
    journal_bytes = _json_bytes(journal)
    (day_dir / UPLOAD_JOURNAL).write_bytes(journal_bytes)
    receipt = {"sha256": _sha(journal_bytes), "bytes": len(journal_bytes),
               "key": f"{PREFIX}/{day}/{UPLOAD_JOURNAL}", "verified_at": VERIFIED_AT}
    (day_dir / UPLOAD_RECEIPT).write_bytes(_json_bytes(receipt))
    remote_day = backend_root / PREFIX / day
    for name, data in list(artifacts.items()) + list(inprogress.items()):
        dest = remote_day / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    remote_journal = {n: e for n, e in journal.items() if n != "windows_live.json"}
    (remote_day / UPLOAD_JOURNAL).write_bytes(_json_bytes(remote_journal))
    if with_inprogress:
        for attempt in (1, 2):
            token = remote_day / "inprogress" / "budget" / f"0001-{attempt}.json"
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_bytes(_json_bytes(
                {"policy": 1, "reserved_at": VERIFIED_AT, "reserved_bytes": 1024}))
    if pending:
        (day_dir / "universe.json").write_bytes(
            artifacts["universe.json"] + b"# corrupted after journaling\n")
    return day_dir


def _remote_path(backend_root, day, name):
    return backend_root / PREFIX / day / name


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        self.backend_root = self.root / "backend"
        self.backend = RecordingBackend(self.backend_root)
        for day in DAYS12:
            _write_cache_day(self.spool, self.backend_root, day)
        os.environ.pop(cache.ARCHIVE_CACHE_PRUNE_ENV, None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop(cache.ARCHIVE_CACHE_PRUNE_ENV, None)

    def _prune(self, **kw):
        kw.setdefault("dry_run", True)
        return cache.prune_archive_cache(self.spool, self.backend, **kw)

    def test_dry_run_lists_prunable_days_and_deletes_nothing(self):
        report = self._prune()
        self.assertEqual(report["decision"], "dry-run")
        self.assertEqual([c["day"] for c in report["candidates"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertTrue(all(p["projected"] for p in report["pruned"]))
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["evidence"]), 2)
        for ev in report["evidence"]:
            self.assertTrue(ev["verified"], ev)
            self.assertEqual({a["name"] for a in ev["artifacts"]},
                             {"summary.json", "universe.json", "sweeps/manifest.jsonl",
                              "sweeps/sweep_0001.jsonl.gz", "windows_live.json"})
            self.assertTrue(all(a["journal_match"] for a in ev["artifacts"]))
            self.assertEqual(ev["remote_journal_missing_entries"], ["windows_live.json"])
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)
        self.assertEqual(self.backend.deleted, [])

    def test_confirm_with_env_deletes_only_verified_older_days(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False)
        self.assertEqual(report["decision"], "pruned")
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertTrue(all(not p["projected"] for p in report["pruned"]))
        self.assertGreater(report["bytes_pruned"], 0)
        self.assertFalse((self.spool / "2026-10-05").exists())
        self.assertFalse((self.spool / "2026-10-06").exists())
        for day in DAYS12[2:]:
            self.assertTrue((self.spool / day).is_dir(), day)
        # Remote untouched: no delete call, pruned days' remote members intact.
        self.assertEqual(self.backend.deleted, [])
        self.assertTrue(_remote_path(self.backend_root, "2026-10-05", "summary.json").is_file())
        self.assertTrue(_remote_path(self.backend_root, "2026-10-05", UPLOAD_JOURNAL).is_file())

    def test_remote_digest_mismatch_refuses_everything(self):
        _remote_path(self.backend_root, "2026-10-05", "summary.json").write_bytes(b"tampered\n")
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue(any("remote digest does not match" in e
                            for err in report["errors"] for e in err["errors"]))
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_remote_missing_member_refuses_everything(self):
        _remote_path(self.backend_root, "2026-10-05", "sweeps/sweep_0001.jsonl.gz").unlink()
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_remote_journal_contradiction_refuses_everything(self):
        remote_journal_path = _remote_path(self.backend_root, "2026-10-05", UPLOAD_JOURNAL)
        journal = json.loads(remote_journal_path.read_text(encoding="utf-8"))
        journal["summary.json"]["sha256"] = "f" * 64
        remote_journal_path.write_bytes(_json_bytes(journal))
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_pending_oldest_day_refuses_run(self):
        shutil.rmtree(self.spool / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05", pending=True)
        archive = DayArchive(self.spool, date(2026, 10, 5), PREFIX, upload=True)
        self.assertEqual(archive.pending_uploads(), ["universe.json"])
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue((self.spool / "2026-10-05").is_dir())
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_pending_newest_day_is_protected_without_blocking(self):
        shutil.rmtree(self.spool / "2026-10-16")
        _write_cache_day(self.spool, self.backend_root, "2026-10-16", pending=True)
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False)
        self.assertEqual(report["decision"], "pruned")
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertTrue((self.spool / "2026-10-16").is_dir())
        self.assertTrue(any(p["day"] == "2026-10-16"
                            and "newest 10" in p["reason"]
                            for p in report["protected"]))

    def test_unfinalized_day_is_protected(self):
        _write_cache_day(self.spool, self.backend_root, "2026-10-19", final=False)
        report = self._prune()
        self.assertEqual([c["day"] for c in report["candidates"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertTrue(any(p["day"] == "2026-10-19" and "not finalized" in p["reason"]
                            for p in report["protected"]))
        self.assertTrue((self.spool / "2026-10-19").is_dir())

    def test_recovery_marked_oldest_day_is_protected(self):
        shutil.rmtree(self.spool / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05", recovery=True)
        report = self._prune()
        # 2026-10-05 is protected; 2026-10-06 is the only candidate
        # (the keep set is the newest 10 finalized sessions, so
        # 2026-10-07 is retained, not a candidate).
        self.assertEqual([c["day"] for c in report["candidates"]],
                         ["2026-10-06"])
        self.assertTrue(any(p["day"] == "2026-10-05" and "recovery" in p["reason"]
                            for p in report["protected"]))
        self.assertTrue((self.spool / "2026-10-05").is_dir())

    def test_symlink_inside_day_protects_it(self):
        day_dir = self.spool / "2026-10-05"
        try:
            os.symlink(self.spool / "2026-10-06" / "summary.json",
                       day_dir / "escape-link")
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        ev = cache.verify_day_remote(day_dir, self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("symlink/junction" in e for e in ev["errors"]))
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue((self.spool / "2026-10-06" / "summary.json").is_file())

    def test_per_run_bounds(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            one = self._prune(dry_run=False, max_days_per_run=1)
        self.assertEqual([p["day"] for p in one["pruned"]], ["2026-10-05"])
        self.assertTrue((self.spool / "2026-10-06").is_dir())
        # A byte cap smaller than one day prunes nothing.
        report = self._prune(dry_run=True, max_bytes_per_run=1)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["pruned"], [])

    def test_env_gate(self):
        with self.assertRaises(ValueError) as ctx:
            self._prune(dry_run=False)
        self.assertIn("OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1", str(ctx.exception))
        # Env set without --confirm is still a dry run: nothing deleted.
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=True)
        self.assertEqual(report["decision"], "dry-run")
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_keep_sessions_below_ten_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._prune(keep_sessions=9)
        self.assertIn("at least the newest 10", str(ctx.exception))

    def test_verify_ledger_mismatch_fails(self):
        shutil.rmtree(self.spool / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05", with_inprogress=True)
        _remote_path(self.backend_root, "2026-10-05",
                     "inprogress/budget/0001-2.json").unlink()
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("budget tokens" in e for e in ev["errors"]))

    def test_verify_build_debris_protects_day(self):
        shutil.rmtree(self.spool / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05", with_inprogress=True)
        (self.spool / "2026-10-05" / "inprogress" / ".build.lock").write_text(
            "stale\n", encoding="utf-8")
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any(".build.lock" in e for e in ev["errors"]))

    def test_verify_unfinalized_and_non_iso(self):
        day_dir = _write_cache_day(self.spool, self.backend_root, "2026-10-19", final=False)
        ev = cache.verify_day_remote(day_dir, self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("not finalized" in e for e in ev["errors"]))
        stray = self.spool / "notes"
        stray.mkdir()
        ev = cache.verify_day_remote(stray, self.backend)
        self.assertFalse(ev["verified"])

    def test_prune_never_touches_non_day_entries(self):
        (self.spool / "notes.txt").write_text("keep me\n", encoding="utf-8")
        (self.spool / "windows_emit").mkdir()
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False)
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertTrue((self.spool / "notes.txt").is_file())
        self.assertTrue((self.spool / "windows_emit").is_dir())

    def test_restore_round_trip(self):
        shutil.rmtree(self.spool / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05", with_inprogress=True)
        before = {p.relative_to(self.spool / "2026-10-05").as_posix(): p.read_bytes()
                  for p in sorted((self.spool / "2026-10-05").rglob("*")) if p.is_file()}
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            pruned = self._prune(dry_run=False)
        self.assertEqual([p["day"] for p in pruned["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        result = cache.restore_day(self.spool, "2026-10-05", self.backend)
        self.assertTrue(result["journal_match"])
        # windows_live.json + inprogress are not in the journal, so
        # this restore is partial by design, not complete.
        self.assertFalse(result["complete"])
        self.assertIn(f"{PREFIX}/2026-10-05/windows_live.json", result["not_restored"])
        self.assertTrue(any("inprogress" in k for k in result["not_restored"]))
        after = {p.relative_to(self.spool / "2026-10-05").as_posix(): p.read_bytes()
                 for p in sorted((self.spool / "2026-10-05").rglob("*")) if p.is_file()}
        # The restored set is exactly the journaled artifacts plus the
        # journal and its fresh receipt; windows_live.json and
        # inprogress/ are reported as not_restored, not silently skipped.
        self.assertEqual(set(after), {
            "summary.json", "universe.json", "sweeps/manifest.jsonl",
            "sweeps/sweep_0001.jsonl.gz", UPLOAD_JOURNAL, UPLOAD_RECEIPT})
        for name in ("summary.json", "universe.json", "sweeps/manifest.jsonl",
                     "sweeps/sweep_0001.jsonl.gz"):
            self.assertEqual(after[name], before[name], name)
        # The restored journal is the remote journal (stale for
        # windows_live.json); the receipt matches the restored bytes.
        restored_journal = json.loads(after[UPLOAD_JOURNAL].decode())
        self.assertNotIn("windows_live.json", restored_journal)
        self.assertEqual(set(restored_journal),
                         {"summary.json", "universe.json", "sweeps/manifest.jsonl",
                          "sweeps/sweep_0001.jsonl.gz"})
        receipt = json.loads(after[UPLOAD_RECEIPT].decode())
        self.assertEqual(receipt["sha256"], _sha(after[UPLOAD_JOURNAL]))
        self.assertEqual(receipt["key"], f"{PREFIX}/2026-10-05/{UPLOAD_JOURNAL}")
        archive = DayArchive(self.spool, date(2026, 10, 5), PREFIX, upload=True)
        self.assertEqual(archive.pending_uploads(), [])
        summary = json.loads((self.spool / "2026-10-05" / "summary.json").read_text())
        self.assertTrue(summary["final"])

    def test_restore_missing_remote_member_raises_and_removes_partial(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            self._prune(dry_run=False)
        _remote_path(self.backend_root, "2026-10-05", "universe.json").unlink()
        with self.assertRaises(ValueError) as ctx:
            cache.restore_day(self.spool, "2026-10-05", self.backend)
        self.assertIn("missing for restore", str(ctx.exception))
        self.assertFalse((self.spool / "2026-10-05").exists())

    def test_restore_digest_mismatch_raises(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            self._prune(dry_run=False)
        _remote_path(self.backend_root, "2026-10-05", "summary.json").write_bytes(b"tampered\n")
        with self.assertRaises(ValueError) as ctx:
            cache.restore_day(self.spool, "2026-10-05", self.backend)
        self.assertIn("digest mismatch", str(ctx.exception))
        self.assertFalse((self.spool / "2026-10-05").exists())

    def test_restore_target_exists_refused(self):
        with self.assertRaises(ValueError) as ctx:
            cache.restore_day(self.spool, "2026-10-05", self.backend)
        self.assertIn("already exists", str(ctx.exception))


class ArchiveCacheCliTests(unittest.TestCase):
    """CLI wiring for archive-cache-prune (thin dispatch over the library)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        self.backend_root = self.root / "backend"
        self.backend = RecordingBackend(self.backend_root)
        for day in DAYS12:
            _write_cache_day(self.spool, self.backend_root, day)
        os.environ.pop(cache.ARCHIVE_CACHE_PRUNE_ENV, None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop(cache.ARCHIVE_CACHE_PRUNE_ENV, None)

    def test_cli_dry_run_reports_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["archive-cache-prune", "--spool", str(self.spool),
                           "--backend-root", str(self.backend_root)])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["decision"], "dry-run")
        self.assertEqual([c["day"] for c in report["candidates"]],
                         ["2026-10-05", "2026-10-06"])
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_cli_confirm_with_env_deletes(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            with contextlib.redirect_stdout(out):
                rc = cli.main(["archive-cache-prune", "--spool", str(self.spool),
                               "--backend-root", str(self.backend_root), "--confirm"])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["decision"], "pruned")
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertFalse((self.spool / "2026-10-05").exists())
        self.assertFalse((self.spool / "2026-10-06").exists())

    def test_cli_confirm_without_env_refuses(self):
        with self.assertRaises(ValueError) as ctx:
            cli.main(["archive-cache-prune", "--spool", str(self.spool),
                      "--backend-root", str(self.backend_root), "--confirm"])
        self.assertIn("OA203_ARCHIVE_CACHE_PRUNE_ENABLED=1", str(ctx.exception))
        for day in DAYS12:
            self.assertTrue((self.spool / day).is_dir(), day)



class HardeningTests(unittest.TestCase):
    """Hard remote budgets, current-day protection, unrecognized files,
    pre-delete recheck, restore completeness. Each failure mode must fail
    closed with nothing deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name) / "spool"
        self.backend_root = Path(self.tmp.name) / "remote"
        self.spool.mkdir()
        self.backend_root.mkdir()
        self.backend = RecordingBackend(self.backend_root)
        for d in DAYS12:
            _write_cache_day(self.spool, self.backend_root, d)

    def tearDown(self):
        self.tmp.cleanup()

    def _prune(self, **kw):
        kw.setdefault("dry_run", True)
        kw.setdefault("today", date(2026, 10, 20))
        return cache.prune_archive_cache(self.spool, self.backend, **kw)

    def test_remote_request_budget_exceeded(self):
        budget = cache.RemoteBudget(max_requests=2, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     budget=budget)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("request budget" in e for e in ev["errors"]),
                        ev["errors"])
        report = self._prune(max_remote_requests=2)
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])
        for d in DAYS12:
            self.assertTrue((self.spool / d).is_dir(), d)

    def test_remote_byte_budget_exceeded(self):
        budget = cache.RemoteBudget(max_requests=512, max_bytes=10,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     budget=budget)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("byte budget" in e for e in ev["errors"]),
                        ev["errors"])
        report = self._prune(max_remote_bytes=10)
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])

    def test_remote_time_budget_exceeded(self):
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=0.000001)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     budget=budget)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("time budget" in e for e in ev["errors"]),
                        ev["errors"])
        report = self._prune(max_verify_seconds=0.000001)
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])

    def test_remote_budget_state_in_dry_run_report(self):
        report = self._prune()
        rb = report["remote_budget"]
        self.assertEqual(rb["max_requests"], cache.DEFAULT_MAX_REMOTE_REQUESTS_PER_RUN)
        self.assertEqual(rb["max_bytes"], cache.DEFAULT_MAX_REMOTE_BYTES_PER_RUN)
        # Two days verified: per day 5 artifact GETs + 1 journal GET
        # (the default fixture has no in-progress members).
        self.assertEqual(rb["requests"], 12)
        self.assertGreater(rb["bytes_read"], 0)

    def test_current_day_protected_even_when_final(self):
        # The newest fixture day is "today": protected by date alone.
        ev = cache.verify_day_remote(self.spool / "2026-10-16", self.backend,
                                     today=date(2026, 10, 16))
        self.assertFalse(ev["verified"])
        self.assertTrue(any("never prunable" in e for e in ev["errors"]),
                        ev["errors"])
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False, today=date(2026, 10, 16))
        self.assertEqual(report["decision"], "pruned")
        self.assertEqual([p["day"] for p in report["pruned"]], ["2026-10-05"])
        self.assertTrue((self.spool / "2026-10-16").is_dir())
        self.assertTrue(any(p["day"] == "2026-10-16" and "current day" in p["reason"]
                            for p in report["protected"]))

    def test_current_day_malformed_final_still_protected(self):
        # Final=true but otherwise malformed (universe.json missing):
        # the date alone protects it; it never reaches verification.
        (self.spool / "2026-10-16" / "universe.json").unlink()
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False, today=date(2026, 10, 16))
        self.assertTrue(any(p["day"] == "2026-10-16" and "current day" in p["reason"]
                            for p in report["protected"]))
        self.assertTrue((self.spool / "2026-10-16").is_dir())
        # The malformed current day blocks nothing: the other old day
        # still prunes.
        self.assertEqual([p["day"] for p in report["pruned"]], ["2026-10-05"])

    def test_unrecognized_tmp_file_aborts(self):
        # Chej's repro: operator-recovery.tmp is skipped by
        # DayArchive.artifacts() and must still abort the day.
        (self.spool / "2026-10-05" / "operator-recovery.tmp").write_text(
            "not uploaded\n")
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("unrecognized" in e for e in ev["errors"]),
                        ev["errors"])
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue((self.spool / "2026-10-05" / "operator-recovery.tmp").is_file())

    def test_unrecognized_tmp_at_every_level_aborts(self):
        (self.spool / "2026-10-05" / "operator-recovery.tmp").write_text("x\n")
        (self.spool / "2026-10-05" / "sweeps" / "stray.tmp").write_text("x\n")
        deep = self.spool / "2026-10-05" / "sweeps" / "sub" / "deep.tmp"
        deep.parent.mkdir(parents=True)
        deep.write_text("x\n")
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend)
        self.assertFalse(ev["verified"])
        self.assertEqual(len(ev["errors"]), 1)
        self.assertIn("operator-recovery.tmp", ev["errors"][0])
        self.assertIn("sweeps/stray.tmp", ev["errors"][0])
        self.assertIn("sweeps/sub/deep.tmp", ev["errors"][0])
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])
        for d in DAYS12:
            self.assertTrue((self.spool / d).is_dir(), d)

    def test_unjournaled_plain_file_still_caught_by_pending(self):
        # Ordinary unjournaled operator-recovery-notes.txt is already
        # protected by pending_uploads (not the inventory check).
        (self.spool / "2026-10-05" / "operator-recovery-notes.txt").write_text("x\n")
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend)
        self.assertFalse(ev["verified"])
        self.assertTrue(any("unuploaded" in e for e in ev["errors"]),
                        ev["errors"])

    def test_recheck_catches_modification_addition_removal(self):
        day_dir = self.spool / "2026-10-05"
        ev = cache.verify_day_remote(day_dir, self.backend)
        self.assertTrue(ev["verified"])
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev), [])
        original = (day_dir / "universe.json").read_bytes()
        (day_dir / "universe.json").write_bytes(b"mutated after verify\n")
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev),
                         ["modified since verification: universe.json"])
        (day_dir / "universe.json").write_bytes(original)
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev), [])
        (day_dir / "added.tmp").write_text("x\n")
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev),
                         ["added since verification: added.tmp"])
        (day_dir / "added.tmp").unlink()
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev), [])
        (day_dir / "universe.json").unlink()
        self.assertEqual(cache._recheck_day_unchanged(day_dir, ev),
                         ["removed since verification: universe.json"])

    def test_prune_aborts_when_day_mutates_after_verify(self):
        real_recheck = cache._recheck_day_unchanged

        def mutating_recheck(day_dir, evidence):
            (day_dir / "universe.json").write_bytes(b"mutated after verify\n")
            return real_recheck(day_dir, evidence)

        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            with mock.patch.object(cache, "_recheck_day_unchanged", mutating_recheck):
                report = self._prune(dry_run=False)
        self.assertTrue(
            report["decision"].startswith("refused: changed since verification"),
            report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue(any("changed since verification" in e
                            for err in report["errors"] for e in err["errors"]))
        for d in DAYS12:
            self.assertTrue((self.spool / d).is_dir(), d)

    def test_restore_complete_when_journal_has_everything(self):
        # A day with no live/inprogress extras: the remote journal lists
        # every remote object, so the restore is complete. Clear the
        # remote day first: the first write left live/in-progress extras
        # in the remote mirror, which would (correctly) make the restore
        # partial.
        shutil.rmtree(self.spool / "2026-10-05")
        shutil.rmtree(self.backend_root / PREFIX / "2026-10-05")
        _write_cache_day(self.spool, self.backend_root, "2026-10-05",
                         with_live=False)
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            self._prune(dry_run=False)
        result = cache.restore_day(self.spool, "2026-10-05", self.backend)
        self.assertTrue(result["complete"])
        self.assertEqual(result["not_restored"], [])
        self.assertEqual(result["restored_from_journal"],
                         ["summary.json", "sweeps/manifest.jsonl",
                          "sweeps/sweep_0001.jsonl.gz", "universe.json"])


class KeepPinTests(unittest.TestCase):
    def test_keep_sessions_equals_builder_lookback(self):
        import market_data.options_scanner.windows_live as live
        self.assertEqual(cache.KEEP_FINALIZED_SESSIONS, live.MAX_SESSIONS)
        self.assertEqual(cache.KEEP_FINALIZED_SESSIONS, 10)


class _FakeBody:
    """StreamingBody stand-in: records bytes read and close()."""

    def __init__(self, data, content_length=None):
        self._data = data
        self._pos = 0
        self.content_length = len(data) if content_length is None else content_length
        self.bytes_read = 0
        self.closed = False
        self.key = None

    def read(self, n=-1):
        if self.closed:
            raise ValueError("read of closed body")
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class StreamingStubBackend:
    """Dict backend with a boto3-style get_stream; records every call."""

    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.get_calls = 0
        self.get_stream_calls = 0
        self.bodies = []
        self.lied_lengths = {}

    def put(self, key, data, content_type=None):
        self.objects[key] = bytes(data)
        return {"sha256": _sha(bytes(data)), "bytes": len(data), "key": key}

    def get(self, key):
        self.get_calls += 1
        try:
            return self.objects[key]
        except KeyError:
            raise KeyError(key) from None

    def get_stream(self, key):
        self.get_stream_calls += 1
        try:
            data = self.objects[key]
        except KeyError:
            raise KeyError(key) from None
        body = _FakeBody(data, self.lied_lengths.get(key))
        body.key = key
        self.bodies.append(body)
        return body, body.content_length

    def put_if_absent(self, key, data, content_type=None):
        if key in self.objects:
            return False
        self.put(key, data, content_type)
        return True

    def list_keys(self, prefix):
        return [{"key": k, "size": len(v), "last_modified": None}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


def _populate_streaming_stub(stub, backend_root):
    for path in sorted(p for p in backend_root.rglob("*") if p.is_file()):
        stub.put(path.relative_to(backend_root).as_posix(), path.read_bytes())


class BoundsTests(unittest.TestCase):
    """The bounds gap: enforcement BEFORE / WHILE consuming, not after.
    An oversized remote body must be refused without being fully read
    into memory, and a preflight must refuse before any GET is issued."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name) / "spool"
        self.backend_root = Path(self.tmp.name) / "remote"
        self.spool.mkdir()
        self.backend_root.mkdir()
        self.stub = StreamingStubBackend()
        for d in DAYS12:
            _write_cache_day(self.spool, self.backend_root, d)
        _populate_streaming_stub(self.stub, self.backend_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_streaming_content_length_refused_before_read(self):
        key = f"{PREFIX}/2026-10-05/universe.json"
        self.stub.objects[key] = b"x" * (10 * 1024 * 1024)
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.stub,
                                     budget=budget, today=date(2026, 10, 20))
        self.assertFalse(ev["verified"])
        self.assertTrue(any("byte budget" in e for e in ev["errors"]), ev["errors"])
        self.assertEqual(ev["bounded_read"], "streaming")
        body = next(b for b in self.stub.bodies if b.key == key)
        self.assertEqual(body.bytes_read, 0)  # refused before any body byte
        self.assertTrue(body.closed)
        for b in self.stub.bodies:
            self.assertTrue(b.closed)

    def test_streaming_lying_content_length_stops_at_bound(self):
        key = f"{PREFIX}/2026-10-05/universe.json"
        self.stub.objects[key] = b"x" * (10 * 1024 * 1024)
        self.stub.lied_lengths[key] = 100  # server claims 100 bytes
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.stub,
                                     budget=budget, today=date(2026, 10, 20))
        self.assertFalse(ev["verified"])
        self.assertTrue(any("byte budget" in e for e in ev["errors"]), ev["errors"])
        body = next(b for b in self.stub.bodies if b.key == key)
        self.assertLessEqual(body.bytes_read, 1024 * 1024 + 1)
        self.assertTrue(body.closed)

    def test_request_preflight_refuses_without_issuing_get(self):
        self.stub.put(f"{PREFIX}/2026-10-05/summary.json", b"{}")
        budget = cache.RemoteBudget(max_requests=1, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        budget.charge_request()  # exhaust the single request
        with self.assertRaises(cache.RemoteBudgetExceeded):
            cache._bounded_get(self.stub, f"{PREFIX}/2026-10-05/summary.json",
                               budget)
        self.assertEqual(self.stub.get_calls, 0)
        self.assertEqual(self.stub.get_stream_calls, 0)

    def test_time_preflight_refuses_without_issuing_get(self):
        self.stub.put(f"{PREFIX}/2026-10-05/summary.json", b"{}")
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=0.000001)
        time.sleep(0.01)
        with self.assertRaises(cache.RemoteBudgetExceeded):
            cache._bounded_get(self.stub, f"{PREFIX}/2026-10-05/summary.json",
                               budget)
        self.assertEqual(self.stub.get_calls, 0)
        self.assertEqual(self.stub.get_stream_calls, 0)

    def test_prune_refuses_oversized_without_deletion(self):
        key = f"{PREFIX}/2026-10-05/universe.json"
        self.stub.objects[key] = b"x" * (10 * 1024 * 1024)
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = cache.prune_archive_cache(
                self.spool, self.stub, dry_run=False, today=date(2026, 10, 20),
                max_remote_bytes=1024 * 1024)
        self.assertTrue(report["decision"].startswith("refused"), report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertEqual(self.stub.deleted, [])
        for d in DAYS12:
            self.assertTrue((self.spool / d).is_dir(), d)
        body = next(b for b in self.stub.bodies if b.key == key)
        self.assertEqual(body.bytes_read, 0)
        self.assertTrue(body.closed)

    def test_bounded_read_label(self):
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.stub,
                                     today=date(2026, 10, 20))
        self.assertTrue(ev["verified"], ev["errors"])
        self.assertEqual(ev["bounded_read"], "streaming")
        ev2 = cache.verify_day_remote(self.spool / "2026-10-06",
                                      RecordingBackend(self.backend_root),
                                      today=date(2026, 10, 20))
        self.assertTrue(ev2["verified"], ev2["errors"])
        self.assertEqual(ev2["bounded_read"], "accounting-only")



class StubR2Backend:
    """In-memory StorageBackend with R2-like semantics (KeyError on missing)."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put(self, key, data, content_type=None):
        self.objects[key] = bytes(data)
        return {"sha256": _sha(bytes(data)), "bytes": len(data), "key": key}

    def get(self, key):
        try:
            return self.objects[key]
        except KeyError:
            raise KeyError(key) from None

    def put_if_absent(self, key, data, content_type=None):
        if key in self.objects:
            return False
        self.put(key, data, content_type)
        return True

    def list_keys(self, prefix):
        return [{"key": k, "size": len(v), "last_modified": None}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


def _load_stub(stub, backend_root):
    """Copy a LocalFilesystemBackend tree into an in-memory stub backend."""
    for path in sorted(p for p in backend_root.rglob("*") if p.is_file()):
        stub.put(path.relative_to(backend_root).as_posix(),
                 path.read_bytes())


class R2PathTests(unittest.TestCase):
    """The R2 path goes through the SAME verification code as the local
    path -- no separate weaker R2 path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name) / "spool"
        self.backend_root = Path(self.tmp.name) / "remote"
        self.spool.mkdir()
        self.backend_root.mkdir()
        self.stub = StubR2Backend()
        for d in DAYS12:
            _write_cache_day(self.spool, self.backend_root, d)
        _load_stub(self.stub, self.backend_root)

    def tearDown(self):
        self.tmp.cleanup()

    def _prune(self, **kw):
        kw.setdefault("dry_run", True)
        kw.setdefault("today", date(2026, 10, 20))
        return cache.prune_archive_cache(self.spool, self.stub, **kw)

    def test_r2_stub_verify_and_prune(self):
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.stub,
                                     today=date(2026, 10, 20))
        self.assertTrue(ev["verified"], ev["errors"])
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._prune(dry_run=False)
        self.assertEqual(report["decision"], "pruned")
        self.assertEqual([p["day"] for p in report["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertFalse((self.spool / "2026-10-05").exists())
        self.assertEqual(self.stub.deleted, [])

    def test_r2_stub_verification_failure_refuses(self):
        # Corrupt one remote member: fresh GET+hash must catch it.
        self.stub.objects[f"{PREFIX}/2026-10-05/universe.json"] = b"corrupt\n"
        report = self._prune()
        self.assertTrue(report["decision"].startswith("refused"),
                        report["decision"])
        self.assertEqual(report["pruned"], [])
        self.assertTrue((self.spool / "2026-10-05").is_dir())

    def test_cli_r2_uses_r2_backend(self):
        with mock.patch.object(cli, "r2_backend", return_value=self.stub) as mk:
            rc = cli.main(["archive-cache-prune", "--spool", str(self.spool),
                           "--r2"])
            self.assertEqual(rc, 0)
            self.assertEqual(mk.call_count, 1)

    def test_cli_r2_destructive_uses_stub(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            with mock.patch.object(cli, "r2_backend", return_value=self.stub):
                rc = cli.main(["archive-cache-prune", "--spool", str(self.spool),
                               "--r2", "--confirm"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.spool / "2026-10-05").exists())
        self.assertEqual(self.stub.deleted, [])

    def test_cli_local_without_backend_root_errors(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main(["archive-cache-prune", "--spool", str(self.spool)])
        self.assertEqual(cm.exception.code, 2)


HOOK_DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
             "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
             "2026-10-19", "2026-10-20"]
HOOK_EXPIRY = "2026-10-23"
HOOK_CONTRACTS = [("AAA261023C00100000", "AAA", "call", 100, HOOK_EXPIRY, "AAA"),
                  ("BBB261023P00200000", "BBB", "put", 200, HOOK_EXPIRY, "BBB")]
HOOK_MIDS = {"AAA261023C00100000": [round(0.10 + 0.01 * i, 2) for i in range(12)],
             "BBB261023P00200000": [round(0.20 - 0.01 * i, 2) for i in range(12)]}


def _write_hook_day(root: Path, day: str) -> Path:
    """One synthetic finalized day in finalize() shape, loadable by the
    window builder; mirrors test_oa203_windows_retention._write_ten_day
    but parameterized by date so 12 sessions can be built."""
    idx = HOOK_DAYS.index(day)
    n = (date.fromisoformat(day) - date(2026, 10, 5)).days
    open_ms, close_ms = 1791207000000 + 86400000 * n, 1791230400000 + 86400000 * n
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": "complete"},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": c[1], "expiration": c[4], "additional_expiration": None}
                     for c in HOOK_CONTRACTS],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for symbol, underlying, otype, strike, expiry, r in HOOK_CONTRACTS:
            mid = HOOK_MIDS[symbol][idx]
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


def _journalize_day(spool: Path, backend_root: Path, day: str) -> None:
    """Add a correct journal + receipt + remote mirror for an existing
    local day (no windows_live.json omission -- the day has none)."""
    day_dir = spool / day
    archive = DayArchive(spool, date.fromisoformat(day), PREFIX, upload=True)
    journal = {}
    for name in archive.artifacts():
        data = (day_dir / name).read_bytes()
        journal[name] = {"sha256": _sha(data), "bytes": len(data),
                         "key": f"{PREFIX}/{day}/{name}", "verified_at": VERIFIED_AT}
    journal_bytes = _json_bytes(journal)
    (day_dir / UPLOAD_JOURNAL).write_bytes(journal_bytes)
    receipt = {"sha256": _sha(journal_bytes), "bytes": len(journal_bytes),
               "key": f"{PREFIX}/{day}/{UPLOAD_JOURNAL}", "verified_at": VERIFIED_AT}
    (day_dir / UPLOAD_RECEIPT).write_bytes(_json_bytes(receipt))
    remote_day = backend_root / PREFIX / day
    for name in archive.artifacts():
        dest = remote_day / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((day_dir / name).read_bytes())
    (remote_day / UPLOAD_JOURNAL).write_bytes(journal_bytes)


class HookTests(unittest.TestCase):
    """Finalize hook: audit under an explicit flag, dry-run by default,
    destructive only with the second flag, failure isolated from
    publication."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name) / "spool"
        self.backend_root = Path(self.tmp.name) / "remote"
        self.spool.mkdir()
        self.backend_root.mkdir()
        for d in HOOK_DAYS:
            _write_hook_day(self.spool, d)
            _journalize_day(self.spool, self.backend_root, d)
        self.backend = pub.LocalFilesystemBackend(self.backend_root)
        self.universe = {"selected": [{"underlying": u, "expiration": HOOK_EXPIRY}
                                      for u in ("AAA", "BBB")]}
        self.now = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(cache.ARCHIVE_CACHE_AUDIT_ENV, None)
        os.environ.pop(cache.ARCHIVE_CACHE_PRUNE_ENV, None)

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _run_hook(self):
        import market_data.options_scanner.windows_live as live
        return live.run_after_finalize(
            self.spool, "2026-10-20", self.universe,
            backend=self.backend, now=self.now)

    def test_hook_off_by_default(self):
        report = self._run_hook()
        self.assertIsNone(report["lifecycle"])
        self.assertGreater(len(report["publish"]), 0)
        for d in HOOK_DAYS:
            self.assertTrue((self.spool / d).is_dir(), d)

    def test_hook_audit_dry_run_persists_report(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_AUDIT_ENV: "1"}):
            report = self._run_hook()
        lc = report["lifecycle"]
        self.assertIsNotNone(lc)
        self.assertEqual(lc["decision"], "dry-run")
        self.assertEqual([c["day"] for c in lc["candidates"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertGreater(len(report["publish"]), 0)
        report_path = Path(lc["report_path"])
        self.assertEqual(str(report_path),
                         str(self.spool / cache.ARCHIVE_CACHE_REPORTS_DIR / "2026-10-20.json"))
        self.assertTrue(report_path.is_file())
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["decision"], "dry-run")
        self.assertEqual(len(saved["evidence"]), 2)
        # Nothing deleted, publication stands.
        for d in HOOK_DAYS:
            self.assertTrue((self.spool / d).is_dir(), d)

    def test_hook_destructive_prunes_when_both_flags(self):
        with mock.patch.dict(os.environ, {
                cache.ARCHIVE_CACHE_AUDIT_ENV: "1",
                cache.ARCHIVE_CACHE_PRUNE_ENV: "1"}):
            report = self._run_hook()
        lc = report["lifecycle"]
        self.assertEqual(lc["decision"], "pruned")
        self.assertEqual([p["day"] for p in lc["pruned"]],
                         ["2026-10-05", "2026-10-06"])
        self.assertFalse((self.spool / "2026-10-05").exists())
        self.assertFalse((self.spool / "2026-10-06").exists())
        self.assertGreater(len(report["publish"]), 0)

    def test_hook_audit_flag_only_is_dry_run(self):
        # Audit flag on, prune flag off: the hook reports a dry run and
        # deletes nothing, even though pruning would be possible.
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_AUDIT_ENV: "1"}):
            with mock.patch.object(cache, "prune_archive_cache",
                                  wraps=cache.prune_archive_cache) as wrapped:
                report = self._run_hook()
        self.assertEqual(wrapped.call_args.kwargs.get("dry_run"), True)
        self.assertEqual(report["lifecycle"]["decision"], "dry-run")
        self.assertTrue((self.spool / "2026-10-05").is_dir())

    def test_hook_failure_isolated_from_publication(self):
        with mock.patch.dict(os.environ, {cache.ARCHIVE_CACHE_AUDIT_ENV: "1"}):
            with mock.patch.object(cache, "prune_archive_cache",
                                  side_effect=RuntimeError("simulated audit failure")):
                report = self._run_hook()
        self.assertGreater(len(report["publish"]), 0)
        lc = report["lifecycle"]
        self.assertIsNotNone(lc)
        self.assertIn("RuntimeError", str(lc["errors"]))
        for d in HOOK_DAYS:
            self.assertTrue((self.spool / d).is_dir(), d)


class _FakeClientError(Exception):
    """boto3 ClientError stand-in: carries .response['Error']['Code']."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _StrictBody(_FakeBody):
    """A streaming body that REFUSES an unbounded read(): read() with no
    size argument raises, so any unbounded body.read() on the cache
    path fails the test loudly instead of silently succeeding."""

    def read(self, n=-1):
        if n is None or n < 0:
            raise AssertionError("unbounded body.read() called on the cache path")
        return super().read(n)


class FakeS3Client:
    """Stubbed boto3 transport for the REAL R2Backend class."""

    def __init__(self):
        self.objects = {}       # key -> bytes
        self.lied_lengths = {}  # key -> fake ContentLength
        self.get_object_calls = []
        self.list_calls = []
        self.bodies = []

    def get_object(self, Bucket, Key):
        self.get_object_calls.append(Key)
        if Key not in self.objects:
            raise _FakeClientError("NoSuchKey")
        body = _StrictBody(self.objects[Key], self.lied_lengths.get(Key))
        body.key = Key
        self.bodies.append(body)
        return {"Body": body, "ContentLength": body.content_length}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        self.list_calls.append(Prefix)
        contents = [{"Key": k, "Size": len(v), "LastModified": None}
                    for k, v in sorted(self.objects.items()) if k.startswith(Prefix)]
        return {"IsTruncated": False, "Contents": contents}


class RealBackendBoundsTests(unittest.TestCase):
    """The production path: the REAL R2Backend class against a stubbed
    S3 transport. Proves the streaming bound is enforced there -- no
    silent fallback to accounting-only, no unbounded body.read()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self.tmp.name) / "spool"
        self.backend_root = Path(self.tmp.name) / "remote"
        self.spool.mkdir()
        self.backend_root.mkdir()
        self.client = FakeS3Client()
        for d in DAYS12:
            _write_cache_day(self.spool, self.backend_root, d)
        for path in sorted(p for p in self.backend_root.rglob("*") if p.is_file()):
            self.client.objects[path.relative_to(self.backend_root).as_posix()] = (
                path.read_bytes())
        self.backend = pub.R2Backend(self.client, "test-bucket")

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_backend_verify_is_streaming(self):
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     today=date(2026, 10, 20))
        self.assertTrue(ev["verified"], ev["errors"])
        self.assertEqual(ev["bounded_read"], "streaming")
        self.assertGreater(len(self.client.bodies), 0)
        for b in self.client.bodies:
            self.assertTrue(b.closed)

    def test_real_backend_oversized_refused_without_full_read(self):
        key = f"{PREFIX}/2026-10-05/universe.json"
        self.client.objects[key] = b"x" * (10 * 1024 * 1024)
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     budget=budget, today=date(2026, 10, 20))
        self.assertFalse(ev["verified"])
        self.assertTrue(any("byte budget" in e for e in ev["errors"]), ev["errors"])
        self.assertEqual(ev["bounded_read"], "streaming")
        body = next(b for b in self.client.bodies if b.key == key)
        self.assertEqual(body.bytes_read, 0)  # refused before any body byte
        self.assertTrue(body.closed)

    def test_real_backend_lying_length_stops_at_bound(self):
        key = f"{PREFIX}/2026-10-05/universe.json"
        self.client.objects[key] = b"x" * (10 * 1024 * 1024)
        self.client.lied_lengths[key] = 100  # server claims 100 bytes
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        ev = cache.verify_day_remote(self.spool / "2026-10-05", self.backend,
                                     budget=budget, today=date(2026, 10, 20))
        self.assertFalse(ev["verified"])
        self.assertTrue(any("byte budget" in e for e in ev["errors"]), ev["errors"])
        body = next(b for b in self.client.bodies if b.key == key)
        self.assertLessEqual(body.bytes_read, 1024 * 1024 + 1)
        self.assertTrue(body.closed)

    def test_real_backend_preflight_issues_no_get(self):
        budget = cache.RemoteBudget(max_requests=1, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        budget.charge_request()  # exhaust the single request
        with self.assertRaises(cache.RemoteBudgetExceeded):
            cache._bounded_get(self.backend, f"{PREFIX}/2026-10-05/summary.json",
                               budget)
        self.assertEqual(self.client.get_object_calls, [])

    def test_real_backend_missing_key_is_keyerror(self):
        budget = cache.RemoteBudget(max_requests=512, max_bytes=1024 * 1024,
                                    max_seconds=300.0)
        with self.assertRaises(KeyError):
            cache._bounded_get(self.backend, f"{PREFIX}/2026-10-05/nope.json",
                               budget)



if __name__ == "__main__":
    unittest.main()
