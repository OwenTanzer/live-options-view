"""Bounded periodic registration-key health for the live runner.

Issue #41's preflight answers "is this deployment provisioned correctly?" once,
before or during startup. That is necessary and not sufficient: a key can drift
*after* a clean start -- someone rotates the Worker secret and forgets Railway,
or restores a backup with an older value -- and nothing in the loop notices.
Every account keeps failing registration with the same generic transport error
until an operator reads a log. This module closes that gap with one bounded,
non-mutating probe on a fixed cadence, reported in a redacted, deduplicated
form through the same structured log route the rest of the runner uses.

Three constraints shaped the design:

* **Scheduled inside the loop, and honest about it.** No thread, no timer, no
  scheduler: `maybe_check` is called once per runner cycle and does work only
  when the interval has elapsed on a monotonic clock. Detection therefore
  depends on the cycle actually running. With a 900 s interval, a nominal 300 s
  cycle and a 15 s probe timeout, a check that starts on the first cycle after
  the interval expires completes within 915 s of scheduled time plus up to one
  cycle period -- 1215 s -- *provided cycles keep to their period*. A cycle
  overrun adds directly, and a hung cycle never reaches the check at all, so no
  unconditional maximum exists. The report states the configured interval, the
  interval actually observed between probes, how late the last probe was, the
  conditional bound, and the condition it depends on; it does not claim a hard
  maximum this module cannot enforce.
* **Redacted.** The probe never returns the key, and this module never logs a
  request body. Everything reported is a state word, an HTTP status, timings
  and the Worker's own short error string, scrubbed again against the key
  before it leaves the process.
* **Deduplicated.** A steady state must not produce one identical line every
  fifteen minutes. An event is emitted when the state changes, when it
  recovers, or when a heartbeat interval has elapsed -- and the emitted state
  is persisted, so a process restart does not re-announce a condition the
  operator has already been told about. The check itself still runs on
  cadence; only the talking is deduplicated.

The probe is deliberately the same empty-registration request as the preflight
(`crassus/preflight.py`), so it cannot create or mutate an account. Key
rotation itself remains an operator action: this module detects drift, it does
not fix it, and it never writes a secret anywhere.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import BASE_URL, BOT_REGISTRATION_KEY, DEFAULT_STATE_DIR
from .observability import configure_logging, event
from .preflight import describe, health_state, probe_registration_key

log = logging.getLogger("crassus.key_health")

#: Check cadence. Fifteen minutes bounds detection to at most one interval plus
#: one probe timeout, and costs 96 requests a day at most -- negligible next to
#: the runner's own quote traffic.
DEFAULT_INTERVAL_S = 900.0

#: A repeated identical state is re-announced at most this often. Four hours
#: keeps a quiet "still accepted" heartbeat in the log without burying the
#: interesting transitions, and proves the check itself is still running.
DEFAULT_HEARTBEAT_S = 4 * 3600.0

#: Matches the preflight probe's timeout. Kept separately configurable so a
#: slow deployment can extend it without loosening the preflight.
DEFAULT_PROBE_TIMEOUT_S = 15.0

STATE_FILE_NAME = "key_health.json"

EXIT_ACCEPTED = 0
EXIT_CONFIG = 2
EXIT_REJECTED = 3
EXIT_UNREACHABLE = 4


def _coerce_seconds(value: Any, default: float, *, allow_zero: bool) -> float:
    """Parse a duration from the environment without trusting it.

    A malformed or non-positive value falls back to the default rather than
    disabling the check: the failure mode this whole module exists to prevent
    is a silent absence of monitoring, so a typo must not be able to create it.
    Zero is honoured only where it means "turn the periodic check off".
    """
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN/inf
        return default
    if seconds < 0 or (seconds == 0 and not allow_zero):
        return default
    return seconds


@dataclass(frozen=True)
class KeyHealthConfig:
    """Cadence and endpoint for the health probe."""

    interval_s: float = DEFAULT_INTERVAL_S
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S
    #: The runner's nominal cycle period, when the caller knows it. It is
    #: *nominal*: it is what the loop intends to sleep between cycles, not a
    #: guarantee about how long a cycle takes.
    cycle_interval_s: float | None = None
    base_url: str = field(default_factory=lambda: BASE_URL)

    @classmethod
    def from_environment(
        cls,
        environ: dict[str, str] | None = None,
        *,
        cycle_interval_s: float | None = None,
    ) -> "KeyHealthConfig":
        env = os.environ if environ is None else environ
        return cls(
            interval_s=_coerce_seconds(env.get("CRASSUS_KEY_HEALTH_INTERVAL_S"), DEFAULT_INTERVAL_S, allow_zero=True),
            heartbeat_s=_coerce_seconds(env.get("CRASSUS_KEY_HEALTH_HEARTBEAT_S"), DEFAULT_HEARTBEAT_S, allow_zero=False),
            probe_timeout_s=_coerce_seconds(env.get("CRASSUS_KEY_HEALTH_TIMEOUT_S"), DEFAULT_PROBE_TIMEOUT_S, allow_zero=False),
            cycle_interval_s=cycle_interval_s,
            base_url=env.get("CRASSUS_BASE_URL") or BASE_URL,
        )

    @property
    def enabled(self) -> bool:
        return self.interval_s > 0

    @property
    def request_timeout_s(self) -> float:
        """The value handed to `requests` as its connect/read timeout.

        Deliberately *not* called a maximum probe duration. `requests` applies
        the value per phase, not as a wall-clock deadline: DNS resolution is not
        covered by it, redirects each get their own read timeout, and a response
        that trickles can be read in several chunks. A probe can therefore
        outlast this number, which is why it is reported as what it is rather
        than as a bound on the probe.
        """
        return self.probe_timeout_s

    @property
    def conditional_detection_estimate_s(self) -> float | None:
        """Rough detection estimate *if* the cadence and the request both behave.

        The check runs once per runner cycle, so a changed key is noticed at the
        first cycle that starts after the interval has elapsed. With a nominal
        cycle period `P`, that is roughly `interval + P` of scheduling delay
        plus one probe. Three things this code cannot enforce make it an
        estimate rather than a bound: a cycle that overruns its period adds the
        overrun directly; a cycle that hangs never reaches the check at all; and
        the HTTP request can outlast its per-phase timeout (DNS, redirects,
        multiple reads), so even the probe term is not a hard ceiling.

        Deliberately returns None when the cycle period is not known rather than
        inventing one; callers must state the caveats alongside the number.
        """
        if self.cycle_interval_s is None:
            return None
        return self.interval_s + self.cycle_interval_s + self.probe_timeout_s

    @property
    def detection_estimate_condition(self) -> str:
        if self.cycle_interval_s is None:
            return (
                "unbounded from this monitor's view: checks run inside the runner "
                "cycle and the cycle period was not supplied"
            )
        return (
            f"conditional on each runner cycle completing within its nominal "
            f"{self.cycle_interval_s:.0f}s period and on the probe returning promptly; "
            f"a cycle overrun adds directly, a hung cycle suspends checks entirely, "
            f"and the request timeout is per-phase so a probe can outlast it"
        )


def _redact(text: str, secret: str | None) -> str:
    """Belt-and-braces scrub for any string that reaches a log line."""
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


class KeyHealthMonitor:
    """One probe per interval, deduplicated reporting, persisted last state.

    The probe, the clocks and the log sink are injectable so the whole
    decision table can be exercised without a network or a real deployment
    (see `crassus/scripts/verify_key_health.py`).
    """

    def __init__(
        self,
        *,
        key: str | None = None,
        config: KeyHealthConfig | None = None,
        probe: Callable[[], dict[str, Any]] | None = None,
        state_path: Path | None = None,
        now_s: Callable[[], float] = time.monotonic,
        now_wall: Callable[[], float] = time.time,
        now_iso: Callable[[], str] | None = None,
        emit: Callable[..., None] | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config or KeyHealthConfig()
        self.key = key if key is not None else BOT_REGISTRATION_KEY
        self._probe = probe or self._default_probe
        self.state_path = Path(state_path) if state_path else Path(DEFAULT_STATE_DIR) / STATE_FILE_NAME
        self._now_s = now_s
        # Wall clock, kept separate from the monotonic cadence clock: "when did
        # we last say this out loud" has to survive a process restart, and a
        # monotonic reading from a previous process is meaningless.
        self._now_wall = now_wall
        self._now_iso = now_iso or _utc_iso
        self._logger = logger or log
        self._emit = emit or (lambda name, **fields: event(self._logger, name, **fields))
        self._last_check_s: float | None = None
        self._next_check_s: float | None = None
        self._state: dict[str, Any] = self._load_state()
        self._state.setdefault("checks", 0)
        self._state.setdefault("accepted", 0)
        self._state.setdefault("rejected", 0)
        self._state.setdefault("unreachable", 0)
        self._state.setdefault("consecutive_failures", 0)
        self._state.setdefault("last_state", None)
        self._state.setdefault("last_emitted_state", None)
        self._state.setdefault("last_emitted_wall", None)
        self._state.setdefault("last_check_at", None)
        self._state.setdefault("last_detail", None)
        # Scheduling truthfulness: what actually happened, not what was planned.
        self._state.setdefault("observed_interval_s", None)
        self._state.setdefault("last_lateness_s", None)
        self._state.setdefault("max_lateness_s", None)

    # -- configuration ----------------------------------------------------

    def _default_probe(self) -> dict[str, Any]:
        if not self.key:
            return {"status": "missing_key"}  # folded to `unreachable` by health_state
        return probe_registration_key(
            self.config.base_url, self.key, timeout_s=self.config.probe_timeout_s
        )

    # -- persistence ------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        except OSError as exc:  # a read-only volume must not stop trading
            self._logger.warning("key_health: could not persist state (%s)", type(exc).__name__)

    # -- scheduling -------------------------------------------------------

    def due(self, now_s: float | None = None) -> bool:
        if not self.config.enabled:
            return False
        if self._next_check_s is None:
            return True
        return (self._now_s() if now_s is None else now_s) >= self._next_check_s

    def maybe_check(self, now_s: float | None = None) -> dict[str, Any] | None:
        """Probe only when due; returns the report when a probe ran."""
        if not self.due(now_s):
            return None
        return self.check(now_s)

    def check(self, now_s: float | None = None) -> dict[str, Any]:
        """Run one probe, update counters, and emit only if warranted."""
        started = self._now_s() if now_s is None else now_s
        try:
            probe = self._probe()
        except Exception as exc:  # a broken probe is a finding, not a crash
            probe = {"status": "unreachable", "detail": type(exc).__name__}

        state = health_state(probe)
        previous = self._state.get("last_state")
        previous_started = self._last_check_s
        if previous_started is not None:
            gap = started - previous_started
            self._state["observed_interval_s"] = round(gap, 3)
            lateness = max(0.0, gap - self.config.interval_s)
            self._state["last_lateness_s"] = round(lateness, 3)
            self._state["max_lateness_s"] = round(
                max(lateness, float(self._state.get("max_lateness_s") or 0.0)), 3
            )
        self._last_check_s = started
        self._next_check_s = started + self.config.interval_s
        self._state["checks"] = int(self._state.get("checks", 0)) + 1
        self._state[state] = int(self._state.get(state, 0)) + 1
        if state == "accepted":
            self._state["consecutive_failures"] = 0
        else:
            self._state["consecutive_failures"] = int(self._state.get("consecutive_failures", 0)) + 1
        self._state["last_state"] = state
        self._state["last_check_at"] = self._now_iso()
        self._state["last_detail"] = self._detail(probe)

        observed_interval = self._state.get("observed_interval_s")
        bound = self.config.conditional_detection_estimate_s
        if observed_interval is None or bound is None:
            within_bound = None
        else:
            within_bound = float(observed_interval) + self.config.probe_timeout_s <= bound
        report = {
            "state": state,
            "status": probe.get("status"),
            "http_status": probe.get("http_status"),
            "detail": self._state["last_detail"],
            "previous_state": previous,
            "checks": self._state["checks"],
            "accepted": self._state["accepted"],
            "rejected": self._state["rejected"],
            "unreachable": self._state["unreachable"],
            "consecutive_failures": self._state["consecutive_failures"],
            "checked_at": self._state["last_check_at"],
            # Scheduling, stated honestly: the configured interval, the interval
            # actually observed between probes, how late the last probe was,
            # and the derived detection bound together with the condition it
            # depends on. No unconditional maximum is claimed, because the check
            # runs inside the runner cycle and this module cannot bound a cycle.
            "check_interval_s": self.config.interval_s,
            "probe_timeout_s": self.config.probe_timeout_s,
            "cycle_interval_s": self.config.cycle_interval_s,
            "observed_interval_s": observed_interval,
            "last_lateness_s": self._state.get("last_lateness_s"),
            "max_lateness_s": self._state.get("max_lateness_s"),
            "conditional_detection_estimate_s": bound,
            "within_detection_estimate": within_bound,
            "detection_estimate_condition": self.config.detection_estimate_condition,
        }

        emitted_state = self._state.get("last_emitted_state")
        last_emitted_wall = self._state.get("last_emitted_wall")
        heartbeat_due = (
            last_emitted_wall is None
            or (self._now_wall() - float(last_emitted_wall)) >= self.config.heartbeat_s
        )
        should_emit = state != emitted_state or heartbeat_due or previous != state
        if should_emit:
            report["event"] = self._event_name(state, previous, emitted_state)
            self._state["last_emitted_state"] = state
            self._state["last_emitted_wall"] = self._now_wall()
            fields = {k: v for k, v in report.items() if k != "event"}
            self._emit(report["event"], **fields)
        else:
            report["event"] = None

        self._save_state()
        return report

    def _event_name(self, state: str, previous: str | None, emitted: str | None) -> str:
        if state == "accepted":
            if emitted in ("rejected", "unreachable") or previous in ("rejected", "unreachable"):
                return "key_health_recovered"
            return "key_health_ok"
        if state == "rejected":
            return "key_health_rejected"
        return "key_health_unreachable"

    def _detail(self, probe: dict[str, Any]) -> str:
        return _redact(describe(probe), self.key)

    # -- reporting --------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Secret-free current picture, suitable for a one-shot CLI or a log."""
        return {
            "state": self._state.get("last_state"),
            "last_check_at": self._state.get("last_check_at"),
            "detail": self._state.get("last_detail"),
            "checks": self._state.get("checks", 0),
            "accepted": self._state.get("accepted", 0),
            "rejected": self._state.get("rejected", 0),
            "unreachable": self._state.get("unreachable", 0),
            "consecutive_failures": self._state.get("consecutive_failures", 0),
            "check_interval_s": self.config.interval_s,
            "probe_timeout_s": self.config.probe_timeout_s,
            "cycle_interval_s": self.config.cycle_interval_s,
            "observed_interval_s": self._state.get("observed_interval_s"),
            "last_lateness_s": self._state.get("last_lateness_s"),
            "max_lateness_s": self._state.get("max_lateness_s"),
            "conditional_detection_estimate_s": self.config.conditional_detection_estimate_s,
            "detection_estimate_condition": self.config.detection_estimate_condition,
            "enabled": self.config.enabled,
        }


