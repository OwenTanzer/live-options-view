#!/usr/bin/env python3
"""Build the S3b grid dataset from the archive (read-only R2), per PROTOCOL.md.

For every QQQ pool day and SPY day: at each 5-minute mark 09:45-15:55 ET take the latest
snapshot at or before it, plus that snapshot's predecessor (needed for the frozen-feed gate).
Apply the Replay gates (age <= 150 s, not frozen, parity-consistent) and keep spot plus the
bid/ask of the five strikes nearest spot, both sides.

    python research/options_replay/calibration/dataset.py            # both symbols
    python research/options_replay/calibration/dataset.py --symbol SPY

Writes .cache/calibration/{SYMBOL}_{YYYYMMDD}.json (gitignored). Dropped grid points are
kept with their reason, never filled.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import archive as A  # noqa: E402

GRID_S3B = [9 * 3600 + 45 * 60 + 300 * i for i in range(75)]      # 09:45 .. 15:55 (S3b dataset)
GRID_S3C = [9 * 3600 + 45 * 60 + 300 * i for i in range(78)]      # 09:45 .. 16:10 (S3c: endpoint window)
GRID = GRID_S3B
PROTOCOL = json.loads((HERE / "protocol.json").read_text())


def day_keys(arc: A.Archive, symbol: str, yyyymmdd: str) -> list[tuple[int, str]]:
    prefix = f"intraday/{yyyymmdd}/" if symbol == "QQQ" else f"intraday/spy/{yyyymmdd}/"
    out = []
    for k in arc.list_keys(prefix):
        name = k.rsplit("/", 1)[-1]
        if not (name.startswith("snapshot_") and name.endswith(".csv")):
            continue
        stamp = name[len("snapshot_"):][:6]
        if stamp.isdigit():
            out.append((int(stamp[:2]) * 3600 + int(stamp[2:4]) * 60 + int(stamp[4:6]), k))
    return sorted(out)


def build_day(arc: A.Archive, symbol: str, yyyymmdd: str, grid: list[int] | None = None) -> dict:
    grid = grid or GRID
    iso = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    keys = day_keys(arc, symbol, yyyymmdd)
    times = [t for t, _ in keys]
    need: dict[str, None] = {}
    plan = []
    import bisect
    for g in grid:
        i = bisect.bisect_right(times, g) - 1
        if i < 0:
            plan.append((g, None, None))
            continue
        cur, prev = keys[i], (keys[i - 1] if i > 0 else None)
        need[cur[1]] = None
        if prev:
            need[prev[1]] = None
        plan.append((g, cur, prev))
    bodies = dict(zip(need, arc.get_many(list(need))))
    parsed = {k: A.parse_snapshot(b, iso) for k, b in bodies.items()}
    points = []
    for g, cur, prev in plan:
        rec = {"t": g}
        if cur is None:
            rec["drop"] = "no snapshot"
            points.append(rec)
            continue
        snap = {"t": cur[0], "key": cur[1], **parsed[cur[1]]}
        A.assess([snap])
        age = g - cur[0]
        if age > A.MAX_QUOTE_AGE_S:
            rec["drop"] = f"stale ({age}s)"
        elif prev is not None and parsed[prev[1]]["spot"] == snap["spot"] and parsed[prev[1]]["q"] == snap["q"] \
                and g - prev[0] > A.MAX_QUOTE_AGE_S:
            rec["drop"] = "frozen"
        elif not snap["ok"]:
            rec["drop"] = snap["reason"]
        if "drop" in rec:
            points.append(rec)
            continue
        spot = snap["spot"]
        strikes = sorted({A.contract_strike(c) for c in snap["q"]}, key=lambda k: abs(k - spot))[:5]
        rec.update({"snap_t": cur[0], "spot": spot, "parity_spot": snap["parity_spot"],
                    "q": {c: snap["q"][c] for k in sorted(strikes) for c in ("C" + A._fmt_strike(k), "P" + A._fmt_strike(k))
                          if c in snap["q"]}})
        points.append(rec)
    return {"symbol": symbol, "day": iso, "grid_points": len(grid),
            "kept": sum(1 for p in points if "drop" not in p), "points": points}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", choices=["QQQ", "SPY", "both"], default="both")
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--grid", choices=["s3b", "s3c"], default="s3b",
                    help="s3b: 09:45-15:55 into calibration/; s3c: 09:45-16:10 into calibration_s3c/")
    args = ap.parse_args()
    arc = A.Archive(args.cache)
    grid = GRID_S3C if args.grid == "s3c" else GRID_S3B
    out = Path(args.cache) / ("calibration_s3c" if args.grid == "s3c" else "calibration")
    out.mkdir(parents=True, exist_ok=True)
    jobs = []
    if args.symbol in ("QQQ", "both"):
        pool = json.loads((Path(args.cache) / "qualified_days.json").read_text())["qualified"]
        jobs += [("QQQ", d.replace("-", "")) for d in pool]
    if args.symbol in ("SPY", "both"):
        spy = sorted({k.split("/")[2] for k in arc.list_keys("intraday/spy/")
                      if len(k.split("/")) > 3 and k.split("/")[2].isdigit()})
        jobs += [("SPY", d) for d in spy
                 if datetime.strptime(d, "%Y%m%d").weekday() < 5 and not A.is_holiday(datetime.strptime(d, "%Y%m%d").date())]
    todo = [(s, d) for s, d in jobs if not (out / f"{s}_{d}.json").exists()]
    print(f"{len(jobs)} symbol-days, {len(todo)} to build", flush=True)

    def run(job):
        s, d = job
        rec = build_day(arc, s, d, grid)
        (out / f"{s}_{d}.json").write_text(json.dumps(rec))
        return s, d, rec["kept"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for n, (s, d, kept) in enumerate(pool.map(run, todo), 1):
            print(f"[{n}/{len(todo)}] {s} {d}: {kept}/{len(grid)} grid points kept", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
