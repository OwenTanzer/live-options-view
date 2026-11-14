"""Sanitized, bounded news views (M4a): fixture projections of the M3 join.

Layout (relative to the output directory):

  news/v1/views/index.json
  news/v1/views/<mode>/<stream>/<YYYY-MM-DD>/page-NNNN.json

One view is one (mode, stream, UTC date). Its window is [date 00:00Z,
min(as_of, date 23:59:59.999999Z)] and is the M3 join window. Rows are the
join rows, sorted by event_at descending, then item_id and revision_number.

Public fields only. No excerpts, raw fetch errors or per-entry failure
details leave the builder; failures are ids, statuses and counts. Titles
and outlets are cleaned of control characters and capped. Link URLs are
checked here (urllib.parse) and again by the panel when it renders.

Limits are measured on the final serialized bytes of each file, metadata and
coverage included (see LIMITS). The mandatory envelope of every page and of
the index is measured first, with its counters at their widest. If even that
cannot fit, the build fails (BuildError) rather than exceed a cap or lose an
incompleteness notice. Coverage may use at most half of a page's bytes; rows
fill the rest and move to the next page when a page is full. At any limit the
file says complete: false, lists overflow_reasons and gives explicit omitted
counts. Nothing is dropped silently.

"Observed, no items". A segment is `quiet` only when all of these hold:
  - the segment is `covered` for the view's stream scope (every relevant
    source of the run's recorded roster was fetched without failure);
  - its whole span is a real gap between two observed runs (not the span
    before the first run, not a missed interval);
  - the span has no new observations and no row events: both counts are
    zero, taken on the full join result before pagination, source filtering
    or truncation. `observations_in_span` counts rows by observed_at in
    (start, end] (what that run newly received); `events_in_span` counts rows
    by event_at in (start, end] (when the row entered this mode's view, which
    a delayed archive write or classification can put after its receipt);
  - nothing in the view makes the zero untrustworthy: no corrupt or
    uncommitted runs, no revisions excluded as not yet classified for the
    stream, and not publication mode (hindsight).
Every other empty span is "no items on this page" with its coverage state.

A row's classification is `at_event` only when it was known by the row's
event_at. In publication mode (and whenever known_at is missing or later)
it is `retrospective` and the panel shows it as later context, with its
classified_at time.

Paging packs every row with the same byte and row limits, so a view cut at
pages_per_view reports the exact number of further pages its rows need.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .join import join
from .normalize import iso, parse_iso
from .prices import PriceFile
from .reader import MODES, Archive

VIEW_SCHEMA = "news-view/v1"
INDEX_SCHEMA = "news-view-index/v1"
ROOT = "news/v1/views"
STREAM_KEYS = ("all", *sorted(config.STREAMS))
TITLE_MAX = 300
OUTLET_MAX = 120
LATER_MAX = 20
URL_MAX = 2048
WIDE_INT = 10 ** 9          # placeholder for counters while measuring the envelope
QUIET_RULE = ("observed, no items only for a covered segment between two observed runs with no new "
              "observations and no row events in its span, both counted before pagination and filtering; "
              "otherwise no items on this page")


class BuildError(Exception):
    """A view or the index cannot be built within its limits."""


@dataclass(frozen=True)
class Limits:
    page_bytes: int = 256 * 1024
    rows_per_page: int = 50
    segments_per_page: int = 200
    price_intervals_per_page: int = 200
    hindsight_per_page: int = 50
    source_ids_per_segment: int = 100
    pages_per_view: int = 20
    index_bytes: int = 256 * 1024
    index_views: int = 500


LIMITS = Limits()


def encode(obj) -> bytes:
    """The one serialization used for both measuring and writing."""
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


# --- sanitization -------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_text(value, limit: int) -> tuple[str, bool]:
    """Visible text only: control and format characters removed, whitespace collapsed, capped."""
    if value is None:
        return "", False
    text = "".join(ch for ch in str(value) if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    text = " ".join(_CONTROL.sub(" ", text).split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…", True
    return text, False


def check_url(value) -> tuple[str | None, str | None]:
    """(url, None) for an acceptable http(s) link, else (None, reason)."""
    if value is None or value == "":
        return None, "missing"
    if not isinstance(value, str):
        return None, "not_a_string"
    if len(value) > URL_MAX:
        return None, "too_long"
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return None, "whitespace_or_control"
    if "\\" in value:
        return None, "backslash"
    try:
        parts = urlsplit(value)
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        return None, "unparseable"
    if parts.scheme.lower() not in ("http", "https"):
        return None, "scheme_not_http"
    if not parts.hostname:
        return None, "no_host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "userinfo"
    if not port_ok:
        return None, "bad_port"
    return value, None


# --- projection -----------------------------------------------------------------

def _capped(items, limit):
    return items[:limit], max(0, len(items) - limit)


def _cls_public(c: dict | None, mapping: dict, event_at: str) -> dict | None:
    if c is None:
        return None
    known = parse_iso(c.get("known_at")) if c.get("known_at") else None
    timing = "at_event" if known is not None and known <= parse_iso(event_at) else "retrospective"
    return {"classification_id": c["classification_id"], "classified_at": c["classified_at"],
            "known_at": c.get("known_at"), "timing": timing, "version": c["version"],
            "stream_memberships": list(c["stream_memberships"]),
            "instrument_mapping": mapping.get(c["classification_id"], "unknown")}


def _price_public(p: dict) -> dict:
    rec = p["record"]
    out = {"status": p["status"], "age_seconds": p["age_seconds"], "record": None}
    if rec is not None:
        out["record"] = {k: rec.get(k) for k in ("record_id", "record_kind", "instrument_id", "granularity",
                                                 "price", "price_basis", "session", "available_at", "synthetic",
                                                 "quote_ts", "bar_start", "bar_end") if k in rec}
    return out


def _time_quality(value) -> dict:
    """{published, updated} quality words only (e.g. explicit, inferred, missing)."""
    if not isinstance(value, dict):
        value = {"published": value}
    return {k: clean_text(value.get(k) or "unknown", 40)[0] for k in ("published", "updated")}


def project_row(row: dict, mapping: dict) -> dict:
    title, title_cut = clean_text(row["title"], TITLE_MAX)
    outlet, _ = clean_text(row["outlet"], OUTLET_MAX)
    url, rejected = check_url(row["canonical_url"])
    later = row["later_context"]
    revs = [{"revision_id": r["revision_id"], "known_at": r["known_at"], "title": clean_text(r["title"], TITLE_MAX)[0]}
            for r in later.get("revisions_after_event", [])]
    clss = [{"known_at": c["known_at"], "version": c["version"], "stream_memberships": list(c["stream_memberships"])}
            for c in later.get("classifications_after_event", [])]
    revs, revs_omitted = _capped(revs, LATER_MAX)
    clss, clss_omitted = _capped(clss, LATER_MAX)
    cls = _cls_public(row["classification_at_event"], mapping, row["event_at"])
    return {
        "item_id": row["item_id"], "revision_id": row["revision_id"], "revision_number": row["revision_number"],
        "revisions_known_at_event": row["revisions_known_at_event"],
        "title": title, "title_truncated": title_cut, "source_id": row["source_id"], "outlet": outlet,
        "canonical_url": url, "url_rejected": rejected,
        "published_at": row["published_at"], "time_quality": _time_quality(row["time_quality"]),
        "observed_at": row["observed_at"], "archived_at": row["archived_at"],
        "event_at": row["event_at"], "event_basis": row["event_basis"], "run_id": row["run_id"],
        "classification_at_event": cls,
        "instruments": {"mapping": cls["instrument_mapping"] if cls else "unknown", "linked": [],
                        "note": "unmapped: no instrument is linked to this item; the price is the page's series"},
        "later_context": {
            "revisions_known_by_cutoff": later.get("revisions_known_by_cutoff"),
            "revisions_after_event": revs, "revisions_after_event_omitted": revs_omitted,
            "classifications_after_event": clss, "classifications_after_event_omitted": clss_omitted,
            **({"stream_membership_removed_at": later["stream_membership_removed_at"]}
               if "stream_membership_removed_at" in later else {}),
        },
        "price": _price_public(row["price"]),
    }


def _segment_public(seg: dict, limits: Limits, counts: dict, quiet: bool) -> tuple[dict, int]:
    out = {"start": seg["start"], "end": seg["end"], "run_id": seg["run_id"], "state": seg["state"],
           "missed_interval": bool(seg.get("missed_interval")), "gap_seconds": seg.get("gap_seconds"),
           "clipped_to_window_start": bool(seg.get("clipped_to_window_start")),
           "before_first_run": "detail_before" in seg, **counts, "quiet": quiet,
           "sources": None}
    src = seg.get("sources")
    if src is None:
        return out, 0
    budget, omitted, lists = limits.source_ids_per_segment, 0, {}
    for key, values in (("attempted", src["attempted"]), ("ok", src["ok"]),
                        ("failed", [{"source_id": f["source_id"], "status": f["status"]} for f in src["failed"]]),
                        ("not_selected", src["not_selected"])):
        kept = values[:budget]
        budget -= len(kept)
        omitted += len(values) - len(kept)
        lists[key] = kept
    out["sources"] = {**lists, "disabled_count": src["disabled_count"],
                      "failed_entries": max(src["failed_entries"], len(src["entry_failures"])),
                      "roster_complete": src["roster_complete"], "source_ids_omitted": omitted}
    return out, omitted


def _quiet_blockers(result: dict) -> list[str]:
    cov, excluded = result["news_coverage"], result["excluded"]
    out = []
    if result["mode"] == "publication":
        out.append("publication_mode_hindsight")
    if cov["corrupt_runs"]:
        out.append("corrupt_runs")
    if cov["uncommitted_runs"]:
        out.append("uncommitted_runs")
    if excluded.get("unclassified_for_stream"):
        out.append("revisions_not_yet_classified_for_stream")
    return out


def _count_in_span(rows: list[dict], seg: dict, field: str) -> int:
    start, end = parse_iso(seg["start"]), parse_iso(seg["end"])
    return sum(1 for r in rows if (start is None or parse_iso(r[field]) > start) and parse_iso(r[field]) <= end)


def _coverage(result: dict, limits: Limits) -> tuple[dict, dict]:
    """Public coverage with count limits applied; returns (coverage, omitted counts)."""
    cov = result["news_coverage"]
    blockers = _quiet_blockers(result)
    segs = []
    for seg in cov["segments"]:
        counts = {"observations_in_span": _count_in_span(result["rows"], seg, "observed_at"),
                  "events_in_span": _count_in_span(result["rows"], seg, "event_at")}
        quiet = (not blockers and seg["state"] == "covered" and "detail_before" not in seg
                 and not seg.get("missed_interval") and not any(counts.values()))
        segs.append(_segment_public(seg, limits, counts, quiet))
    # Keep the most recent segments and intervals (rows are newest first too).
    kept = segs[-limits.segments_per_page:] if segs else []
    intervals = sorted([dict(i, kind=k) for k in ("missing", "stale") for i in result["price_coverage"][k]],
                       key=lambda i: i["start"])
    kept_iv = intervals[-limits.price_intervals_per_page:] if intervals else []
    hind = [{"end": h["end"], "state": h["state"]} for h in cov["after_cutoff_hindsight"]]
    kept_h = hind[: limits.hindsight_per_page]
    omitted = {"segments_omitted": len(segs) - len(kept), "intervals_omitted": len(intervals) - len(kept_iv),
               "hindsight_omitted": len(hind) - len(kept_h),
               "source_ids_omitted": sum(o for _, o in kept)}
    public = {
        "stream_scope": cov["source_scope"],
        "note": "coverage is for the view's stream and is not narrowed by the panel's source filter",
        "segments": [s for s, _ in kept],
        "tail": None if cov["tail"] is None else {k: cov["tail"][k] for k in ("start", "end", "seconds")},
        "after_cutoff_hindsight": kept_h,
        "pending_availability_runs": len(cov["pending_availability_runs"]),
        "corrupt_runs": len(cov["corrupt_runs"]),
        "uncommitted_runs": len(cov["uncommitted_runs"]),
        "quiet_rule": QUIET_RULE,
        "quiet_blocked_by": blockers,
        "price": {k: [{f: i[f] for f in ("start", "end", "seconds", "last_price_time") if f in i}
                      for i in kept_iv if i["kind"] == k] for k in ("missing", "stale")},
        "coverage_complete": True,
    }
    return public, omitted


def _trim_coverage_to_budget(cov: dict, omitted: dict, measure, budget: int) -> None:
    """Drop the oldest coverage entries until the coverage part fits its byte budget (counted, never silent)."""
    def size():
        return measure(cov)
    for key, counter in (("segments", "segments_omitted"), ("after_cutoff_hindsight", "hindsight_omitted")):
        while size() > budget and cov[key]:
            if key == "segments":
                dropped = cov[key].pop(0)
                omitted["source_ids_omitted"] -= (dropped["sources"] or {}).get("source_ids_omitted", 0)
            else:
                cov[key].pop()
            omitted[counter] += 1
    while size() > budget and (cov["price"]["missing"] or cov["price"]["stale"]):
        lists = [l for l in (cov["price"]["missing"], cov["price"]["stale"]) if l]
        min(lists, key=lambda l: l[0]["start"]).pop(0)
        omitted["intervals_omitted"] += 1


# --- views ----------------------------------------------------------------------

def page_key(mode: str, stream: str, date: str, n: int) -> str:
    """The builder's unqualified page key (in memory only). Publication uses generation_page_key (G6)."""
    return f"{ROOT}/{mode}/{stream}/{date}/page-{n:04d}.json"


