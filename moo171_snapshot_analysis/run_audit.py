"""CLI: inventory + audit the five archived sessions per MOO-171 step 1.

Usage: railway run python run_audit.py
(needs R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME)

Writes a source manifest (out/source_manifest.json) alongside the audit
report: the exact object keys/etags/sizes used, the configuration constants
in force, and the code revision -- so a later reproduction can verify it
used the same bytes and configuration, not merely re-derive a similar-
looking result. This IS a real freeze of the *reviewed* configuration at
the moment this script is run; it is NOT a retroactive claim that the
original (pre-review) commit was frozen before its outcomes were inspected
-- it wasn't, and the report says so.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import outcomes
import panel
from baselines import STALENESS_SECONDS as BASELINE_STALENESS_SECONDS
from baselines import VOL_WINDOW_MINUTES
from panel import MAX_INTERVAL_SECONDS, REGULAR_CLOSE, REGULAR_OPEN, REPORTED_SNAPSHOT_COUNTS, load_raw_snapshots, recompute_interval_volume
from r2_source import SnapshotSource

OUT_DIR = Path(__file__).parent / "out"


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True,
        ).strip()
    except Exception:
        return None


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
        print(f"  premarket={audit.premarket_snapshots} regular={audit.regular_hours_snapshots} "
              f"afterhours={audit.afterhours_snapshots}")
        print(f"  max gap (regular hours): {audit.max_gap_seconds}s, median: {d['median_gap_seconds']}s")
        print(f"  distinct contracts={audit.distinct_contracts} distinct strikes={audit.distinct_strikes}")
        print(f"  rows_total={audit.rows_total}")
        print(f"  field coverage: {audit.field_coverage}")
        print(f"  nonfinite_greeks={audit.nonfinite_greeks} negative_volumes={audit.negative_volumes} "
              f"negative_oi={audit.negative_open_interest} crossed_quotes={audit.crossed_quotes}")
        print(f"  duplicate_snapshot_symbol_rows={audit.duplicate_snapshot_symbol_rows} "
              f"symbol_mismatch_rows={audit.symbol_mismatch_rows} "
              f"spot_inconsistent_snapshots={audit.spot_inconsistent_snapshots}")
        print(f"  contracts_with_oi_change={audit.contracts_with_oi_change} "
              f"(of {audit.distinct_contracts}); contracts_with_oi_baseline={audit.contracts_with_oi_baseline}")
        print(f"  max_repeated_mid_run={audit.max_repeated_mid_run}")

    flag_counts = option_rows["dv_flag"].value_counts().to_dict()
    report["flag_counts_overall"] = flag_counts
    print("\n=== dv_flag counts across all sessions ===")
    for flag, count in flag_counts.items():
        print(f"  {flag}: {count}")

    with open(OUT_DIR / "audit_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Frozen source manifest: exact keys/etags/sizes for every object this
    # run actually used (snapshot objects only -- excluded non-snapshot
    # objects are recorded separately per session in audit_report.json),
    # plus the configuration constants and code revision in force.
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_revision": _git_revision(),
        "config": {
            "MAX_INTERVAL_SECONDS": MAX_INTERVAL_SECONDS,
            "REGULAR_OPEN": REGULAR_OPEN,
            "REGULAR_CLOSE": REGULAR_CLOSE,
            "ANCHOR_INTERVAL_MINUTES": outcomes.ANCHOR_INTERVAL_MINUTES,
            "OUTCOME_HORIZON_MINUTES": outcomes.OUTCOME_HORIZON_MINUTES,
            "MAX_STALENESS_SECONDS": outcomes.MAX_STALENESS_SECONDS,
            "STRIKES_PER_SIDE": outcomes.STRIKES_PER_SIDE,
            "VOL_WINDOW_MINUTES": VOL_WINDOW_MINUTES,
            "BASELINE_STALENESS_SECONDS": BASELINE_STALENESS_SECONDS,
            "CONTRACT_MULTIPLIER": 100,
        },
        "objects": {
            yyyymmdd: source.snapshot_objects(yyyymmdd) for yyyymmdd in dates
        },
    }
    with open(OUT_DIR / "source_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nWrote {OUT_DIR / 'audit_report.json'}, {OUT_DIR / 'source_manifest.json'}, "
          f"option_rows.parquet, spot_series.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
