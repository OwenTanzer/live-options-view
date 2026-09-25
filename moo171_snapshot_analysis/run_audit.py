"""CLI: inventory + audit the five archived sessions per MOO-171 step 1.

Usage:
  python run_audit.py            # reproduce/verify against the frozen out/source_manifest.json
  python run_audit.py --new-run  # deliberately start a new run and write a NEW manifest
(a new run, or a verify run with an empty cache, needs R2_ACCOUNT_ID /
R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME, e.g. via `railway run`)

Default (reproduction) mode treats the committed source_manifest.json as the
frozen run and checks against it BEFORE anything under out/ is replaced:
config and source-file identity must match, the listed snapshot keys must
equal the frozen keys exactly, and every snapshot's bytes must hash to the
frozen sha256 (checked in r2_source before the bytes are parsed). Any
mismatch exits non-zero with every out/ file -- including the frozen
manifest -- left untouched. On success the manifest is retained unchanged
and out/reproduction_check.json records the verification. Only `--new-run`
writes a new manifest.

The manifest identifies the actual executed source two ways, not one --
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
returned for that key (see r2_source.SnapshotSource), not merely the
key/size/etag copied from the R2 listing -- a saved object key alone does
not verify the bytes actually parsed.

This IS a real freeze of the *reviewed* configuration and code at the
moment a new run is made; it is NOT a retroactive claim that the original
(pre-review) commit was frozen before its outcomes were inspected -- it
wasn't, and the report says so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import outcomes
from baselines import STALENESS_SECONDS as BASELINE_STALENESS_SECONDS
from baselines import VOL_WINDOW_MINUTES
from measures import CONTRACT_MULTIPLIER, compute_concentration_and_activity
from panel import (
    CONTRACT_MULTIPLIER_EVIDENCE, MAX_INTERVAL_SECONDS, REGULAR_CLOSE, REGULAR_OPEN,
    REPORTED_SNAPSHOT_COUNTS, load_raw_snapshots, recompute_interval_volume,
)
from r2_source import ManifestMismatch, SnapshotSource

MODULE_DIR = Path(__file__).parent
OUT_DIR = MODULE_DIR / "out"
RECONCILIATION_REL_TOL = 1e-9


class ReconciliationMismatch(RuntimeError):
    """A hand reconciliation disagrees with the measures output."""


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
    whether it has been committed.

    Source identity is defined over the canonical LF bytes that git stores
    and that .gitattributes forces into every checkout. A CRLF source file
    (e.g. a checkout made before that policy, or an editor re-saving with
    CRLF) would record/verify a platform-dependent hash that a fresh
    checkout cannot reproduce, so it is refused rather than hashed."""
    hashes = {}
    crlf = []
    for path in sorted(MODULE_DIR.glob("*.py")):
        body = path.read_bytes()
        if b"\r" in body:
            crlf.append(path.name)
        hashes[path.name] = hashlib.sha256(body).hexdigest()
    if crlf:
        raise ManifestMismatch(
            f"source files with CR line endings (not the canonical LF checkout): {crlf}; "
            "re-checkout with the module's .gitattributes (eol=lf) before running"
        )
    return hashes


def _usable_nonneg(value) -> bool:
    return value is not None and not pd.isna(value) and np.isfinite(value) and value >= 0


def _measure_value(measures_df: pd.DataFrame, date, snap, strike, column: str) -> float:
    hit = measures_df[
        (measures_df["date"] == date) & (measures_df["snapshot_key"] == snap)
        & (measures_df["Strike"] == strike)
    ]
    if len(hit) != 1:
        raise ReconciliationMismatch(
            f"{column}: expected exactly one measures row for ({date}, {snap}, {strike}), found {len(hit)}"
        )
    return float(hit.iloc[0][column])


def _record_agreement(result: dict, measure_value: float) -> dict:
    manual = result["manual_result"]
    finite = bool(np.isfinite(measure_value))
    result["measure_output"] = measure_value
    result["abs_diff"] = abs(manual - measure_value) if finite else None
    result["rel_tol"] = RECONCILIATION_REL_TOL
    result["agrees"] = finite and math.isclose(manual, measure_value, rel_tol=RECONCILIATION_REL_TOL)
    return result


