"""Prepare the frozen October/November 2026 SPY practice pool from dated snapshot objects."""
from collections import defaultdict
from datetime import date, timedelta
import json

import archive as A
from qualify import qualify


def main():
    if A.SYMBOL != "SPY":
        raise SystemExit("Set OPTIONS_TRAINER_SYMBOL=SPY for the separate SPY cache and practice store.")
    arc = A.Archive()
    days = [date(2026, 10, 8) + timedelta(days=i) for i in range(37)]
    keys = [key for day in days if day.weekday() < 5
            for key in arc.list_keys(A.PREFIX + day.strftime("%Y%m%d") + "/")]
    marker = arc.cache_dir / "listings/intraday__spy.json"
    marker.write_text(json.dumps(keys))
    pool = qualify(arc, False, 25, .9, 600, .9)
    index = arc.snapshot_index()
    donors = []
    for day in pool["qualified"]:
        compact = day.replace("-", "")
        pack = A.load_or_build_pack(arc, compact, index[compact])
        if not A.pack_qualifies(pack)[0]:
            continue
        groups = defaultdict(list)
        for snap in pack["snapshots"]:
            if pack["open_s"] <= snap["t"] < pack["close_s"] and snap["ok"]:
                groups[snap["t"] // 300].append(snap)
        if len(groups) != 78:
            continue
        bars = {key: [] for key in ("t", "o", "h", "l", "c", "v")}
        for interval, group in sorted(groups.items()):
            prices = [s["spot"] for s in group]
            for field, value in zip(bars, (interval*300, prices[0], max(prices), min(prices),
                                           prices[-1], sum(s["volume"] for s in group))):
                bars[field].append(value)
        donors.append({"day": day, **bars})
    if not donors:
        raise RuntimeError("No complete SPY donor sessions")
    out = arc.cache_dir / "sim_inputs/spy_donors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(donors, separators=(",", ":")).encode()
    if out.exists() and out.read_bytes() != data:
        raise RuntimeError("Existing SPY donor bytes differ; preserve them for saved sessions")
    out.write_bytes(data)
    (arc.cache_dir / "qualified_days.json").write_text(json.dumps(pool))
    print(len(pool["qualified"]), "qualified SPY Replay days;", len(donors), "Simulation donors")


if __name__ == "__main__":
    main()
