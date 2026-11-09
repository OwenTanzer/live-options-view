#!/usr/bin/env python3
"""Combine #130 full-universe batch replays into globally ranked outputs.

Reads the 14 bounded batch replays (each <=50 underlyings, covering the
union of all selected archived underlyings for Oct 5-7, 2026), concatenates
all contract rows per window, globally re-ranks before any shortlist,
retains losses/exclusions, and asserts no duplicate/missing contracts.

Conflict semantics (fail closed):
A contract (OCC symbol) that appears under more than one underlying with
DIFFERING evidence — different reasons, status, values, sessions or
timestamps — is a conflict, not a duplicate. Its identity/deliverable/path
evidence is ambiguous, so it is EXCLUDED from the combined ranked output:
it is not ranked, not eligible, and no canonical economic identity is
asserted from root spelling. Its paths are NOT stitched. Both
provenance/coverage records are preserved in `conflicts.csv` (one row per
attribution), and the summary records the conflict count and the exclusion.
A symbol whose rows are byte-identical in evidence is a pure duplicate and
is deduped to one row. Nothing is silently chosen.

Batch validation (fail closed):
Before any combination, the script validates the exact planned batch
membership (batch-01..batch-14 from replay-130-batches.json), the disjoint
union of the planned 661 underlyings (no underlying in two batches, union
exactly the planned set), and that every batch's source_lock.json has the
same (key, sha256) set. A missing, unexpected or duplicate batch, a
non-disjoint or incomplete union, or an inconsistent source lock FAILS the
run (exit non-zero) instead of producing a partial result.

Usage: python3 combine-full-universe.py [--batches-dir DIR] [--out DIR]
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

WINDOWS = [
    "overnight-2026-10-05_2026-10-06",
    "overnight-2026-10-06_2026-10-07",
    "sessions2-2026-10-05_2026-10-06",
    "sessions2-2026-10-06_2026-10-07",
    "sessions3-2026-10-05_2026-10-07",
]
COLUMNS = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
           "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]
# Evidence fields that must agree for two rows of the same symbol to be a
# pure duplicate rather than a conflict. Attribution (underlying/batch)
# may differ; evidence must not. Identity fields (option_type, strike,
# expiration) and quality fields (flags, abs_change_per_contract) are
# evidence too: differing identity/quality must not be silently selected.
EVIDENCE_FIELDS = ["status", "reasons", "pct", "entry", "exit", "abs_change_per_contract",
                   "entry_session", "exit_session", "entry_ms", "exit_ms",
                   "option_type", "strike", "expiration", "flags"]
CONFLICT_COLUMNS = ["window", "symbol", "batch", "underlying", "option_type", "strike", "expiration",
                    "status", "reasons", "pct", "entry", "exit", "abs_change_per_contract",
                    "entry_session", "exit_session", "entry_ms", "exit_ms", "flags"]


def pct_of(row):
    try:
        return float(row["pct"])
    except (TypeError, ValueError):
        return None


def fail(msg):
    print(f"VALIDATION FAILED: {msg}", file=sys.stderr, flush=True)
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batches-dir", type=Path, default=Path("replay-130-full"))
    ap.add_argument("--out", type=Path, default=Path("replay-130-full-combined"))
    ap.add_argument("--planned-batches", type=Path, default=Path("replay-130-batches.json"))
    ap.add_argument("--planned-union", type=Path, default=Path("replay-130-union.json"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    # --- Batch validation (fail closed) ---
    planned = json.loads(a.planned_batches.read_text())
    planned_union = set(json.loads(a.planned_union.read_text()))
    expected_dirs = {f"batch-{i:02d}" for i in range(1, len(planned) + 1)}
    present = {d.name for d in a.batches_dir.iterdir() if d.is_dir() and d.name.startswith("batch-")}
    missing = expected_dirs - present
    unexpected = present - expected_dirs
    if missing:
        return fail(f"missing batch dirs: {sorted(missing)}")
    if unexpected:
        return fail(f"unexpected batch dirs: {sorted(unexpected)}")
    batch_dirs = sorted(a.batches_dir / n for n in sorted(expected_dirs))
    print(f"batches: {len(batch_dirs)} (exact planned membership)", flush=True)

    # Disjoint union of the planned 661 underlyings, and each batch's
    # summary underlyings must match its planned batch.
    seen_under = {}
    for d, planned_batch in zip(batch_dirs, planned):
        s = json.loads((d / "replay_summary.json").read_text())
        actual = set(s["underlyings"])
        if actual != set(planned_batch):
            return fail(f"{d.name}: summary underlyings != planned batch "
                        f"(extra {sorted(actual - set(planned_batch))}, "
                        f"missing {sorted(set(planned_batch) - actual)})")
        for u in actual:
            if u in seen_under:
                return fail(f"underlying {u} in both {seen_under[u]} and {d.name} (not disjoint)")
            seen_under[u] = d.name
    if set(seen_under) != planned_union:
        return fail(f"union of batch underlyings != planned 661 "
                    f"(extra {sorted(set(seen_under) - planned_union)[:5]}, "
                    f"missing {sorted(planned_union - set(seen_under))[:5]})")
    print(f"underlyings: {len(seen_under)} (disjoint union == planned 661)", flush=True)

    # Source locks consistent across batches: identical (key, sha256) sets.
    lock0 = None
    for d in batch_dirs:
        lock = json.loads((d / "source_lock.json").read_text())
        sig = {(s["key"], s["sha256"]) for s in lock["sources"]}
        if lock0 is None:
            lock0 = sig
        elif sig != lock0:
            return fail(f"{d.name}: source lock differs from batch-01 "
                        f"(keys/hashes not identical)")
    print(f"source locks: identical across all {len(batch_dirs)} batches ({len(lock0)} blobs)", flush=True)

    # Per-batch contract counts (from each batch's summary) for the
    # no-missing assertion.
    expected_total = 0
    for d in batch_dirs:
        s = json.loads((d / "replay_summary.json").read_text())
        expected_total += s["contracts"]
    print(f"expected total contracts (sum of batch summaries): {expected_total}", flush=True)

    report = {"batches": len(batch_dirs), "expected_total_contracts": expected_total,
              "underlyings": len(seen_under), "source_blobs": len(lock0),
              "windows": {}, "files": {}, "conflicts": {}}
    all_conflicts = []
    for win in WINDOWS:
        rows = []
        for d in batch_dirs:
            f = d / f"{win}.csv"
            with open(f, newline="") as src:
                for r in csv.DictReader(src):
                    r["_batch"] = d.name
                    rows.append(r)
        # Group by OCC symbol. A symbol appearing more than once with
        # DIFFERING evidence is a conflict: exclude it from the combined
        # output (fail closed) and preserve both records. A symbol whose
        # rows are identical in evidence is a pure duplicate: keep one.
        by_symbol = defaultdict(list)
        for r in rows:
            by_symbol[r["symbol"]].append(r)
        kept = []
        conflicts = []
        pure_dupe_groups = 0
        removed_dupes = 0
        for sym, group in by_symbol.items():
            if len(group) == 1:
                kept.append(group[0])
                continue
            first = {k: group[0].get(k, "") for k in EVIDENCE_FIELDS}
            if all({k: r.get(k, "") for k in EVIDENCE_FIELDS} == first for r in group[1:]):
                pure_dupe_groups += 1
                removed_dupes += len(group) - 1
                kept.append(group[0])
                continue
            # Conflict: differing evidence. Exclude from the ranked output;
            # preserve every attribution in the conflict report. Do not
            # assert canonical identity from root spelling; do not stitch.
            conflicts.append((sym, group))
        for sym, group in conflicts:
            for r in group:
                all_conflicts.append({"window": win, **{c: r.get(c, "") for c in CONFLICT_COLUMNS[1:]},
                                      "batch": r["_batch"]})
        # Raw-row accounting: every raw row is accounted for exactly once —
        # either in the combined output, as a removed duplicate row, or as
        # a conflicting attribution in the conflict report.
        accounted = len(kept) + removed_dupes + sum(len(g) for _, g in conflicts)
        assert accounted == expected_total, (
            f"{win}: accounted {accounted} != expected {expected_total}")
        # Distinct-symbol accounting (separate from raw rows): every
        # distinct symbol is either kept once or conflicting. `kept`
        # already contains one entry per pure-duplicate group, so the
        # duplicate-group count is NOT added here.
        assert len(kept) + len(conflicts) == len(by_symbol), (
            f"{win}: kept {len(kept)} + conflicting {len(conflicts)} "
            f"!= distinct symbols {len(by_symbol)}")
        if conflicts:
            print(f"{win}: {len(conflicts)} conflicting symbols excluded (fail closed); "
                  f"{sum(len(g) for _, g in conflicts)} rows preserved in conflict report", flush=True)
        if pure_dupe_groups:
            print(f"{win}: {pure_dupe_groups} pure-duplicate groups deduped "
                  f"({removed_dupes} rows removed)", flush=True)
        rows = kept
        # Global re-rank (qualified basis): eligible by pct desc, symbol asc.
        elig = sorted((r for r in rows if r["status"] == "eligible"),
                      key=lambda r: (-pct_of(r), r["symbol"]))
        for i, r in enumerate(elig, 1):
            r["rank"] = str(i)
        for r in rows:
            if r["status"] != "eligible":
                r["rank"] = ""
        ordered = elig + [r for r in rows if r["status"] != "eligible"]
        out_path = a.out / f"{win}.csv"
        with open(out_path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
            wr.writeheader()
            for r in ordered:
                wr.writerow({c: r.get(c, "") for c in COLUMNS})
        n_elig = len(elig)
        reasons = Counter()
        for r in rows:
            if r["status"] == "excluded":
                for why in (r.get("reasons") or "").split(";"):
                    why = why.strip()
                    if why:
                        reasons[why.split(":")[0]] += 1
        report["windows"][win] = {
            "contracts": len(rows), "eligible": n_elig,
            "excluded": len(rows) - n_elig,
            "conflicting_symbols": len(conflicts),
            "pure_duplicate_groups": pure_dupe_groups,
            "pure_duplicate_rows_removed": removed_dupes,
            "top_exclusion_reasons": dict(reasons.most_common(5)),
            "top": [{"rank": r["rank"], "symbol": r["symbol"], "pct": r["pct"],
                     "entry": r["entry"], "exit": r["exit"]} for r in elig[:5]],
        }
        print(f"{win}: {len(rows)} contracts, {n_elig} eligible, {len(rows)-n_elig} excluded", flush=True)

    # Conflict report: every conflicting attribution, preserved with its
    # batch, underlying, status, reasons and values.
    conflict_path = a.out / "conflicts.csv"
    with open(conflict_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=CONFLICT_COLUMNS, lineterminator="\n")
        wr.writeheader()
        for r in sorted(all_conflicts, key=lambda x: (x["window"], x["symbol"], x["batch"])):
            wr.writerow({c: r.get(c, "") for c in CONFLICT_COLUMNS})
    n_conflict_symbols = len({r["symbol"] for r in all_conflicts})
    report["conflicts"] = {
        "symbols": n_conflict_symbols, "rows": len(all_conflicts),
        "report": "conflicts.csv",
        "semantics": ("A symbol with differing evidence across attributions is excluded "
                      "from the combined ranked output (fail closed). No canonical "
                      "economic identity is asserted from root spelling; paths are not "
                      "stitched. Both provenance/coverage records are preserved here."),
    }
    print(f"conflicts: {n_conflict_symbols} symbols, {len(all_conflicts)} rows -> conflicts.csv", flush=True)

    # Hashes for every combined output CSV and the conflict report. (The
    # summary's own hash is not recorded inside itself; it is
    # self-referential. Report the summary hash from the file on disk.)
    for f in sorted(a.out.glob("*.csv")):
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        report["files"][f.name] = {"sha256": h, "bytes": f.stat().st_size}
    (a.out / "full_universe_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"combined -> {a.out}", flush=True)
    print(f"full_universe_summary.json sha256: {hashlib.sha256((a.out / 'full_universe_summary.json').read_bytes()).hexdigest()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
