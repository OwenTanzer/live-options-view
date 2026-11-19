"""G2: inventory September 10, 11, 14, 15 and 16 for the OA-178 reconstruction.

    python research/oa241/g2_inventory.py SNAPSHOT_ROOT TRADIER_LISTING.json > research/oa241/g2_inventory.json

SNAPSHOT_ROOT holds intraday/YYYYMMDD/snapshot_*.csv (read-only copy of the chain archive).
TRADIER_LISTING.json is a read-only listing of moo144/tradier/{date}/ keys per date
({date: [[key, bytes], ...]}). One row per date, missing dates preserved.
Snapshot usability follows OA-171: non-finite or negative OI/Gamma are unusable.
"""
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DATES = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16"]
GAP_S = 120  # a chain snapshot older than two minutes is a gap (same rule as the Order Flow view)


def ok(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def snap_time(day, name):
    s = name.removeprefix("snapshot_").removesuffix(".csv")
    return datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), int(s[:2]), int(s[2:4]), int(s[4:6]),
                    int(s[6:12].ljust(6, "0")), tzinfo=ET)


def inventory(root, listing, day):
    files = sorted((root / "intraday" / day.replace("-", "")).glob("snapshot_*.csv"))
    open_t, close_t = [datetime.fromisoformat(f"{day}T{h}").replace(tzinfo=ET) for h in ("09:30:00", "16:00:00")]
    trade_keys = listing.get(day, [])
    row = dict(date=day, trade_prints_and_preceding_quotes=dict(
        archived_segments=sum(k.endswith(".ndjson.gz") for k, _ in trade_keys),
        status="none archived" if not trade_keys else "present"))
    if not files:
        row.update(snapshots=0, strongest_estimate="none: no chain snapshots")
        return row
    times, oi_first, decision, gamma_cells, spot_n, strikes, vol_cells, cells = [], {}, {}, [0, 0], 0, set(), [0, 0], 0
    for f in files:
        t = snap_time(day, f.name)
        rows = [r for r in csv.DictReader(f.open(newline="")) if r.get("Expiration") == day]
        times.append(t)
        rth = open_t <= t <= close_t
        if rth:
            spots = {ok(r.get("UnderlyingPrice")) for r in rows} - {None}
            spot_n += len(spots) == 1
        for r in rows:
            sym = r["OptionSymbol"]
            oi = ok(r.get("OpenInterest"))
            if oi is not None and (sym not in oi_first or oi_first[sym][0] != oi):
                oi_first[sym] = (oi, t)
            if t <= open_t:
                decision[sym] = (oi, t)
            if rth:
                strikes.add(float(r["Strike"]))
                gamma_cells[0] += 1
                gamma_cells[1] += ok(r.get("Gamma")) is not None
                vol_cells[0] += 1
                vol_cells[1] += ok(r.get("Volume")) is not None
    rth_times = [t for t in times if open_t <= t <= close_t]
    edges = [open_t] + rth_times + [close_t]
    gaps = [(a.strftime("%H:%M:%S"), b.strftime("%H:%M:%S"), round((b - a).total_seconds()))
            for a, b in zip(edges, edges[1:]) if (b - a).total_seconds() > GAP_S]
    refreshed = sorted({t for _, t in oi_first.values() if t <= open_t})
    usable_decision = sum(1 for v, _ in decision.values() if v is not None)
    row.update(
        snapshots=len(files), rth_snapshots=len(rth_times),
        first_snapshot=times[0].strftime("%H:%M:%S"), last_snapshot=times[-1].strftime("%H:%M:%S"),
        decision_time_settled_oi=dict(contracts_with_oi_at_0930=usable_decision, contracts=len(decision),
                                      last_oi_change_before_open=refreshed[-1].strftime("%H:%M:%S") if refreshed else None,
                                      clearing_date="not recorded; inferred prior session"),
        spot=dict(rth_snapshots_with_single_spot=spot_n, source="snapshot UnderlyingPrice at snapshot time"),
        gamma=dict(rth_contract_cells=gamma_cells[0], usable=gamma_cells[1],
                   usable_share=round(gamma_cells[1] / gamma_cells[0], 4) if gamma_cells[0] else None,
                   timestamp="snapshot filename time, ET"),
        captured_strikes=dict(count=len(strikes), low=min(strikes) if strikes else None, high=max(strikes) if strikes else None,
                              note="0DTE chain rows in each snapshot; the strike set moves with the collector's window"),
        cumulative_volume=dict(rth_contract_cells=vol_cells[0], usable=vol_cells[1]),
        rth_gaps_over_120s=gaps,
        strongest_estimate=("unsigned only: settled-OI gamma concentration (OA-171 C) and unsigned minute volume "
                            "activity (OA-171 A) by strike and time. Signed flow, the OA-178 S-B dealer accumulation and "
                            "its attribution endpoints are unsupported: no trade prints or preceding quotes were archived."
                            if not trade_keys else "signed reconstruction possible; see the trade archive"),
    )
    return row


if __name__ == "__main__":
    root, listing = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps([inventory(root, listing, d) for d in DATES], indent=1))