def _reconcile_c(regular: pd.DataFrame, measures_df: pd.DataFrame) -> dict:
    """ONE real (date, snapshot, Strike) C value summed directly over its raw
    source rows, then compared with the measures output for that same key.
    A real 0DTE strike almost always has both a call and a put contract, so
    this reconciles the FULL sum (every contributing row shown), not the
    contrived one-contract case."""
    valid_mask = [
        _usable_nonneg(g) and _usable_nonneg(oi) for g, oi in zip(regular["Gamma"], regular["OpenInterest"])
    ]
    valid_rows = regular[valid_mask]
    if valid_rows.empty:
        return {"note": "no strike with a valid OI/Gamma contract found to reconcile"}
    first = valid_rows.sort_values(["date", "snapshot_key", "Strike"]).iloc[0]
    date, snap, strike = first["date"], first["snapshot_key"], first["Strike"]
    valid = valid_rows[
        (valid_rows["date"] == date) & (valid_rows["snapshot_key"] == snap) & (valid_rows["Strike"] == strike)
    ]

    spot = float(valid.iloc[0]["UnderlyingPrice"])
    contributions = []
    manual = 0.0
    for _, row in valid.iterrows():
        oi, gamma = float(row["OpenInterest"]), float(row["Gamma"])
        contribution = CONTRACT_MULTIPLIER * oi * gamma
        manual += contribution
        contributions.append({
            "OptionSymbol": row["OptionSymbol"], "Type": row.get("Type"),
            "OpenInterest": oi, "Gamma": gamma,
            "contribution_before_spot_squared": contribution,
        })
    result = {
        "date": date, "snapshot_key": snap, "strike": float(strike),
        "UnderlyingPrice": spot, "contract_multiplier_used": CONTRACT_MULTIPLIER,
        "n_valid_contracts_at_strike": len(valid),
        "contributing_rows": contributions,
        "formula": "C[k,t] = UnderlyingPrice^2 * sum_over_valid_contracts(multiplier * OpenInterest * Gamma)",
        "manual_result": manual * spot ** 2,
    }
    return _record_agreement(result, _measure_value(measures_df, date, snap, strike, "C"))


def _prior_observation(option_rows: pd.DataFrame, date, symbol, ts_et):
    """Latest same-session observation of this contract strictly before
    ts_et, looked up directly in the raw rows (any session phase -- the
    interval ending at the first regular snapshot starts in premarket)."""
    prior = option_rows[
        (option_rows["date"] == date) & (option_rows["OptionSymbol"] == symbol)
        & (option_rows["ts_et"] < ts_et)
    ]
    if prior.empty:
        return None
    return prior.sort_values("ts_et").iloc[-1]


