"""Cost evidence: count every store operation of scheduled pilot cycles, against local stores.

    python tests/news_cycle_cost_measure.py OUT_DIR [--cycles 96] [--expiry] [--live-feeds]

Runs `publish.pilot_cycle` with the shipped (inactive) news-pilot.json, once per simulated
15-minute tick, against two counting LocalObjectStore directories (private, public). Each store
call is recorded under the R2 operation it maps to in R2ObjectStore (archive.py):

  put / put_if_absent / put_if_match  -> PutObject       (Class A)
  list / list_meta                    -> ListObjectsV2   (Class A, one request per 1,000 keys)
  get / get_versioned                 -> GetObject       (Class B)
  last_modified                       -> HeadObject      (Class B)
  delete                              -> DeleteObject    (free on R2)

It also records bytes written, objects and bytes held at the end of every cycle, local wall and
CPU time, and the publication outcome. Writes the per-cycle rows and a summary to
OUT_DIR/measurements.json.

What this is and is not:
  * OBSERVED: operation counts, request classes, bytes and object growth, publication outcomes and
    local CPU/wall time of the code as shipped, on this machine.
  * NOT OBSERVED: provider latency, provider-side retries, real feed volume over a day, R2 billing.
    Those are projections (see the cost packet) or need provider access.
  * The simulated clock advances 15 minutes per cycle. The store's own clock (last_modified) is the
    simulated time of the write, so generation expiry can be exercised with --expiry (which sets
    generations.clock_bound_verified true in a temporary copy of the config); the shipped config
    keeps it false, and so does this harness unless --expiry is passed.
  * Feeds: the repo fixtures by default (static: every cycle after the first sees no new items);
    --live-feeds uses the real public RSS fetcher for the four pilot sources (reads only).

No bucket, credential or provider call is made in any mode.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data.news import publish as pub  # noqa: E402
from market_data.news.__main__ import DEFAULT_FIXTURES, fixture_fetcher  # noqa: E402
from market_data.news.archive import LocalObjectStore  # noqa: E402

CONFIG = ROOT / "market_data/news/deploy/news-pilot.json"
T0 = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)   # a Monday, 00:00 UTC
LIST_PAGE = 1000

R2_OP = {
    "put": ("PutObject", "A"), "put_if_absent": ("PutObject", "A"), "put_if_match": ("PutObject", "A"),
    "get": ("GetObject", "B"), "get_versioned": ("GetObject", "B"),
    "last_modified": ("HeadObject", "B"), "delete": ("DeleteObject", "free"),
}


class SimClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class CountingStore(LocalObjectStore):
    """A LocalObjectStore that counts the R2 requests each call would make and reports the
    simulated write time as the store's own clock."""

    def __init__(self, root, clock: SimClock):
        super().__init__(root)
        self.clock = clock
        self.ops: Counter = Counter()
        self.requests: Counter = Counter()
        self.put_bytes = 0
        self.written_at: dict[str, datetime] = {}

    def reset(self):
        self.ops, self.requests, self.put_bytes = Counter(), Counter(), 0

    def _count(self, method: str, n_requests: int = 1):
        self.ops[method] += 1
        op, cls = R2_OP.get(method, ("ListObjectsV2", "A"))
        self.requests[f"{cls}:{op}"] += n_requests

    def _wrote(self, key, data):
        self.put_bytes += len(data)
        self.written_at[key] = self.clock()

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        self._count("put")
        super().put(key, data, content_type, cache_control)
        self._wrote(key, data)

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        self._count("put_if_absent")
        created = super().put_if_absent(key, data, content_type, cache_control)
        if created:
            self._wrote(key, data)
        return created

    def put_if_match(self, key, data, etag, content_type="application/octet-stream", cache_control=None,
                     write_id=None):
        self._count("put_if_match")
        # The base class composes put_if_absent/put; count only this one request.
        ops, requests, put_bytes = self.ops.copy(), self.requests.copy(), self.put_bytes
        out = super().put_if_match(key, data, etag, content_type, cache_control, write_id)
        self.ops, self.requests = ops, requests
        self.put_bytes = put_bytes
        if out.applied:
            self._wrote(key, data)
        return out

    def get(self, key):
        self._count("get")
        return super().get(key)

    def get_versioned(self, key):
        self._count("get_versioned")
        body = LocalObjectStore.get(self, key)
        from market_data.news.archive import content_etag
        return None if body is None else (body, content_etag(body))

    def last_modified(self, key):
        self._count("last_modified")
        if not self._path(key).is_file():
            return None
        return self.written_at.get(key) or super().last_modified(key)

    def delete(self, key):
        self._count("delete")
        super().delete(key)
        self.written_at.pop(key, None)

    def _listed(self, method, keys):
        self._count(method, max(1, math.ceil(len(keys) / LIST_PAGE)))
        return keys

    def list(self, prefix):
        return self._listed("list", super().list(prefix))

    def list_meta(self, prefix, start_after=None):
        # The base list_meta calls self.list; count it once, as one listing (StartAfter honoured).
        out = {}
        for key in LocalObjectStore.list(self, prefix):
            if start_after is not None and key.encode() <= start_after.encode():
                continue
            st = self._path(key).stat()
            out[key] = f"{st.st_size}:{st.st_mtime_ns}"
        self._listed("list_meta", list(out))
        return out

    def held(self) -> tuple[int, int]:
        files = [p for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".tmp-")]
        return len(files), sum(p.stat().st_size for p in files)


