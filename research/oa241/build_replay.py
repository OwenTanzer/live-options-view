"""Build site/order-flow-replay.json (schema order-flow-replay/v2) for one archived session.

    python research/oa241/build_replay.py ARCHIVE_ROOT SNAPSHOT_ROOT [DAY] > site/order-flow-replay.json

ARCHIVE_ROOT holds moo144/tradier/... (OA-180 fetch.py); SNAPSHOT_ROOT holds
intraday/YYYYMMDD/snapshot_*.csv copied read-only from the chain archive.

Prints come from the unchanged display path (market_data/tradier/recent.py), which G1
reconciles print-for-print with the accepted OA-180 audit; the build asserts the same
contract totals. Chain snapshots supply settled open interest, option gamma and spot for
the archive's exact contracts only (same underlying, expiry, strike and type). Values
that fail the OA-171 usability rules (non-finite or negative) are kept as null.
"""
from collections import deque
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g1_reconcile import RUNS, OA180_COMBINED, events, load_recent  # noqa: E402

ET = ZoneInfo("America/New_York")
SIDES = ("buy", "sell", "unknown")


def snapshot_ms(day, name):
    hhmmss = name.removeprefix("snapshot_").removesuffix(".csv")
    stamp = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), int(hhmmss[:2]), int(hhmmss[2:4]),
                     int(hhmmss[4:6]), int(hhmmss[6:12].ljust(6, "0")), tzinfo=ET)
    return int(stamp.timestamp() * 1000)


def usable(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"),) and v >= 0 else None


def snapshots(root, day, symbols):
    compact = day.replace("-", "")
    out = []
    for path in sorted((root / "intraday" / compact).glob("snapshot_*.csv")):
        rows = {r["OptionSymbol"]: r for r in csv.DictReader(path.open(newline="")) if r.get("OptionSymbol") in symbols}
        spots = {usable(r.get("UnderlyingPrice")) for r in rows.values()} - {None}
        out.append(dict(t=snapshot_ms(compact, path.name), key=f"intraday/{compact}/{path.name}",
                        spot=spots.pop() if len(spots) == 1 else None,
                        gamma=[usable(rows[s]["Gamma"]) if s in rows else None for s in symbols],
                        oi=[(lambda v: int(v) if v is not None else None)(usable(rows[s]["OpenInterest"])) if s in rows else None
                            for s in symbols]))
    return out


def oi_observations(snaps, symbols):
    """Each distinct settled OI value per contract with its first observed snapshot."""
    out = []
    for i, _ in enumerate(symbols):
        seen = []
        for s in snaps:
            v = s["oi"][i]
            if v is not None and (not seen or seen[-1]["value"] != v):
                seen.append(dict(value=v, first_seen=s["t"], key=s["key"]))
        out.append(seen)
    return out


def build(archive, snaproot, day):
    owner = RUNS[day]
    recent = load_recent()
    feed = recent.RecentPrints()
    feed.rows = deque()
    gaps, summary = [], None
    for summary, e in events(archive, day, owner):
        feed.observe(e)
        if e.get("type") == "gap":
            gaps.append(e)
    rows = sorted(feed.rows, key=lambda r: r["t"])
    totals = {s: sum(r["size"] for r in rows if r["side"] == s) for s in SIDES}
    assert totals == OA180_COMBINED[day], "display path differs from the accepted OA-180 audit"
    symbols = sorted(summary["universe"]["option_metadata"])
    index = {s: i for i, s in enumerate(symbols)}
    snaps = snapshots(snaproot, day, symbols)
    open_ms, close_ms = [int(datetime.fromisoformat(summary[k]).timestamp() * 1000) for k in ("session_open", "session_close")]
    prices = [round(r["price"] * 100) for r in rows]
    assert all(abs(p / 100 - r["price"]) < 1e-9 for p, r in zip(prices, rows)), "sub-cent print price"
    manifest = archive / f"moo144/tradier/{day}/manifest-{owner}.json"
    identity = hashlib.sha256("\n".join(f"{r['symbol']},{r['seq']}" for r in rows).encode()).hexdigest()
    return dict(
        schema="order-flow-replay/v2", session=day,
        source=dict(manifest=f"moo144/tradier/{day}/manifest-{owner}.json",
                    manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    display_path="market_data/tradier/recent.py", identity_sha256=identity,
                    audit="OA-180 combined method (1 s quote midpoint, else tick)", audit_contracts=totals,
                    snapshots=f"intraday/{day.replace('-', '')}/snapshot_*.csv", snapshot_count=len(snaps)),
        coverage=dict(open=open_ms, close=close_ms, first_print=rows[0]["t"], last_print=rows[-1]["t"],
                      gap_seconds=summary["gap_seconds"], reconnects=summary["reconnects"],
                      gaps=[dict(receipt=g.get("receipt_timestamp"), outage_seconds=g.get("outage_seconds")) for g in gaps],
                      contracts="16 same-day contracts selected at 09:29 ET; strikes outside 740-747 are not captured"),
        multiplier=100,
        contracts=[dict(symbol=s, strike=summary["universe"]["option_metadata"][s]["strike"],
                        type=summary["universe"]["option_metadata"][s]["option_type"][0].upper(),
                        expiration=summary["universe"]["option_metadata"][s]["expiration"]) for s in symbols],
        oi=oi_observations(snaps, symbols),
        snapshots=snaps,
        prints=dict(count=len(rows), t0=rows[0]["t"],
                    dt=[b["t"] - a["t"] for a, b in zip([rows[0]] + rows, rows)],
                    contract=[index[r["symbol"]] for r in rows], size=[r["size"] for r in rows],
                    cents=prices, side=[SIDES.index(r["side"]) for r in rows]),
    )


if __name__ == "__main__":
    day = sys.argv[3] if len(sys.argv) > 3 else "2026-09-25"
    print(json.dumps(build(Path(sys.argv[1]), Path(sys.argv[2]), day), separators=(",", ":")))