def _reconcile_a(option_rows: pd.DataFrame, regular: pd.DataFrame, measures_df: pd.DataFrame) -> dict:
    """ONE real (date, snapshot, Strike) A value rebuilt from the same
    contracts' consecutive cumulative Volume observations: prior/current
    timestamps and Volume, elapsed interval, dV derived here by subtraction
    (checked against panel's dV), dv_flag, current Gamma/spot, and each
    contract's contribution -- including contracts excluded by the joint
    dV/Gamma validity rule, with the reason. Picks the first strike whose A
    is strictly positive, so the example exercises real activity."""
    gamma = pd.to_numeric(regular["Gamma"], errors="coerce")
    candidates = regular[
        (regular["dv_flag"] == "ok") & (regular["dV"] > 0) & (gamma > 0) & np.isfinite(gamma)
    ]
    if candidates.empty:
        return {"note": "no strike with positive usable activity found to reconcile"}
    first = candidates.sort_values(["date", "ts_et", "Strike", "OptionSymbol"]).iloc[0]
    date, snap, strike = first["date"], first["snapshot_key"], first["Strike"]
    group = regular[
        (regular["date"] == date) & (regular["snapshot_key"] == snap) & (regular["Strike"] == strike)
    ].sort_values("OptionSymbol")

    spot = float(group.iloc[0]["UnderlyingPrice"])
    ts_now = group.iloc[0]["ts_et"]
    rows = []
    manual = 0.0
    n_included = 0
    for _, row in group.iterrows():
        prior = _prior_observation(option_rows, date, row["OptionSymbol"], ts_now)
        vol_now = float(row["Volume"])
        g = float(row["Gamma"])
        entry = {
            "OptionSymbol": row["OptionSymbol"], "Type": row.get("Type"),
            "prior_snapshot_key": None if prior is None else prior["snapshot_key"],
            "prior_ts_et": None if prior is None else str(prior["ts_et"]),
            "prior_cumulative_volume": None if prior is None else float(prior["Volume"]),
            "current_ts_et": str(ts_now),
            "current_cumulative_volume": vol_now,
            "elapsed_seconds": None if prior is None else (ts_now - prior["ts_et"]).total_seconds(),
            "dV_derived_here": None if prior is None else vol_now - float(prior["Volume"]),
            "dV_from_panel": None if pd.isna(row["dV"]) else float(row["dV"]),
            "dv_flag": row["dv_flag"],
            "Gamma": g,
        }
        if row["dv_flag"] != "ok":
            entry["included"], entry["exclusion_reason"] = False, f"dv_flag={row['dv_flag']}"
        elif not _usable_nonneg(g):
            entry["included"] = False
            entry["exclusion_reason"] = "Gamma missing/negative/non-finite (joint dV/Gamma validity)"
        else:
            # An "ok" interval must be a real consecutive, in-cap,
            # non-decreasing observation -- check that against the raw rows
            # rather than trusting the flag, and require panel's dV to be
            # exactly the subtraction done here.
            if (prior is None or entry["elapsed_seconds"] > MAX_INTERVAL_SECONDS
                    or entry["dV_derived_here"] < 0):
                raise ReconciliationMismatch(f"A: {row['OptionSymbol']} flagged ok but raw observations disagree: {entry}")
            if entry["dV_derived_here"] != entry["dV_from_panel"]:
                raise ReconciliationMismatch(f"A: {row['OptionSymbol']} derived dV != panel dV: {entry}")
            contribution = CONTRACT_MULTIPLIER * entry["dV_derived_here"] * g
            entry["included"] = True
            entry["contribution_before_spot_squared"] = contribution
            manual += contribution
            n_included += 1
        rows.append(entry)

    result = {
        "date": date, "snapshot_key": snap, "strike": float(strike),
        "UnderlyingPrice": spot, "contract_multiplier_used": CONTRACT_MULTIPLIER,
        "max_interval_seconds": MAX_INTERVAL_SECONDS,
        "n_contracts_at_strike": len(group), "n_included_contracts": n_included,
        "contract_rows": rows,
        "contribution_sum_before_spot_squared": manual,
        "formula": "A[k,t] = UnderlyingPrice^2 * sum_over_contracts_with_ok_dV_and_valid_Gamma(multiplier * dV * Gamma)",
        "manual_result": manual * spot ** 2,
    }
    return _record_agreement(result, _measure_value(measures_df, date, snap, strike, "A"))


def _representative_reconciliation(option_rows: pd.DataFrame) -> dict:
    """Hand-reconciles one C and one A value from raw source rows
    (independently of measures.py's arithmetic) and records/asserts
    agreement with the actual measures output -- the same
    compute_concentration_and_activity call on the same regular-hours rows
    that run_measures.py writes to measures.parquet. Raises
    ReconciliationMismatch on any disagreement; run_measures.py re-checks
    its written output against these recorded values."""
    regular = option_rows[option_rows["regular_hours"]]
    measures_df = compute_concentration_and_activity(regular)
    result = {
        "C": _reconcile_c(regular, measures_df),
        "A": _reconcile_a(option_rows, regular, measures_df),
    }
    for name, rec in result.items():
        if rec.get("agrees") is False:
            raise ReconciliationMismatch(
                f"{name}: manual {rec['manual_result']!r} != measures output {rec['measure_output']!r}"
            )
    return result


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


