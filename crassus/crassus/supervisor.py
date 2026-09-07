"""Independent completed-cycle watchdog for the continuously running CLI.

The supervisor never instantiates an executor, opens a ledger, or retries a
trade. It owns exactly one runner process group. A hung Python/Playwright
worker is terminated as a group and the supervisor exits nonzero so Railway's
ALWAYS policy can restart the existing durable recovery path.
"""
from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import selectors
from dataclasses import dataclass
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .observability import event

log = logging.getLogger("crassus")
HEARTBEAT_FD = "_CRASSUS_HEARTBEAT_FD"
FAILURE_EXIT = 75


def report_cycle(name: str, **fields: Any) -> None:
    """Small atomic pipe records, separate from logs and durable trading state."""
    fd = os.environ.get(HEARTBEAT_FD)
    if fd is None:
        return  # Direct Runner users and --once retain their existing lifecycle.
    payload = (json.dumps({"event": name, **fields}, sort_keys=True) + "\n").encode()
    try:
        if len(payload) > 4096:
            raise ValueError("oversized heartbeat")
        os.write(int(fd), payload)
    except (OSError, ValueError):
        # Loss of the supervisor channel cannot silently disable protection.
        # Existing pending intents are already fsynced before order submission.
        os._exit(FAILURE_EXIT)


@dataclass(frozen=True)
class Process:
    parent: int
    started: int
    rss: int
    state: str


def proc_visible() -> bool:
    """Some development sandboxes expose /proc from a different PID namespace."""
    try:
        return int(Path("/proc/self/stat").read_text().split(" ", 1)[0]) == os.getpid()
    except (OSError, ValueError):
        return False


