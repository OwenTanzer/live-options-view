"""Disposable read-only Reddit transport; never imports a runner or executor.

The process boundary bounds blocking requests, Playwright launch, navigation,
DOM extraction and cleanup together. Threads/timeouts alone cannot cancel a
wedged synchronous Playwright driver. Only this child's identity-checked
process tree is terminated; unrelated sources/processes are not touched.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from .sentiment import RedditFetchError, RedditSentimentReader, _ACQUISITION_TIMEOUT_S
from .supervisor import Process, process_tree, proc_visible, _signal_process

CLEANUP_TIMEOUT_S = 1.0
_PENDING_REAPS: dict[int, Process] = {}
_REAP_LOCK = threading.Lock()


def _read_process(stat_file: Path) -> Process | None:
    try:
        raw = stat_file.read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return Process(int(fields[1]), int(fields[19]), int(fields[21]), fields[0])
    except (OSError, ValueError, IndexError):
        return None


def _remember_unreaped(processes: dict[int, Process]) -> None:
    with _REAP_LOCK:
        _PENDING_REAPS.update(processes)


def _reap_pending() -> None:
    """Retry only retained owned identities; never change process-wide adoption."""
    with _REAP_LOCK:
        pending = dict(_PENDING_REAPS)
    for pid, identity in pending.items():
        removed = False
        try:
            current = _read_process(Path(f"/proc/{pid}/stat"))
            if current is None or current.started != identity.started:
                removed = True
            else:
                if current.state != 'Z':
                    _signal_process(pid, identity, signal.SIGKILL)
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    removed = bool(waited)
                except ChildProcessError:
                    pass  # The existing supervisor/init reaps its own adoptees.
        except (OSError, ValueError, IndexError):
            removed = True
        if removed:
            with _REAP_LOCK:
                if _PENDING_REAPS.get(pid) == identity:
                    _PENDING_REAPS.pop(pid, None)


def _marked_processes(token: str) -> dict[int, Process]:
    """Find only processes carrying this acquisition's nonsecret ownership tag."""
    found = {}
    marker = f"CRASSUS_ACQUISITION_ID={token}".encode()
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            before = _read_process(directory / "stat")
            if before is None:
                continue
            if marker not in (directory / "environ").read_bytes().split(b"\0"):
                continue
            after = _read_process(directory / "stat")
            if after is not None and after.started == before.started:
                found[int(directory.name)] = after
        except (OSError, ValueError, IndexError):
            continue
    return found


def _stop(child: subprocess.Popen, token: str) -> None:
    # The direct child may have exited while a detached browser still lives.
    # The tag survives reparenting and closed stdout; PID/start-time checks
    # prevent signaling a reused PID or an unrelated process.
    deadline = time.monotonic() + CLEANUP_TIMEOUT_S
    descendants = _marked_processes(token)
    if child.pid in descendants:
        tree = process_tree(child.pid)
        if child.pid in tree and tree[child.pid].started == descendants[child.pid].started:
            descendants.update(tree)
    for pid, identity in descendants.items():
        _signal_process(pid, identity, signal.SIGSTOP)
    descendants.update(_marked_processes(token))
    for pid, identity in descendants.items():
        _signal_process(pid, identity, signal.SIGKILL)
    try:
        child.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # No unbounded wait even if the kernel cannot immediately reap a task.
        _remember_unreaped(descendants)
        raise RedditFetchError("cleanup_timeout: Reddit acquisition process") from None
    # Reap owned adoptees only if this caller is already their reaper. The
    # production supervisor retains its existing permanent adoption policy.
    remaining = set(descendants) - {child.pid}
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            current = _read_process(Path(f"/proc/{pid}/stat"))
            if current is None or current.started != descendants[pid].started:
                remaining.remove(pid)
                continue
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    remaining.remove(pid)
            except ChildProcessError:
                # The permanent outer supervisor may own an adopted zombie.
                # Its matching PID/start time is already dead; the supervisor
                # will reap it on its own polling cadence.
                if current.state == "Z" or not Path(f"/proc/{pid}").exists():
                    remaining.remove(pid)
        if remaining:
            time.sleep(.01)
    if remaining:
        _remember_unreaped({pid: descendants[pid] for pid in remaining})
        raise RedditFetchError("cleanup_timeout: unreaped Reddit acquisition descendants")


def _parse_output(output: bytes) -> dict[str, Any]:
    observed: dict[str, Any] = dict(phase="source", rate_limited_until=0.0, result=None, error=None)
    for line in output.splitlines():
        try:
            record = json.loads(line)
            observed["result"] = record if isinstance(record, dict) else None
            if not isinstance(record, dict):
                continue
            if record.get("phase") in ("http", "browser", "cleanup"):
                observed["phase"] = record["phase"]
            if isinstance(record.get("error"), str):
                observed["error"] = record["error"]
            value = record.get("rate_limited_until")
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                observed["rate_limited_until"] = max(observed["rate_limited_until"], value)
        except (ValueError, TypeError):
            observed["result"] = None
    return observed


