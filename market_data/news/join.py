"""Offline as-of join of archived news to price records (M3).

Each row is one news revision at the time it became available in the chosen
mode, matched BACKWARD to the latest price that was itself available by then:

  collector    event_at = observed_at (when the collector received the revision)
  archive      event_at = latest of observed_at and archived_at (the run's
               committed_at from commit.json). A run without an availability
               record is not in archive mode at any cutoff yet.
  publication  RETROSPECTIVE hindsight: event_at = the source's published_at,
               using everything known now. Never a substitute for the two
               availability modes.

Event-known history. In the availability modes a row describes only what was
known at its event_at: `revisions_known_at_event` counts the item's revisions
known by then, and `classification_at_event` is the newest classification
known by then (a classification's known time is classified_at, and in archive
mode also its archived_at). Anything learned later but before the cutoff is
listed separately under `later_context` and never changes the row.

With a stream filter, membership is knowledge too: a revision's stream event
is the later of its availability and the FIRST classification known by the
cutoff that put it in the stream. A later reclassification never moves that
event or its price; if it removes the membership, `later_context` says when.

Price matching uses only records with available_at <= event_at <= cutoff, so
a later tick, a late-arriving quote or an unclosed bar is never used. The
match is `matched` when the price's own time is within `tolerance_s` of
event_at, `stale` when the newest usable price is older than that (it is
reported, flagged), and `missing` when no price was available yet. The price
series is one instrument and one kind: underlying quotes, 1-minute bars or
daily bars, never option quotes for an underlying.

The window is [start, cutoff] and applies to all three outputs: rows whose
event_at falls in it, price intervals inside it, and news segments that
overlap it (clipped to start). A start after the cutoff is rejected. In
publication mode, runs after the cutoff (hindsight) are listed only under
news_coverage.after_cutoff_hindsight, never as segments.

Coverage is reported beside the rows, because an empty interval is only
evidence of no news when a committed, available run checked the relevant
sources. Relevant sources are those of the requested stream (all sources
without one). Each collect-run segment is:
  covered               every relevant enabled source was fetched without
                        failure and no entry failed to normalize
  partial               some relevant sources were fetched but others failed,
                        had entry failures, or were not selected (their
                        coverage is unknown; listed)
  unknown               no relevant source was fetched in that run
  pending_availability  archive mode only: the collector has the run but it
                        has no availability record by the cutoff
Relevant sources come from the run's recorded roster (manifest source_scope),
never from the surviving source rows alone: a stream-filtered run omits rows
for other streams' sources. A run recorded without a roster can be partial
or unknown but never covered (`roster_complete: false`). Disabled sources
are outside the collector's scope and are only counted. A segment is also
flagged `missed_interval` after a gap longer than two
intervals, and the tail since the last run is reported.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import config
from .normalize import iso, parse_iso
from .prices import PriceFile, PriceRecord
from .reader import MODES, Archive, _known

JOIN_SCHEMA = "news-price-join/v2"
SERIES = {"underlying_quote": ("underlying_quote", "tick"), "bar_1m": ("bar", "1m"), "bar_1d": ("bar", "1d")}


class PriceSeries:
    """One instrument, one kind of price; lookups only see what was available."""

    def __init__(self, records: list[PriceRecord], instrument_id: str, series: str):
        if series not in SERIES:
            raise ValueError(f"series must be one of {tuple(SERIES)} (option quotes are not an underlying price)")
        kind, granularity = SERIES[series]
        self.instrument_id, self.series = instrument_id, series
        self.records = sorted((r for r in records if r.instrument_id == instrument_id
                               and r.record_kind == kind and r.granularity == granularity),
                              key=lambda r: (r.available_at, r.price_time, r.record_id))

    def latest(self, t: datetime) -> PriceRecord | None:
        """Newest price (by its own time) among records available at t."""
        best = None
        for r in self.records:
            if r.available_at > t:
                break
            if best is None or (r.price_time, r.available_at) > (best.price_time, best.available_at):
                best = r
        return best

    def intervals(self, start: datetime, end: datetime, tolerance: timedelta) -> dict:
        """Missing (nothing available yet) and stale (newest price older than tolerance) intervals."""
        points = sorted({r.available_at for r in self.records if start < r.available_at < end})
        edges = [start, *points, end]
        out = {"missing": [], "stale": []}
        for a, b in zip(edges, edges[1:]):
            latest = self.latest(a)
            if latest is None:
                _add(out["missing"], a, b, None)
            elif latest.price_time + tolerance < b:
                _add(out["stale"], max(a, latest.price_time + tolerance), b, latest)
        return {k: [{"start": iso(s), "end": iso(e), "seconds": int((e - s).total_seconds()),
                     **({"last_price_record_id": r.record_id, "last_price_time": iso(r.price_time)} if r else {})}
                    for s, e, r in v] for k, v in out.items()}


def _add(intervals: list, a, b, rec):
    if intervals and intervals[-1][1] == a and intervals[-1][2] is rec:
        intervals[-1] = (intervals[-1][0], b, rec)
    else:
        intervals.append((a, b, rec))


def match_price(series: PriceSeries, event_at: datetime, cutoff: datetime, tolerance: timedelta) -> dict:
    rec = series.latest(min(event_at, cutoff))
    if rec is None:
        return {"status": "missing", "record": None, "age_seconds": None}
    assert rec.available_at <= event_at and rec.available_at <= cutoff
    age = (event_at - rec.price_time).total_seconds()
    return {"status": "matched" if age <= tolerance.total_seconds() else "stale",
            "record": rec.summary(), "age_seconds": age}




def _latest(*times: str) -> datetime:
    return max(parse_iso(t) for t in times)


def _revision_known_at(o: dict, mode: str) -> datetime | None:
    """When a revision became known in an availability mode (None: not yet available)."""
    if mode == "archive":
        return None if o["archived_at"] is None else _latest(o["observed_at"], o["archived_at"])
    return parse_iso(o["observed_at"])


def _classification_known_at(c: dict, mode: str) -> datetime | None:
    if mode == "archive":
        return None if c["archived_at"] is None else _latest(c["classified_at"], c["archived_at"])
    return parse_iso(c["classified_at"])


def _cls_summary(c: dict, known_at: datetime | None) -> dict:
    return {"classification_id": c["classification_id"], "classified_at": c["classified_at"],
            "classification_archived_at": c["archived_at"], "known_at": None if known_at is None else iso(known_at),
            "version": c["version"], "stream_memberships": c["stream_memberships"]}


def _classifications_by_revision(archive: Archive, cutoff, mode) -> dict[str, list[tuple]]:
    """revision_id -> [(known_at, classification)] known by the cutoff, oldest first."""
    out: dict[str, list[tuple]] = {}
    for c in archive.classifications:
        if _known(c["classified_at"], c["archived_at"], cutoff, mode):
            known = None if mode == "publication" else _classification_known_at(c, mode)
            out.setdefault(c["input_revision_id"], []).append((known, c))
    for v in out.values():
        v.sort(key=lambda kc: (kc[0] or parse_iso(kc[1]["classified_at"]), kc[1]["classified_at"],
                               kc[1]["classification_id"]))
    return out


def news_events(archive: Archive, cutoff: datetime, mode: str, stream: str | None = None) -> tuple[list[dict], dict]:
    """One event per revision known in `mode` by `cutoff`, timed by when it became available."""
    classified = _classifications_by_revision(archive, cutoff, mode)
    chains: dict[str, list[dict]] = {}
    for o in sorted(archive.observations, key=lambda o: (o["observed_at"], o["revision_id"])):
        chains.setdefault(o["item_id"], []).append(o)
    events, excluded = [], {"unclassified_for_stream": 0, "not_in_stream": 0, "no_publication_time": 0}
    for chain in chains.values():
        known_chain = [r for r in chain if _known(r["observed_at"], r["archived_at"], cutoff, mode)]
        for n, o in enumerate(chain, 1):
            if not _known(o["observed_at"], o["archived_at"], cutoff, mode):
                continue
            cls = classified.get(o["revision_id"], [])
            later: dict = {"revisions_known_by_cutoff": len(known_chain)}
            if mode == "publication":
                if o["published_at"] is None:
                    excluded["no_publication_time"] += 1
                    continue
                event_at, basis = parse_iso(o["published_at"]), "published_at (hindsight)"
                if event_at > cutoff:
                    continue
                current = cls[-1][1] if cls else None  # hindsight: newest known now
                if stream and (current is None or stream not in current["stream_memberships"]):
                    excluded["unclassified_for_stream" if current is None else "not_in_stream"] += 1
                    continue
                at_event, known_revisions = (None if current is None else _cls_summary(current, None)), None
            else:
                event_at = _revision_known_at(o, mode)
                basis = "archived_at" if mode == "archive" else "observed_at"
                if stream:
                    first_in = next(((k, c) for k, c in cls if stream in c["stream_memberships"]), None)
                    if first_in is None:
                        if cls:
                            excluded["not_in_stream"] += 1
                        elif stream in o["source_streams"]:
                            excluded["unclassified_for_stream"] += 1
                        continue
                    if first_in[0] > event_at:
                        event_at = first_in[0]
                        basis = "classification archived_at" if mode == "archive" else "classified_at"
                known_now = [(k, c) for k, c in cls if k <= event_at]
                at_event = _cls_summary(*reversed(known_now[-1])) if known_now else None
                known_revisions = sum(1 for r in known_chain if _revision_known_at(r, mode) <= event_at)
                later["revisions_after_event"] = [
                    {"revision_id": r["revision_id"], "known_at": iso(_revision_known_at(r, mode)), "title": r["title"]}
                    for r in known_chain if _revision_known_at(r, mode) > event_at]
                later["classifications_after_event"] = [_cls_summary(c, k) for k, c in cls if k > event_at]
                if stream:
                    removed = next((k for k, c in cls if k > event_at and stream not in c["stream_memberships"]), None)
                    later["stream_membership_removed_at"] = None if removed is None else iso(removed)
            events.append({
                "item_id": o["item_id"], "revision_id": o["revision_id"], "revision_number": n,
                "revisions_known_at_event": known_revisions, "title": o["title"], "source_id": o["source_id"],
                "outlet": o["outlet"], "canonical_url": o["canonical_url"], "url_status": o.get("url_status"),
                "published_at": o["published_at"], "time_quality": o["time_quality"],
                "observed_at": o["observed_at"], "archived_at": o["archived_at"], "run_id": o["run_id"],
                "classification_at_event": at_event,
                "event_at": iso(event_at), "event_basis": basis, "_event_at": event_at,
                "later_context": later,
            })
    events.sort(key=lambda e: (e["_event_at"], e["item_id"], e["revision_number"]))
    return events, excluded


def _source_scope(run: dict, stream: str | None) -> dict:
    """Relevant sources for one run, from its recorded roster (never from the surviving rows alone)."""
    by_id = {s["source_id"]: s for s in run["sources"]}
    roster = (run.get("source_scope") or {}).get("roster")
    if roster is None:  # a manifest without a roster (v2): rows may omit sources of other streams
        rows = [s for s in run["sources"] if stream is None or stream in s.get("streams", [])]
    else:
        rows = [by_id.get(r["source_id"]) or {"source_id": r["source_id"], "streams": r["streams"],
                                              "status": "not_selected" if r["enabled"] else "disabled"}
                for r in roster if stream is None or stream in r["streams"]]
    attempted = [s for s in rows if s["status"] not in ("disabled", "not_selected")]
    return {
        "roster_complete": roster is not None,
        "attempted": [s["source_id"] for s in attempted],
        "ok": [s["source_id"] for s in attempted if s["status"] == "ok"],
        "failed": [{"source_id": s["source_id"], "status": s["status"], "error": s.get("error", "")}
                   for s in attempted if s["status"] != "ok"],
        "not_selected": [s["source_id"] for s in rows if s["status"] == "not_selected"],
        "disabled_count": sum(1 for s in rows if s["status"] == "disabled"),
        "entry_failures": [{"source_id": s["source_id"], **f} for s in attempted for f in s.get("entry_failures", [])],
        "failed_entries": sum(s.get("failed_entries", 0) for s in attempted),
    }


def news_coverage(archive: Archive, cutoff: datetime, mode: str, stream: str | None = None, start=None,
                  interval_s: int = config.INTERVAL_S) -> dict:
    """Per-run coverage segments in [start, cutoff], scoped to the relevant sources."""
    observed = sorted((r for r in archive.runs if r["run_kind"] == "collect"
                       and (mode == "publication" or parse_iso(r["finished_at"]) <= cutoff)),
                      key=lambda r: (r["started_at"], r["run_id"]))
    segments, prev, pending = [], None, []
    for run in observed:
        available = mode != "archive" or (run["committed_at"] is not None
                                          and parse_iso(run["committed_at"]) <= cutoff)
        seg = {"start": prev["started_at"] if prev else None, "end": run["started_at"], "run_id": run["run_id"],
               "committed_at": run["committed_at"]}
        if not available:
            pending.append(run["run_id"])
            seg.update(state="pending_availability", detail="run observed and its manifest committed, but no "
                       "availability record by the cutoff: coverage unknown in archive mode, not a quiet interval")
        else:
            scope = _source_scope(run, stream)
            if not scope["attempted"]:
                state = "unknown"
            elif (scope["failed"] or scope["entry_failures"] or scope["not_selected"]
                  or not scope["roster_complete"]):
                state = "partial"
            else:
                state = "covered"
            seg.update(state=state, run_status=run["status"], sources=scope)
        if prev is None:
            seg.update(detail_before="before the first observed run: no news coverage")
        elif (parse_iso(run["started_at"]) - parse_iso(prev["started_at"])).total_seconds() > 2 * interval_s:
            seg.update(missed_interval=True,
                       gap_seconds=int((parse_iso(run["started_at"]) - parse_iso(prev["started_at"])).total_seconds()))
        segments.append(seg)
        prev = run
    # In publication mode later runs are hindsight context, never segments of [start, cutoff].
    after_cutoff = [s for s in segments if parse_iso(s["end"]) > cutoff]
    segments = [s for s in segments if parse_iso(s["end"]) <= cutoff]
    last_run_start = parse_iso(segments[-1]["end"]) if segments else None
    if start is not None:
        kept = []
        for seg in segments:
            if parse_iso(seg["end"]) < start:
                continue
            if seg["start"] is None or parse_iso(seg["start"]) < start:
                seg = dict(seg, start=iso(start), clipped_to_window_start=True)
            kept.append(seg)
        segments = kept
    tail = None
    if last_run_start is not None and last_run_start < cutoff:
        tail_start = max(last_run_start, start) if start is not None else last_run_start
        tail = {"start": iso(tail_start), "end": iso(cutoff), "state": "since_last_observed_run",
                "seconds": int((cutoff - tail_start).total_seconds())}
    return {"stream": stream, "source_scope": "sources of the requested stream" if stream else "all sources",
            "segments": segments, "tail": tail,
            "after_cutoff_hindsight": [{"run_id": s["run_id"], "start": s["start"], "end": s["end"], "state": s["state"]}
                                       for s in after_cutoff],
            "pending_availability_runs": [r for r in pending if any(s["run_id"] == r for s in segments)],
            "corrupt_runs": [p["run_id"] for p in archive.problems],
            "uncommitted_runs": [u["run_id"] for u in archive.uncommitted]}


def join(archive: Archive, prices: PriceFile, cutoff, mode: str, *, tolerance_s: float,
         instrument_id: str = "etf.qqq", series: str = "underlying_quote", stream: str | None = None,
         start=None, interval_s: int = config.INTERVAL_S) -> dict:
    """News revisions in [start, cutoff], each matched backward to a price available by its event time."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if tolerance_s is None or not (tolerance_s >= 0) or tolerance_s == float("inf"):
        raise ValueError("tolerance_s is required and must be a finite number >= 0")
    cutoff = parse_iso(cutoff) if isinstance(cutoff, str) else cutoff
    tolerance = timedelta(seconds=tolerance_s)
    px = PriceSeries(prices.records, instrument_id, series)
    events, excluded = news_events(archive, cutoff, mode, stream)
    if start is None:
        firsts = [parse_iso(r["started_at"]) for r in archive.runs] + [e["_event_at"] for e in events]
        start = min(firsts) if firsts else cutoff
        start = min(start, cutoff)
    start = parse_iso(start) if isinstance(start, str) else start
    if start > cutoff:
        raise ValueError(f"window start {iso(start)} is after the cutoff {iso(cutoff)}")
    rows, excluded["before_window"] = [], 0
    for e in events:
        event_at = e.pop("_event_at")
        if event_at < start:
            excluded["before_window"] += 1
            continue
        rows.append(dict(e, price=match_price(px, event_at, cutoff, tolerance)))
    return {
        "schema_version": JOIN_SCHEMA,
        "mode": mode,
        "hindsight": mode == "publication",
        "label": ("RETROSPECTIVE publication-time join: news chosen with everything known now (hindsight)"
                  if mode == "publication" else f"{mode}-as-of join: news and prices only as available by each event"),
        "cutoff": iso(cutoff),
        "window": {"start": iso(start), "end": iso(cutoff),
                   "applies_to": "rows (event_at), price_coverage and news_coverage"},
        "stream": stream,
        "price_series": {"instrument_id": instrument_id, "series": series, "tolerance_s": tolerance_s,
                         "path": prices.path, "sha256": prices.sha256,
                         "synthetic": bool(prices.header and prices.header.get("synthetic")),
                         "records_in_series": len(px.records),
                         # Only when no price input is connected (pilot): every price is then missing.
                         **({"input": "unavailable"} if prices.header and prices.header.get("price_input") == "unavailable"
                            else {})},
        "rows": rows,
        "excluded": excluded,
        "price_coverage": px.intervals(start, cutoff, tolerance) if start < cutoff else {"missing": [], "stale": []},
        "news_coverage": news_coverage(archive, cutoff, mode, stream, start, interval_s),
    }
