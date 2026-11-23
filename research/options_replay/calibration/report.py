#!/usr/bin/env python3
"""Write the S3b manifest and results table from the dataset and saved held-out results.

    python research/options_replay/calibration/report.py

Outputs calibration/manifest.json (days, splits, per-day kept points, dataset file hashes,
protocol/evaluator commits) and calibration/RESULTS.md (pass/miss per cell for every
held-out attempt, quiet-spot decomposition, expiration convergence, correction table).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import archive as A  # noqa: E402
import evaluate as E  # noqa: E402

RUNS = [("sim-v1 (S3a reference)", "heldout_ref_sim-v1.json"),
        ("baseline (elementary)", "heldout_1_baseline.json"),
        ("candidate (as declared)", "heldout_1_candidate.json"),
        ("correction 1: + closetail", "heldout_2_candidate_closetail.json"),
        ("correction 2: + closetail + skew", "heldout_2_candidate_closetail_skew.json")]
CHECKS = ["A1", "A2", "A3", "A4", "B1", "B2"]


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, cwd=HERE).stdout.strip()


def fmt(x, d=2):
    return "-" if x is None else f"{x:.{d}f}"


def main() -> int:
    cache = Path(A.DEFAULT_CACHE)
    manifest = {"protocol_commit": git("log", "-1", "--format=%H %cI", "--", "protocol.json"),
                "evaluator_commit": git("log", "-1", "--format=%H %cI", "--", "evaluate.py"),
                "symbols": {}}
    for symbol in ("QQQ", "SPY"):
        days = E.load(cache, symbol)
        dev, held, expl = E.split(symbol, sorted(days))
        files = sorted((cache / "calibration").glob(f"{symbol}_*.json"))
        manifest["symbols"][symbol] = {
            "development": dev, "held_out": held, "exploratory_in_held_out_window": expl,
            "kept_grid_points": {d: days[d]["kept"] for d in sorted(days)},
            "dropped_reasons": {d: sorted({p["drop"] for p in days[d]["points"] if "drop" in p}) for d in sorted(days)
                                if days[d]["kept"] < 75},
            "dataset_sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    res = {}
    for label, fn in RUNS:
        p = HERE / "results" / fn
        if p.exists():
            res[label] = json.loads(p.read_text())
    tol = E.TOL
    lines = ["# S3b results: waiting-experience calibration (held-out)", "",
             f"Protocol: `PROTOCOL.md` / `protocol.json` committed {manifest['protocol_commit']} (before fitting).",
             f"Evaluator: `evaluate.py` {manifest['evaluator_commit']}. Every held-out run is in `attempts.jsonl`.", "",
             "Tolerances (unchanged throughout): "
             f"A1 ≤ ${tol['A1_median_abs_level_usd']:.2f}, A2 ≤ ${tol['A2_abs_median_bias_usd']:.2f}, "
             f"A3 ≤ ${tol['A3_median_abs_hold_pnl_usd']:.2f}, A4 ≤ ${tol['A4_median_abs_quiet_change_usd']:.2f}, "
             f"B1 in {tol['B1_median_ratio_band']}, B2 ≤ ${tol['B2_median_abs_hold_pnl_usd']:.2f} (USD per contract).", "",
             "Held-out days: " + ", ".join(f"{s} {len(manifest['symbols'][s]['held_out'])}" for s in manifest["symbols"]) +
             ". Development days: " + ", ".join(f"{s} {len(manifest['symbols'][s]['development'])}" for s in manifest["symbols"]) +
             ". Next-session expiry: **unsupported** (no intraday archive).", "",
             "## Summary (cells passing all six checks / cells)", "",
             "| Model | QQQ | SPY | checks passed (of 144) |", "|---|---|---|---|"]
    for label, r in res.items():
        npass = sum(1 for s in r["symbols"].values() for c in s["cells"].values() for v in c.get("checks", {}).values() if v)
        lines.append(f"| {label} | {r['symbols']['QQQ']['summary']['pass']}/12 | {r['symbols']['SPY']['summary']['pass']}/12 | {npass} |")
    lines += ["", "## Medians per check across the 12 cells of each symbol (USD per contract; B1 ratio)", "",
              "| Model | Symbol | " + " | ".join(CHECKS) + " |", "|---|---|" + "---|" * len(CHECKS)]
    import statistics as st
    for label, r in res.items():
        for s, sr in r["symbols"].items():
            vals = []
            for ck in CHECKS:
                xs = [c[ck] if ck != "A2" else c["A2_signed"] for c in sr["cells"].values() if c.get(ck) is not None]
                vals.append(fmt(st.median(xs), 3 if ck == "B1" else 2) if xs else "-")
            lines.append(f"| {label} | {s} | " + " | ".join(vals) + " |")
    for label, r in res.items():
        lines += ["", f"## {label}: per cell", "",
                  "| Symbol | Cell | n | A1 | A2 (signed) | A3 | A4 | B1 | B2 | recorded mid | verdict |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for s, sr in r["symbols"].items():
            for name, c in sr["cells"].items():
                ch = c.get("checks", {})
                mark = lambda k, v: f"**{v}**" if ch.get(k) is False else v  # noqa: E731
                lines.append(f"| {s} | {name} | {c['n']} | {mark('A1', fmt(c['A1']))} | {mark('A2', fmt(c['A2_signed']))} | "
                             f"{mark('A3', fmt(c['A3']))} | {mark('A4', fmt(c['A4']))} | {mark('B1', fmt(c['B1'], 3))} | "
                             f"{mark('B2', fmt(c['B2']))} | ${fmt(c['recorded_mid_usd_median'])} | {c['verdict']} |")
        lines.append("")
        lines.append("Bold = that check missed. A cell passes only if all six pass.")
    cand = res.get("candidate (as declared)")
    if cand:
        lines += ["", "## Quiet-spot decomposition (candidate, condition A; 15-minute pairs with |ΔS| ≤ 0.02 %)", "",
                  "| Symbol | Cell | n | recorded Δextrinsic | model Δ | fixed-spot / less-time Δ | ATM vol part | share within half-spread |",
                  "|---|---|---|---|---|---|---|---|"]
        for s, sr in cand["symbols"].items():
            for name, q in sr["quiet_spot"].items():
                lines.append(f"| {s} | {name} | {q['n']} | {fmt(q['recorded_dx_median'])} | {fmt(q['model_dx_median'])} | "
                             f"{fmt(q['fixed_spot_less_time_dx_median'])} | {fmt(q['vol_part_median'])} | "
                             f"{fmt(q['share_within_half_spread'], 3)} |")
        lines += ["", "## Expiration convergence (ATM extrinsic, USD per contract, medians)", "",
                  "| Model | Symbol | time / side | recorded | model |", "|---|---|---|---|---|"]
        for label in ("candidate (as declared)", "correction 1: + closetail"):
            r = res.get(label)
            if not r:
                continue
            for s, sr in r["symbols"].items():
                for k, v in sr["expiration_convergence"].items():
                    lines.append(f"| {label} | {s} | {k} | {fmt(v['recorded_extrinsic_median'])} | {fmt(v['model_extrinsic_median'])} |")
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote RESULTS.md and manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
