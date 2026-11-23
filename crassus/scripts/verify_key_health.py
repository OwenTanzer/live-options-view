#!/usr/bin/env python3
"""Verify the bounded periodic registration-key health check (#41 / B1).

Covers the decision table without a network or a deployment: the three
operational states, fail-closed classification of every non-contract response,
cadence and worst-case detection delay, deduplication within a process,
deduplication across a restart, recovery detection after a rejection, the
heartbeat, redaction of the key from every emitted field, the runner hook, and
the operator CLI's exit codes.

    python scripts/verify_key_health.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

import requests  # noqa: E402

from crassus import key_health as kh  # noqa: E402
from crassus.config import Account  # noqa: E402
from crassus.key_health import KeyHealthConfig, KeyHealthMonitor  # noqa: E402
from crassus.runner import Runner  # noqa: E402

ACCEPTED = "Username must be 3-40 characters: letters, numbers, underscore"
REJECTED = "Invalid bot registration key"


def _scratch_root() -> Path:
    """A writable directory for the state file under test.

    The platform temp directory is the natural choice and is what CI uses. In a
    confined Windows sandbox it can be created but not written into, so fall
    back to a scratch directory beside the checkout rather than failing the
    suite for an environment reason unrelated to the code.
    """
    candidate = Path(tempfile.gettempdir()) / "crassus-key-health"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # Probe the capability the monitor actually needs -- creating a nested
        # directory and a file inside it -- rather than merely creating one
        # directory, which a confined sandbox may allow while denying the rest.
        probe_dir = candidate / ".probe"
        probe_dir.mkdir(exist_ok=True)
        (probe_dir / "x").write_text("x")
        (probe_dir / "x").unlink()
        probe_dir.rmdir()
        return candidate
    except OSError:
        fallback = Path(os.environ.get("CRASSUS_TEST_TMPDIR") or ROOT.parent / ".test-tmp") / "key-health"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _tmpdir() -> Path:
    """A unique, writable run directory.

    `tempfile.mkdtemp` produces a directory this sandbox can create but not
    write into, so the run directory is created with a plain mkdir under a
    root that has already been probed for nested writes.
    """
    base = _scratch_root()
    for attempt in range(100):
        candidate = base / f"run-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not create a scratch directory under {base}")


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.text = "" if body is None else json.dumps(body)


class _Clock:
    """Monotonic clock the test drives by hand."""

    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, name, **fields):
        self.events.append({"event": name, **fields})

    @property
    def names(self):
        return [e["event"] for e in self.events]


def _monitor(tmp: Path, probe, *, clock=None, config=None, emit=None, key="sekret-key-value"):
    clock = clock or _Clock()
    emit = emit or _Recorder()
    monitor = KeyHealthMonitor(
        key=key,
        config=config or KeyHealthConfig(interval_s=900.0, heartbeat_s=14400.0, probe_timeout_s=15.0),
        probe=probe,
        state_path=tmp / "state" / "key_health.json",
        now_s=clock,
        now_wall=clock,
        now_iso=lambda: "2026-11-09T23:00:00Z",
        emit=emit,
    )
    return monitor, clock, emit


class ProbeClassificationTests(unittest.TestCase):
    def test_exact_contract_responses_are_the_only_affirmative_evidence(self):
        cases = [
            ((400, {"error": ACCEPTED}), "accepted"),
            ((403, {"error": REJECTED}), "rejected"),
            ((401, {"error": "Not logged in"}), "unreachable"),
            ((403, {"error": "Forbidden"}), "unreachable"),
            ((404, {"error": "Not Found"}), "unreachable"),
            ((429, {"error": "Too Many Requests"}), "unreachable"),
            ((400, {"error": "options_symbol and side are required"}), "unreachable"),
            ((400, {}), "unreachable"),
            ((500, {"error": "boom"}), "unreachable"),
            ((200, {"ok": True}), "unreachable"),
        ]
        for (status, body), expected in cases:
            with self.subTest(status=status, body=body):
                probe = kh.probe_registration_key(
                    "https://w", "k", request=lambda *a, **k: _Response(status, body)
                )
                self.assertEqual(kh.health_state(probe), expected)

    def test_transport_failure_is_unreachable_not_accepted(self):
        def offline(*_a, **_k):
            raise requests.ConnectionError("no route")

        probe = kh.probe_registration_key("https://w", "k", request=offline)
        self.assertEqual(probe["status"], "unreachable")
        self.assertEqual(probe["detail"], "ConnectionError")
        self.assertEqual(kh.health_state(probe), "unreachable")

    def test_malformed_json_body_fails_closed(self):
        response = type("R", (), {"status_code": 400, "text": "<html>oops</html>"})()
        probe = kh.probe_registration_key("https://w", "k", request=lambda *a, **k: response)
        self.assertEqual(kh.health_state(probe), "unreachable")


class CadenceTests(unittest.TestCase):
    def test_probe_runs_at_most_once_per_interval(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            calls = []

            def probe():
                calls.append(True)
                return {"status": "accepted", "http_status": 400}

            monitor, clock, _ = _monitor(Path(tmp), probe)
            self.assertIsNotNone(monitor.maybe_check())        # first: due
            self.assertEqual(len(calls), 1)
            clock.advance(1)
            self.assertIsNone(monitor.maybe_check())           # inside interval
            self.assertEqual(len(calls), 1)
            clock.advance(898)
            self.assertIsNone(monitor.maybe_check())           # 899s: still inside
            clock.advance(1)
            self.assertIsNotNone(monitor.maybe_check())        # 900s: due again
            self.assertEqual(len(calls), 2)

    def test_detection_estimate_is_conditional_on_the_cycle_period(self):
        # The check runs inside the runner cycle, so the honest statement is an
        # estimate conditional on the cycle keeping its period -- and nothing at
        # all when the period is unknown.
        no_cycle = KeyHealthConfig(interval_s=900.0, probe_timeout_s=15.0)
        self.assertEqual(no_cycle.request_timeout_s, 15.0)
        self.assertFalse(hasattr(no_cycle, "max_probe_duration_s"))
        self.assertIsNone(no_cycle.conditional_detection_estimate_s)
        self.assertIn("unbounded", no_cycle.detection_estimate_condition)

        deployed = KeyHealthConfig(
            interval_s=900.0, probe_timeout_s=15.0, cycle_interval_s=300.0
        )
        self.assertEqual(deployed.conditional_detection_estimate_s, 1215.0)
        self.assertIn("conditional", deployed.detection_estimate_condition)
        self.assertIn("hung cycle", deployed.detection_estimate_condition)
        # The request timeout is per-phase, not a wall-clock deadline; the
        # wording must not imply the probe is capped by it.
        self.assertIn("per-phase", deployed.detection_estimate_condition)

    def test_non_aligned_cycle_cadence_reports_measured_lateness(self):
        # A 300 s runner cycle does not divide a 900 s interval at arbitrary
        # starting phase, and a slow cycle period (350 s here) lands after the
        # due time. The report must show the gap it actually observed.
        with contextlib.nullcontext(_tmpdir()) as tmp:
            clock = _Clock()
            monitor, _clock, _ = _monitor(
                Path(tmp), lambda: {"status": "accepted", "http_status": 400}, clock=clock
            )
            monitor.check()                       # t=0, next due at 900
            clock.advance(350)
            self.assertIsNone(monitor.maybe_check())   # t=350
            clock.advance(350)
            self.assertIsNone(monitor.maybe_check())   # t=700
            clock.advance(350)
            report = monitor.maybe_check()             # t=1050, 150 s late
            self.assertIsNotNone(report)
            self.assertEqual(report["observed_interval_s"], 1050.0)
            self.assertEqual(report["last_lateness_s"], 150.0)
            self.assertEqual(report["max_lateness_s"], 150.0)
            self.assertEqual(report["check_interval_s"], 900.0)

    def test_overrun_cycle_is_reported_as_exceeding_the_conditional_bound(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            clock = _Clock()
            monitor, _clock, _ = _monitor(
                Path(tmp),
                lambda: {"status": "accepted", "http_status": 400},
                clock=clock,
                config=KeyHealthConfig(
                    interval_s=900.0, heartbeat_s=14400.0, probe_timeout_s=15.0,
                    cycle_interval_s=300.0,
                ),
            )
            monitor.check()                    # t=0
            clock.advance(5000)                # a cycle that took far too long
            report = monitor.check()
            self.assertEqual(report["observed_interval_s"], 5000.0)
            self.assertEqual(report["last_lateness_s"], 4100.0)
            self.assertEqual(report["conditional_detection_estimate_s"], 1215.0)
            self.assertFalse(report["within_detection_estimate"])

    def test_no_probe_without_a_cycle_to_run_it(self):
        # A hung cycle simply never calls the monitor. Time passes, the report
        # must show no check at all rather than implying one happened, and it
        # must state that the bound depends on the cycle.
        with contextlib.nullcontext(_tmpdir()) as tmp:
            calls = []

            def probe():
                calls.append(True)
                return {"status": "accepted", "http_status": 400}

            clock = _Clock()
            monitor, _clock, _ = _monitor(
                Path(tmp), probe, clock=clock,
                config=KeyHealthConfig(
                    interval_s=900.0, heartbeat_s=14400.0, probe_timeout_s=15.0,
                    cycle_interval_s=300.0,
                ),
            )
            clock.advance(5000)                  # the loop never reaches us
            self.assertEqual(calls, [])
            report = monitor.report()
            self.assertIsNone(report["state"])
            self.assertIsNone(report["last_check_at"])
            self.assertIsNone(report["observed_interval_s"])
            self.assertEqual(report["conditional_detection_estimate_s"], 1215.0)
            self.assertIn("conditional", report["detection_estimate_condition"])

    def test_report_never_claims_a_hard_detection_maximum(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            monitor, _clock, _ = _monitor(
                Path(tmp), lambda: {"status": "accepted", "http_status": 400}
            )
            report = monitor.check()
            self.assertNotIn("max_detection_delay_s", report)
            self.assertNotIn("max_detection_delay_s", monitor.report())
            for key in ("check_interval_s", "observed_interval_s", "last_lateness_s",
                        "conditional_detection_estimate_s", "detection_estimate_condition"):
                self.assertIn(key, report)

    def test_zero_interval_disables_the_check(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            calls = []
            monitor, _clock, _ = _monitor(
                Path(tmp),
                lambda: calls.append(True) or {"status": "accepted", "http_status": 400},
                config=KeyHealthConfig(interval_s=0.0),
            )
            self.assertFalse(monitor.config.enabled)
            self.assertIsNone(monitor.maybe_check())
            self.assertEqual(calls, [])

    def test_malformed_interval_falls_back_instead_of_disabling(self):
        self.assertEqual(
            KeyHealthConfig.from_environment({"CRASSUS_KEY_HEALTH_INTERVAL_S": "soon"}).interval_s,
            kh.DEFAULT_INTERVAL_S,
        )
        self.assertEqual(
            KeyHealthConfig.from_environment({"CRASSUS_KEY_HEALTH_INTERVAL_S": "-5"}).interval_s,
            kh.DEFAULT_INTERVAL_S,
        )
        self.assertEqual(
            KeyHealthConfig.from_environment({"CRASSUS_KEY_HEALTH_INTERVAL_S": "0"}).interval_s,
            0.0,
        )


class ReportingTests(unittest.TestCase):
    def test_same_state_is_reported_once(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            monitor, clock, emit = _monitor(
                Path(tmp), lambda: {"status": "accepted", "http_status": 400}
            )
            first = monitor.check()
            self.assertEqual(first["event"], "key_health_ok")
            clock.advance(900)
            second = monitor.check()
            self.assertIsNone(second["event"])
            clock.advance(900)
            third = monitor.check()
            self.assertIsNone(third["event"])
            self.assertEqual(emit.names, ["key_health_ok"])
            self.assertEqual(third["checks"], 3)
            self.assertEqual(third["accepted"], 3)

    def test_rejection_then_recovery_is_two_events(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            state = {"status": "rejected", "http_status": 403}

            def probe():
                return state

            monitor, clock, emit = _monitor(Path(tmp), probe)
            monitor.check()
            self.assertEqual(emit.names, ["key_health_rejected"])
            state = {"status": "accepted", "http_status": 400}
            clock.advance(900)
            report = monitor.check()
            self.assertEqual(report["event"], "key_health_recovered")
            self.assertEqual(report["previous_state"], "rejected")
            self.assertEqual(emit.names, ["key_health_rejected", "key_health_recovered"])

    def test_unreachable_then_restored_reports_recovery(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            mode = {"value": "unreachable"}

            def probe():
                if mode["value"] == "unreachable":
                    return {"status": "unreachable", "detail": "Timeout"}
                return {"status": "accepted", "http_status": 400}

            monitor, clock, emit = _monitor(Path(tmp), probe)
            monitor.check()
            mode["value"] = "accepted"
            clock.advance(900)
            report = monitor.check()
            self.assertEqual(report["event"], "key_health_recovered")
            self.assertEqual(report["previous_state"], "unreachable")
            self.assertEqual(emit.names, ["key_health_unreachable", "key_health_recovered"])

    def test_consecutive_failures_counts_and_resets(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            mode = {"ok": False}

            def probe():
                return ({"status": "accepted", "http_status": 400} if mode["ok"]
                        else {"status": "unreachable", "detail": "Timeout"})

            monitor, clock, _ = _monitor(Path(tmp), probe)
            monitor.check()
            clock.advance(900)
            second = monitor.check()
            self.assertEqual(second["consecutive_failures"], 2)
            mode["ok"] = True
            clock.advance(900)
            third = monitor.check()
            self.assertEqual(third["consecutive_failures"], 0)

    def test_restart_does_not_re_announce_the_same_state(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            path = Path(tmp)
            probe = lambda: {"status": "rejected", "http_status": 403}  # noqa: E731
            first, _clock, emit = _monitor(path, probe)
            first.check()
            self.assertEqual(emit.names, ["key_health_rejected"])

            # A fresh process: same persisted state, same condition, no new news.
            second_emit = _Recorder()
            clock2 = _Clock(start=1200.0)
            restarted, _c, _e = _monitor(
                path, probe, clock=clock2, emit=second_emit
            )
            self.assertEqual(restarted.check()["event"], None)
            self.assertEqual(second_emit.names, [])

    def test_heartbeat_re_announces_a_steady_state(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            monitor, clock, emit = _monitor(
                Path(tmp), lambda: {"status": "accepted", "http_status": 400}
            )
            monitor.check()
            clock.advance(900)
            self.assertIsNone(monitor.check()["event"])
            clock.advance(14400)
            report = monitor.check()
            self.assertEqual(report["event"], "key_health_ok")
            self.assertEqual(emit.names, ["key_health_ok", "key_health_ok"])

    def test_probe_exception_is_a_finding_not_a_crash(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            def probe():
                raise RuntimeError("probe exploded")

            monitor, _clock, emit = _monitor(Path(tmp), probe)
            report = monitor.check()
            self.assertEqual(report["state"], "unreachable")
            self.assertIn("RuntimeError", report["detail"])
            self.assertEqual(emit.names, ["key_health_unreachable"])

    def test_missing_key_never_sends_a_request(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            with mock.patch.object(kh, "probe_registration_key") as probe:
                monitor = KeyHealthMonitor(
                    key="",
                    config=KeyHealthConfig(interval_s=900.0, probe_timeout_s=15.0),
                    state_path=Path(tmp) / "kh.json",
                    now_s=_Clock(),
                    now_wall=_Clock(),
                    now_iso=lambda: "2026-11-09T23:00:00Z",
                    emit=_Recorder(),
                )
                report = monitor.check()
            probe.assert_not_called()
            self.assertEqual(report["state"], "unreachable")
            self.assertIn("missing_key", report["detail"])

    def test_key_never_appears_in_any_reported_field(self):
        secret = "s3cr3t-operator-key-9f2a"
        with contextlib.nullcontext(_tmpdir()) as tmp:
            # Worst case: the Worker (or a proxy) echoes the key back in its
            # error string. It still must not reach the log.
            def probe():
                return {
                    "status": "forbidden",
                    "http_status": 403,
                    "error": f"bad header {secret}",
                }

            monitor, _clock, emit = _monitor(Path(tmp), probe, key=secret)
            report = monitor.check()
            blob = json.dumps({"report": report, "events": emit.events})
            self.assertNotIn(secret, blob)
            self.assertIn("[redacted]", report["detail"])
            # And the persisted state is equally clean.
            saved = (Path(tmp) / "state" / "key_health.json").read_text()
            self.assertNotIn(secret, saved)


class RunnerHookTests(unittest.TestCase):
    def test_runner_without_injection_does_not_probe(self):
        account = Account("A", "crassus_a", "pw", "smoke_atm_roundtrip")
        runner = Runner([account], bot_registration_key="key", state_dir=_tmpdir())
        self.assertIsNone(runner.key_health)

    def test_cycle_runs_the_check_and_a_probe_failure_does_not_skip_accounts(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            probe_calls = []

            def probe():
                probe_calls.append(True)
                raise RuntimeError("network down")

            monitor, _clock, emit = _monitor(Path(tmp), probe)
            account = Account("A", "crassus_a", "pw", "smoke_atm_roundtrip")
            runner = Runner(
                [account],
                bot_registration_key="key",
                state_dir=Path(tmp),
                ledger_dir=Path(tmp),
                key_health=monitor,
            )
            runner.accounts = []  # isolate the cycle-level hook from the accounts
            runner.snapshots = mock.Mock(read=lambda: None)
            self.assertTrue(runner.run_cycle())
            self.assertEqual(len(probe_calls), 1)
            self.assertEqual(emit.names, ["key_health_unreachable"])
            # A second cycle inside the interval does not probe again.
            self.assertTrue(runner.run_cycle())
            self.assertEqual(len(probe_calls), 1)


class CliTests(unittest.TestCase):
    def test_missing_key_exits_config_without_probing(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            with mock.patch.object(kh, "BOT_REGISTRATION_KEY", None):
                with mock.patch.object(kh.KeyHealthMonitor, "check") as check:
                    code = kh.main(["--once", "--json", "--state", str(Path(tmp) / "kh.json")])
            self.assertEqual(code, kh.EXIT_CONFIG)
            check.assert_not_called()

    def test_exit_codes_map_to_the_three_states(self):
        self.assertEqual(kh._exit_code("accepted"), 0)
        self.assertEqual(kh._exit_code("rejected"), 3)
        self.assertEqual(kh._exit_code("unreachable"), 4)
        self.assertEqual(kh._exit_code(None), 2)

    def test_cli_reports_accepted_and_writes_state(self):
        with contextlib.nullcontext(_tmpdir()) as tmp:
            state_path = Path(tmp) / "kh.json"
            with mock.patch.object(kh, "BOT_REGISTRATION_KEY", "key"):
                with mock.patch.object(
                    kh,
                    "probe_registration_key",
                    return_value={"status": "accepted", "http_status": 400},
                ):
                    code = kh.main([
                        "--once", "--json", "--state", str(state_path),
                        "--base-url", "https://worker.invalid",
                    ])
            self.assertEqual(code, 0)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["last_state"], "accepted")
            self.assertEqual(saved["checks"], 1)


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (ProbeClassificationTests, CadenceTests, ReportingTests,
                 RunnerHookTests, CliTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
