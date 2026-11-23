"""Start a local Worker harness from its stdout handshake.

The harness binds loopback ports chosen by the OS and announces its PID, nonce
and both ports. The parent verifies the identity endpoint on the announced
worker port before any test request, so an unrelated listener can never be
mistaken for the child. A child exit is surfaced with its captured output.

Diagnostics before the handshake (2026-11-13): the backend's operational
storage logs one JSON line per bucket write (`{"event": "r2_write", ...}`),
and both harnesses publish the bot index before announcing themselves, so the
handshake is not necessarily the first stdout line. Lines that are not a
handshake candidate (not JSON, or JSON without a `ready` key) are kept as
diagnostics and skipped -- all within ONE fixed startup deadline measured from
launch, never reset per line. The first line carrying `ready` IS the
handshake and gets every identity check below; a wrong PID, host, port or
nonce still fails rather than being skipped.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


MAX_DIAGNOSTICS_REPORTED = 5


def start_harness(node: str, script: Path, timeout: float = 25.0):
    proc = subprocess.Popen(
        [node, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        if proc.stdout is None:
            lines.put(None)
            return
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # stdout closed: the child has exited

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    diagnostics: list[str] = []

    def recent() -> str:
        return " | ".join(d.strip()[:200] for d in diagnostics[-MAX_DIAGNOSTICS_REPORTED:]) or "(none)"

    info = None
    while info is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.terminate()
            raise RuntimeError(f"harness did not emit a startup handshake within {timeout:.0f}s; "
                               f"{len(diagnostics)} diagnostic line(s), last: {recent()}")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            continue  # the loop re-checks the single deadline
        if line is None:
            rc = proc.wait(timeout=5)
            raise RuntimeError(f"harness exited with {rc} before its handshake; "
                               f"{len(diagnostics)} diagnostic line(s), last: {recent()}")
        try:
            candidate = json.loads(line)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and "ready" in candidate:
            info = candidate
        else:
            diagnostics.append(line)
    if info.get("ready") is not True or info.get("pid") != proc.pid:
        proc.terminate()
        raise RuntimeError(f"harness handshake identity mismatch: {info!r}")
    if info.get("host") != "127.0.0.1":
        proc.terminate()
        raise RuntimeError(f"harness must bind loopback only: {info!r}")
    worker_port = info.get("worker_port")
    quote_port = info.get("quote_port")
    if not isinstance(worker_port, int) or not isinstance(quote_port, int) or \
            not 0 < worker_port < 65536 or not 0 < quote_port < 65536 or worker_port == quote_port:
        proc.terminate()
        raise RuntimeError(f"invalid harness ports: {info!r}")
    base = f"http://127.0.0.1:{worker_port}"
    try:
        identity = json.loads(urllib.request.urlopen(base + "/__control/identity", timeout=3).read())
    except Exception as exc:
        proc.terminate()
        raise RuntimeError(f"cannot read harness identity: {exc}") from exc
    if identity.get("pid") != proc.pid or identity.get("nonce") != info.get("nonce"):
        proc.terminate()
        raise RuntimeError(f"listener identity does not match child: {identity!r}")
    return proc, info, base


def assert_alive(proc: subprocess.Popen) -> None:
    rc = proc.poll()
    if rc is not None:
        output = ""
        if proc.stdout is not None:
            try:
                output = proc.stdout.read()[-800:]
            except Exception:
                pass
        raise RuntimeError(f"harness exited with {rc}: {output}")