# --- generations and incarnations (G3 generations; G6 fresh namespaces and settlement) -------------
#
# The builder emits an unqualified build (fixed news/v1 keys, in memory only; never published).
# qualify_generation turns it into the published form of one *incarnation* of that build:
#   gen          <build_as_of as YYYYMMDDTHHMMSSZ>-<first 16 hex of files_digest(unqualified files)>:
#                the logical generation, fixed before any key is qualified;
#   incarnation  <gen>.<n>, n = 1..9999: one page namespace of that generation. A namespace in which
#                a page delete has been started is never written again; the same build is then
#                published under n + 1 (publish.py's head record decides n);
#   pages        news/v2/views/p/<gen>.<n>/<mode>/<stream>/<date>/page-NNNN.json, each with
#                "gen": "<gen>.<n>" and prev_page/next_page rewritten to keys of the same incarnation;
#   index        the fixed news/v2/views/index.json (the only commit point): schema news-view-index/v2,
#                "state": "published", "gen": "<gen>.<n>", "logical_gen", views[].first_page rewritten,
#                "pages_sha256", and, added per write by commit_index, "commit": {"nonce": <32 hex>}.
#                Every index write carries a fresh nonce, so no two index bodies are ever identical.
#   tombstone    the same key and schema with "state": "withdrawn" (or "unpublished" before the first
#                publication), a commit nonce and no views: readers show "no news published".
# Legacy keys (news/v1/views/index.json, news/v1/views/g/...) are never read, written or deleted.

