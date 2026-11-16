#!/usr/bin/env python3
"""Diagnostic ablation of the s3c-dev2 corrections (development days only; no new correction).

Each of the four Amendment-1 corrections is applied alone to the frozen model and evaluated on
development days, so the before/after table can attribute each change. Writes
results/s3c_dev_ablation.json.

    python research/options_replay/calibration/ablate_dev2.py
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import archive as A  # noqa: E402
import evaluate_s3c as V  # noqa: E402
import pricing as PR  # noqa: E402
import pricing_dev2 as PD  # noqa: E402

KEYS = ["A1", "A3", "A4", "N1", "U1", "S1"]


def summarize(r: dict) -> dict:
    out = {}
    for w in ("open", "midday", "close", "endpoint"):
        cells = {k: c for k, c in r["cells"].items() if k.endswith("|" + w)}
        out[w] = {k: (st.median([c[k] for c in cells.values() if c.get(k) is not None])
                      if any(c.get(k) is not None for c in cells.values()) else None) for k in KEYS}
        out[w]["cells_pass"] = sum(1 for c in cells.values() if c["verdict"] == "pass")
    out["E1"] = {k: v["median_abs_usd"] for k, v in r["E1"].items()}
    out["invariants_pass"] = all(r["invariants"][k] for k in ("S2_pass", "C1_pass", "P1_pass"))
    return out


def main() -> int:
    cache = Path(A.DEFAULT_CACHE)
    frozen = PR.load_params()
    dev2 = PD.load_dev2()
    report = {}
    for symbol in ("QQQ", "SPY"):
        days = V.load(cache, symbol)
        dev, _ = V.split(sorted(days))
        fz = frozen["symbols"][symbol]
        d2 = dev2["symbols"][symbol]
        variants = {
            "frozen": (fz, PR.SymbolModel),
            "clock only (implied clock, refit sequence)": (V.fit_symbol(days, dev, weights_override=d2["weights"]), PR.SymbolModel),
            "vol feedback only (eta on frozen)": ({**fz, "eta": d2["eta"]}, PD.Dev2Model),
            "expiration only (fitted horizon + tail)": ({**fz, "tail": d2["tail"], "post_close_horizon_s": d2["post_close_horizon_s"]}, PD.Dev2Model),
            "spreads only (ATM/non-ATM)": ({**fz, "spread_atm": d2["spread_atm"], "spread_otm": d2["spread_otm"]}, PD.Dev2Model),
            "all four (s3c-dev2)": (d2, PD.Dev2Model),
        }
        report[symbol] = {}
        for name, (p, cls) in variants.items():
            r = V.evaluate(p, days, dev, model_cls=cls)
            report[symbol][name] = summarize(r)
            s = report[symbol][name]
            print(symbol, f"{name:44s}", " ".join(f"{w}:A1={s[w]['A1']:.2f},A3={s[w]['A3'] or 0:.2f},A4={s[w]['A4']:.2f},N1={s[w]['N1'] or 0:.1f}"
                                                   for w in ("open", "midday", "close")), "E1", {k: round(v, 2) for k, v in s["E1"].items()},
                  flush=True)
    (HERE / "results" / "s3c_dev_ablation.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
