#!/usr/bin/env python3
"""Hermetic lifecycle and real-process failure injection; never uses accounts."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.sentiment import RedditSentimentReader
from crassus.supervisor import Progress, supervise, memory_snapshot, proc_visible
from crassus.sentiment import _release_browser


class Reliability(unittest.TestCase):
    def test_continuous_cli_supervises_before_opening_trading_state(self):
        from crassus.runner import main
        with patch.dict(os.environ, {}, clear=True), \
             patch("crassus.runner.supervise", return_value=75) as supervisor, \
             patch("crassus.runner.load_accounts") as accounts, \
             patch("crassus.runner.configure_logging"):
            self.assertEqual(main(["--interval", "45", "--dry-run"]), 75)
        accounts.assert_not_called()
        self.assertEqual(supervisor.call_args.args[1], 45)
        self.assertEqual(supervisor.call_args.args[0][-3:], ["--interval", "45", "--dry-run"])

    def test_start_events_do_not_extend_deadline(self):
        progress = Progress(300, 0)
        progress.accept(dict(event="cycle_started", cycle_sequence=1,
                             run_id="test", cycle_started_at="first"), 599)
        self.assertFalse(progress.overdue(600))
        self.assertTrue(progress.overdue(600.001))
        self.assertIsNone(progress.fields["last_cycle_completed_at"])

    def test_completion_moves_deadline_once(self):
        progress = Progress(300, 0)
        progress.accept(dict(event="cycle_started", cycle_sequence=1,
                             run_id="test", cycle_started_at="first"), 10)
        record = dict(event="cycle_completed", cycle_sequence=1,
                      cycle_completed_at="done", duration_seconds=5)
        progress.accept(record, 15)
        self.assertFalse(progress.overdue(615))
        self.assertTrue(progress.overdue(616))
        with self.assertRaises(ValueError):
            progress.accept(record, 616)

    def test_resources_closed_on_success_error_timeout_and_cancellation(self):
        for failure in (None, RuntimeError("fetch"), TimeoutError("timeout"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                reader = RedditSentimentReader(session_factory=Mock, analyzer_factory=Mock)
                playwright, browser, context = Mock(), Mock(), Mock()
                def collect():
                    reader._playwright, reader._browser, reader._browser_context = playwright, browser, context
                    if failure is not None:
                        raise failure
                    return iter([])
                reader._collect_texts = collect
                if failure is None:
                    reader.read()
                    reader.read()  # Cache hit must not re-open the browser.
                else:
                    with self.assertRaises(type(failure)):
                        reader.read()
                context.close.assert_called_once()
                browser.close.assert_called_once()
                playwright.stop.assert_called_once()
                self.assertIsNone(reader._playwright)
                self.assertIsNone(reader._browser)
                self.assertIsNone(reader._browser_context)

    def test_cleanup_errors_do_not_leave_other_resources_open(self):
        reader = RedditSentimentReader()
        reader._browser_context, reader._browser, reader._playwright = Mock(), Mock(), Mock()
        reader._browser_context.close.side_effect = RuntimeError("already dead")
        browser, playwright = reader._browser, reader._playwright
        reader._close_browser()
        browser.close.assert_called_once()
        playwright.stop.assert_called_once()

    def test_repeated_reads_release_all_resources(self):
        reader = RedditSentimentReader(session_factory=Mock, analyzer_factory=Mock)
        live = set()
        calls = []
        class Resource:
            def __init__(self):
                live.add(id(self))
            def close(self):
                live.remove(id(self))
            stop = close
        def collect():
            reader._playwright, reader._browser, reader._browser_context = Resource(), Resource(), Resource()
            calls.append(1)
            return iter([])
        reader._collect_texts = collect
        for _ in range(200):
            reader.read(force=True)
            self.assertEqual(live, set())
        self.assertEqual(len(calls), 200)

    def run_child(self, source, interval=.15):
        with self.assertLogs("crassus", logging.INFO) as captured:
            result = supervise([sys.executable, "-c", source], interval,
                               poll_s=.01, shutdown_grace_s=.05, memory_interval_s=.05)
        return result, [json.loads(r.getMessage()) for r in captured.records]

    def test_hung_worker_is_unhealthy_and_exits_nonzero(self):
        started = time.monotonic()
        result, records = self.run_child("import time, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        self.assertEqual(result, 75)
        self.assertLess(time.monotonic() - started, 3)
        unhealthy = [r for r in records if r["event"] == "runner_unhealthy"]
        self.assertEqual(unhealthy[-1]["reason"], "missing_completed_cycles")
        self.assertIsNone(unhealthy[-1]["last_cycle_completed_at"])

    def test_fatal_driver_death_cannot_leave_hung_parent_green_and_restart_advances(self):
        # Required driver dies; Python remains alive in a simulated blocked call.
        result, records = self.run_child("import subprocess, sys, time; subprocess.run([sys.executable, '-c', 'import os; os._exit(137)']); time.sleep(60)")
        self.assertEqual(result, 75)
        result, records = self.run_child('''
import os, json, time
fd = int(os.environ['_CRASSUS_HEARTBEAT_FD'])
for sequence in range(1, 4):
    os.write(fd, (json.dumps(dict(event='cycle_started', cycle_sequence=sequence, run_id='recovered', cycle_started_at=str(sequence)))+'\\n').encode())
    os.write(fd, (json.dumps(dict(event='cycle_completed', cycle_sequence=sequence, cycle_completed_at=str(sequence), duration_seconds=.01))+'\\n').encode())
    time.sleep(.08)
''')
        self.assertEqual(result, 75)  # Continuous worker must not silently exit.
        health = [r for r in records if r["event"] == "runner_health"]
        self.assertEqual(health[-1]["cycle_sequence"], 3)
        self.assertEqual(health[-1]["last_cycle_completed_at"], "3")

    def test_unexpected_worker_exit_is_unhealthy(self):
        result, records = self.run_child("import os; os._exit(42)")
        self.assertNotEqual(result, 0)
        self.assertTrue(any(r["event"] == "runner_unhealthy" for r in records))

    def test_zero_exit_is_still_unhealthy_for_continuous_worker(self):
        result, records = self.run_child("pass")
        self.assertEqual(result, 75)
        self.assertTrue(any(r["event"] == "runner_unhealthy" for r in records))

    def test_interrupted_cleanup_attempts_remaining_resources(self):
        context, browser, driver = Mock(), Mock(), Mock()
        context.close.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            _release_browser(context, browser, driver)
        browser.close.assert_called_once()
        driver.stop.assert_called_once()

    @unittest.skipUnless(proc_visible(), "sandbox /proc uses a different PID namespace; required in Linux CI")
    def test_detached_browser_is_reaped_after_worker_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = str(Path(tmp) / "browser.pid")
            result, records = self.run_child(f"""
