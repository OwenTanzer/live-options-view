"""CLI: compute C/A measures and the toward/away outcome dataset from the
cached panel produced by run_audit.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from measures import compute_concentration_and_activity
from outcomes import assert_non_overlapping, build_outcome_dataset, generate_anchors

OUT_DIR = Path(__file__).parent / "out"


def main() -> int:
    option_rows = pd.read_parquet(OUT_DIR / "option_rows.parquet")
    spot_series = pd.read_parquet(OUT_DIR / "spot_series.parquet")
    print(f"Loaded {len(option_rows)} option rows, {len(spot_series)} spot rows from cache.")

    regular = option_rows[option_rows["regular_hours"]]
    print(f"Regular-hours option rows: {len(regular)}")

    measures = compute_concentration_and_activity(regular)
    measures.to_parquet(OUT_DIR / "measures.parquet", index=False)
    print(f"Computed measures for {len(measures)} (snapshot, strike) pairs.")
    print(measures[["C", "A", "distance", "side"]].describe())

    anchors, excluded = generate_anchors(spot_series)
    assert_non_overlapping(anchors)  # generated-data check, not just a unit test
    anchors.to_parquet(OUT_DIR / "anchors.parquet", index=False)
    excluded.to_parquet(OUT_DIR / "excluded_anchor_bins.parquet", index=False)
    print(f"\nGenerated {len(anchors)} anchors (verified non-overlapping); "
          f"{len(excluded)} candidate bins excluded.")
    print(anchors.groupby("date").size())
    print("\nExclusion reasons:")
    print(excluded["reason"].value_counts() if not excluded.empty else "(none)")

    outcome_ds = build_outcome_dataset(anchors, measures)
    outcome_ds.to_parquet(OUT_DIR / "outcome_dataset.parquet", index=False)
    print(f"\nBuilt outcome dataset: {len(outcome_ds)} (anchor, strike) rows.")
    print(outcome_ds.groupby("date").size())
    print(outcome_ds.groupby("side").size())
    print("\ntoward stats:")
    print(outcome_ds["toward"].describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
