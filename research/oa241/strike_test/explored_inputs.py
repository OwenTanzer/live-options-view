"""Inputs-only check on the 34 already-viewed sessions (exploratory). Never reads a price after the anchor.

    python research/oa241/strike_test/explored_inputs.py FEATURES_DIR > research/oa241/strike_test/explored_inputs.json

FEATURES_DIR is the repaired G4 feature set (manifest b655f75). At each old G5c anchor (every 5 min,
09:45-15:50) it reports: whether the old reference strike K* (largest |central flow gamma| within
$2) has scenario endpoints that agree in sign, whether any eligible strike does, whether the old
distance-matched control strike exists, and how far K* is from spot. Scenario endpoints use the
unknown-side volume as in the Order Flow view: low = (S - B - U) x unit, high = (S - B + U) x unit.
"""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np


def load(fdir):
    man = json.loads((fdir / "manifest.json").read_text())
    for day in sorted(man["sessions"]):
        for ext in ("csv", "npz"):
            assert hashlib.sha256((fdir / f"{day}.{ext}").read_bytes()).hexdigest() == man["sessions"][day][f"{ext}_sha256"]
        with (fdir / f"{day}.csv").open() as fh:
            head = fh.readline().strip().split(",")
            col = head.index("qqq_mid")
            mid = np.array([float(v) if v else np.nan for v in (line.rstrip("\n").split(",")[col] for line in fh)])
        yield day, np.load(fdir / f"{day}.npz"), mid, man["sessions"][day]["strikes"]


def minute(hhmm):
    h, m = map(int, hhmm.split(":"))
    return (h - 9) * 60 + m - 30


def main():
    out = dict(anchors_with_quote=0, anchors_no_covered_strike_within_2=0, kstar_events=0, kstar_sign_uncertain=0,
               anchors_with_any_agreeing_strike=0, old_control_missing=0, kstar_within_025=0,
               old_control_missing_when_within_025=0, by_session={})
    for day, z, mid, strikes in load(Path(sys.argv[1])):
        strikes = np.array(strikes)
        ks = np.array(sorted(set(strikes)))
        unit = 100.0 * z["gamma"] * z["spot"][:, None] ** 2
        B, S, U = z["cum"][..., 0], z["cum"][..., 1], z["cum"][..., 2]

        def by_strike(q):
            x = np.where(np.isfinite(unit), q * unit, np.nan)
            return np.stack([np.nansum(np.where(strikes == k, x, 0), axis=1) for k in ks], axis=1)
        cov = np.stack([np.any(np.isfinite(unit[:, strikes == k]), axis=1) for k in ks], axis=1)
        lo, central, hi = by_strike(S - B - U), by_strike(S - B), by_strike(S - B + U)
        agree = cov & (np.sign(lo) == np.sign(hi)) & (lo != 0)
        row = dict(kstar_events=0, any_agreeing=0)
        for t in range(minute("09:45"), minute("15:50") + 1, 5):
            if not np.isfinite(mid[t]):
                continue
            out["anchors_with_quote"] += 1
            dist = np.abs(mid[t] - ks)
            elig = (dist <= 2.0) & cov[t]
            if not elig.any():
                out["anchors_no_covered_strike_within_2"] += 1
                continue
            i = int(np.argmax(np.where(elig, np.abs(central[t]), -np.inf)))
            out["kstar_events"] += 1
            row["kstar_events"] += 1
            out["kstar_sign_uncertain"] += int(not agree[t, i])
            a = bool((elig & agree[t]).any())
            out["anchors_with_any_agreeing_strike"] += a
            row["any_agreeing"] += a
            missing = not any(j != i and abs(dist[j] - dist[i]) <= 0.5 for j in range(len(ks)))
            near = bool(dist[i] < 0.25)
            out["old_control_missing"] += missing
            out["kstar_within_025"] += near
            out["old_control_missing_when_within_025"] += missing and near
        out["by_session"][day] = row
    json.dump(out, sys.stdout, indent=1, default=int)
    print()


if __name__ == "__main__":
    main()