import subprocess, sys
from pathlib import Path
browser = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
Path({pidfile!r}).write_text(str(browser.pid))
""")
            self.assertEqual(result, 75)
            browser_pid = int(Path(pidfile).read_text())
            self.assertFalse(Path(f"/proc/{browser_pid}").exists())

    def test_malformed_heartbeat_fails_closed(self):
        result, records = self.run_child("import os, time; os.write(int(os.environ['_CRASSUS_HEARTBEAT_FD']), b'bad json\\n'); time.sleep(60)")
        self.assertEqual(result, 75)
        self.assertTrue(any(r.get("reason") == "supervisor_failure" for r in records))

    def test_transient_launch_failure_recovers_on_next_read(self):
        resources = lambda: (Mock(), Mock(), Mock())
        factory = Mock(side_effect=[resources(), RuntimeError("transient"), resources()])
        reader = RedditSentimentReader(browser_factory=factory, session_factory=Mock, analyzer_factory=Mock)
        def collect():
            reader._get_browser_context()
            return []
        reader._collect_texts = collect
        reader.read(force=True)
        with self.assertRaises(RuntimeError):
            reader.read(force=True)
        reader.read(force=True)
        self.assertEqual(factory.call_count, 3)

    def test_memory_sample_reports_current_process(self):
        sample = memory_snapshot(os.getpid())
        if not proc_visible():
            self.assertEqual(sample, {"memory_sample_unavailable": True})
            return
        self.assertGreater(sample["worker_rss_bytes"], 0)
        self.assertGreaterEqual(sample["descendant_count"], 0)


if __name__ == "__main__":
    unittest.main()