INDEX_KEY = f"{ROOT}/index.json"                       # the builder's unqualified index (in memory only)
# R1 (P1 research under generation protection): the sanitized research projection is one more page of
# the generation. Unqualified (in memory only) at RESEARCH_KEY; published at
# news/v2/views/p/<gen>.<n>/research/view.json and named only by the index's "research" entry, so it
# shares the head CAS, page-cap reservation, quarantine, retirement and index-only withdrawal of the
# news view pages. The private ledger (news/v1/research/**) is never part of a build.
RESEARCH_KEY = f"{ROOT}/research/view.json"
RESEARCH_VIEW_SCHEMA = "news-research-view/v1"
RESEARCH_STATES = ("published", "none", "omitted")
PUBLISHED_ROOT = "news/v2/views"
PUBLISHED_INDEX_KEY = f"{PUBLISHED_ROOT}/index.json"
PUBLISHED_INDEX_SCHEMA = "news-view-index/v2"
PAGES_ROOT = f"{PUBLISHED_ROOT}/p"
GEN_PATTERN = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}"
INCARNATION_MAX = 9999
INCARNATION_PATTERN = GEN_PATTERN + r"\.[1-9][0-9]{0,3}"
NONCE_PATTERN = r"[0-9a-f]{32}"
INDEX_STATES = ("published", "withdrawn", "unpublished")
_GEN_RE = re.compile(rf"^{GEN_PATTERN}$")
_INCARNATION_RE = re.compile(rf"^({GEN_PATTERN})\.([1-9][0-9]{{0,3}})$")
_NONCE_RE = re.compile(rf"^{NONCE_PATTERN}$")
_UNQUALIFIED_RE = re.compile(r"^news/v1/views/(collector|archive|publication)/(all|ai_buildout|oil)/"
                             r"([0-9]{4}-[0-9]{2}-[0-9]{2})/page-([0-9]{4})\.json$")