def run(out_dir: Path, cycles: int, expiry: bool, live_feeds: bool, start: datetime = T0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="news-cost-"))
    cfg_path = CONFIG
    if expiry:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["generations"]["clock_bound_verified"] = True
        cfg_path = work / "news-pilot.expiry.json"
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = pub.load_pilot_config(cfg_path)
    clock = SimClock(start)
    private, public = CountingStore(work / "private", clock), CountingStore(work / "public", clock)
    if live_feeds:
        from market_data.news.adapters.rss import http_fetch as fetch
    else:
        fetch = fixture_fetcher(DEFAULT_FIXTURES)
    rows = []
    try:
        for i in range(cycles):
            clock.now = start + timedelta(seconds=i * cfg["schedule"]["interval_s"])
            private.reset(), public.reset()
            wall, cpu = time.perf_counter(), time.process_time()
            error = None
            try:
                out = pub.pilot_cycle(private, PublicViewStore(public), work / "spool", fetch, clock, cfg)
            except Exception as exc:   # a failed cycle is a measurement too
                out, error = {"status": "error"}, f"{type(exc).__name__}: {exc}"[:300]
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
            p_objs, p_bytes = private.held()
            q_objs, q_bytes = public.held()
            publication = out.get("publication") or {}
            run_info = out.get("run") or {}
            rows.append({
                "cycle": i, "at": clock.now.isoformat(), "status": out.get("status"), "error": error,
                "run_status": run_info.get("status"),
                "new_items": (run_info.get("counts") or {}).get("new_items"),
                "publication": {k: publication.get(k) for k in ("action", "written", "unchanged", "deleted",
                                                                "retired", "public_pages", "quarantined")},
                "wall_s": round(wall, 4), "cpu_s": round(cpu, 4),
                "private": {"requests": dict(private.requests), "put_bytes": private.put_bytes,
                            "objects": p_objs, "bytes": p_bytes},
                "public": {"requests": dict(public.requests), "put_bytes": public.put_bytes,
                           "objects": q_objs, "bytes": q_bytes},
            })
    finally:
        if live_feeds:
            shutil.copytree(work / "private", out_dir / "private-store", dirs_exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    result = {"schema": "news-cycle-cost-measure/v1", "config": str(CONFIG.relative_to(ROOT)),
              "cycles": cycles, "start": start.isoformat(), "expiry_simulated": expiry, "feeds": "live" if live_feeds else "fixtures",
              "interval_s": cfg["schedule"]["interval_s"], "rows": rows, "summary": summarize(rows)}
    (out_dir / "measurements.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def summarize(rows: list[dict]) -> dict:
    def total(side, cls):
        return sum(v for r in rows for k, v in r[side]["requests"].items() if k.startswith(cls + ":"))

    def per_cycle(side, cls):
        vals = [sum(v for k, v in r[side]["requests"].items() if k.startswith(cls + ":")) for r in rows]
        return {"first": vals[0], "max": max(vals), "last": vals[-1], "mean": round(sum(vals) / len(vals), 1)}

    statuses = Counter((r["status"], (r["publication"] or {}).get("action")) for r in rows)
    return {
        "cycles": len(rows),
        "statuses": {f"{a}/{b}": n for (a, b), n in statuses.items()},
        "errors": [r["error"] for r in rows if r["error"]][:5],
        "class_a": {"private": per_cycle("private", "A"), "public": per_cycle("public", "A"),
                    "total": total("private", "A") + total("public", "A")},
        "class_b": {"private": per_cycle("private", "B"), "public": per_cycle("public", "B"),
                    "total": total("private", "B") + total("public", "B")},
        "deletes": total("private", "free") + total("public", "free"),
        "held_end": {"private": {"objects": rows[-1]["private"]["objects"], "bytes": rows[-1]["private"]["bytes"]},
                     "public": {"objects": rows[-1]["public"]["objects"], "bytes": rows[-1]["public"]["bytes"]}},
        "put_bytes_total": sum(r["private"]["put_bytes"] + r["public"]["put_bytes"] for r in rows),
        "wall_s": {"max": max(r["wall_s"] for r in rows), "mean": round(sum(r["wall_s"] for r in rows) / len(rows), 3)},
        "cpu_s": {"max": max(r["cpu_s"] for r in rows), "mean": round(sum(r["cpu_s"] for r in rows) / len(rows), 3)},
    }


PublicViewStore = pub.PublicViewStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_dir")
    ap.add_argument("--cycles", type=int, default=96)
    ap.add_argument("--expiry", action="store_true", help="simulate a verified store clock bound (expiry on)")
    ap.add_argument("--live-feeds", action="store_true", help="fetch the four public pilot feeds (reads only)")
    ap.add_argument("--start", help="first cycle time (UTC ISO); default 2026-11-16T00:00Z, or now with --live-feeds")
    a = ap.parse_args(argv)
    start = (datetime.fromisoformat(a.start.replace("Z", "+00:00")) if a.start else
             datetime.now(timezone.utc).replace(microsecond=0) if a.live_feeds else T0)
    result = run(Path(a.out_dir), a.cycles, a.expiry, a.live_feeds, start)
    print(json.dumps(result["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