def current_config() -> dict:
    return {
        "MAX_INTERVAL_SECONDS": MAX_INTERVAL_SECONDS,
        "REGULAR_OPEN": list(REGULAR_OPEN),
        "REGULAR_CLOSE": list(REGULAR_CLOSE),
        "ANCHOR_INTERVAL_MINUTES": outcomes.ANCHOR_INTERVAL_MINUTES,
        "OUTCOME_HORIZON_MINUTES": outcomes.OUTCOME_HORIZON_MINUTES,
        "MAX_STALENESS_SECONDS": outcomes.MAX_STALENESS_SECONDS,
        "STRIKES_PER_SIDE": outcomes.STRIKES_PER_SIDE,
        "VOL_WINDOW_MINUTES": VOL_WINDOW_MINUTES,
        "BASELINE_STALENESS_SECONDS": BASELINE_STALENESS_SECONDS,
        "CONTRACT_MULTIPLIER": CONTRACT_MULTIPLIER,
    }


def verify_frozen_identity(frozen: dict, config: dict, source_hashes: dict[str, str],
                           dates: list[str]) -> dict[str, str]:
    """Checks config, source-file identity and the frozen session/object set
    against the current run, and returns {key: expected_sha256}. Raises
    ManifestMismatch listing EVERY difference found, not just the first."""
    problems = []
    if frozen.get("config") != config:
        problems.append(f"config differs: frozen={frozen.get('config')} current={config}")
    frozen_src = frozen.get("source_file_sha256") or {}
    for name in sorted(set(frozen_src) | set(source_hashes)):
        if frozen_src.get(name) != source_hashes.get(name):
            problems.append(f"source file {name}: frozen={frozen_src.get(name)} current={source_hashes.get(name)}")
    objects = frozen.get("objects") or {}
    if sorted(objects) != sorted(dates):
        problems.append(f"sessions differ: frozen={sorted(objects)} current={sorted(dates)}")
    expected: dict[str, str] = {}
    for entries in objects.values():
        for entry in entries:
            if entry.get("sha256"):
                expected[entry["key"]] = entry["sha256"]
            else:
                problems.append(f"{entry.get('key')}: frozen manifest has no sha256 (unverifiable)")
    if problems:
        raise ManifestMismatch("frozen manifest mismatch:\n  " + "\n  ".join(problems))
    return expected


def verify_listing(source: SnapshotSource, frozen: dict, dates: list[str]) -> None:
    """The snapshot keys listed now must equal the frozen keys exactly -- an
    added or dropped snapshot would change the analysis without any single
    object's hash changing."""
    problems = []
    for yyyymmdd in dates:
        frozen_keys = {e["key"] for e in frozen["objects"].get(yyyymmdd, [])}
        listed_keys = {o["key"] for o in source.snapshot_objects(yyyymmdd)}
        problems += [f"{k}: in frozen manifest, not listed now" for k in sorted(frozen_keys - listed_keys)]
        problems += [f"{k}: listed now, not in frozen manifest" for k in sorted(listed_keys - frozen_keys)]
    if problems:
        raise ManifestMismatch("snapshot listing mismatch:\n  " + "\n  ".join(problems))


def verify_all_read(source: SnapshotSource, expected: dict[str, str]) -> None:
    unread = sorted(set(expected) - set(source._read_sha256))
    if unread:
        raise ManifestMismatch(f"{len(unread)} frozen objects were never read, e.g. {unread[:3]}")