def _utc_iso() -> str:
    from . import clock

    return clock.iso_utc()


def _exit_code(state: str | None) -> int:
    if state == "accepted":
        return EXIT_ACCEPTED
    if state == "rejected":
        return EXIT_REJECTED
    if state is None:
        return EXIT_CONFIG
    return EXIT_UNREACHABLE


def main(argv: list[str] | None = None) -> int:
    """Operator entry point: `python -m crassus.key_health --once`.

    One check and an exit code, or a bounded loop at the configured interval.
    This is the same code path the runner uses, so a post-deploy run is
    evidence about the deployed key, not a parallel implementation of it.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="one probe, then exit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()
    config = KeyHealthConfig.from_environment()
    if args.base_url:
        config = KeyHealthConfig(
            interval_s=config.interval_s,
            heartbeat_s=config.heartbeat_s,
            probe_timeout_s=config.probe_timeout_s,
            cycle_interval_s=config.cycle_interval_s,
            base_url=args.base_url,
        )
    if args.interval is not None:
        config = KeyHealthConfig(
            interval_s=_coerce_seconds(args.interval, config.interval_s, allow_zero=True),
            heartbeat_s=config.heartbeat_s,
            probe_timeout_s=config.probe_timeout_s,
            cycle_interval_s=config.cycle_interval_s,
            base_url=config.base_url,
        )

    if not BOT_REGISTRATION_KEY:
        report = {"state": None, "detail": "BOT_REGISTRATION_KEY is not set"}
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("key health: BOT_REGISTRATION_KEY is not set")
        return EXIT_CONFIG

    monitor = KeyHealthMonitor(config=config, state_path=args.state)
    while True:
        report = monitor.check()
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            bound = report["conditional_detection_estimate_s"]
            bound_text = "unbounded (no cycle period supplied)" if bound is None else f"{bound:.0f}s"
            print(
                f"key health: {report['state']} at {report['checked_at']} "
                f"({report['detail']}); checks={report['checks']} "
                f"interval={report['check_interval_s']:.0f}s "
                f"lateness={report['last_lateness_s'] or 0:.0f}s "
                f"conditional_bound={bound_text}"
            )
        if args.once or not config.enabled:
            return _exit_code(report["state"])
        time.sleep(config.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
