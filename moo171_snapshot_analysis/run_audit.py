"""CLI: inventory + audit the five archived sessions per MOO-171 step 1.

Usage: railway run python run_audit.py
(needs R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from panel import REPORTED_SNAPSHOT_COUNTS, load_raw_snapshots, recompute_interval_volume
from r2_source import SnapshotSource

OUT_DIR = Path(__file__).parent / "out"


def main() -> int:
    dates = sorted(REPORTED_SNAPSHOT_COUNTS)
    source = SnapshotSource()

    print(f"Loading {len(dates)} sessions from R2 (cached to {source.cache_dir})...")
    option_rows, spot_series, audits = load_raw_snapshots(source, dates)
    print(f"Loaded {len(option_rows)} option rows, {len(spot_series)} spot observations.")

    print("Recomputing interval volume...")
    option_rows = recompute_interval_volume(option_rows)

    OUT_DIR.mkdir(exist_ok=True)
    option_rows.to_parquet(OUT_DIR / "option_rows.parquet", index=False)
    spot_series.to_parquet(OUT_DIR / "spot_series.parquet", index=False)

    report = {"sessions": {}, "flag_counts_overall": {}}
    for audit in audits:
        d = audit.to_dict()
        gaps = audit.gap_seconds
        d["median_gap_seconds"] = sorted(gaps)[len(gaps) // 2] if gaps else None
        d["count_reconciled"] = audit.snapshot_count == audit.reported_count
        report["sessions"][audit.date] = d
        print(f"\n=== {audit.date} ===")
        print(f"  snapshots: {audit.snapshot_count} (reported: {audit.reported_count}, "
              f"reconciled: {d['count_reconciled']})")
        print(f"  excluded non-snapshot objects: {audit.excluded_non_snapshot}")
        print(f"  window ET: {audit.first_ts_et} .. {audit.last_ts_et}")
        print(f"  premarket={audit.premarket_snapshots} regular_hours={audit.regular_hours_snapshots}")
        print(f"  max gap (regular hours): {audit.max_gap_seconds}s, median: {d['median_gap_seconds']}s")
        print(f"  distinct contracts={audit.distinct_contracts} distinct strikes={audit.distinct_strikes}")
        print(f"  rows_total={audit.rows_total}")
        print(f"  field coverage: {audit.field_coverage}")
        print(f"  nonfinite_greeks={audit.nonfinite_greeks} negative_volumes={audit.negative_volumes} "
              f"negative_oi={audit.negative_open_interest} crossed_quotes={audit.crossed_quotes}")

    flag_counts = option_rows["dv_flag"].value_counts().to_dict()
    report["flag_counts_overall"] = flag_counts
    print("\n=== dv_flag counts across all sessions ===")
    for flag, count in flag_counts.items():
        print(f"  {flag}: {count}")

    with open(OUT_DIR / "audit_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nWrote {OUT_DIR / 'audit_report.json'}, option_rows.parquet, spot_series.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