def process_tree(root_pid: int) -> dict[int, Process]:
    """Identify descendants by PID plus kernel start time, without credentials."""
    if not proc_visible():
        return {}
    processes = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            raw = (directory / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            processes[int(directory.name)] = Process(int(fields[1]), int(fields[19]), int(fields[21]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    descendants = {root_pid} if root_pid in processes else set()
    while True:
        expanded = descendants | {pid for pid, info in processes.items() if info.parent in descendants}
        if expanded == descendants:
            return {pid: processes[pid] for pid in descendants}
        descendants = expanded


def _signal_process(pid: int, identity: Process, sig: int) -> None:
    # Playwright may launch Chromium in another process group. Capture its
    # identity before stopping Python, and avoid signaling a reused PID.
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        if int(raw[raw.rfind(")") + 2:].split()[19]) == identity.started:
            os.kill(pid, sig)
    except (OSError, ValueError, IndexError):
        pass


def memory_snapshot(root_pid: int) -> dict[str, Any]:
    """Current Linux RSS, including descendants; never inspect cmdline/env."""
    try:
        processes = process_tree(root_pid)
        if root_pid not in processes:
            return {"memory_sample_unavailable": True}
        page_bytes = os.sysconf("SC_PAGE_SIZE")
        return {
            "worker_rss_bytes": processes[root_pid].rss * page_bytes,
            "descendant_rss_bytes": sum(info.rss for pid, info in processes.items() if pid != root_pid) * page_bytes,
            "descendant_count": len(processes) - 1,
        }
    except (OSError, ValueError):
        return {"memory_sample_unavailable": True}


class Progress:
    def __init__(self, interval_s: float, now: float):
        self.interval_s = interval_s
        self.last_completed_monotonic = now
        self.sequence = 0
        self.completed_sequence = 0
        self.fields: dict[str, Any] = {
            "cycle_sequence": 0, "completed_cycle_count": 0, "last_cycle_started_at": None,
            "last_cycle_completed_at": None, "cycle_duration_seconds": None,
            "last_fatal_error": None,
        }

    def accept(self, record: dict[str, Any], now: float) -> None:
        name = record["event"]
        sequence = record["cycle_sequence"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("invalid heartbeat sequence")
        if name == "cycle_started":
            if sequence != self.sequence + 1:
                raise ValueError("out-of-order cycle start")
            self.sequence = sequence
            self.fields.update(cycle_sequence=sequence, run_id=record["run_id"],
                               last_cycle_started_at=record["cycle_started_at"])
        elif name == "cycle_completed":
            if sequence != self.sequence or sequence <= self.completed_sequence:
                raise ValueError("out-of-order cycle completion")
            self.completed_sequence = sequence
            self.last_completed_monotonic = now
            self.fields.update(completed_cycle_count=sequence,
                               last_cycle_completed_at=record["cycle_completed_at"],
                               cycle_duration_seconds=record["duration_seconds"])
        elif name == "cycle_failed":
            self.fields["last_fatal_error"] = record["error_type"]
        else:
            raise ValueError("unknown heartbeat event")

    def overdue(self, now: float) -> bool:
        return now - self.last_completed_monotonic > 2 * self.interval_s


def _subreaper(value: int | None = None) -> int:
    """Adopt detached browser orphans when the worker dies (Linux runtime)."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot inspect child subreaper")
    if value is not None and libc.prctl(36, value, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot set child subreaper")
    return previous.value


def reap_adopted(worker_pid: int | None, baseline: set[int]) -> int:
    """Reap exited browser helpers regularly, preserving Popen's worker status."""
    count = 0
    for pid, info in process_tree(os.getpid()).items():
        if info.parent != os.getpid() or pid == worker_pid or pid in baseline or info.state != "Z":
            continue
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            count += bool(waited)
        except ChildProcessError:
            pass
    return count


def _stop_group(child: subprocess.Popen, grace_s: float, baseline: set[int]) -> None:
    # The supervisor creates no other children. Existing children are excluded
    # for direct test callers; newly adopted orphans belong to this worker.
    descendants = {pid: info for pid, info in process_tree(os.getpid()).items()
                   if pid != os.getpid() and pid not in baseline}
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    descendants.update({pid: info for pid, info in process_tree(os.getpid()).items()
                        if pid != os.getpid() and pid not in baseline})
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid, identity in descendants.items():
        _signal_process(pid, identity, signal.SIGKILL)
    child.wait()
    # Reap adopted Chromium/driver processes too, without touching unrelated
    # child processes. All have been signaled; allow a bounded reap window.
    deadline = time.monotonic() + 1
    remaining = set(descendants) - {child.pid}
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    remaining.remove(pid)
            except ChildProcessError:
                # A grandchild may be adopted just after its parent exits.
                # Do not forget it while it still exists and needs reaping.
                if not Path(f"/proc/{pid}").exists():
                    remaining.remove(pid)
        if remaining:
            time.sleep(.01)


def supervise(command: list[str], interval_s: float, *, poll_s: float = 1.0,
              memory_interval_s: float = 30.0, shutdown_grace_s: float = 5.0) -> int:
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("interval must be finite and positive")
    baseline = set(process_tree(os.getpid())) - {os.getpid()}
    previous_subreaper = _subreaper(1)
    stop_signal: list[int] = []
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda number, frame: stop_signal.append(number))
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    env = dict(os.environ, **{HEARTBEAT_FD: str(write_fd)})
    try:
        child = subprocess.Popen(command, env=env, pass_fds=(write_fd,), start_new_session=True)
    except BaseException:
        os.close(read_fd)
        _subreaper(previous_subreaper)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        raise
    finally:
        os.close(write_fd)
    selector = selectors.DefaultSelector()
    selector.register(read_fd, selectors.EVENT_READ)
    progress = Progress(interval_s, time.monotonic())
    next_memory = 0.0
    buffer = b""
    event(log, "runner_supervisor_started", worker_pid=child.pid, interval_seconds=interval_s,
          missing_cycle_deadline_seconds=2 * interval_s)
    try:
        while True:
            if stop_signal:
                event(log, "runner_supervisor_stopping", signal=stop_signal[0])
                return 128 + stop_signal[0]
            for _, _ in selector.select(timeout=min(poll_s, interval_s / 4)):
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    # The writer disappeared even if a required child kept the
                    # worker process half alive. Do not wait for another cycle.
                    try:
                        code = child.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        code = None
                    event(log, "runner_unhealthy", level=logging.ERROR,
                          reason="heartbeat_channel_closed", worker_exit_code=code, **progress.fields)
                    return FAILURE_EXIT
                buffer += chunk
                if len(buffer) > 65536:
                    raise ValueError("oversized heartbeat stream")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    progress.accept(json.loads(line), time.monotonic())
            now = time.monotonic()
            reaped = reap_adopted(child.pid, baseline)
            if reaped:
                event(log, "runner_children_reaped", count=reaped)
            code = child.poll()
            if code is not None:
                event(log, "runner_unhealthy", level=logging.ERROR, reason="worker_exited",
                      worker_exit_code=code, **progress.fields)
                return FAILURE_EXIT
            if progress.overdue(now):
                event(log, "runner_unhealthy", level=logging.ERROR, reason="missing_completed_cycles",
                      seconds_since_completion=round(now - progress.last_completed_monotonic, 3),
                      **progress.fields, **memory_snapshot(child.pid))
                return FAILURE_EXIT
            if now >= next_memory:
                event(log, "runner_health", healthy=True,
                      seconds_since_completion=round(now - progress.last_completed_monotonic, 3),
                      **progress.fields, **memory_snapshot(child.pid))
                next_memory = now + memory_interval_s
    except (ValueError, KeyError, TypeError, OSError) as exc:
        event(log, "runner_unhealthy", level=logging.ERROR, reason="supervisor_failure",
              error_type=type(exc).__name__, **progress.fields)
        return FAILURE_EXIT
    finally:
        _stop_group(child, shutdown_grace_s, baseline)
        _subreaper(previous_subreaper)
        selector.close()
        os.close(read_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