# Headroom the pilot build reserves so that qualified pages and the index stay within their caps:
# each link gains "p/<gen>.<n>/" (2 + 33 + 1 + up to 4 + 1 = 41 bytes) and pages/index gain a few fields
# (gen, logical_gen, state, commit). The G3 value (26) undercounted the 33-character gen.
QUALIFY_SEGMENT = len("p/") + 33 + len(".") + 4 + len("/")
QUALIFY_PAGE_RESERVE = 512
QUALIFY_INDEX_RESERVE = 512


def is_gen(value) -> bool:
    """The generation grammar, with a real UTC instant."""
    if not isinstance(value, str) or not _GEN_RE.match(value):
        return False
    try:
        return datetime.strptime(value[:16], "%Y%m%dT%H%M%SZ").strftime("%Y%m%dT%H%M%SZ") == value[:16]
    except ValueError:
        return False


def incarnation_id(gen: str, n: int) -> str:
    if not is_gen(gen) or not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= INCARNATION_MAX:
        raise BuildError(f"not a generation incarnation: {gen!r}.{n!r}")
    return f"{gen}.{n}"


def split_incarnation(value) -> tuple[str, int] | None:
    """(gen, n) of an incarnation id, or None."""
    if not isinstance(value, str):
        return None
    m = _INCARNATION_RE.match(value)
    if not m or not is_gen(m.group(1)):
        return None
    return m.group(1), int(m.group(2))


def is_incarnation(value) -> bool:
    return split_incarnation(value) is not None


def is_nonce(value) -> bool:
    return isinstance(value, str) and bool(_NONCE_RE.match(value))


def generation_page_key(inc: str, mode: str, stream: str, date: str, n: int) -> str:
    """A published page key: inc is the incarnation id <gen>.<n>."""
    return f"{PAGES_ROOT}/{inc}/{mode}/{stream}/{date}/page-{n:04d}.json"


def generation_research_key(inc: str) -> str:
    """The published research page of incarnation inc (R1)."""
    return f"{PAGES_ROOT}/{inc}/research/view.json"


