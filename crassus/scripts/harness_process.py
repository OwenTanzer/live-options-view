"""Start a local Worker harness from its stdout handshake.

The harness binds loopback ports chosen by the OS and announces its PID, nonce
and both ports. The parent verifies the identity endpoint on the announced
worker port before any test request, so an unrelated listener can never be
mistaken for the child. A child exit is surfaced with its captured output.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import urllib.request
from pathlib import Path


def start_harness(node: str, script: Path, timeout: float = 25.0):
    proc = subprocess.Popen(
        [node, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    try:
        line = lines.get(timeout=timeout)
    except queue.Empty:
        proc.terminate()
        raise RuntimeError("harness did not emit a startup handshake")
    try:
        info = json.loads(line)
    except ValueError:
        proc.terminate()
        raise RuntimeError(f"harness handshake is not JSON: {line[:200]!r}")
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
