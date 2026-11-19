"""G1 / display archive check: reconcile the Order Flow display path with the accepted OA-180 audit.

Usage (offline, after fetching the archive with OA-180's fetch.py):
    python research/oa241/g1_reconcile.py ARCHIVE_ROOT [DAY ...] > research/oa241/g1_results.json

Both paths read the same verified manifest segments in key order (archive receipt order).
* Display path: market_data/tradier/recent.py RecentPrints, unchanged, fed every event, with
  the 2,048-row display deque widened so every row it accepts is retained; the stock 2,048 cap
  is replayed separately to report what the screen holds.
* Audit path: OA-180 audit.py's per-print rules (combined = 1 s quote midpoint, else tick),
  re-expressed per print so each print can be joined by (symbol, seq). Its contract totals are
  asserted equal to OA-180 results.json before any comparison.
Windows compare identical contracts and provider-time intervals.
"""
from collections import Counter, deque
from datetime import datetime
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OA240_REPLAY_REV = "6f78e7f"
RUNS = {"2026-09-25": "2fe15b0976d1", "2026-09-29": "64d13a1597e1", "2026-10-01": "66314af0dcf4"}
MANIFEST_HASHES = {
    "2026-09-25": "727933ec55d1710f977175887d0090fa70086abaa360e2ff8c5acc3097e4349f",
    "2026-09-29": "e40c6a01de3eccf5ecf6437152f885b7760aa5a2dd5033bd40fbf5f744a0583d",
    "2026-10-01": "19e8f99d30b80b331e04c1e2526f4eabb996a0953a645ba6ce004b0cec2f977a",
}
# OA-180 results.json, method "combined", per session (accepted option contracts by side).
OA180_COMBINED = json.loads((HERE / "oa180_combined.json").read_text())