def files_digest(files: dict[str, bytes]) -> str:
    """SHA-256 over every key and its bytes (length-prefixed, sorted by key)."""
    import hashlib

    h = hashlib.sha256()
    for key in sorted(files):
        body = files[key]
        h.update(f"{len(key)}:{key}:{len(body)}:".encode("utf-8"))
        h.update(body)
    return h.hexdigest()


def pages_digest(files: dict[str, bytes]) -> str:
    """Final integrity of a generation's pages: files_digest over every key except the index
    (the builder's or the published one)."""
    return files_digest({k: v for k, v in files.items() if k not in (INDEX_KEY, PUBLISHED_INDEX_KEY)})


def generation_name(files: dict[str, bytes]) -> str:
    """The logical generation id of an unqualified build (deterministic: same build, same gen)."""
    index = json.loads(files[INDEX_KEY])
    built = parse_iso(index["build_as_of"])
    return f"{built.strftime('%Y%m%dT%H%M%SZ')}-{files_digest(files)[:16]}"


def qualify_generation(files: dict[str, bytes], n: int = 1) -> tuple[str, dict[str, bytes]]:
    """(incarnation id, published files) for incarnation n of an unqualified build that passed validation.

    Pure and deterministic in (files, n). Every page key becomes generation_page_key(<gen>.<n>, ...);
    every link that names a page must name one of this build's pages (BuildError otherwise: no link
    may leave the incarnation). The index (at PUBLISHED_INDEX_KEY) has no commit nonce yet: each index
    write adds a fresh one with commit_index, so the bytes written are never the bytes returned here.
    """
    gen = generation_name(files)
    inc = incarnation_id(gen, n)
    mapping = {}
    for key in files:
        if key == INDEX_KEY:
            continue
        if key == RESEARCH_KEY:
            mapping[key] = generation_research_key(inc)
            continue
        m = _UNQUALIFIED_RE.match(key)
        if not m:
            raise BuildError(f"{key} is not an unqualified view page key")
        mapping[key] = generation_page_key(inc, m.group(1), m.group(2), m.group(3), int(m.group(4)))

    def relink(value, where):
        if value is None:
            return None
        if value not in mapping:
            raise BuildError(f"{where} links outside the generation: {value!r}")
        return mapping[value]

    out = {}
    for key, body in files.items():
        if key == INDEX_KEY:
            continue
        page = json.loads(body)
        page["gen"] = inc
        for field in ("prev_page", "next_page"):
            if field in page:
                page[field] = relink(page[field], f"{key} {field}")
        out[mapping[key]] = encode(page)
    index = json.loads(files[INDEX_KEY])
    index["views"] = [dict(v, first_page=relink(v["first_page"], "index first_page")) for v in index["views"]]
    if isinstance(index.get("research"), dict) and index["research"].get("page") is not None:
        index["research"] = dict(index["research"], page=relink(index["research"]["page"], "index research page"))
    index["schema_version"] = PUBLISHED_INDEX_SCHEMA
    index["state"] = "published"
    index["gen"] = inc
    index["logical_gen"] = gen
    index["pages_sha256"] = pages_digest(out)
    out[PUBLISHED_INDEX_KEY] = encode(index)
    return inc, out


def commit_index(index_bytes: bytes, nonce: str) -> bytes:
    """The index bytes of one write: the qualified index (or a live index being resealed) with this
    write's commit nonce. The nonce is outside the logical digest and the pages digest."""
    if not is_nonce(nonce):
        raise BuildError(f"not a commit nonce: {nonce!r}")
    index = json.loads(index_bytes)
    index["commit"] = {"nonce": nonce}
    return encode(index)


def tombstone_index(nonce: str, state: str = "withdrawn") -> bytes:
    """The index written by withdrawal ("withdrawn") or before the first publication ("unpublished").
    It names no generation and no page. The index key is never deleted, so this is what keeps an
    older create-only or conditional request from ever finding the key absent or unchanged."""
    if not is_nonce(nonce) or state not in ("withdrawn", "unpublished"):
        raise BuildError(f"not a tombstone: {state!r} {nonce!r}")
    return encode({"schema_version": PUBLISHED_INDEX_SCHEMA, "state": state, "commit": {"nonce": nonce}})


def commit_nonce_of(index_bytes: bytes) -> str | None:
    """The commit nonce in an index or tombstone body, or None if it has none (a foreign object)."""
    try:
        doc = json.loads(index_bytes)
    except ValueError:
        return None
    nonce = doc.get("commit", {}).get("nonce") if isinstance(doc, dict) and isinstance(doc.get("commit"), dict) else None
    return nonce if is_nonce(nonce) else None


