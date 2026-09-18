"""Bounded child-process recovery for the Railway Tradier cron worker.

Railway's effective cron restart policy is NEVER. Retry failed collector
processes up to ten times, waiting beyond the lease TTL before reacquisition.
Clean exits and operator termination never retry. The collector retains
calendar gating, ownership checks, partial status, and spool reconciliation.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

def run(command, *, retries=10, delay=310):
    stopped = threading.Event()
    child = None

    def terminate(signum, _frame):
        stopped.set()
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    prior = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for attempt in range(retries + 1):
            if stopped.is_set():
                return 0
            child = subprocess.Popen(command)
            # Handle a signal received between the preceding check and spawn.
            if stopped.is_set():
                child.terminate()
            while True:
                try:
                    rc = child.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if stopped.is_set():
                        try:
                            child.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        return 0
            if stopped.is_set() or rc == 0:
                return 0
            print(json.dumps({"event": "collector_process_failed", "attempt": attempt + 1,
                              "returncode": rc, "retries_remaining": retries - attempt}), flush=True)
            if attempt == retries:
                return 1
            if stopped.wait(delay):
                return 0
        return 1
    finally:
        for sig, handler in prior.items():
            signal.signal(sig, handler)

if __name__ == "__main__":
    collector = Path(__file__).resolve().parent / "scripts" / "moo144_tradier_collector.py"
    lease_ttl = int(os.environ.get("MOO144_LEASE_TTL_SECONDS", "300"))
    raise SystemExit(run([sys.executable, "-u", str(collector)], delay=max(310, lease_ttl + 10)))