def load_recent():
    spec = importlib.util.spec_from_file_location("recent", ROOT / "market_data/tradier/recent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(root, day, owner):
    manifest = root / f"moo144/tradier/{day}/manifest-{owner}.json"
    raw = manifest.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_HASHES[day], "manifest digest"
    summary = json.loads((manifest.parent / f"summary-{owner}.json").read_bytes())
    for artifact in sorted(json.loads(raw)["artifacts"], key=lambda a: a["key"]):
        data = (root / artifact["key"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"], artifact["key"]
        if "records" not in artifact:
            continue
        for line in gzip.decompress(data).splitlines():
            yield summary, json.loads(line)


def audit_prints(root, day, owner):
    """OA-180 audit.py loop, emitting one record per accepted option print."""
    quotes, prior, seen, out = {}, {}, set(), {}
    summary = None
    for summary, e in events(root, day, owner):
        if e["type"] == "quote":
            quotes[e["symbol"]] = e
        if e["type"] != "timesale" or e["symbol"] == "QQQ":
            continue
        market_open, market_close = [datetime.fromisoformat(summary[k]).timestamp() * 1000
                                     for k in ("session_open", "session_close")]
        symbol, ts, receipt = e["symbol"], int(e["date"]), e["collector_receipt_timestamp"]
        price, size = round(float(e["last"]) * 10000), int(e["size"])
        identity = (symbol, e.get("seq"))
        reason = next((k for k in ("cancel", "correction", "duplicate_in_run") if e.get(k)), None)
        if reason or identity[1] is None or identity in seen or not market_open <= ts <= market_close or size <= 0:
            continue
        seen.add(identity)
        tick = "unknown"
        previous = prior.get(symbol)
        if previous and ts > previous[0]:
            tick = "buy" if price > previous[1] else "sell" if price < previous[1] else previous[2]
        if not previous or ts >= previous[0]:
            prior[symbol] = (ts, price, tick)
        quote = quotes.get(symbol) or e.get("preceding_quote_context") or {}
        q = "unknown"
        if all(quote.get(k) is not None for k in ("bid", "ask", "biddate", "askdate", "collector_receipt_timestamp")):
            bid, ask = [round(float(quote[k]) * 10000) for k in ("bid", "ask")]
            dates = [int(quote[k]) for k in ("biddate", "askdate")]
            if (quote["symbol"] == symbol and quote["collector_receipt_timestamp"] < receipt
                    and datetime.fromisoformat(quote["collector_receipt_timestamp"]).timestamp() * 1000 < ts
                    and max(dates) < ts and 0 < bid < ask and ts - min(dates) <= 1000):
                delta = 2 * price - bid - ask
                q = "buy" if delta > 0 else "sell" if delta < 0 else "unknown"
        out[identity] = dict(t=ts, size=size, side=q if q != "unknown" else tick)
    return summary, out


def display_prints(root, day, owner, recent):
    wide, capped = recent.RecentPrints(), recent.RecentPrints()
    wide.rows = deque()
    for _, e in events(root, day, owner):
        wide.observe(e)
        capped.observe(e)
    rows = {(r["symbol"], r["seq"]): r for r in wide.rows}
    assert len(rows) == len(wide.rows), "display emitted a duplicate identity"
    return rows, list(capped.rows)


def tally(prints, keys):
    out = {s: dict(prints=0, contracts=0) for s in ("buy", "sell", "unknown")}
    for k in keys:
        p = prints[k]
        out[p["side"]]["prints"] += 1
        out[p["side"]]["contracts"] += p["size"]
    out["total"] = dict(prints=sum(v["prints"] for v in out.values()),
                        contracts=sum(v["contracts"] for v in out.values()))
    return out


def compare(display, audit, lo, hi):
    d = {k for k, p in display.items() if lo <= p["t"] <= hi}
    a = {k for k, p in audit.items() if lo <= p["t"] <= hi}
    both = d & a
    side = Counter((display[k]["side"], audit[k]["side"]) for k in both)
    return dict(window_ms=[lo, hi], window_et=[et(lo), et(hi)], display=tally(display, d), audit=tally(audit, a),
                display_only=len(d - a), audit_only=len(a - d), joined=len(both),
                same_side=sum(v for (x, y), v in side.items() if x == y),
                side_pairs={f"{x}->{y}": v for (x, y), v in sorted(side.items()) if x != y})


def et(ms):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ms / 1000, ZoneInfo("America/New_York")).strftime("%H:%M:%S.%f")[:-3]


def reconcile(root, day, recent):
    owner = RUNS[day]
    summary, audit = audit_prints(root, day, owner)
    totals = tally(audit, audit)
    expected = OA180_COMBINED[day]
    assert {s: totals[s]["contracts"] for s in ("buy", "sell", "unknown")} == expected, "audit != OA-180 results.json"
    display, capped = display_prints(root, day, owner, recent)
    open_ms, close_ms = [int(datetime.fromisoformat(summary[k]).timestamp() * 1000) for k in ("session_open", "session_close")]
    windows = {"full_session": compare(display, audit, open_ms, close_ms)}
    if day == "2026-09-25":
        # The 45-second replay OA-240 published (PR #259), read from git so the check survives its replacement.
        replay = json.loads(subprocess.run(["git", "show", f"{OA240_REPLAY_REV}:site/order-flow-replay.json"], cwd=ROOT,
                                           check=True, capture_output=True).stdout)["rows"]
        lo, hi = min(r["t"] for r in replay), max(r["t"] for r in replay)
        windows["published_replay"] = compare(display, audit, lo, hi)
        published = {(r["symbol"], r["seq"]): r for r in replay}
        windows["published_replay"]["published_file_vs_display_path"] = dict(
            rows=len(replay), identical=all(display.get(k, {}) == r for k, r in published.items()),
            display_rows_in_window_not_published=len({k for k, p in display.items()
                                                    if lo <= p["t"] <= hi} - set(published)),
            in_receipt_order_not_time_order=any(a["t"] > b["t"] for a, b in zip(replay, replay[1:])))
    last = capped[-1]["t"]
    cap_keys = {(r["symbol"], r["seq"]) for r in capped}
    windows["screen_at_close"] = compare({k: display[k] for k in cap_keys}, audit, capped[0]["t"], last)
    windows["screen_at_close"]["note"] = "the 2,048 rows retained when the last print arrived, against the audit over the same provider-time span"
    outside = {k: p for k, p in display.items() if k not in audit}
    return dict(day=day, manifest=f"moo144/tradier/{day}/manifest-{owner}.json",
                session=[summary["session_open"], summary["session_close"]],
                audit_matches_oa180_results=True, windows=windows,
                display_only_reasons=Counter("outside_session" if not open_ms <= p["t"] <= close_ms else "other"
                                             for p in outside.values()),
                audit_only=len(set(audit) - set(display)))


if __name__ == "__main__":
    recent = load_recent()
    days = sys.argv[2:] or list(RUNS)
    print(json.dumps([reconcile(Path(sys.argv[1]), d, recent) for d in days], indent=1, default=dict))