def qualified_limits(limits: "Limits" = None) -> "Limits":
    """Limits for a build that will be qualified: room for the longer keys and the generation fields."""
    limits = limits or LIMITS
    return replace(limits, page_bytes=limits.page_bytes - QUALIFY_PAGE_RESERVE,
                   index_bytes=limits.index_bytes - QUALIFY_INDEX_RESERVE - QUALIFY_SEGMENT * limits.index_views)


def date_window(date: str, as_of: datetime) -> tuple[datetime, datetime] | None:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if start > as_of:
        return None
    return start, min(as_of, start + timedelta(days=1) - timedelta(microseconds=1))


def view_dates(archive: Archive, as_of: datetime) -> list[str]:
    times = [parse_iso(r["started_at"]) for r in archive.runs]
    for o in archive.observations:
        times += [parse_iso(t) for t in (o["observed_at"], o["published_at"]) if t]
    times = [t for t in times if t is not None and t <= as_of]
    if not times:
        return [as_of.strftime("%Y-%m-%d")]
    day, last, out = min(times).date(), as_of.date(), []
    while day <= last:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _instrument_mapping(archive: Archive) -> dict:
    return {c["classification_id"]: c.get("instrument_mapping", "unknown") for c in archive.classifications}


def build_view(archive: Archive, prices: PriceFile, *, mode: str, stream: str, date: str, as_of: datetime,
               tolerance_s: float, instrument_id: str, series: str, data_label: str,
               limits: Limits = LIMITS, mapping: dict | None = None) -> tuple[dict[str, bytes], dict]:
    """All pages of one view, plus its index entry."""
    window = date_window(date, as_of)
    if window is None:
        raise BuildError(f"date {date} is after as_of {iso(as_of)}")
    start, end = window
    result = join(archive, prices, end, mode, tolerance_s=tolerance_s, instrument_id=instrument_id,
                  series=series, stream=None if stream == "all" else stream, start=start)
    mapping = _instrument_mapping(archive) if mapping is None else mapping
    # event_at descending, then item_id and revision_number ascending (stable sorts, least significant first).
    rows = sorted(result["rows"], key=lambda r: r["revision_number"])
    rows = sorted(rows, key=lambda r: r["item_id"])
    rows = sorted(rows, key=lambda r: parse_iso(r["event_at"]), reverse=True)
    rows = [project_row(r, mapping) for r in rows]
    coverage, cov_omitted = _coverage(result, limits)
    synthetic = bool(result["price_series"]["synthetic"])

    def envelope(n: int, page_rows, cov, omitted, reasons, wide=False):
        w = (lambda v: WIDE_INT) if wide else (lambda v: v)
        last_key = page_key(mode, stream, date, limits.pages_per_view)
        return {
            "schema_version": VIEW_SCHEMA, "data_label": data_label, "synthetic_prices": synthetic,
            "mode": mode, "hindsight": result["hindsight"], "mode_label": result["label"],
            "as_of": result["cutoff"], "build_as_of": iso(as_of), "window": result["window"],
            "stream": stream, "date": date,
            "page_number": w(n), "page_count": w(None),
            "prev_page": last_key if wide else None, "next_page": last_key if wide else None,
            "rows_offset": w(None), "rows_on_page": len(page_rows),
            "view": {"rows_total": len(rows), "rows_omitted": w(None), "pages_omitted": w(None),
                     "complete": False if wide else None},
            "complete": False if wide else None,
            "overflow_reasons": sorted({*reasons, *ALL_REASONS}) if wide else sorted(reasons),
            "omitted": {k: w(v) for k, v in omitted.items()},
            "limits": asdict(limits),
            "price_series": {k: result["price_series"][k] for k in ("instrument_id", "series", "tolerance_s",
                                                                    "synthetic", "sha256", "input")
                             if k in result["price_series"]},
            "excluded": {k: w(v) for k, v in result["excluded"].items()},
            "coverage": cov,
            "rows": page_rows,
        }

    empty_cov = dict(coverage, segments=[], after_cutoff_hindsight=[], price={"missing": [], "stale": []},
                     tail=None if coverage["tail"] is None else {"start": coverage["tail"]["start"],
                                                                 "end": coverage["tail"]["end"],
                                                                 "seconds": WIDE_INT})
    floor = len(encode(envelope(1, [], empty_cov, {**cov_omitted, "rows_omitted": 0}, [], wide=True)))
    if floor > limits.page_bytes:
        raise BuildError(f"{mode}/{stream}/{date}: the mandatory page envelope ({floor} bytes) "
                         f"exceeds the page cap ({limits.page_bytes} bytes)")

    # Coverage may use at most half a page; measured with the widest envelope.
    budget = limits.page_bytes // 2
    reasons = set()
    if any(cov_omitted.values()):
        reasons.add("coverage_over_count_limits")

    def cov_bytes(cov):
        return len(encode(envelope(1, [], cov, {**cov_omitted, "rows_omitted": 0}, reasons, wide=True)))
    before = dict(cov_omitted)
    _trim_coverage_to_budget(coverage, cov_omitted, cov_bytes, max(budget, floor))
    if cov_omitted != before:
        reasons.add("coverage_over_byte_budget")
    if any(cov_omitted.values()):
        coverage["coverage_complete"] = False

    # Pack every row into pages (a full page moves the next row to the next page), then keep the
    # first pages_per_view. Packing past the cap uses the same limits, so pages_omitted is exact.
    packed, i, oversize = [], 0, 0
    while i < len(rows):
        page_rows = []
        while i < len(rows) and len(page_rows) < limits.rows_per_page:
            trial = envelope(len(packed) + 1, page_rows + [rows[i]], coverage,
                             {**cov_omitted, "rows_omitted": 0}, reasons, wide=True)
            if len(encode(trial)) <= limits.page_bytes:
                page_rows.append(rows[i])
                i += 1
            elif not page_rows:  # one row larger than an empty page can hold
                oversize += 1
                i += 1
            else:
                break
        if page_rows:
            packed.append((i - len(page_rows), page_rows))
    pages = packed[: limits.pages_per_view]
    pages_omitted = len(packed) - len(pages)
    rows_omitted = oversize + sum(len(r) for _, r in packed[limits.pages_per_view:])
    if oversize:
        reasons.add("row_exceeds_page_bytes")
    if pages_omitted:
        reasons.add("rows_beyond_max_pages")
    if not pages:
        pages = [(0, [])]
    complete = not reasons
    count = len(pages)
    files = {}
    for n, (offset, page_rows) in enumerate(pages, 1):
        page = envelope(n, page_rows, coverage, {**cov_omitted, "rows_omitted": rows_omitted}, reasons)
        page.update(page_count=count, rows_offset=offset,
                    prev_page=page_key(mode, stream, date, n - 1) if n > 1 else None,
                    next_page=page_key(mode, stream, date, n + 1) if n < count else None,
                    view={"rows_total": len(rows), "rows_omitted": rows_omitted, "pages_omitted": pages_omitted,
                          "complete": complete},
                    complete=complete)
        body = encode(page)
        if len(body) > limits.page_bytes:  # cannot happen: measured with wider placeholders
            raise BuildError(f"{page_key(mode, stream, date, n)}: {len(body)} bytes exceeds the page cap")
        files[page_key(mode, stream, date, n)] = body
    entry = {"mode": mode, "stream": stream, "date": date, "first_page": page_key(mode, stream, date, 1),
             "page_count": count, "rows_total": len(rows), "rows_omitted": rows_omitted,
             "pages_omitted": pages_omitted, "complete": complete,
             "coverage_complete": coverage["coverage_complete"], "hindsight": result["hindsight"]}
    return files, entry