def _failure(primary: str | None, secondary: str, observed: dict[str, Any]) -> RedditFetchError:
    exc = RedditFetchError(f"{primary}; {secondary}" if primary else secondary)
    exc.rate_limited_until = observed["rate_limited_until"]
    if secondary.startswith("cleanup_"):
        exc.cleanup_error = secondary
    return exc


def _run(config: dict[str, Any], *, timeout_s: float = _ACQUISITION_TIMEOUT_S,
         command: list[str] | None = None) -> dict[str, Any]:
    if os.name != "posix":
        raise RedditFetchError("source_unavailable: bounded Reddit acquisition requires POSIX")
    if not proc_visible():
        raise RedditFetchError("source_unavailable: acquisition process-tree visibility required")
    started = time.monotonic()
    _reap_pending()
    # No account passwords, broker tokens or archive credentials in this child.
    allowed = {
        "PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH", "REDDIT_USER_AGENT",
        # Preserve existing requests routing/trust configuration, without
        # copying unrelated account, broker or archive credentials.
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    token = uuid.uuid4().hex
    env["CRASSUS_ACQUISITION_ID"] = token
    child = subprocess.Popen(
        command or [sys.executable, "-m", "crassus.reddit_acquisition"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    observed = _parse_output(b"")
    primary: BaseException | None = None
    result = None
    try:
        try:
            output, _ = child.communicate(json.dumps(config).encode(),
                                          timeout=max(0, timeout_s - (time.monotonic() - started)))
        except subprocess.TimeoutExpired as exc:
            observed = _parse_output(exc.output or b"")
            raise _failure(observed["error"],
                           f"{observed['phase']}_timeout: acquisition exceeded {timeout_s:g}s", observed) from None
        observed = _parse_output(output)
        result = observed["result"]
        if child.returncode or not isinstance(result, dict) or not ("texts" in result or "error" in result):
            secondary = ("cleanup_failure: acquisition worker exited during cleanup"
                         if child.returncode and observed["phase"] == "cleanup"
                         else "parsing_failure: acquisition worker result")
            raise _failure(observed["error"], secondary, observed)
        return result
    except BaseException as exc:
        primary = exc
        if isinstance(exc, RedditFetchError):
            exc.rate_limited_until = observed["rate_limited_until"]
        raise
    finally:
        try:
            _stop(child, token)
        except RedditFetchError as exc:
            if primary is not None and not isinstance(primary, RedditFetchError):
                primary.add_note(str(exc))  # Preserve cancellation/control flow.
            else:
                cause = str(primary) if primary is not None else observed["error"]
                raise _failure(cause, str(exc), observed) from primary
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def acquire(reader: RedditSentimentReader) -> list[str]:
    try:
        result = _run(dict(subreddits=reader.subreddits, keywords=reader.keywords,
                           post_limit=reader.post_limit, min_interval_s=reader.min_interval_s,
                           rate_limited_until=reader._rate_limited_until))
    except RedditFetchError as exc:
        reader._rate_limited_until = max(reader._rate_limited_until,
                                         getattr(exc, "rate_limited_until", 0.0))
        raise
    reader._source_expires = min(reader._source_expires, result.get("source_expires", float("inf")))
    reader._rate_limited_until = max(reader._rate_limited_until,
                                     result.get("rate_limited_until", 0.0))
    if "error" in result:
        raise RedditFetchError(result["error"])
    texts = result.get("texts")
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        raise RedditFetchError("parsing_failure: acquisition text records")
    return texts


def main() -> None:
    config = json.load(sys.stdin)
    reader = RedditSentimentReader(subreddits=tuple(config["subreddits"]),
                                   keywords=tuple(config["keywords"]),
                                   post_limit=config["post_limit"],
                                   min_interval_s=config.get("min_interval_s", 300.0))
    reader._rate_limited_until = config["rate_limited_until"]
    reader._source_expires = time.monotonic() + reader.min_interval_s
    reader._phase_callback = lambda phase: print(json.dumps({"phase": phase}), flush=True)
    reader._rate_limit_callback = lambda until: print(json.dumps({
        "rate_limited_until": until, "error": "source_rate_limited: Reddit HTTP 429"}), flush=True)
    result: dict[str, Any] = {}
    try:
        reader._session = reader._session_factory()
        result["texts"] = list(reader._collect_texts())
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, RedditFetchError) else f"source_unavailable: {type(exc).__name__}"
        print(json.dumps({"error": result["error"]}), flush=True)
    finally:
        reader._phase_callback("cleanup")
        reader._close_browser()
        if reader._session is not None:
            reader._session.close()
    result["source_expires"] = reader._source_expires
    result["rate_limited_until"] = reader._rate_limited_until
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
