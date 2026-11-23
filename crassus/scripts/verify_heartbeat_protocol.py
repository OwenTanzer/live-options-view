#!/usr/bin/env python3
"""Verify the supervisor heartbeat protocol end to end (#41 / B1 regression).

Production restarted every ~70 seconds after PR205 because `run_cycle` sent a
`key_health` diagnostic down the supervisor pipe. `Progress.accept` implements a
strict three-event protocol and raises on anything else; the supervisor turns
that into `supervisor_failure` and exits nonzero, so the diagnostic became a
restart loop with `completed_cycle_count=0`.

This suite drives the real producer, the real OS pipe and the real consumer:

* a Runner cycle writes `cycle_started`/`cycle_completed` to an actual pipe and
  those records are fed to `Progress.accept`, which must accept them and count a
  completed cycle;
* key-health emissions during that cycle produce ordinary log events and write
  nothing at all to the pipe;
* `report_cycle` refuses a non-protocol name instead of writing it;
* the strict consumer still rejects unknown events, malformed sequences and
  out-of-order records;
* the watchdog threshold is unchanged (`2 * interval`).

`supervise()` itself needs Linux `prctl`, so this suite exercises the same pipe
and consumer without spawning a supervised child; the Linux-only end-to-end run
stays in `verify_reliability.py`.

    python scripts/verify_heartbeat_protocol.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

from crassus import supervisor  # noqa: E402
from crassus.audit import DecisionLedger  # noqa: E402
from crassus.key_health import KeyHealthConfig, KeyHealthMonitor  # noqa: E402
from crassus.runner import Runner  # noqa: E402

if sys.platform == "win32":  # POSIX directory fsync has no meaning here
    import crassus.audit as _audit
    import crassus.durability as _durability

    _durability.sync_directory = lambda path: None
    _audit.sync_directory = lambda path: None

PROTOCOL_EVENTS = ("cycle_started", "cycle_completed", "cycle_failed")


def _scratch_root() -> Path:
    candidate = Path(os.environ.get("CRASSUS_TEST_TMPDIR") or ROOT.parent / ".test-tmp") / "heartbeat"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tmpdir() -> Path:
    base = _scratch_root()
    for attempt in range(100):
        candidate = base / f"run-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not create a scratch directory under {base}")


class _Pipe:
    """A real anonymous pipe standing in for the supervisor's channel."""

    def __init__(self, tmp: Path):
        self.read_fd, self.write_fd = os.pipe()
        self._saved = os.environ.get(supervisor.HEARTBEAT_FD)
        os.environ[supervisor.HEARTBEAT_FD] = str(self.write_fd)
        self.tmp = tmp

    def read_records(self) -> list[dict]:
        """Return every complete record written so far."""
        payload = os.read(self.read_fd, 65536)
        return [json.loads(line) for line in payload.decode().splitlines() if line.strip()]

    def close(self):
        if self._saved is None:
            os.environ.pop(supervisor.HEARTBEAT_FD, None)
        else:
            os.environ[supervisor.HEARTBEAT_FD] = self._saved
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _monitor(tmp: Path, emit) -> KeyHealthMonitor:
    return KeyHealthMonitor(
        key="test-key",
        config=KeyHealthConfig(interval_s=900.0, probe_timeout_s=15.0, cycle_interval_s=300.0),
        probe=lambda: {"status": "accepted", "http_status": 400},
        state_path=tmp / "state" / "key_health.json",
        now_s=time.monotonic,
        now_wall=time.time,
        now_iso=lambda: "2026-11-09T20:00:00Z",
        emit=emit,
    )


def _runner(tmp: Path, monitor: KeyHealthMonitor) -> Runner:
    runner = object.__new__(Runner)
    runner.cycle_count = 0
    runner.ledger = DecisionLedger(tmp / "logs")
    runner.accounts = []
    runner.snapshots = type("Snapshots", (), {"read": staticmethod(lambda: None)})()
    runner._stop = threading.Event()
    runner.stop_file = tmp / "STOP"
    runner.key_health = monitor
    return runner


