#!/usr/bin/env python3
"""Run the #130 full-universe replay as bounded batches (cross-platform).

Covers the union of all selected archived underlyings for Oct 5-7, 2026
(661 underlyings) in batches of at most 50 (MAX_UNDERLYINGS bound).
Each batch is a standard replay with --cache-dir reusing the source blobs.
Output: replay-130-full/batch-XX/ (one replay per batch).
Idempotent: a batch that already produced output is skipped.

Usage: python3 run-full-universe-batches.py [--cache-dir DIR] [--out DIR]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=HERE / "replay-cache")
    ap.add_argument("--out", type=Path, default=HERE / "replay-130-full")
    ap.add_argument("--batches", type=Path, default=HERE / "replay-130-batches.json")
    a = ap.parse_args()
    batches = json.loads(a.batches.read_text())
    a.out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(HERE)}
    for i, batch in enumerate(batches, 1):
        tag = f"batch-{i:02d}"
        dest = a.out / tag
        if (dest / "replay_summary.json").exists():
            print(f"{tag}: already done, skipping", flush=True)
            continue
        cmd = [sys.executable, str(HERE / "market_data" / "options_scanner" / "windows_replay.py"),
               "--days", "2026-10-05", "2026-10-06", "2026-10-07",
               "--underlyings"] + batch + [
               "--out", str(dest), "--cache-dir", str(a.cache_dir)]
        print(f"{tag}: {len(batch)} underlyings, starting", flush=True)
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        print(r.stdout.strip(), flush=True)
        if r.returncode != 0:
            print(f"{tag}: FAILED (exit {r.returncode})", flush=True)
            print(r.stderr[-2000:], flush=True)
            return 1
        print(f"{tag}: done", flush=True)
    print("ALL BATCHES DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
