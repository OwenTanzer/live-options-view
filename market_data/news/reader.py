"""As-of reads over committed history, plus the derived index/latest views.

Modes:
  collector    what our collector had observed by the cutoff (observed_at <= cutoff)
  archive      what was durably committed by the cutoff (also committed_at <= cutoff,
               stamped only after the run's manifest read back)
  publication  retrospective, labeled hindsight: items by source publication
               time, using everything known now. Never a substitute for the two
               availability modes.

Classifications are separate events: in the availability modes one is used
only if it was made (and, in archive mode, committed) by the cutoff and was
made for the exact revision shown. Joining to price records is in join.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .archive import INDEX_PREFIX, LATEST_KEY, encode_json
from .checkpoint import read_history
from .normalize import iso, parse_iso

MODES = ("collector", "archive", "publication")


@dataclass
class Archive:
    observations: list[dict]      # each with archived_at from its run manifest
    classifications: list[dict]   # each with archived_at
    runs: list[dict]              # committed manifests
    problems: list[dict]
    uncommitted: list[dict]
    history: dict | None = None   # how it was read: checkpoint plus tail, or full replay (and why)


def load_archive(store, *, use_checkpoint: bool = True) -> Archive:
    """Committed history. Read-only: uses a usable checkpoint, else replays every run; never writes."""
    scan = read_history(store, use_checkpoint=use_checkpoint)
    obs, cls = [], []
    for run in scan.runs:
        committed = run.manifest["committed_at"]
        obs.extend(dict(o, archived_at=committed) for o in run.observations)
        cls.extend(dict(c, archived_at=committed) for c in run.classifications)
    return Archive(obs, cls, [r.manifest for r in scan.runs], scan.problems, scan.uncommitted, scan.checkpoint)


def _known(record_time: str | None, archived_at: str, cutoff, mode: str) -> bool:
    if mode == "publication":
        return True
    t = parse_iso(record_time)
    if t is None or t > cutoff:
        return False
    if mode != "archive":
        return True
    # No availability record yet: not provably archived at any cutoff.
    return archived_at is not None and parse_iso(archived_at) <= cutoff


def as_of(archive: Archive, cutoff, mode: str = "collector", stream: str | None = None,
          start=None, end=None, limit: int | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cutoff = parse_iso(cutoff) if isinstance(cutoff, str) else cutoff

    revisions: dict[str, list[dict]] = {}
    for o in archive.observations:
        if _known(o["observed_at"], o["archived_at"], cutoff, mode):
            revisions.setdefault(o["item_id"], []).append(o)
    by_revision: dict[str, list[dict]] = {}
    for c in archive.classifications:
        if _known(c["classified_at"], c["archived_at"], cutoff, mode):
            by_revision.setdefault(c["input_revision_id"], []).append(c)

    items, excluded = [], {"unclassified_for_stream": 0, "no_publication_time": 0, "outside_window": 0}
    for item_id, revs in revisions.items():
        revs.sort(key=lambda r: (r["observed_at"], r["archived_at"]))
        current = revs[-1]
        cls = sorted(by_revision.get(current["revision_id"], []), key=lambda c: c["classified_at"])
        classification = cls[-1] if cls else None
        if mode == "publication":
            when = parse_iso(current["published_at"])
            if when is None:
                excluded["no_publication_time"] += 1
                continue
            if when > cutoff:
                continue
        else:
            when = parse_iso(current["first_seen_at"])
        if (start and when < parse_iso(start)) or (end and when > parse_iso(end)):
            excluded["outside_window"] += 1
            continue
        if stream:
            if classification is None:
                if stream in current["source_streams"]:
                    excluded["unclassified_for_stream"] += 1
                continue
            if stream not in classification["stream_memberships"]:
                continue
        items.append({
            "item_id": item_id,
            "source_id": current["source_id"],
            "source_name": current["source_name"],
            "outlet": current["outlet"],
            "title": current["title"],
            "excerpt": current["excerpt"],
            "original_url": current["original_url"],
            "canonical_url": current["canonical_url"],
            "published_at": current["published_at"],
            "published_raw": current["published_raw"],
            "updated_at": current["updated_at"],
            "time_quality": current["time_quality"],
            "first_seen_at": current["first_seen_at"],
            "observed_at": current["observed_at"],
            "archived_at": current["archived_at"],
            "revision_id": current["revision_id"],
            "revision_count": len(revs),
            "revisions": [{"revision_id": r["revision_id"], "title": r["title"], "observed_at": r["observed_at"],
                           "archived_at": r["archived_at"]} for r in revs],
            "classification": None if classification is None else {
                k: classification[k] for k in ("classification_id", "classified_at", "method", "version",
                                               "stream_memberships", "labels", "instrument_mapping")},
        })
    items.sort(key=lambda i: (i["published_at"] if mode == "publication" else i["first_seen_at"]) or "", reverse=True)
    if limit is not None:
        items = items[:limit]
    runs = [r for r in archive.runs if mode == "publication" or _known(r["finished_at"], r["committed_at"], cutoff, mode)]
    failures = [{"run_id": r["run_id"], "source_id": s["source_id"], "status": s["status"], "error": s.get("error", "")}
                for r in runs for s in r["sources"] if s["status"] not in ("ok", "disabled", "not_selected")]
    return {
        "mode": mode,
        "label": ("RETROSPECTIVE publication-time view: uses everything known now (hindsight)"
                  if mode == "publication" else f"{mode}-as-of view: only what was available by the cutoff"),
        "hindsight": mode == "publication",
        "cutoff": iso(cutoff),
        "stream": stream,
        "items": items,
        "excluded": excluded,
        "coverage": {"runs": len(runs), "source_failures": failures,
                     "corrupt_runs": [p["run_id"] for p in archive.problems],
                     "uncommitted_runs": [u["run_id"] for u in archive.uncommitted]},
    }


def _gaps(collect_runs: list[dict], interval_s: int) -> list[dict]:
    out = []
    for prev, cur in zip(collect_runs, collect_runs[1:]):
        delta = (parse_iso(cur["started_at"]) - parse_iso(prev["started_at"])).total_seconds()
        if delta > 2 * interval_s:
            out.append({"after_run": prev["run_id"], "before_run": cur["run_id"], "gap_seconds": int(delta),
                        "observation_date": cur["observation_date"]})
    return out


def rebuild_indexes(store, clock, interval_s: int = config.INTERVAL_S) -> dict:
    """Rewrite every daily index and latest.json from committed manifests alone."""
    archive = load_archive(store)
    now = clock()
    collect = sorted((r for r in archive.runs if r["run_kind"] == "collect"), key=lambda r: r["started_at"])
    gaps = _gaps(collect, interval_s)
    dates = sorted({r["observation_date"] for r in archive.runs}
                   | {p["prefix"].split("/")[3] for p in archive.problems + archive.uncommitted})
    for date in dates:
        runs = sorted((r for r in archive.runs if r["observation_date"] == date), key=lambda r: r["started_at"])
        store.put(f"{INDEX_PREFIX}{date}.json", encode_json({
            "schema_version": config.INDEX_SCHEMA,
            "derived": True,
            "generated_at": iso(now),
            "observation_date": date,
            "runs": [{"run_id": r["run_id"], "run_kind": r["run_kind"], "status": r["status"],
                      "started_at": r["started_at"], "committed_at": r["committed_at"], "counts": r["counts"],
                      "manifest_key": f"{config.NAMESPACE}/runs/{date}/{r['run_id']}/manifest.json"} for r in runs],
            "missed_intervals": [g for g in gaps if g["observation_date"] == date],
            "corrupt_runs": [p for p in archive.problems if f"/{date}/" in p["prefix"]],
            "uncommitted_runs": [u for u in archive.uncommitted if f"/{date}/" in u["prefix"]],
        }), "application/json")
    latest = as_of(archive, now, "archive", limit=config.LATEST_LIMIT)
    store.put(LATEST_KEY, encode_json({
        "schema_version": config.INDEX_SCHEMA, "derived": True, "generated_at": iso(now),
        "mode": latest["mode"], "limit": config.LATEST_LIMIT, "coverage": latest["coverage"],
        "items": [{k: v for k, v in i.items() if k != "revisions"} for i in latest["items"]],
    }), "application/json")
    return {"dates": dates, "latest_items": len(latest["items"]), "missed_intervals": len(gaps)}
