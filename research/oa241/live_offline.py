"""G3-live offline exercise: drive the live order-flow path with archived events.

    python research/oa241/live_offline.py ARCHIVE_ROOT [DAY] > research/oa241/live_offline_DAY.json

Scenario on one archived session, no provider or network:
1. Process A receives the first half of the archive as live events, with a reconnect
   gap marker injected (as capture.py writes after an outage).
2. Process A "crashes". Process B starts, rebuilds from the segments already "uploaded"
   (an in-memory stand-in for R2 holding the first half of the parts), then receives live
   events starting two segments *before* the crash point (a deliberate overlap).
3. Checks: B's session counters equal the accepted OA-180 audit per side and per contract;
   overlaps were not double counted; the display stays capped at 2,048 rows while the
   session counts every print; the reconnect and restart gaps are published; the restart
   gap closes at B's first live receipt. The published payload is written for the browser
   test that compares live exposure with replay exposure.
"""
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g1_reconcile import RUNS, OA180_COMBINED, MANIFEST_HASHES, load_recent  # noqa: E402
import hashlib  # noqa: E402


class FakeR2:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 50]
        more = start + 50 < len(keys)
        return {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + 50)} if more else {})}

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[Key])}


def parts(root, day):
    owner = RUNS[day]
    manifest = root / f"moo144/tradier/{day}/manifest-{owner}.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == MANIFEST_HASHES[day]
    arts = sorted((a for a in json.loads(manifest.read_bytes())["artifacts"] if "records" in a), key=lambda a: a["key"])
    return [(a["key"], (root / a["key"]).read_bytes()) for a in arts]


def lines(blob):
    return [json.loads(x) for x in gzip.decompress(blob).splitlines()]


def run(root, day):
    recent = load_recent()
    segs = parts(root, day)
    crash = len(segs) // 2
    a = recent.RecentPrints()
    for i, (_, blob) in enumerate(segs[:crash]):
        for e in lines(blob):
            a.observe(e)
        if i == crash // 2:
            a.observe(dict(type="gap", reason="stream_reconnect_resumed", outage_seconds=4.25,
                           receipt_timestamp=e["collector_receipt_timestamp"]))
    a_payload = a.payload()
    prefix = f"moo144/tradier/{day}"
    b = recent.RecentPrints()
    restart_at = datetime.now(timezone.utc)
    b.rebuild(FakeR2(dict(segs[:crash])), "bucket", prefix, now=restart_at)
    after_rebuild = b.payload()["session"]
    first_live = None
    through = after_rebuild["rebuilt"]["through"]
    for _, blob in segs[crash - 2:]:
        for e in lines(blob):
            r = e.get("collector_receipt_timestamp")
            if first_live is None and r and recent._ms(r) > through:
                first_live = r
            b.observe(e)
    out = b.payload()
    s = out["session"]
    sides = Counter()
    for c in s["contracts"].values():
        sides["buy"] += c["B"]
        sides["sell"] += c["S"]
        sides["unknown"] += c["U"]
    checks = {
        "session_equals_oa180_audit": dict(sides) == OA180_COMBINED[day],
        "overlap_not_double_counted": s["prints"] == sum(c["prints"] for c in s["contracts"].values()),
        "display_capped_at_2048": len(out["rows"]) == 2048 and out["cap"] == 2048,
        "session_counts_beyond_cap": s["prints"] > 2048,
        "reconnect_gap_in_crashed_process": any(g["reason"] == "stream_reconnect_resumed" for g in a_payload["session"]["gaps"]),
        "rebuild_status": after_rebuild["rebuilt"]["status"] == "rebuilt" and after_rebuild["rebuilt"]["segments"] == crash,
        "restart_gap_open_until_live": after_rebuild["gaps"][-1]["end"] is None,
        "restart_gap_closed_at_first_live_receipt": s["gaps"][-1]["end"] == recent._ms(first_live),
    }
    return dict(day=day, segments=len(segs), crash_after_segment=crash, overlap_segments=2, checks=checks,
                session_sides=dict(sides), prints=s["prints"], gaps=s["gaps"], payload=out)


if __name__ == "__main__":
    day = sys.argv[2] if len(sys.argv) > 2 else "2026-09-25"
    result = run(Path(sys.argv[1]), day)
    print(json.dumps(result, separators=(",", ":")))
    print(json.dumps({k: v for k, v in result.items() if k != "payload"}, indent=1), file=sys.stderr)
    assert all(result["checks"].values()), result["checks"]
