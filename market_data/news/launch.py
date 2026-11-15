"""Bounded one-shot launcher for the scheduled News pilot cycle.

    python -m market_data.news.launch --config market_data/news/deploy/news-pilot.json [pilot-cycle options]

The scheduler runs this, not `pilot-cycle` directly. `cycle_budget_s` bounds only the collection
phase; nothing bounds a cycle's views, research, publication, store calls, SDK retries or a hung
connection. This launcher gives the whole process one explicit wall-clock deadline, from its own
start, covering every phase, retry and subprocess of the cycle, and always ends with exactly one
terminal record on stdout and a definite exit code:

  1. It loads and validates the pilot config (`launcher` block included). An invalid config is a
     terminal failure before anything runs.
  2. It runs the cycle as a child process in its own process group (`--run-cycle`, below), which runs
     `python -m market_data.news pilot-cycle --config <config> <options>` in-process.
  3. At `deadline_s` it interrupts the child's whole group: the child raises CycleInterrupted (a
     BaseException), so the cycle's existing failure handling runs and its writer/publish leases are
     released by their context managers, exactly as on any other exception. Nothing new is written.
  4. If the group is still alive `interrupt_grace_s` later, it is killed. A killed cycle cannot release
     its leases: the record says so, and later cycles exit non-zero ("lease held") until the lease
     expires and an operator who has confirmed no writer runs passes --break-stale-lease
     (publish-status reports stale_*_lease). Nothing here breaks a lease.
  5. A SIGTERM/SIGINT to the launcher (a scheduler stop) starts the same interrupt-then-kill sequence.

`load_pilot_config` requires deadline_s + interrupt_grace_s + kill_wait_s < schedule.interval_s and
<= the lease TTL, so a launcher always exits before the next scheduled tick (no self-overlap) and a
live cycle never holds a lease past its expiry. Exit codes:

  0    the cycle finished and its own exit code was 0
  1/2  the cycle finished with that exit code (pilot-cycle's own failure codes)
  2    the config is invalid (no child is started)
  124  deadline: the cycle was interrupted and exited (its leases were released by the cycle)
  137  deadline or stop: the cycle did not exit within the grace; its group was killed and confirmed
       gone (leases may remain held)
  126  deadline or stop: the group was killed but its termination could NOT be confirmed within
       kill_wait_s (a writer may still be running; reported, never assumed away)
  143  stopped by a signal to the launcher; the cycle was interrupted and its group exited
  125  the child could not be started

"The cycle" is the child's whole owned process group (a new session): a leader that exits while a
descendant remains does not end the cycle; the launcher waits for the group under the same deadline.
Only an observed-empty group is reported as group_terminated: true.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

LAUNCH_SCHEMA = "news-launch/v1"
CHILD_INTERRUPTED = 3
WINDOWS = os.name == "nt"


class CycleInterrupted(BaseException):
    """Raised in the cycle process when the launcher interrupts it (deadline or stop)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_cycle(argv: list[str]) -> int:
    """The child: run pilot-cycle in this process, turning the launcher's interrupt into an exception."""
    from .__main__ import main

    def interrupted(signum, frame):
        raise CycleInterrupted(f"signal {signum}")

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)
    try:
        return main(["pilot-cycle", *argv])
    except CycleInterrupted as exc:
        print(json.dumps({"status": "interrupted", "reason": str(exc)}), file=sys.stderr, flush=True)
        return CHILD_INTERRUPTED


def _spawn(cmd: list[str]) -> subprocess.Popen:
    if WINDOWS:
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(cmd, start_new_session=True)


def _group_alive(proc: subprocess.Popen) -> bool:
    """Is any process of the cycle's owned group still present?

    POSIX: the child leads a new session and process group (pgid == its pid). The leader is reaped
    first (poll), then signal 0 to the group asks whether any member remains: a descendant that
    outlived the leader keeps the group alive. A group ID cannot be reused while any member exists, so
    signalling it only while this says True never reaches an unrelated group. A descendant that leaves
    the group itself (setsid/setpgid) is not tracked; nothing in the cycle does that.
    Windows (local development only): only the leader is observable."""
    proc.poll()
    if WINDOWS:
        return proc.returncode is None
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group(proc: subprocess.Popen, timeout: float, monotonic=time.monotonic) -> bool:
    """Wait up to timeout for the whole owned group to be gone; True only when confirmed gone."""
    end = monotonic() + timeout
    while _group_alive(proc):
        if monotonic() >= end:
            return False
        time.sleep(0.05)
    return True