INDEX_REASONS = ("index_bytes", "index_views")   # the only reasons an index is cut; each omits views
ALL_REASONS = ("coverage_over_count_limits", "coverage_over_byte_budget", "row_exceeds_page_bytes",
               "rows_beyond_max_pages")


def build_index(entries: list[dict], *, as_of: datetime, data_label: str, limits: Limits = LIMITS) -> bytes:
    """index.json with its own complete flag, overflow reasons and view counts."""
    return _build_index(entries, as_of=as_of, data_label=data_label, limits=limits)[0]


def _build_index(entries: list[dict], *, as_of: datetime, data_label: str,
                 limits: Limits = LIMITS, research: dict | None = None) -> tuple[bytes, list[int]]:
    """(index.json, positions in entries of the views it lists).

    A cut (index_views, then index_bytes) retains whole views newest date first; within a date it keeps
    the build order (mode, then stream), so the views dropped first are the oldest date's last ones.
    Retained views are listed in build order, so an uncut index is unchanged.

    research (R1): the index's "research" entry, or None for a build without one (the key is then
    absent, so builds without research are byte-identical to before). It is measured with its page
    key widened by the qualification segment, so the qualified index stays within its cap.
    """
    measured = research
    if research is not None and research.get("page"):
        measured = dict(research, page=research["page"] + "x" * QUALIFY_SEGMENT)

    def doc(views, omitted, reasons, wide=False):
        d = {"schema_version": INDEX_SCHEMA, "data_label": data_label, "build_as_of": iso(as_of),
             "root": ROOT, "modes": list(MODES), "streams": list(STREAM_KEYS),
             "limits": asdict(limits), "complete": False if wide else not reasons,
             "overflow_reasons": list(INDEX_REASONS) if wide else sorted(reasons),
             "views_total": WIDE_INT if wide else len(entries),
             "views_omitted": WIDE_INT if wide else omitted, "views": views}
        if research is not None:
            d["research"] = measured if wide else research
        return d
    floor = len(encode(doc([], 0, set(), wide=True)))
    if floor > limits.index_bytes:
        raise BuildError(f"the mandatory index envelope ({floor} bytes) exceeds the index cap "
                         f"({limits.index_bytes} bytes)")
    by_priority = sorted(range(len(entries)), key=lambda i: (_neg_date(entries[i]["date"]), i))
    reasons, retained = set(), by_priority
    if len(retained) > limits.index_views:
        retained, reasons = retained[: limits.index_views], {"index_views"}

    def listed():
        return [entries[i] for i in sorted(retained)]
    while retained and len(encode(doc(listed(), 0, reasons, wide=True))) > limits.index_bytes:
        retained = retained[:-1]
        reasons.add("index_bytes")
    body = encode(doc(listed(), len(entries) - len(retained), reasons))
    assert len(body) <= limits.index_bytes
    return body, sorted(retained)