def main(argv: list[str] | None = None, *, source: SnapshotSource | None = None,
         out_dir: Path = OUT_DIR, dates: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MOO-171 inventory + coverage audit")
    parser.add_argument(
        "--new-run", action="store_true",
        help="deliberately start a new run and write a new source manifest "
             "(default: verify against the frozen manifest and retain it)",
    )
    args = parser.parse_args(argv)
    dates = dates or sorted(REPORTED_SNAPSHOT_COUNTS)
    manifest_path = out_dir / "source_manifest.json"
    config = current_config()
    frozen = expected = frozen_bytes = None

    # Every check in this block runs BEFORE anything under out_dir is written.
    try:
        source_hashes = _source_file_sha256()
        if args.new_run:
            source = source or SnapshotSource()
        else:
            if not manifest_path.exists():
                print(f"No frozen manifest at {manifest_path}; pass --new-run to start a new run.",
                      file=sys.stderr)
                return 2
            frozen_bytes = manifest_path.read_bytes()
            frozen = json.loads(frozen_bytes)
            expected = verify_frozen_identity(frozen, config, source_hashes, dates)
            source = source or SnapshotSource()
            source.expected_sha256 = expected
            verify_listing(source, frozen, dates)
            print(f"Verifying against frozen manifest ({len(expected)} objects, "
                  f"generated {frozen.get('generated_at_utc')})")

        print(f"Loading {len(dates)} sessions (cache: {source.cache_dir})...")
        option_rows, spot_series, audits = load_raw_snapshots(source, dates)
        if expected is not None:
            verify_all_read(source, expected)
        print(f"Loaded {len(option_rows)} option rows, {len(spot_series)} spot observations.")

        print("Recomputing interval volume...")
        option_rows = recompute_interval_volume(option_rows)
        reconciliation = _representative_reconciliation(option_rows)
    except (ManifestMismatch, ReconciliationMismatch) as exc:
        print(f"\nFAILED ({type(exc).__name__}) -- nothing under {out_dir} was written:\n{exc}",
              file=sys.stderr)
        return 1

    for name, rec in reconciliation.items():
        if "manual_result" in rec:
            print(f"Reconciled {name} at ({rec['date']}, {rec['snapshot_key']}, {rec['strike']}): "
                  f"manual={rec['manual_result']!r} measures={rec['measure_output']!r}")

    out_dir.mkdir(exist_ok=True)
    option_rows.to_parquet(out_dir / "option_rows.parquet", index=False)
    spot_series.to_parquet(out_dir / "spot_series.parquet", index=False)

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

    with open(out_dir / "audit_report.json", "w", newline="\n") as f:
        json.dump(report, f, indent=2, default=str)

    if frozen is not None:
        # Reproduction run: the frozen manifest is accepted evidence and is
        # retained byte-for-byte; this verification is recorded separately.
        check = {
            "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            "frozen_manifest_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
            "frozen_manifest_generated_at_utc": frozen.get("generated_at_utc"),
            "git_base_revision": _git_base_revision(),
            "git_dirty": _git_dirty(),
            "objects_verified": len(expected),
            "checks_passed": [
                "config identity", "source_file_sha256 identity", "snapshot key set",
                "per-object content sha256 (before parse)", "every frozen object read",
            ],
            "reconciliation_agrees": {name: rec.get("agrees") for name, rec in reconciliation.items()},
        }
        with open(out_dir / "reproduction_check.json", "w", newline="\n") as f:
            json.dump(check, f, indent=2, default=str)
        print(f"\nVerified against frozen manifest; retained {manifest_path} unchanged. Wrote "
              f"audit_report.json, reproduction_check.json, option_rows.parquet, spot_series.parquet")
        return 0

    # --new-run only: write a new frozen source manifest -- exact
    # keys/verified-sha256/etags/sizes for every object this run actually
    # used (snapshot objects only -- excluded non-snapshot objects are
    # recorded separately per session in audit_report.json), the
    # configuration constants in force, and TWO independent identifications
    # of the executed source code (see module docstring).
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_base_revision": _git_base_revision(),
        "git_dirty": _git_dirty(),
        "source_file_sha256": source_hashes,
        "config": config,
        "objects": {
            yyyymmdd: source.verified_manifest(yyyymmdd) for yyyymmdd in dates
        },
        "representative_reconciliation": reconciliation,
        "multiplier_evidence": _multiplier_evidence(audits),
    }
    # newline="\n": evidence is LF on every platform (see .gitattributes),
    # so its sha256 matches the committed blob on any fresh checkout.
    with open(manifest_path, "w", newline="\n") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nNew run: wrote {manifest_path}, audit_report.json, option_rows.parquet, spot_series.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
