"""CLI: inventory + audit the five archived sessions per MOO-171 step 1.

Usage: railway run python run_audit.py
(needs R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME)

Writes a source manifest (out/source_manifest.json) alongside the audit
report. This identifies the actual executed source two ways, not one --
because a base git commit hash alone cannot identify a run made against an
uncommitted (dirty) working tree, and requiring the manifest to embed the
hash of the very commit that will contain it is self-referential and
unresolvable:

1. `git_base_revision` + `git_dirty`: the base commit HEAD was on, and
   whether the working tree differed from it at run time.
2. `source_file_sha256`: a sha256 of every *.py file in this module's
   directory AS ACTUALLY READ AT RUN TIME -- this is what makes the
   manifest verifiable regardless of git/commit state, since it is a direct
   content fingerprint of the code that executed, not a derived claim about
   which commit that code belongs to.

Object entries also carry a `sha256` of the exact bytes `_get_bytes`
returned for that key THIS run (see r2_source.SnapshotSource), not merely
the key/size/etag copied from the R2 listing -- a saved object key alone
does not verify the bytes actually parsed.

This IS a real freeze of the *reviewed* configuration and code at the
moment this script is run; it is NOT a retroactive claim that the original
(pre-review) commit was frozen before its outcomes were inspected -- it
wasn't, and the report says so.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import outcomes
import panel
from baselines import STALENESS_SECONDS as BASELINE_STALENESS_SECONDS
from baselines import VOL_WINDOW_MINUTES
from panel import (
    CONTRACT_MULTIPLIER_EVIDENCE, MAX_INTERVAL_SECONDS, REGULAR_CLOSE, REGULAR_OPEN,
    REPORTED_SNAPSHOT_COUNTS, load_raw_snapshots, recompute_interval_volume,
)
from r2_source import SnapshotSource

MODULE_DIR = Path(__file__).parent
OUT_DIR = MODULE_DIR / "out"


def _git_base_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=MODULE_DIR, text=True,
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=MODULE_DIR, text=True,
        )
        return len(status.strip()) > 0
    except Exception:
        return None


def _source_file_sha256() -> dict[str, str]:
    """sha256 of every *.py file in this directory as actually read at run
    time -- a content fingerprint of the executed code, independent of
    whether it has been committed."""
    hashes = {}
    for path in sorted(MODULE_DIR.glob("*.py")):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _representative_reconciliation(option_rows) -> dict:
    """Hand-reconciles ONE real (date, snapshot, Strike) group's C value
    against the C[k,t] formula, summed here directly over its raw source
    rows -- independently of measures.py (not by calling it and printing
    its own answer back at itself). A real 0DTE strike almost always has
    both a call and a put contract, so this picks the first regular-hours
    group with at least one valid contract and reconciles the FULL sum
    (every contributing row shown individually), rather than requiring the
    contrived case of exactly one contract.
    """
    import numpy as np

    df = option_rows[option_rows["regular_hours"]].copy()
    gamma_ok = df["Gamma"].notna() & np.isfinite(df["Gamma"]) & (df["Gamma"] >= 0)
    oi_ok = df["OpenInterest"].notna() & np.isfinite(df["OpenInterest"]) & (df["OpenInterest"] >= 0)
    df["_valid"] = gamma_ok & oi_ok
    multiplier = 100

    for (date, snap, strike), group in df.groupby(["date", "snapshot_key", "Strike"]):
        valid = group[group["_valid"]]
        if valid.empty:
            continue
        spot = float(valid.iloc[0]["UnderlyingPrice"])
        contributions = []
        manual_c = 0.0
        for _, row in valid.iterrows():
            oi, gamma = float(row["OpenInterest"]), float(row["Gamma"])
            contribution = multiplier * oi * gamma
            manual_c += contribution
            contributions.append({
                "OptionSymbol": row["OptionSymbol"], "Type": row.get("Type"),
                "OpenInterest": oi, "Gamma": gamma,
                "contribution_before_spot_squared": contribution,
            })
        manual_c *= spot ** 2
        return {
            "date": date, "snapshot_key": snap, "strike": float(strike),
            "UnderlyingPrice": spot, "contract_multiplier_used": multiplier,
            "n_valid_contracts_at_strike": len(valid),
            "contributing_rows": contributions,
            "formula": "C[k,t] = UnderlyingPrice^2 * sum_over_valid_contracts(multiplier * OpenInterest * Gamma)",
            "manual_result": manual_c,
            "note": (
                "Computed directly from the raw source rows here, independently "
                "of measures.compute_concentration_and_activity -- run_measures.py's "
                "output for this exact (date, snapshot_key, Strike) should equal "
                "this value exactly."
            ),
        }
    return {"note": "no strike with a valid contract found in this archive to reconcile"}


def _multiplier_evidence(audits: list) -> dict:
    """The archive itself cannot prove the deliverable multiplier (that is
    an external fact about the option contract, not encoded in the CSV) --
    what the archive CAN provide is evidence against the presence of any
    non-standard (adjusted) contract, which is the case where multiplier
    100 would be wrong. Every retained OptionSymbol matches the plain OCC
    root+YYMMDD+C/P+strike*1000 pattern (panel.parse_option_symbol) with
    zero symbol_mismatch_rows across all sessions -- adjusted-deliverable
    contracts are conventionally flagged with a differently-shaped symbol
    (e.g. a numeric suffix on the root) that would fail this exact regex
    and be counted as a mismatch instead. See CONTRACT_MULTIPLIER_EVIDENCE
    in panel.py for the standing documentation this supplements.
    """
    total_symbol_mismatches = sum(a.symbol_mismatch_rows for a in audits)
    total_rows = sum(a.rows_total for a in audits)
    return {
        "claim": CONTRACT_MULTIPLIER_EVIDENCE,
        "supporting_check": "panel.parse_option_symbol against every retained OptionSymbol",
        "symbol_mismatch_rows_across_all_sessions": total_symbol_mismatches,
        "total_option_rows_checked": total_rows,
        "interpretation": (
            "0 symbol_mismatch_rows means no retained symbol failed the standard "
            "unadjusted OCC pattern -- consistent with (not an independent proof "
            "of) a uniform 100-share deliverable across every contract observed."
        ),
    }


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

    # Frozen source manifest: exact keys/verified-sha256/etags/sizes for
    # every object this run actually used (snapshot objects only --
    # excluded non-snapshot objects are recorded separately per session in
    # audit_report.json), the configuration constants in force, and TWO
    # independent identifications of the executed source code (see module
    # docstring: a base-commit+dirty flag is not sufficient by itself).
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_base_revision": _git_base_revision(),
        "git_dirty": _git_dirty(),
        "source_file_sha256": _source_file_sha256(),
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
            yyyymmdd: source.verified_manifest(yyyymmdd) for yyyymmdd in dates
        },
        "representative_reconciliation": _representative_reconciliation(option_rows),
        "multiplier_evidence": _multiplier_evidence(audits),
    }
    with open(OUT_DIR / "source_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nWrote {OUT_DIR / 'audit_report.json'}, {OUT_DIR / 'source_manifest.json'}, "
          f"option_rows.parquet, spot_series.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