class ProducerConsumerTests(unittest.TestCase):
    def test_a_cycle_writes_only_protocol_events_and_completes(self):
        tmp = _tmpdir()
        pipe = _Pipe(tmp)
        emitted = []
        try:
            runner = _runner(tmp, _monitor(tmp, lambda name, **fields: emitted.append((name, fields))))
            self.assertTrue(runner.run_cycle())
            records = pipe.read_records()
        finally:
            pipe.close()

        self.assertEqual([r["event"] for r in records], ["cycle_started", "cycle_completed"])
        # The key-health diagnostic was emitted as a log event...
        self.assertEqual([name for name, _ in emitted], ["key_health_ok"])
        health, fields = emitted[0]
        self.assertEqual(fields["state"], "accepted")
        self.assertIn("observed_interval_s", fields)
        # ...and never touched the supervisor channel.
        self.assertNotIn("key_health", [r["event"] for r in records])

        # The strict consumer accepts both records and counts one cycle.
        progress = supervisor.Progress(300.0, time.monotonic())
        for record in records:
            progress.accept(record, time.monotonic())
        self.assertEqual(progress.fields["completed_cycle_count"], 1)
        self.assertEqual(progress.fields["cycle_sequence"], 1)
        self.assertIsNone(progress.fields["last_fatal_error"])

    def test_two_cycles_keep_the_sequence_strict_and_progressing(self):
        tmp = _tmpdir()
        pipe = _Pipe(tmp)
        try:
            runner = _runner(tmp, _monitor(tmp, lambda *a, **k: None))
            self.assertTrue(runner.run_cycle())
            self.assertTrue(runner.run_cycle())
            records = pipe.read_records()
        finally:
            pipe.close()

        self.assertEqual([r["event"] for r in records],
                         ["cycle_started", "cycle_completed"] * 2)
        progress = supervisor.Progress(300.0, time.monotonic())
        for record in records:
            progress.accept(record, time.monotonic())
        self.assertEqual(progress.fields["completed_cycle_count"], 2)

    def test_report_cycle_refuses_a_diagnostic_name(self):
        tmp = _tmpdir()
        pipe = _Pipe(tmp)
        try:
            supervisor.report_cycle("key_health", cycle_sequence=1, state="accepted")
            supervisor.report_cycle("cycle_started", cycle_sequence=1, run_id="r1",
                                    cycle_started_at="2026-11-09T20:00:00Z")
            records = pipe.read_records()
        finally:
            pipe.close()

        self.assertEqual([r["event"] for r in records], ["cycle_started"])


class StrictConsumerTests(unittest.TestCase):
    def _progress(self) -> supervisor.Progress:
        return supervisor.Progress(300.0, now=1000.0)

    def test_unknown_event_is_still_rejected(self):
        progress = self._progress()
        with self.assertRaises(ValueError):
            progress.accept({"event": "key_health", "cycle_sequence": 1}, 1000.0)

    def test_malformed_records_are_still_rejected(self):
        cases = [
            ({"event": "cycle_started"}, 1000.0),                     # no sequence
            ({"event": "cycle_started", "cycle_sequence": "1"}, 1000.0),   # wrong type
            ({"event": "cycle_started", "cycle_sequence": 0}, 1000.0),     # out of range
            ({"event": "cycle_completed", "cycle_sequence": 1}, 1000.0),   # before start
            ({"event": "cycle_failed"}, 1000.0),                      # no sequence
        ]
        for record, now in cases:
            with self.subTest(record=record):
                with self.assertRaises((ValueError, KeyError)):
                    self._progress().accept(record, now)

    def test_out_of_order_records_are_still_rejected(self):
        progress = self._progress()
        progress.accept({"event": "cycle_started", "cycle_sequence": 1,
                         "run_id": "r", "cycle_started_at": "t"}, 1000.0)
        with self.assertRaises(ValueError):  # skipping a start
            progress.accept({"event": "cycle_started", "cycle_sequence": 3,
                             "run_id": "r", "cycle_started_at": "t"}, 1001.0)
        progress.accept({"event": "cycle_completed", "cycle_sequence": 1,
                         "cycle_completed_at": "t", "duration_seconds": 1.0}, 1001.0)
        with self.assertRaises(ValueError):  # duplicate completion
            progress.accept({"event": "cycle_completed", "cycle_sequence": 1,
                             "cycle_completed_at": "t", "duration_seconds": 1.0}, 1002.0)

    def test_watchdog_threshold_is_unchanged(self):
        progress = supervisor.Progress(interval_s=300.0, now=1000.0)
        self.assertFalse(progress.overdue(1299.0))
        self.assertFalse(progress.overdue(1600.0))   # exactly 2 * interval is not overdue
        self.assertTrue(progress.overdue(1601.0))
        progress.accept({"event": "cycle_started", "cycle_sequence": 1,
                         "run_id": "r", "cycle_started_at": "t"}, 1700.0)
        progress.accept({"event": "cycle_completed", "cycle_sequence": 1,
                         "cycle_completed_at": "t", "duration_seconds": 1.0}, 1700.0)
        self.assertFalse(progress.overdue(1900.0))
        self.assertTrue(progress.overdue(2301.0))


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (ProducerConsumerTests, StrictConsumerTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
