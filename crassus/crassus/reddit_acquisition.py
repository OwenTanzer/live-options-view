"""Disposable read-only Reddit transport; never imports a runner or executor.

The process boundary bounds blocking requests, Playwright launch, navigation,
DOM extraction and cleanup together. Threads/timeouts alone cannot cancel a
wedged synchronous Playwright driver. Only this child's identity-checked
process tree is terminated; unrelated sources/processes are not touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from .sentiment import RedditFetchError, RedditSentimentReader, _ACQUISITION_TIMEOUT_S
from .supervisor import process_tree, proc_visible, _signal_process

CLEANUP_TIMEOUT_S = 1.0


def _stop(child: subprocess.Popen) -> None:
    # Freeze the owned group before inspecting detached browser descendants.
    try:
        os.killpg(child.pid, signal.SIGSTOP)
    except ProcessLookupError:
        pass
    descendants = process_tree(child.pid)
    for pid, identity in descendants.items():
        _signal_process(pid, identity, signal.SIGSTOP)
    descendants.update(process_tree(child.pid))
    # Kill descendants before their parent so detached Chromium processes do
    # not escape /proc attribution. The outer runner supervisor reaps adopted
    # grandchildren using its existing subreaper, without restarting the runner.
    for pid, identity in descendants.items():
        if pid != child.pid:
            _signal_process(pid, identity, signal.SIGKILL)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=CLEANUP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # No unbounded wait even if the kernel cannot immediately reap a task.
        raise RedditFetchError("cleanup_timeout: Reddit acquisition process") from None


def _run(config: dict[str, Any], *, timeout_s: float = _ACQUISITION_TIMEOUT_S,
         command: list[str] | None = None) -> dict[str, Any]:
    if os.name != "posix":
        raise RedditFetchError("source_unavailable: bounded Reddit acquisition requires POSIX")
    if command is None and not proc_visible():
        raise RedditFetchError("source_unavailable: acquisition process-tree visibility required")
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
    child = subprocess.Popen(
        command or [sys.executable, "-m", "crassus.reddit_acquisition"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        try:
            output, _ = child.communicate(json.dumps(config).encode(), timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            phase = "source"
            for line in (exc.output or b"").splitlines():
                try:
                    record = json.loads(line)
                    if record.get("phase") in ("http", "browser", "cleanup"):
                        phase = record["phase"]
                except (ValueError, AttributeError):
                    pass
            raise RedditFetchError(f"{phase}_timeout: acquisition exceeded {timeout_s:g}s") from None
        try:
            result = json.loads(output.splitlines()[-1])
            if child.returncode or not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, IndexError):
            raise RedditFetchError("parsing_failure: acquisition worker result") from None
    finally:
        try:
            if child.poll() is None:
                _stop(child)
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


def acquire(reader: RedditSentimentReader) -> list[str]:
    result = _run(dict(subreddits=reader.subreddits, keywords=reader.keywords,
                       post_limit=reader.post_limit, min_interval_s=reader.min_interval_s,
                       rate_limited_until=reader._rate_limited_until))
    reader._source_expires = min(reader._source_expires, result.get("source_expires", float("inf")))
    reader._rate_limited_until = result.get("rate_limited_until", 0.0)
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
    result: dict[str, Any] = {}
    try:
        reader._session = reader._session_factory()
        result["texts"] = list(reader._collect_texts())
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, RedditFetchError) else f"source_unavailable: {type(exc).__name__}"
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
