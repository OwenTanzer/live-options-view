"""Monthly cost projection for the News pilot, from measured cycles (news_cycle_cost_measure.py).

    python tests/news_cost_projection.py MEASUREMENTS.json [--months 12] [--interval-min 15]

Everything printed is labelled OBSERVED (taken from the measurement file) or PROJECTED (arithmetic on
it plus the stated assumptions). Prices are the published list prices checked 2026-11-14 (Cloudflare
R2 Standard; Railway standard services) and are inputs here, not measurements. The R2 free tier is
account-wide and already shared with the market collectors, so it is NOT subtracted: these are
incremental costs as if no free allowance were left.

Model (bounded run listing, checkpoint frontier):
  * Per-cycle requests are the measured steady-state cycle (the last measured cycle). Its listings of
    news/v1/runs/ start after the checkpoint frontier, so they stay one request each however long the
    history (tests/test_news_checkpoint.py BoundedListing measures this over 1,500 runs).
  * news/v1/runs/ grows by RUN_OBJECTS_PER_CYCLE objects per cycle (measured: 4) and is never pruned.
    Once per FULL_VERIFY_S (24 h) a writer lists it in full FULL_LISTINGS_PER_VERIFICATION times (the
    verifying read and its save), one ListObjectsV2 request per 1,000 keys: the only growing term. That
    verification compares listing tags; it re-reads only changed runs, so it adds no per-run GETs.
  * Failure headroom: every request count is multiplied by FAILURE_FACTOR (a retried or failed cycle
    repeats work; a 412 is billed as Class A).
  * Compute: each cycle runs RUNTIME_S (projected: measured local wall time plus REQUEST_LATENCY_S per
    request, serial) at the measured peak memory and CPU_SHARE of a vCPU, billed per minute of use.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

PRICES = {   # list prices, checked 2026-11-14
    "r2_class_a_per_million": 4.50, "r2_class_b_per_million": 0.36, "r2_storage_gb_month": 0.015,
    "railway_vcpu_minute": 0.000463, "railway_gb_ram_minute": 0.000231,
}
RUN_OBJECTS_PER_CYCLE = 4
FULL_LISTINGS_PER_VERIFICATION = 2
VERIFICATIONS_PER_DAY = 1
FAILURE_FACTOR = 1.25
REQUEST_LATENCY_S = 0.15        # assumed per R2 request from the scheduler host (NOT measured)
FEED_FETCH_S = 10.0             # assumed for four live feeds (one live local cycle took ~2 s)
CPU_SHARE = 0.5                 # assumed vCPU fraction while running
PEAK_RAM_GB = 0.1               # measured ~0.03 GB (one fixture cycle on Linux); 0.1 GB assumed with headroom
ITEMS_PER_DAY = 35              # live feeds' own rates, 2026-11-14 (a Saturday)
PRIVATE_BYTES_PER_ITEM = 4000   # assumed (82 live items bootstrap: ~1.0 MB private incl. one snapshot)


def classes(row, side):
    a = sum(v for k, v in row[side]["requests"].items() if k.startswith("A:"))
    b = sum(v for k, v in row[side]["requests"].items() if k.startswith("B:"))
    return a, b


def project(meas: dict, months: int, interval_min: int) -> dict:
    rows = meas["rows"]
    last = rows[-1]
    n_measured = len(rows)
    pa, pb = classes(last, "private")
    qa, qb = classes(last, "public")
    base_a = pa + qa
    base_b = pb + qb
    private_growth = (rows[-1]["private"]["bytes"] - rows[min(10, n_measured - 1)]["private"]["bytes"]) / max(1, n_measured - 1 - min(10, n_measured - 1))
    public_bytes = last["public"]["bytes"]
    requests = (pa + qa + pb + qb) * FAILURE_FACTOR
    runtime_s = last["wall_s"] + FEED_FETCH_S + requests * REQUEST_LATENCY_S
    cycles_per_month = int(30 * 24 * 60 / interval_min)
    out = {"observed": {
        "measured_cycles": n_measured, "feeds": meas["feeds"], "expiry_simulated": meas["expiry_simulated"],
        "last_cycle_class_a": pa + qa, "last_cycle_class_b": pb + qb,
        "private_bytes_per_cycle": round(private_growth), "public_bytes_held": public_bytes,
        "public_objects_held": last["public"]["objects"], "local_wall_s_last_cycle": last["wall_s"]},
        "assumptions": {"failure_factor": FAILURE_FACTOR, "request_latency_s": REQUEST_LATENCY_S,
                        "feed_fetch_s": FEED_FETCH_S, "cpu_share": CPU_SHARE, "peak_ram_gb": PEAK_RAM_GB,
                        "items_per_day": ITEMS_PER_DAY, "private_bytes_per_item": PRIVATE_BYTES_PER_ITEM,
                        "interval_min": interval_min, "free_tier_subtracted": False, "prices": PRICES},
        "projected": {"runtime_s_per_cycle": round(runtime_s, 1), "months": []}}
    stored = 0.0
    for m in range(1, months + 1):
        first, last_cycle = cycles_per_month * (m - 1), cycles_per_month * m
        cycles_per_day = cycles_per_month // 30
        run_pages = sum(FULL_LISTINGS_PER_VERIFICATION * max(1, math.ceil(RUN_OBJECTS_PER_CYCLE * n / 1000))
                        for n in range(first, last_cycle, max(1, cycles_per_day // VERIFICATIONS_PER_DAY)))
        class_a = (base_a * cycles_per_month + run_pages) * FAILURE_FACTOR
        class_b = base_b * cycles_per_month * FAILURE_FACTOR
        stored += private_growth * cycles_per_month + ITEMS_PER_DAY * 30 * PRIVATE_BYTES_PER_ITEM
        gb = (stored + public_bytes) / 1e9
        r2 = (class_a / 1e6 * PRICES["r2_class_a_per_million"] + class_b / 1e6 * PRICES["r2_class_b_per_million"]
              + gb * PRICES["r2_storage_gb_month"])
        minutes = cycles_per_month * runtime_s / 60
        railway = minutes * (CPU_SHARE * PRICES["railway_vcpu_minute"] + PEAK_RAM_GB * PRICES["railway_gb_ram_minute"])
        out["projected"]["months"].append({
            "month": m, "class_a": round(class_a), "class_b": round(class_b),
            "full_listing_share_of_class_a": round(run_pages * FAILURE_FACTOR / class_a, 3),
            "stored_gb_end": round(gb, 3), "r2_usd": round(r2, 2), "railway_usd": round(railway, 2),
            "total_usd": round(r2 + railway, 2)})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("measurements")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--interval-min", type=int, default=15)
    a = ap.parse_args(argv)
    meas = json.loads(Path(a.measurements).read_text(encoding="utf-8"))
    print(json.dumps(project(meas, a.months, a.interval_min), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
