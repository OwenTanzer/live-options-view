"""Crypto collector CLI.

Deployed command (hourly, continuous 24/7 -- crypto has no equity
session close)::

    python -u -m market_data.crypto_collector run --publish

``run`` collects once and writes ``crypto/latest.json`` plus the
``crypto/latest.provenance.json`` sidecar to ``--out`` (default
``./crypto-out``). ``--publish`` additionally uploads both objects
to R2 (see :mod:`publish` for credentials); without it, nothing
leaves the machine. ``loop`` repeats ``run`` every ``--interval``
seconds (default 3600). ``smoke`` fetches the providers and prints a
one-line summary without writing anything.

``--previous`` points at a previous ``latest.json``; it supplies the
"jump since last update" and dominance-shift patterns. Without it
those two detectors are skipped on the first run, not invented.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .collector import collect, utcnow
from .publish import publish_r2, write_local


def _load_previous(path):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring unreadable --previous {path}: {exc}",
              file=sys.stderr)
        return None


def run_once(*, out, publish, previous):
    snapshot = collect(previous=previous)
    counts = snapshot["counts"]
    print(f"crypto snapshot {snapshot['constructed_at']}: "
          f"{counts['available']} ok, {counts['partial']} partial, "
          f"{counts['source_unavailable']} source-unavailable, "
          f"{len(snapshot['patterns'])} patterns", flush=True)
    for row in snapshot["coins"]:
        if row["status"] != "ok":
            print(f"  {row['symbol']}: {row['status']} -- "
                  f"{row['status_reason']}", flush=True)
    result = write_local(out, snapshot)
    print(f"wrote {result['latest']} and {result['sidecar']}", flush=True)
    if publish:
        publish_r2(snapshot)
        print("published crypto/latest.json to R2", flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
            ("run", "collect once, write local files, optionally publish"),
            ("loop", "repeat run on an hourly cycle (continuous, no close)"),
            ("smoke", "fetch providers and print a summary; write nothing")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--out", type=Path, default=Path("./crypto-out"))
        cmd.add_argument("--previous", type=Path, default=None,
                         help="previous crypto/latest.json for jump patterns")
        cmd.add_argument("--publish", action="store_true",
                         help="also upload latest.json + sidecar to R2")
        if name == "loop":
            cmd.add_argument("--interval", type=int, default=3600,
                             help="seconds between runs (default 3600)")
    args = parser.parse_args(argv)
    previous = _load_previous(args.previous)
    if args.command == "smoke":
        snapshot = collect(previous=previous)
        counts = snapshot["counts"]
        print(json.dumps({
            "constructed_at": snapshot["constructed_at"],
            "counts": counts,
            "patterns": len(snapshot["patterns"]),
            "fear_greed": snapshot["market"]["fear_greed"],
        }, indent=2))
        return 0
    if args.command == "loop":
        while True:
            started = utcnow()
            run_once(out=args.out, publish=args.publish, previous=previous)
            previous_path = args.out / "crypto" / "latest.json"
            previous = _load_previous(previous_path)
            elapsed = (utcnow() - started).total_seconds()
            time.sleep(max(0, args.interval - elapsed))
    return run_once(out=args.out, publish=args.publish, previous=previous)


if __name__ == "__main__":
    sys.exit(main())
