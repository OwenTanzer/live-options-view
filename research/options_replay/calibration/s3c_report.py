#!/usr/bin/env python3
"""Write calibration/S3C_RESULTS.md and s3c_manifest.json from the saved S3c result files.

    python research/options_replay/calibration/s3c_report.py
"""

from __future__ import annotations

import hashlib
import json
import statistics as st
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = [("combined (sim-v2)", "s3c_dev_combined.json"), ("elementary baseline", "s3c_dev_baseline.json"),
        ("sim-v1", "s3c_dev_sim-v1.json")]
CHECKS = ["A1", "A2", "A3", "A4", "Q1", "B1", "B2", "S1", "U1", "N1"]


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, cwd=HERE).stdout.strip()


def f(x, d=2):
    return "-" if x is None else f"{x:.{d}f}"


def main() -> int:
    res = {lbl: json.loads((HERE / "results" / fn).read_text()) for lbl, fn in RUNS if (HERE / "results" / fn).exists()}
    params = json.loads((HERE / "s3c_params.json").read_text())
    manifest = {"protocol_commit": git("log", "-1", "--format=%H %cI", "--", "s3c_protocol.json"),
                "frozen_model_commit": git("log", "-1", "--format=%H %cI", "--", "s3c_params.json"),
                "params_sha256": hashlib.sha256((HERE / "s3c_params.json").read_bytes()).hexdigest(),
                "development": {s: {"days": p["development_days"], "range": p["development_range"]}
                                for s, p in params["symbols"].items()},
                "held_out": {"from": "2026-11-16", "sessions_available": 0, "required_per_symbol": 10},
                "dataset_sha256": {}}
    cache = HERE.parent / ".cache" / "calibration_s3c"
    for p in sorted(cache.glob("*.json")):
        manifest["dataset_sha256"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    (HERE / "s3c_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    L = ["# S3c results: combined pricing model (sim-v2)", "",
         f"Protocol: `S3C_PROTOCOL.md` / `s3c_protocol.json`, committed {manifest['protocol_commit']}, before fitting.",
         f"Frozen model: `pricing.py` + `s3c_params.json`, {manifest['frozen_model_commit']}"
         f" (params sha256 {manifest['params_sha256'][:16]}…).", "",
         "## Status", "",
         "**There is no held-out result yet.** Every existing intraday day informed S3b or S3c choices, so held-out "
         "is forward collector sessions from 2026-11-16 on. The first 10 gated sessions per symbol are needed "
         "(about 2026-11-30). Every number below is **development (in-sample)** and is not validation. "
         "Next-session expiry is unsupported (no intraday archive).", "",
         "## Development comparison (identical checks, same days)", "",
         "| Model | Symbol | cells pass / 16 | S2 quote validity | C1 convexity violations | P1 max parity $/share | E1 15:55 / 16:05 / 16:10 ($) |",
         "|---|---|---|---|---|---|---|"]
    for lbl, r in res.items():
        for s, sr in r["symbols"].items():
            inv = sr["invariants"]
            e1 = " / ".join(f(sr["E1"][k]["median_abs_usd"]) + (" ✓" if sr["E1"][k]["pass"] else "") for k in sorted(sr["E1"]))
            L.append(f"| {lbl} | {s} | {sr['summary']['pass']} | {'pass' if inv['S2_pass'] else 'FAIL'} "
                     f"({inv["crossed"]} crossed/locked, {inv['negative_bid']} neg, {inv['off_grid']} off-grid of {inv['quotes']}) | "
                     f"{inv['convexity_violations']} | {inv['max_parity_abs']:.2g} | {e1} |")
    L += ["", "Medians across the open/midday/close cells (USD per contract; B1 and U1 are ratios; S1 is per share):", "",
          "| Model | Symbol | " + " | ".join(CHECKS) + " |", "|---|---|" + "---|" * len(CHECKS)]
    for lbl, r in res.items():
        for s, sr in r["symbols"].items():
            row = []
            for ck in CHECKS:
                xs = [c[ck if ck != "A2" else "A2_signed"] for k, c in sr["cells"].items()
                      if not k.endswith("endpoint") and c.get(ck if ck != "A2" else "A2_signed") is not None]
                row.append(f(st.median(xs), 3 if ck in ("B1", "U1", "S1") else 2) if xs else "-")
            L.append(f"| {lbl} | {s} | " + " | ".join(row) + " |")
    comb = res.get("combined (sim-v2)")
    if comb:
        L += ["", "## Combined model per cell (development)", "",
              "| Symbol | Cell | n | " + " | ".join(CHECKS) + " | missed |", "|---|---|---|" + "---|" * len(CHECKS) + "---|"]
        for s, sr in comb["symbols"].items():
            for name, c in sr["cells"].items():
                vals = [f(c.get(ck if ck != "A2" else "A2_signed"), 3 if ck in ("B1", "U1", "S1") else 2) for ck in CHECKS]
                miss = ", ".join(k for k, v in c["checks"].items() if not v) or "none"
                L.append(f"| {s} | {name} | {c['n']} | " + " | ".join(vals) + f" | {miss} |")
    L += ["", "## Component status (development only; 'acceptable' requires a held-out pass)", "",
          "| Component | Development evidence | Status |", "|---|---|---|",
          "| call/put consistency (P1) | exact parity by construction, max residual ~2e-13 | development pass, held-out pending |",
          "| quote validity (S2) / convexity (C1) | 0 invalid quotes, 0 violations | development pass, held-out pending |",
          "| spreads (S1) | within a tick except QQQ ATM endpoint (0.015) | development miss at QQQ ATM endpoint |",
          "| underlying response (U1) | delta ratio 0.87-1.10 in every cell | development pass, held-out pending |",
          "| theta net of IV movement (Q1) | $0.9-1.8 outside the endpoint window | development pass, held-out pending |",
          "| volatility level (A1/A2/B1) | bias ~$0 and ratio in band, absolute error $9-12 midday/close | carried miss (A1) |",
          "| hold profit (A3/B2/N1) | $4-7 per contract, mostly over $4 | carried miss |",
          "| opening decay (A4 open) | $2.9-4.6 against $3 | carried miss |",
          "| expiration path (E1) | QQQ $6.9-7.6, SPY $5.7-7.3 against $6 | development miss (SPY 16:05 passes) |", "",
          "## Development revisions before the freeze (logged in `s3c_attempts.jsonl`)", "",
          "- **D1 skew/convexity:** the unbounded quadratic smile in z made far out-of-the-money prices rise with distance "
          "late in the day (6.7k QQQ violations). Fix: smooth saturation g = 3·tanh(z/3) at the fitted range. "
          "After it there are 0 violations.",
          "- **D2 volatility response:** time-of-day drift γ per 30-minute bucket, on top of the spot response. "
          "The midday/close level bias went from +$6-9 to about $0.",
          "- **D3 evaluation:** N1 is not applicable in the endpoint window. A 30-minute hold opened after 15:50 cannot "
          "finish before the 16:15 trading close.", "",
          "## Settlement convention used", "",
          "These conventions are verified and cited in S3C_PROTOCOL.md Amendment 1, and kept distinct:",
          "- **Last trading time:** 16:15 ET for expiring SPY/QQQ options (SEC Release No. 34-105153, SR-CBOE-2025-079, read).",
          "- **Exercise-by-exception reference:** OCC Rule 805 at $0.01 in the money against the regular-hours last sale "
          "(the 16:00 close), physical delivery. The threshold comes from the 2008 filing; the reference price is "
          "secondary (OCC pages return 403 here).",
          "- **Contrary-instruction deadline:** 17:30 ET exercise-decision cut-off (FINRA Rule 2360(b)(23)(A), read). "
          "Firms may set earlier cut-offs.",
          "- **Modelling assumption, not a settlement rule:** the model's remaining variance after 16:00 is a post-close "
          "term decaying to 17:30. No rule establishes that decay. It was fitted on 16:05 "
          f"development quotes: tail = {params['symbols']['QQQ']['tail']:.4f} (QQQ) and "
          f"{params['symbols']['SPY']['tail']:.4f} (SPY) of a day's variance.",
          "- Recorded quotes keep updating until about 16:14. The generated sim-v2 session still ends at 16:00 (stated limitation).", "",
          "## Carried forward (not resolved)", "",
          "S3b misses A1 level, A3/B2 hold profit and A4 opening decay remain open. The development improvements above "
          "do not validate them. The held-out run on forward sessions decides."]
    abl_path = HERE / "results" / "s3c_dev_ablation.json"
    if abl_path.exists():
        abl = json.loads(abl_path.read_text())
        L += ["", "## Development candidate s3c-dev2 (Amendment 1): before/after and attribution", "",
              "This is development (in-sample) only, with one declared correction per component. `pricing.py`, "
              "`s3c_params.json` and generator sim-v2 are unchanged. dev2 lives in `pricing_dev2.py` + "
              "`s3c_params_dev2.json`. Medians over the cells of each window, USD per contract; E1 is ATM.", "",
              "| Symbol | Variant | open A1 / A3 / A4 / N1 | midday A1 / A3 / A4 / N1 | close A1 / A3 / A4 / N1 | E1 15:55 / 16:05 / 16:10 |",
              "|---|---|---|---|---|---|"]
        for sym, variants in abl.items():
            for name, v in variants.items():
                cols = [" / ".join(f(v[w][k]) for k in ("A1", "A3", "A4", "N1")) for w in ("open", "midday", "close")]
                e1 = " / ".join(f(v["E1"].get(k)) for k in ("15:55", "16:05", "16:10"))
                L.append(f"| {sym} | {name} | " + " | ".join(cols) + f" | {e1} |")
        L += ["", "Attribution (development):",
              "- **Volatility feedback (eta)** lowers close-window level error (QQQ A1 11.1 to 8.4, SPY 11.2 to 9.2). It "
              "**worsens the open** (QQQ A1 6.1 to 10.0, A3 5.9 to 7.9, N1 7 to 9), because the trailing 60-minute "
              "realized vol is undefined or noisy in the first hour.",
              "- **Implied clock** barely changes the open and **worsens the endpoint** (QQQ E1 16:05 6.9 to 10.1; "
              "SPY 5.7 to 11.5).",
              "- **Fitted post-close horizon** (a modelling assumption) improves the QQQ endpoint (E1 16:05 6.85 to 5.86, "
              "16:10 7.41 to 6.43) and is mixed for SPY. The SPY horizon fit sits at the 18:30 edge of the search grid.",
              "- **ATM/non-ATM spreads** fix the QQQ ATM endpoint spread (S1 0.015 to 0.010) and leave mids unchanged.",
              "- **Combined dev2** is mixed: better at the close, worse at the open and at the QQQ endpoint. The "
              "correction budget (one per component) is now spent. The frozen candidate stays the declared held-out "
              "comparison, and dev2 is not promoted.", "",
              "Remaining misses (development): A1 level ($8-11), A3/B2 hold profit (open $6-8.5, elsewhere $4-5), "
              "A4 opening decay ($3-5), E1 endpoint (QQQ $6-9), and with dev2 the SPY 1-OTM call underlying response at "
              "the close (U1 0.755)."]
    (HERE / "S3C_RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("wrote S3C_RESULTS.md and s3c_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