def _interrupt(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


KILLED_NOTE = ("the cycle's process group was killed: its writer/publish leases may remain held until they "
               "expire; later cycles exit non-zero until an operator confirms no writer runs and passes "
               "--break-stale-lease (publish-status reports stale_*_lease)")
UNCONFIRMED_NOTE = ("termination of the cycle's process group could NOT be confirmed after the kill wait: a "
                    "writer may still be running. Its leases are not broken; later cycles refuse while they "
                    "are held. Confirm no writer runs before any --break-stale-lease")


FENCE_KEY = "news/v1/_launcher/fence.json"


def _claim_fence(store):
    current = store.get_versioned(FENCE_KEY)
    if current is not None and json.loads(current[0])["state"] != "idle":
        return None
    token = uuid.uuid4().hex
    body = json.dumps({"state": "running", "owner": token, "at": _now()}).encode()
    claim = store.put_if_match(FENCE_KEY, body, current[1] if current else None)
    if claim.status != "applied" or not claim.etag:
        return None
    return claim.etag, token


def launch(config_path: str, cycle_args: list[str], *, monotonic=time.monotonic, cmd_prefix=None) -> dict:
    """Run one bounded cycle; return the terminal record (its exit_code is the process exit code).

    The bound covers the cycle's whole owned process group, not only the direct child: a cycle has
    ended only when no member of the group remains. If the leader exits while a descendant remains,
    the launcher keeps waiting for the group under the same deadline, then interrupts and kills the
    group as for a running cycle. Every outcome other than config_invalid/spawn_failed states
    group_terminated: true only after the group was observed gone; otherwise the outcome is
    termination_unconfirmed."""
    start = monotonic()
    record = {"schema": LAUNCH_SCHEMA, "config": config_path, "started_at": _now(),
              "group_check": "leader_only (Windows)" if WINDOWS else "posix_process_group"}

    def finish(outcome: str, exit_code: int, **extra) -> dict:
        record.update(outcome=outcome, exit_code=exit_code, ended_at=_now(),
                      elapsed_s=round(monotonic() - start, 3), **extra)
        return record

    try:
        from .publish import load_pilot_config
        cfg = load_pilot_config(config_path)
    except Exception as exc:   # an unreadable or invalid config never starts a cycle
        return finish("config_invalid", 2, error=f"{type(exc).__name__}: {exc}"[:500])
    bounds = cfg["launcher"]
    deadline, grace, kill_wait = bounds["deadline_s"], bounds["interrupt_grace_s"], bounds["kill_wait_s"]
    record.update(deadline_s=deadline, interrupt_grace_s=grace, kill_wait_s=kill_wait)

    from .archive import LocalObjectStore
    from .publish import bucket_store_from_env
    options = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    options.add_argument("--private", default="./news-private-local")
    options.add_argument("--live", action="store_true")
    opts, _ = options.parse_known_args(cycle_args)
    if opts.live and (not cfg.active or os.environ.get("NEWS_ALLOW_BUCKET_WRITES") != "1"):
        return finish("cycle_failed", 2, child_exit=None)
    try:
        store = (bucket_store_from_env(os.environ, cfg["storage"]["private"]["env_prefix"])
                 if opts.live else LocalObjectStore(opts.private))
        claim = _claim_fence(store)
    except Exception as exc:
        return finish("fence_unavailable", 126, error=type(exc).__name__)
    if claim is None:
        return finish("fence_held", 126)
    fence_etag, token = claim
    record["fence_owner"] = token
    stop = {"signal": None}
    previous = {}

    def on_stop(signum, frame):
        stop["signal"] = signum

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name)
        previous[sig] = signal.signal(sig, on_stop)
    try:
        cmd = (cmd_prefix or [sys.executable, "-m", "market_data.news.launch"]) + \
            ["--run-cycle", "--config", config_path, *cycle_args]
        try:
            proc = _spawn(cmd)
        except OSError as exc:
            return finish("spawn_failed", 125, error=f"{type(exc).__name__}: {exc}"[:500])
        record["child_pid"] = proc.pid

        reason = None
        while True:
            if stop["signal"] is not None:
                reason = "stopped"
                break
            remaining = deadline - (monotonic() - start)
            if remaining <= 0:
                reason = "deadline"
                break
            if proc.returncode is None:
                try:
                    proc.wait(timeout=min(remaining, 1.0))
                except subprocess.TimeoutExpired:
                    continue
                record["child_exit"] = proc.returncode
            if not _group_alive(proc):
                code = proc.returncode
                if record.get("descendants_outlived_leader"):
                    record["group_ended_at"] = _now()
                if code == 0:
                    return finish("ok", 0, group_terminated=True)
                return finish("cycle_failed", code if code in (1, 2) else 1, group_terminated=True)
            # The leader has exited but a descendant remains: the cycle is not over.
            if not record.get("descendants_outlived_leader"):
                record["descendants_outlived_leader"] = True
                record["leader_exited_at"] = _now()
            time.sleep(min(0.1, max(0.0, remaining)))

        _interrupt(proc)
        record["interrupted_at"] = _now()
        if _wait_group(proc, grace, monotonic):
            record["child_exit"] = proc.returncode
            if reason == "stopped":
                return finish("stopped_interrupted", 143, group_terminated=True, signal=stop["signal"])
            return finish("deadline_interrupted", 124, group_terminated=True)
        _kill(proc)
        record["killed_at"] = _now()
        confirmed = _wait_group(proc, kill_wait, monotonic)
        record["child_exit"] = proc.returncode
        if confirmed:
            return finish("killed", 137, group_terminated=True, cause=reason, lease_note=KILLED_NOTE)
        return finish("termination_unconfirmed", 126, group_terminated=False, cause=reason,
                      lease_note=UNCONFIRMED_NOTE)
    finally:
        if record.get("group_terminated") or record.get("outcome") == "spawn_failed":
            idle = json.dumps({"state": "idle", "previous_owner": token}).encode()
            try:
                released = store.put_if_match(FENCE_KEY, idle, fence_etag)
                cleared = released.status == "applied"
            except Exception:
                cleared = False
            if not cleared:
                record.update(outcome="fence_release_unconfirmed", exit_code=126)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--run-cycle"]:
        return run_cycle(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m market_data.news.launch",
                                 description="bounded one-shot launcher for the News pilot cycle")
    ap.add_argument("--config", required=True)
    args, cycle_args = ap.parse_known_args(argv)
    record = launch(args.config, cycle_args)
    print(json.dumps(record, sort_keys=True), flush=True)
    return record["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