def _neg_date(date: str) -> tuple[int, ...]:
    """Sort key putting the newest YYYY-MM-DD first."""
    return tuple(-int(part) for part in date.split("-"))


def research_page(projection: dict | None, *, as_of: datetime, limits: Limits = LIMITS,
                  runs: int = 0, problems: int = 0) -> tuple[dict, bytes | None]:
    """R1/R2: (index "research" entry, page bytes or None) for one build.

    projection is research.project(...) at this build's cutoff, or None when the ledger has no verified
    run. The view is FIXED: mode "research", cutoff == build_as_of (R2 pins both; a different mode or
    cutoff is refused here, not chosen). A page over the page cap is omitted with its reason instead
    of failing the news build. problems counts ledger runs that failed verification (never shown)."""
    built = iso(as_of)
    entry = {"schema_version": RESEARCH_VIEW_SCHEMA, "mode": "research", "cutoff": built, "fixed": True,
             "runs": runs, "unverified_runs": problems}
    if projection is None:
        return dict(entry, state="none", page=None, items=0), None
    if projection.get("schema_version") != RESEARCH_VIEW_SCHEMA or projection.get("mode") != "research" \
            or projection.get("cutoff") != built:
        raise BuildError(f"the research view must be mode 'research' at the build cutoff {built}, not "
                         f"{projection.get('mode')!r} at {projection.get('cutoff')!r}")
    page = dict(projection, build_as_of=built, page_kind="research",
                fixed_view="one view per publication: research mode at the build cutoff; no other mode or "
                           "cutoff is published")
    body = encode(page)
    # The qualified page gains "gen" (<= 40 bytes with its key); keep the same reserve as view pages.
    if len(body) > limits.page_bytes:
        return dict(entry, state="omitted", page=None, items=len(projection["items"]),
                    reason="research_view_bytes", bytes=len(body)), None
    return dict(entry, state="published", page=RESEARCH_KEY, items=len(projection["items"])), body


def build_views(archive: Archive, prices: PriceFile, *, as_of, tolerance_s: float, data_label: str,
                instrument_id: str = "etf.qqq", series: str = "underlying_quote", modes=MODES,
                streams=STREAM_KEYS, dates=None, limits: Limits = LIMITS,
                research: tuple[dict, bytes | None] | None = None) -> dict[str, bytes]:
    """index.json plus the pages of every view it lists, keyed by relative path. Pure: nothing is written.

    If the index is cut (index_views or index_bytes), the omitted views' pages are not emitted either:
    a generation never contains a page its index does not name. The index counts them in
    views_omitted (views_total is every view built) and says complete: false with the reason.

    research: research_page(...)'s (entry, page) for this build, or None (no "research" entry at all).
    """
    as_of = parse_iso(as_of) if isinstance(as_of, str) else as_of
    if not data_label:
        raise BuildError("data_label is required (for example 'SYNTHETIC fixture data')")
    mapping = _instrument_mapping(archive)
    view_files, entries = [], []
    for date in dates or view_dates(archive, as_of):
        for mode in modes:
            for stream in streams:
                f, entry = build_view(archive, prices, mode=mode, stream=stream, date=date, as_of=as_of,
                                      tolerance_s=tolerance_s, instrument_id=instrument_id, series=series,
                                      data_label=data_label, limits=limits, mapping=mapping)
                view_files.append(f)
                entries.append(entry)
    entry, body = research if research is not None else (None, None)
    index, kept = _build_index(entries, as_of=as_of, data_label=data_label, limits=limits, research=entry)
    files = {}
    for i in kept:
        files.update(view_files[i])
    if body is not None:
        files[RESEARCH_KEY] = body
    files[f"{ROOT}/index.json"] = index
    return files


def write_views(out_dir, files: dict[str, bytes]) -> list[str]:
    """Write into an empty (or absent) directory; refuses to mix with stale pages."""
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"{out} is not empty; build views into a new directory")
    for key, body in sorted(files.items()):
        path = out / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return sorted(files)


def limits_from(**overrides) -> Limits:
    return replace(LIMITS, **{k: v for k, v in overrides.items() if v is not None})
