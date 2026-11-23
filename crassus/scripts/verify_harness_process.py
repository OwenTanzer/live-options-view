#!/usr/bin/env python3
"""Focused checks for harness_process.start_harness's startup handshake.

Stand-in children (small Python scripts run in place of `node`) emit backend-
style diagnostic lines before, instead of, or around the handshake and serve
the /__control/identity endpoint on loopback, so every branch of the startup
contract is exercised without the real Worker harnesses.

    python scripts/verify_harness_process.py
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_process import start_harness  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


CHILD = textwrap.dedent('''
    import http.server, json, os, sys, threading, time
    MODE = sys.argv[1]
    nonce = "n-123"
    pid = os.getpid() + (1 if MODE == "wrong_pid" else 0)
    ident_nonce = "other" if MODE == "wrong_nonce" else nonce
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"pid": os.getpid(), "nonce": ident_nonce}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    w = lambda o: (print(o if isinstance(o, str) else json.dumps(o)), sys.stdout.flush())
    if MODE in ("diagnostics_first", "wrong_pid", "wrong_nonce", "garbage_first", "bad_host", "same_ports"):
        w({"event": "r2_write", "binding": "PAPER_TRADES", "outcome": "written", "caller": "bot_index",
           "key": "system/bot-membership-v1.json"})
        w({"event": "r2_write", "binding": "PAPER_TRADES", "outcome": "written", "caller": "bot_index",
           "key": "system/bot-membership-v1.json"})
    if MODE == "garbage_first":
        w("ExperimentalWarning: SQLite is an experimental feature")
    if MODE == "exit_before":
        w({"event": "r2_write", "outcome": "not_written"}); sys.exit(3)
    if MODE == "diagnostics_forever":
        while True:
            w({"event": "r2_write", "outcome": "written"}); time.sleep(0.05)
    if MODE == "silent":
        time.sleep(60)
    host = "0.0.0.0" if MODE == "bad_host" else "127.0.0.1"
    quote = port if MODE == "same_ports" else port + 1
    w({"ready": True, "pid": pid, "nonce": nonce, "host": host, "worker_port": port, "quote_port": quote})
    time.sleep(30)
''')

with tempfile.TemporaryDirectory() as tmp:
    script = Path(tmp) / "child.py"
    script.write_text(CHILD, encoding="utf-8")

    def launch(mode, timeout=10.0):
        # start_harness runs [node, script]; pass "python -u child.py MODE" via a wrapper file.
        wrapper = Path(tmp) / f"run_{mode}.py"
        wrapper.write_text(f"import runpy, sys\nsys.argv = ['child', {mode!r}]\n"
                           f"runpy.run_path({str(script)!r}, run_name='__main__')\n", encoding="utf-8")
        return start_harness(sys.executable, wrapper, timeout=timeout)

    def expect_error(mode, needle, timeout=10.0):
        t0 = time.monotonic()
        try:
            proc, _, _ = launch(mode, timeout)
            proc.terminate()
            return False, "no error", time.monotonic() - t0
        except RuntimeError as exc:
            return needle in str(exc), str(exc)[:200], time.monotonic() - t0

    proc, info, base = launch("diagnostics_first")
    check("r2_write diagnostics before the handshake are skipped", info["ready"] is True and base.startswith(
        "http://127.0.0.1:"), str(info))
    proc.terminate()
    proc, info, _ = launch("garbage_first")
    check("non-JSON diagnostic lines are skipped too", info["ready"] is True)
    proc.terminate()

    ok, msg, _ = expect_error("wrong_pid", "identity mismatch")
    check("a handshake with the wrong PID still fails (not skipped)", ok, msg)
    ok, msg, _ = expect_error("wrong_nonce", "listener identity does not match")
    check("identity endpoint nonce mismatch still fails", ok, msg)
    ok, msg, _ = expect_error("bad_host", "loopback")
    check("non-loopback host still fails", ok, msg)
    ok, msg, _ = expect_error("same_ports", "invalid harness ports")
    check("equal worker/quote ports still fail", ok, msg)
    ok, msg, _ = expect_error("exit_before", "exited with 3 before its handshake")
    check("child exit before handshake reports exit code", ok, msg)
    check("child exit reports the diagnostic lines", "r2_write" in msg, msg)
    ok, msg, elapsed = expect_error("diagnostics_forever", "within 2s", timeout=2.0)
    check("endless diagnostics hit ONE fixed deadline (not reset per line)", ok and elapsed < 4.0,
          f"{msg} after {elapsed:.1f}s")
    check("timeout reports diagnostics seen", "diagnostic line(s)" in msg and "r2_write" in msg, msg)
    ok, msg, elapsed = expect_error("silent", "within 2s", timeout=2.0)
    check("silent child times out at the deadline", ok and elapsed < 4.0, f"{msg} after {elapsed:.1f}s")

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
