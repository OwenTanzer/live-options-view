"""Publish sanitized news views (M5 local preparation; G3 generations; G6 settlement): private -> public.

Two stores, never the same one:

  private   the raw archive (news/v1/runs/, indexes, latest.json, writer lease)
            plus the publisher's own state under news/v1/publish/:
              head.json                   THE shared record, changed only by compare-and-swap: live commit,
                                          incarnations (pinned / retiring, their reserved pages and
                                          lifecycle events), prepared commits, high-water marks, withdrawing
              commits/<nonce>.json        one durable record per index write, written before it is sent
              state.json                  informational: clock high-water marks, last failure, blocked /
                                          paused notes, a summary of what is live (no decision reads it)
              heartbeat.json              rewritten every run; its store modification time is the store clock
              stop.json                   operator stop marker (cycles and publication no-op)
              lease.json                  single-publisher lease (avoids wasted work; safety is the head)
              snapshots/<gen>/...         the exact unqualified build of recent generations (restore)
              receipts/<time>-<gen>.json  one per publication / restore / reseal / withdrawal
  public    only the exact view keys the panel and proxy accept (fresh namespaces, G6):
              news/v2/views/index.json                                                the commit point
              news/v2/views/p/<gen>.<n>/<mode>/<stream>/<YYYY-MM-DD>/page-NNNN.json  immutable
            Legacy keys (news/v1/views/index.json, news/v1/views/g/...) are never read, written or deleted.

Settlement (the accepted G5 design and its lifecycle addendum):

  - Every index write is a compare-and-swap on the ETag of the index it replaces (the first one is
    create-only), sent once, with a fresh 128-bit nonce in its body, so no two index bodies are equal.
  - Durable intent comes first: the commit record, then the commit in the head, then the index write.
  - Outcomes are explicit. 412 is a known rejection. Anything else is ambiguous: the commit stays open
    in the head. It can land only while the index it was conditioned on is live; any later landed write
    ends that. No timer, no retry and no operator note settles it; observation does.
  - Landed evidence and cessation are separate. A generation ceases only when a later full observation
    finds a different committed incarnation, or a tombstone, live; the stamp is a clock reading taken
    after that observation, never a commit's preparation time.
  - A page namespace (incarnation <gen>.<n>) in which a delete has been started is never written again.
    Restore and republication reuse a still-pinned incarnation or take <gen>.<n+1>; without a snapshot
    the restore is refused.
  - The index key is never deleted. Withdrawal writes a tombstone over it and deletes every page at once;
    it is unconfirmed until a tombstone is read back live.
  - Admission, commit preparation, cleanup and withdrawal each change the head before touching the
    public store, so a pin and a retirement of the same incarnation cannot both win, and the page cap
    is reserved in the same compare-and-swap that pins.

Each run: read the clock and the head; observe the live index in full (index bytes, commit nonce, every
page hash) and record what that proves; clean up (mark orphan/expired incarnations retiring, delete
their pages, record empties); pin an incarnation within the cap; record the page-upload attempt in the
head; write its pages create-if-absent; record that every upload was answered; prepare the commit; send
the index; observe again. An incarnation with an upload attempt that was never answered is quarantined:
a page PUT may still land, so it keeps its head entry and full reservation and is never released (no
listing, elapsed time, process exit or tombstone counts as settlement; there is no in-code release).

PROVIDER / SDK ASSUMPTIONS (documented or assumed in archive.py; none verified by this module): atomic
conditional PutObject, 412 on a failed condition, one request per call on the no-retry client, a returned
ETag, distinct bodies never sharing an ETag, and the same behaviour on the private store.
Nothing here contacts a network or a bucket unless a caller passes such a store.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .archive import (LEASE_KEY, ArchiveError, LocalObjectStore, R2ObjectStore, WriterLease, check_key,
                      collect_once, encode_json)
from .normalize import iso, parse_iso, sha256_hex
from .prices import PriceFile
from .reader import MODES, load_archive
from .views import (INCARNATION_MAX, INCARNATION_PATTERN, INDEX_KEY as BUILDER_INDEX, INDEX_REASONS, LIMITS,
                    PAGES_ROOT, PUBLISHED_INDEX_KEY, PUBLISHED_INDEX_SCHEMA, PUBLISHED_ROOT, RESEARCH_KEY,
                    RESEARCH_STATES, RESEARCH_VIEW_SCHEMA, STREAM_KEYS, build_views, commit_index, commit_nonce_of,
                    files_digest, generation_name, generation_page_key, generation_research_key, is_gen,
                    is_incarnation, page_key, pages_digest, qualified_limits, qualify_generation, research_page,
                    split_incarnation, tombstone_index)

PUBLIC_PREFIX = f"{PUBLISHED_ROOT}/"                 # news/v2/views/ : the fresh public namespace (G6)
PUBLIC_INDEX = PUBLISHED_INDEX_KEY                  # news/v2/views/index.json : the only commit point
PAGES_PREFIX = f"{PAGES_ROOT}/"                     # news/v2/views/p/<gen>.<n>/...
BUILDER_INDEX_SCHEMA = "news-view-index/v1"         # the builder's unqualified index (in memory only)
PUBLISH_PREFIX = f"{config.NAMESPACE}/publish/"
STATE_KEY = PUBLISH_PREFIX + "state.json"
STOP_KEY = PUBLISH_PREFIX + "stop.json"
PUBLISH_LEASE_KEY = PUBLISH_PREFIX + "lease.json"
SNAPSHOT_PREFIX = PUBLISH_PREFIX + "snapshots/"
RECEIPT_PREFIX = PUBLISH_PREFIX + "receipts/"
HEAD_KEY = PUBLISH_PREFIX + "head.json"
COMMIT_PREFIX = PUBLISH_PREFIX + "commits/"
HEARTBEAT_KEY = PUBLISH_PREFIX + "heartbeat.json"
STATE_SCHEMA = "news-publish-state/v3"            # v3: informational only; the head is the record (G6)
RECEIPT_SCHEMA = "news-publish-receipt/v1"
SNAPSHOT_SCHEMA = "news-publish-snapshot/v2"      # v2: the unqualified build (re-qualified on restore)
HEAD_SCHEMA = "news-publish-head/v1"
COMMIT_SCHEMA = "news-publish-commit/v1"
PILOT_SCHEMA = "news-pilot-config/v1"
INDEX_CACHE_CONTROL = "no-store"
PAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
WITHDRAW_NOTE = ("tombstone confirmed live and origin pages deleted; copies already cached by browsers or "
                 "intermediaries, and downloads, cannot be recalled")
UNCONFIRMED_NOTE = ("origin pages deleted, but no tombstone is confirmed live: the public index may still be the old "
                    "index, or one from an earlier commit that lands late, naming pages that no longer exist. This "
                    "has no time bound. Run publish-withdraw again. Cached copies cannot be recalled")
UPLOADS_UNSETTLED_NOTE = ("a page upload sent before the withdrawal has no recorded answer and may still land: a "
                          "deleted page can reappear, named by no index. Its incarnation keeps its head entry and "
                          "page reservation; every later withdraw or cleanup run deletes what lands. This has no "
                          "time bound and nothing in code settles it")

# Builder output (unqualified) and published generation pages (qualified).
_PAGE_RE = re.compile(r"^news/v1/views/(collector|archive|publication)/(all|ai_buildout|oil)/"
                      r"([0-9]{4})-([0-9]{2})-([0-9]{2})/page-([0-9]{4})\.json$")
_INC_PAGE_RE = re.compile(rf"^news/v2/views/p/({INCARNATION_PATTERN})/(collector|archive|publication)/"
                          r"(all|ai_buildout|oil)/([0-9]{4})-([0-9]{2})-([0-9]{2})/page-([0-9]{4})\.json$")
# R1: the generation's one research page (news/v2/views/p/<gen>.<n>/research/view.json).
_INC_RESEARCH_RE = re.compile(rf"^news/v2/views/p/({INCARNATION_PATTERN})/research/view\.json$")


class PublishError(ArchiveError):
    pass


class PublishStopped(PublishError):
    """The operator stop marker is present; nothing was written."""


class PublishIntegrityError(PublishError):
    """A public object did not read back as written, or an immutable key holds different bytes."""


class PublishBlocked(PublishError):
    """History is uncertain (unmatched live index or unreadable ledger): nothing written or deleted."""


class PublishPaused(PublishError):
    """Cleanup ran, but a new generation was not admitted (the page cap, or a withdrawal in progress)."""


class PublishConflict(PublishError):
    """A compare-and-swap of the head or the index was rejected, or a head write could not be verified
    as the committed head. Nothing further was done for that intent; a later run re-reads and decides."""


def _real_date(y: str, m: str, d: str) -> bool:
    date = f"{y}-{m}-{d}"
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") == date
    except ValueError:
        return False


def is_builder_view_key(key: str) -> bool:
    """The builder's unqualified keys (index, or mode/stream/real date/page 0001..pages_per_view)."""
    if key in (BUILDER_INDEX, RESEARCH_KEY):
        return True
    m = _PAGE_RE.match(key)
    return bool(m) and 1 <= int(m.group(6)) <= LIMITS.pages_per_view and _real_date(m.group(3), m.group(4), m.group(5))


def incarnation_of(key: str) -> str | None:
    """The incarnation (<gen>.<n>) of a published page key (a view page or the research page), or None."""
    r = _INC_RESEARCH_RE.match(key)
    if r:
        return r.group(1) if is_incarnation(r.group(1)) else None
    m = _INC_PAGE_RE.match(key)
    if not m or not is_incarnation(m.group(1)):
        return None
    if not 1 <= int(m.group(7)) <= LIMITS.pages_per_view or not _real_date(m.group(4), m.group(5), m.group(6)):
        return None
    return m.group(1)


def is_public_view_key(key: str) -> bool:
    """The same exact rule as the panel (isNewsViewKey), the proxy (isPublicNewsViewKey) and the gateway."""
    return key == PUBLIC_INDEX or incarnation_of(key) is not None


# --- the two stores ---------------------------------------------------------------

class PublicViewStore:
    """The public side: refuses any key that is not an exact view key, and offers only the writes the
    settlement design allows. Pages: create-if-absent and delete. The index: compare-and-swap only;
    there is no unconditional index put and no index delete on this store."""

    def __init__(self, inner):
        self.inner = inner

    def _check(self, key):
        if not is_public_view_key(key):
            raise PublishError(f"refusing non-view key on the public store: {key!r}")
        return key

    def _page(self, key):
        if self._check(key) == PUBLIC_INDEX:
            raise PublishError("the index is written only by compare-and-swap and is never deleted")
        return key

    def put_if_absent(self, key, data, content_type="application/json", cache_control=None) -> bool:
        return self.inner.put_if_absent(self._page(key), data, content_type=content_type, cache_control=cache_control)

    def get(self, key):
        return self.inner.get(self._check(key))

    def get_versioned(self, key=PUBLIC_INDEX):
        """(bytes, ETag) of the index, or None. Only the index is read with its version."""
        if key != PUBLIC_INDEX:
            raise PublishError(f"only the index is read with its ETag, not {key!r}")
        return self.inner.get_versioned(key)

    def put_if_match(self, key, data, etag, content_type="application/json", cache_control=None, write_id=None):
        """The one way the index is written: If-Match on the expected ETag (None: create-only)."""
        if key != PUBLIC_INDEX:
            raise PublishError(f"only the index is written by compare-and-swap, not {key!r}")
        return self.inner.put_if_match(key, data, etag, content_type=content_type, cache_control=cache_control,
                                       write_id=write_id)

    def delete(self, key):
        self.inner.delete(self._page(key))

    def list_views(self) -> list[str]:
        """Every view key present (other keys under the prefix are reported, never touched)."""
        return [k for k in self.inner.list(PUBLIC_PREFIX) if is_public_view_key(k)]

    def pages_by_incarnation(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in self.inner.list(PAGES_PREFIX):
            inc = incarnation_of(key)
            if inc is not None:
                out.setdefault(inc, []).append(key)
        return {g: sorted(v) for g, v in sorted(out.items())}

    def foreign_keys(self) -> list[str]:
        return [k for k in self.inner.list(PUBLIC_PREFIX) if not is_public_view_key(k)]


def store_identity(store) -> tuple:
    if isinstance(store, PublicViewStore):
        store = store.inner
    if isinstance(store, LocalObjectStore):
        return ("local", str(Path(store.root).resolve()))
    if isinstance(store, R2ObjectStore):
        return ("r2", store.bucket)
    return ("object", id(store))


def assert_separate(private, public) -> None:
    """The raw archive and the public views must not share a store (or nest inside each other)."""
    a, b = store_identity(private), store_identity(public)
    if a[0] != b[0]:
        return
    if a == b:
        raise PublishError(f"private and public stores are the same {a[0]} store ({a[1]})")
    if a[0] == "local":
        pa, pb = Path(a[1]), Path(b[1])
        if pa in pb.parents or pb in pa.parents:
            raise PublishError(f"private and public stores nest inside each other ({pa}, {pb})")


# --- prices -----------------------------------------------------------------------

def unavailable_prices() -> PriceFile:
    """No price input connected: every row's price is missing and pages say input=unavailable."""
    return PriceFile(path="unavailable:no-price-input", sha256=sha256_hex(b""),
                     header={"record_kind": "fixture_header", "price_input": "unavailable", "synthetic": False},
                     records=[])


# --- state ------------------------------------------------------------------------

def _read_json(store, key):
    raw = store.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PublishError(f"{key} is unreadable: {exc}") from exc


def read_state(private) -> dict:
    state = _read_json(private, STATE_KEY)
    if state is None:
        return {"schema_version": STATE_SCHEMA, "committed": None, "in_progress": None, "last_failure": None,
                "withdrawn": None, "blocked": None, "paused": None, "clock": None}
    if state.get("schema_version") != STATE_SCHEMA:
        raise PublishError(f"{STATE_KEY} has schema {state.get('schema_version')!r}")
    return state


def _write_state(private, state) -> None:
    private.put(STATE_KEY, encode_json(state), "application/json")


def stopped(private) -> dict | None:
    return _read_json(private, STOP_KEY)


def stop(private, clock, reason: str) -> dict:
    marker = {"stopped_at": iso(clock()), "reason": reason}
    private.put(STOP_KEY, encode_json(marker), "application/json")
    return marker


def resume(private) -> bool:
    was = private.get(STOP_KEY) is not None
    private.delete(STOP_KEY)
    return was


# --- validation -------------------------------------------------------------------

_PAGE_KEYS = ("data_label", "mode", "as_of", "window", "stream", "date", "page_number", "page_count", "view",
              "complete", "overflow_reasons", "omitted", "coverage", "rows")


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def page_render_problems(page) -> list[str]:
    """The structure the panel's validatePage requires before it renders a page (kept in step by a test)."""
    if not isinstance(page, dict):
        return ["not an object"]
    problems = []
    if page.get("schema_version") != "news-view/v1":
        problems.append("schema_version is not news-view/v1")
    problems += [f"missing {k}" for k in _PAGE_KEYS if k not in page]
    if problems:
        return problems
    for k in ("data_label", "mode", "as_of", "stream", "date"):
        if not isinstance(page[k], str):
            problems.append(f"{k} is not text")
    w = page["window"]
    if not (isinstance(w, dict) and isinstance(w.get("start"), str) and isinstance(w.get("end"), str)):
        problems.append("window is not {start, end}")
    if not (_int(page["page_number"]) and _int(page["page_count"])):
        problems.append("page_number/page_count are not integers")
    if not (isinstance(page["view"], dict) and _int(page["view"].get("rows_total"))):
        problems.append("view is not an object with rows_total")
    if not isinstance(page["complete"], bool):
        problems.append("complete is not true/false")
    if not isinstance(page["overflow_reasons"], list):
        problems.append("overflow_reasons is not a list")
    if not isinstance(page["omitted"], dict):
        problems.append("omitted is not an object")
    cov = page["coverage"]
    if not isinstance(cov, dict):
        problems.append("coverage is not an object")
    else:
        if not (isinstance(cov.get("segments"), list) and all(isinstance(s, dict) for s in cov["segments"])):
            problems.append("coverage.segments is not a list of objects")
        if not isinstance(cov.get("after_cutoff_hindsight"), list):
            problems.append("coverage.after_cutoff_hindsight is not a list")
        if not isinstance(cov.get("price"), dict):
            problems.append("coverage.price is not an object")
    rows = page["rows"]
    if not (isinstance(rows, list) and all(isinstance(r, dict) for r in rows)):
        problems.append("rows is not a list of objects")
    elif rows and not _int(page.get("rows_offset")):
        problems.append("rows_offset is not an integer")
    return problems


# The complete shape the panel's renderer dereferences (M5.2). validatePage is shallow: a page can pass
# it and still make renderPage throw (rows[0].instruments = null, segments[0].sources = {},
# coverage.price.missing = {}). These specs cover every field renderPage, viewPicker, indexBanner and
# mount read, so a generation that passes renders without an exception or an undefined/NaN/null
# placeholder; a sweep in test_news_publish_contract runs the real renderer to keep them in step.
# Spec forms: "str" | "int" (non-negative) | "num" | "bool", "?" suffix = may be null; [spec] = list of
# spec; {name: spec} = object with those fields ("?name" = may be absent); ("?", {...}) = object or
# null; ("map", spec) = object whose values all match spec.
_INTERVAL = {"start": "str", "end": "str", "seconds": "num", "?last_price_time": "str?"}
_SOURCES = {"attempted": ["str"], "ok": ["str"], "failed": [{"source_id": "str", "status": "str"}],
            "not_selected": ["str"], "failed_entries": "int", "disabled_count": "int",
            "roster_complete": "bool", "source_ids_omitted": "int"}
_SEGMENT = {"start": "str?", "end": "str?", "state": "str", "quiet": "bool", "?before_first_run": "bool",
            "?clipped_to_window_start": "bool", "?missed_interval": "bool", "?gap_seconds": "num?",
            "observations_in_span": "int", "events_in_span": "int", "?sources": ("?", _SOURCES)}
_ROW = {
    "revision_id": "str", "source_id": "str", "title": "str", "title_truncated": "bool", "outlet": "str",
    "canonical_url": "str?", "url_rejected": "str?", "event_at": "str", "event_basis": "str",
    "published_at": "str?", "observed_at": "str", "archived_at": "str?",
    "time_quality": {"published": "str", "updated": "str"},
    "revision_number": "int", "revisions_known_at_event": "int?",
    "classification_at_event": ("?", {"timing": "str", "classified_at": "str", "stream_memberships": ["str"]}),
    "instruments": {"mapping": "str"},
    "later_context": {"revisions_after_event": [{"known_at": "str", "title": "str"}],
                      "revisions_after_event_omitted": "int",
                      "classifications_after_event": [{"known_at": "str", "stream_memberships": ["str"]}],
                      "classifications_after_event_omitted": "int", "?stream_membership_removed_at": "str?"},
    "price": {"status": "str", "age_seconds": "num?",
              "record": ("?", {"record_id": "str", "price": "num", "?synthetic": "bool"})},
}
PAGE_SPEC = {
    "data_label": "str", "mode_label": "str", "mode": "str", "as_of": "str", "stream": "str", "date": "str",
    "?hindsight": "bool", "window": {"start": "str", "end": "str"},
    "?price_series": ("?", {"instrument_id": "str", "series": "str", "tolerance_s": "num", "?synthetic": "bool"}),
    "?prev_page": "str?", "?next_page": "str?", "page_number": "int", "page_count": "int", "?rows_offset": "int",
    "?gen": "str",
    "view": {"rows_total": "int", "?rows_omitted": "int", "?pages_omitted": "int"},
    "complete": "bool", "overflow_reasons": ["str"], "omitted": ("map", "int"),
    "coverage": {"coverage_complete": "bool", "stream_scope": "str", "note": "str", "?quiet_blocked_by": ["str"],
                 "segments": [_SEGMENT], "?tail": ("?", {"start": "str", "end": "str", "seconds": "num"}),
                 "after_cutoff_hindsight": [{"end": "str", "state": "str"}],
                 "?pending_availability_runs": "int", "?corrupt_runs": "int", "?uncommitted_runs": "int",
                 "price": {"?missing": [_INTERVAL], "?stale": [_INTERVAL]}},
    "rows": [_ROW],
}
INDEX_SPEC = {
    "data_label": "str", "build_as_of": "str", "complete": "bool", "overflow_reasons": ["str"],
    "views_total": "int", "views_omitted": "int", "?gen": "str", "?pages_sha256": "str",
    "views": [{"mode": "str", "stream": "str", "date": "str", "first_page": "str", "page_count": "int",
               "rows_total": "int", "complete": "bool"}],
}
_SCALARS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: _int(v) and v >= 0,
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"),
    "bool": lambda v: isinstance(v, bool),
}


def _shape(value, spec, path: str, out: list) -> None:
    if len(out) >= 20:
        return
    if isinstance(spec, str):
        if value is None and spec.endswith("?"):
            return
        kind = spec.rstrip("?")
        if not _SCALARS[kind](value):
            out.append(f"{path} is not {'a non-negative integer' if kind == 'int' else kind}"
                       f"{' or null' if spec.endswith('?') else ''}")
    elif isinstance(spec, list):
        if not isinstance(value, list):
            out.append(f"{path} is not a list")
            return
        for i, item in enumerate(value):
            _shape(item, spec[0], f"{path}[{i}]", out)
    elif isinstance(spec, tuple) and spec[0] == "map":
        if not isinstance(value, dict):
            out.append(f"{path} is not an object")
            return
        for k, item in value.items():
            _shape(item, spec[1], f"{path}.{k}", out)
    elif isinstance(spec, tuple):
        if value is not None:
            _shape(value, spec[1], path, out)
    else:
        if not isinstance(value, dict):
            out.append(f"{path} is not an object")
            return
        for name, sub in spec.items():
            optional = name.startswith("?")
            name = name.lstrip("?")
            if name not in value:
                if not optional:
                    out.append(f"{path}.{name} is missing")
                continue
            _shape(value[name], sub, f"{path}.{name}", out)


def page_contract_problems(page) -> list[str]:
    """Everything the unchanged renderer needs: validatePage's checks, then every nested shape it reads."""
    problems = page_render_problems(page)
    if problems:
        return problems
    out: list[str] = []
    _shape(page, PAGE_SPEC, "page", out)
    return out


def index_contract_problems(index, schema: str = BUILDER_INDEX_SCHEMA) -> list[str]:
    """Every index field the panel and the publisher read, with its counts consistent."""
    if not isinstance(index, dict) or index.get("schema_version") != schema:
        return [f"index.json is not a {schema} document"]
    out: list[str] = []
    _shape(index, INDEX_SPEC, "index", out)
    if out:
        return out
    try:
        parse_iso(index["build_as_of"])
    except (TypeError, ValueError):
        out.append("index.build_as_of is not an ISO-8601 UTC time")
    if index["views_total"] != len(index["views"]) + index["views_omitted"]:
        out.append(f"index.views_total {index['views_total']} != {len(index['views'])} listed + "
                   f"{index['views_omitted']} omitted")
    if index["complete"] != (not index["overflow_reasons"] and index["views_omitted"] == 0):
        out.append("index.complete disagrees with its overflow_reasons/views_omitted")
    reasons = index["overflow_reasons"]
    if reasons != sorted(set(reasons)) or not set(reasons) <= set(INDEX_REASONS):
        out.append(f"index.overflow_reasons {reasons} must be sorted, distinct and from {list(INDEX_REASONS)}")
    if bool(reasons) != (index["views_omitted"] > 0):
        out.append("index.overflow_reasons must name why views are omitted, and only when they are")
    if not index["views"] and index["views_total"] > 0:
        # Cap-induced emptiness: views were built but none fit. Publishing it would replace the last
        # good generation with nothing to show. (views_total 0, no views built at all, stays valid.)
        out.append(f"index lists no views although {index['views_total']} were built (all cut by "
                   f"{', '.join(reasons) or 'its caps'}); refused so the previous publication stays live")
    return out


def _page_key(mode, stream, date, n):
    """The builder's unqualified page key (validate_generation checks builder output)."""
    return page_key(mode, stream, date, n)


_RESEARCH_ENTRY_KEYS = {"schema_version", "mode", "cutoff", "fixed", "runs", "unverified_runs", "state", "page",
                        "items", "reason", "bytes"}


def _check_research(files: dict[str, bytes], index: dict, research_key: str, gen: str | None) -> set[str]:
    """R1/R2: the index's optional "research" entry and the one page it names.

    The view is fixed: mode "research" at cutoff == build_as_of. state "published" names exactly the
    generation's research key, whose page is a news-research-view/v1 projection agreeing with the entry
    (and carrying gen when qualified); "none" and "omitted" name no page, and the build must hold none.
    Returns the set of keys it named (the research page, or nothing)."""
    entry = index.get("research")
    if entry is None:
        if research_key in files:
            raise PublishError("the generation has a research page but its index has no research entry")
        return set()
    if not isinstance(entry, dict) or set(entry) - _RESEARCH_ENTRY_KEYS:
        raise PublishError(f"index.research is not a research entry: {sorted(entry) if isinstance(entry, dict) else entry!r}")
    want = {"schema_version": RESEARCH_VIEW_SCHEMA, "mode": "research", "cutoff": index.get("build_as_of"), "fixed": True}
    wrong = {k: entry.get(k) for k, v in want.items() if entry.get(k) != v}
    if wrong or entry.get("state") not in RESEARCH_STATES:
        raise PublishError(f"index.research must be the fixed research view at the build cutoff: {wrong or entry.get('state')}")
    for name in ("runs", "unverified_runs", "items"):
        if not _int(entry.get(name)) or entry[name] < 0:
            raise PublishError(f"index.research.{name} is not a count")
    if entry["state"] != "published":
        if entry.get("page") is not None or research_key in files:
            raise PublishError(f"index.research is {entry['state']!r} but a research page is present or named")
        if entry["state"] == "omitted" and entry.get("reason") != "research_view_bytes":
            raise PublishError("an omitted research view must give its reason")
        return set()
    if entry.get("page") != research_key:
        raise PublishError(f"index.research.page {entry.get('page')!r} is not this generation's research page {research_key}")
    if research_key not in files:
        raise PublishError(f"index names a research page the generation lacks: {research_key}")
    try:
        page = json.loads(files[research_key])
    except ValueError as exc:
        raise PublishError(f"{research_key} is not JSON: {exc}") from exc
    expect = {"schema_version": RESEARCH_VIEW_SCHEMA, "page_kind": "research", "mode": "research",
              "cutoff": index.get("build_as_of"), "build_as_of": index.get("build_as_of"), "hindsight": False}
    if gen is not None:
        expect["gen"] = gen
    wrong = {k: page.get(k) for k, v in expect.items() if not isinstance(page, dict) or page.get(k) != v}
    if wrong:
        raise PublishError(f"{research_key} disagrees with its index entry: {wrong}")
    if not isinstance(page.get("items"), list) or len(page["items"]) != entry["items"]:
        raise PublishError(f"{research_key}: items differ from its index entry")
    return {research_key}


def _check_views(files: dict[str, bytes], index: dict, key_of, limits, gen: str | None, index_key: str,
                 research_key_of: str = RESEARCH_KEY) -> None:
    """Every page the index names: present, renderable, agreeing with its key, entry and page chain."""
    named, seen_views = set(), set()
    for v in index["views"]:
        ident = (v["mode"], v["stream"], v["date"])
        if ident in seen_views:
            raise PublishError(f"index lists {'/'.join(ident)} twice")
        seen_views.add(ident)
        count = v.get("page_count")
        if not _int(count) or not 1 <= count <= limits.pages_per_view:
            raise PublishError(f"{'/'.join(ident)}: page_count {count!r} is not 1..{limits.pages_per_view}")
        if v.get("first_page") != key_of(*ident, 1):
            raise PublishError(f"{'/'.join(ident)}: first_page {v.get('first_page')!r} is not page-0001 of its view")
        for n in range(1, count + 1):
            key = key_of(*ident, n)
            named.add(key)
            if key not in files:
                raise PublishError(f"index names a page the generation lacks: {key}")
            try:
                page = json.loads(files[key])
            except ValueError as exc:
                raise PublishError(f"{key} is not JSON: {exc}") from exc
            problems = page_contract_problems(page)
            if problems:
                raise PublishError(f"{key} would not render: {'; '.join(problems[:5])}")
            expect = {"mode": ident[0], "stream": ident[1], "date": ident[2], "page_number": n,
                      "page_count": count, "build_as_of": index["build_as_of"],
                      "prev_page": key_of(*ident, n - 1) if n > 1 else None,
                      "next_page": key_of(*ident, n + 1) if n < count else None}
            if gen is not None:
                expect["gen"] = gen
            wrong = {k: page.get(k) for k, want in expect.items() if page.get(k) != want}
            if wrong:
                raise PublishError(f"{key} disagrees with its key, index entry or page chain: {wrong}")
            if page["view"].get("rows_total") != v.get("rows_total") or page["complete"] != v.get("complete"):
                raise PublishError(f"{key}: rows_total/complete differ from its index entry")
    named |= _check_research(files, index, research_key_of, gen)
    orphans = sorted(set(files) - named - {index_key})
    if orphans:
        raise PublishError(f"pages the index does not name: {orphans[:5]}")


def _load_index(files: dict[str, bytes], index_key: str, schema: str) -> dict:
    try:
        index = json.loads(files[index_key])
    except ValueError as exc:
        raise PublishError(f"index.json is not JSON: {exc}") from exc
    problems = index_contract_problems(index, schema)
    if problems:
        raise PublishError(f"index.json would not render or publish: {'; '.join(problems[:5])}")
    return index


def _check_caps(files: dict[str, bytes], limits, index_key: str) -> None:
    for key, body in files.items():
        cap = limits.index_bytes if key == index_key else limits.page_bytes
        if len(body) > cap:
            raise PublishError(f"{key} is {len(body)} bytes, over its {cap}-byte cap")


def validate_generation(files: dict[str, bytes], limits=LIMITS) -> dict:
    """Builder output (unqualified keys), checked before it is qualified and before anything is written.

    Exact keys and byte caps; an index with every field the panel and publisher read
    (index_contract_problems); every page the index names present, renderable by the unchanged
    renderer (page_contract_problems) and agreeing with its key and its index entry; first/previous/
    next links forming exactly the chain page-0001 .. page-<page_count>; no page the index does not name.
    """
    if BUILDER_INDEX not in files:
        raise PublishError("generation has no index.json")
    bad = sorted(k for k in files if not is_builder_view_key(k))
    if bad:
        raise PublishError(f"generation has non-view keys: {bad[:5]}")
    _check_caps(files, limits, BUILDER_INDEX)
    index = _load_index(files, BUILDER_INDEX, BUILDER_INDEX_SCHEMA)
    _check_views(files, index, _page_key, limits, None, BUILDER_INDEX)
    return index


def validate_qualified(inc: str, files: dict[str, bytes], limits=LIMITS) -> dict:
    """A published-form incarnation (views.qualify_generation, before its commit nonce is added): every
    page key under p/<inc>/, every page and link of that incarnation, a news-view-index/v2 index with
    state "published", gen == inc, logical_gen == its generation and pages_sha256 == the pages' digest,
    and all of validate_generation's checks."""
    parts = split_incarnation(inc)
    if parts is None:
        raise PublishError(f"{inc!r} is not a generation incarnation")
    if PUBLIC_INDEX not in files:
        raise PublishError("generation has no index.json")
    bad = sorted(k for k in files if k != PUBLIC_INDEX and incarnation_of(k) != inc)
    if bad:
        raise PublishError(f"incarnation {inc} has keys outside {PAGES_PREFIX}{inc}/: {bad[:5]}")
    _check_caps(files, limits, PUBLIC_INDEX)
    index = _load_index(files, PUBLIC_INDEX, PUBLISHED_INDEX_SCHEMA)
    if index.get("state") != "published":
        raise PublishError(f"index.state {index.get('state')!r} is not 'published'")
    if index.get("gen") != inc or index.get("logical_gen") != parts[0]:
        raise PublishError(f"index.gen {index.get('gen')!r} / logical_gen {index.get('logical_gen')!r} is not {inc}")
    if index.get("pages_sha256") != pages_digest(files):
        raise PublishError("index.pages_sha256 does not match the incarnation's pages")
    _check_views(files, index, lambda m, s, d, n: generation_page_key(inc, m, s, d, n), limits, inc, PUBLIC_INDEX,
                 generation_research_key(inc))
    return index


# Final integrity over every key and byte (snapshots, ledger). The same digest as views.files_digest.
generation_digest = files_digest


def generation_id(files: dict[str, bytes], index: dict | None = None) -> str:
    """The logical generation id of an unqualified build (views.generation_name)."""
    return generation_name(files)


def page_write_order(files: dict[str, bytes]) -> list[str]:
    """Pages only, in key order. Generation pages are immutable and unreachable until the index names
    them, so the order no longer matters to readers; it is fixed only to keep runs reproducible."""
    return sorted(k for k in files if k not in (PUBLIC_INDEX, BUILDER_INDEX))


# --- snapshots, receipts, failures --------------------------------------------------

def _snapshot(private, gen_id: str, files: dict[str, bytes], clock) -> None:
    """Keep the exact *unqualified* build of a generation (G6). Restore re-qualifies it under whichever
    incarnation is safe to write, so a snapshot must not be tied to one page namespace."""
    prefix = f"{SNAPSHOT_PREFIX}{gen_id}/"
    manifest_key = prefix + "snapshot.json"
    wanted = {k: {"bytes": len(b), "sha256": sha256_hex(b)} for k, b in sorted(files.items())}
    existing = _read_json(private, manifest_key)
    if existing is not None:
        # The id binds every key and byte, so a match is the same generation; anything else is refused.
        if (existing.get("schema_version") != SNAPSHOT_SCHEMA or existing.get("files") != wanted
                or existing.get("generation_sha256") != generation_digest(files)):
            raise PublishError(f"snapshot {gen_id} exists with different content; refusing to reuse it")
        return
    for key, body in sorted(files.items()):
        private.put(prefix + key, body, "application/json")
    private.put(manifest_key, encode_json({
        "schema_version": SNAPSHOT_SCHEMA, "gen_id": gen_id, "created_at": iso(clock()),
        "generation_sha256": generation_digest(files), "files": wanted}), "application/json")


def snapshot_ids(private) -> list[str]:
    ids = set()
    for key in private.list(SNAPSHOT_PREFIX):
        parts = key[len(SNAPSHOT_PREFIX):].split("/")
        if len(parts) == 2 and parts[1] == "snapshot.json":
            ids.add(parts[0])
    return sorted(ids)


def load_snapshot(private, gen_id: str) -> dict[str, bytes]:
    """The unqualified build of gen_id, verified against its manifest and against the id itself."""
    check_key(f"{SNAPSHOT_PREFIX}{gen_id}/snapshot.json")
    meta = _read_json(private, f"{SNAPSHOT_PREFIX}{gen_id}/snapshot.json")
    if meta is None:
        raise PublishError(f"no snapshot {gen_id}; have {snapshot_ids(private)}")
    if meta.get("schema_version") != SNAPSHOT_SCHEMA:
        raise PublishError(f"snapshot {gen_id} has schema {meta.get('schema_version')!r}: it holds one page namespace's "
                           "bytes, not the build, so it cannot be restored into a fresh incarnation")
    files = {}
    for key, info in meta["files"].items():
        body = private.get(f"{SNAPSHOT_PREFIX}{gen_id}/{key}")
        if body is None or len(body) != info["bytes"] or sha256_hex(body) != info["sha256"]:
            raise PublishError(f"snapshot {gen_id}: {key} is missing or altered")
        files[key] = body
    try:
        name = generation_name(files)
    except (KeyError, ValueError, TypeError, AttributeError):
        name = None
    if meta.get("generation_sha256") != generation_digest(files) or name != gen_id:
        raise PublishError(f"snapshot {gen_id}: its files do not match the generation id")
    return files


def _prune_snapshots(private, keep: int, protect: set[str]) -> list[str]:
    ids = snapshot_ids(private)
    drop = [g for g in ids[: max(0, len(ids) - keep)] if g not in protect]
    for gen in drop:
        prefix = f"{SNAPSHOT_PREFIX}{gen}/"
        manifest = prefix + "snapshot.json"
        for key in private.list(prefix):
            if key != manifest:
                private.delete(key)
        private.delete(manifest)   # last: an interrupted prune leaves a detectable, reloadable-or-failing snapshot
    return drop


def _receipt(private, clock, report: PublishReport, extra: dict) -> str:
    now = clock()
    key = f"{RECEIPT_PREFIX}{now.strftime('%Y%m%dT%H%M%S%fZ')}-{report.action}-{report.gen_id}.json"
    private.put(key, encode_json({"schema_version": RECEIPT_SCHEMA, "at": iso(now), **report.summary(), **extra}),
                "application/json")
    return key


def _record_failure(private, state, stage: str, exc: BaseException, clock) -> None:
    try:
        state["last_failure"] = {"at": iso(clock()), "stage": stage, "error": f"{type(exc).__name__}: {exc}"[:500]}
        _write_state(private, state)
    except Exception:  # the original failure is the one that matters
        pass


# --- publication: head record, commits and settlement (G6, the accepted G5 design and addendum) ------

@dataclass
class PublishReport:
    gen_id: str
    action: str
    written: list[str] = field(default_factory=list)
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)
    recovered_from: str | None = None
    foreign_keys: list[str] = field(default_factory=list)
    snapshots_pruned: list[str] = field(default_factory=list)
    retired: dict = field(default_factory=dict)        # incarnation -> why its pages were deleted
    public_pages: int = 0
    incarnation: str | None = None
    commit: str | None = None
    quarantined: list[str] = field(default_factory=list)   # retiring, emptied, kept: an upload may still land

    def summary(self) -> dict:
        return {"gen_id": self.gen_id, "action": self.action, "written": len(self.written),
                "unchanged": self.unchanged, "deleted": len(self.deleted), "recovered_from": self.recovered_from,
                "foreign_keys": self.foreign_keys, "snapshots_pruned": self.snapshots_pruned,
                "retired": self.retired, "public_pages": self.public_pages, "incarnation": self.incarnation,
                "commit": self.commit, "quarantined": self.quarantined}


@dataclass(frozen=True)
class GenerationPolicy:
    """Owner-approved defaults (config.GEN_*). clock_bound_verified stays False until a deployment has
    actually verified the store-clock bound; until then expiry deletion is refused (visibly)."""
    reader_grace_s: int = config.GEN_READER_GRACE_S
    clock_epsilon_s: int = config.GEN_CLOCK_EPSILON_S
    clock_skew_s: int = config.GEN_CLOCK_SKEW_S
    page_cap: int = config.GEN_PAGE_CAP
    clock_bound_verified: bool = False

    @property
    def delete_after_s(self) -> int:
        """Earliest deletion after a generation ceases (49 min with the defaults), plus the run delay."""
        return self.reader_grace_s + self.clock_epsilon_s + self.clock_skew_s


DEFAULT_POLICY = GenerationPolicy()
LIFECYCLE_EVENTS = ("published", "restored", "ceased")
INDEX_KINDS = ("publish", "restore", "reseal")          # commits whose body is an index naming an incarnation
TOMBSTONE_KINDS = ("genesis", "tombstone")              # commits whose body names no incarnation
WITHDRAW_ROUNDS = config.GEN_WITHDRAW_TOMBSTONE_ATTEMPTS   # per withdraw call; each has a recorded result


def _new_nonce() -> str:
    """128 bits from the OS random source. One per index write and per head write, never reused."""
    return secrets.token_hex(16)


def _empty_head() -> dict:
    return {"schema_version": HEAD_SCHEMA, "nonce": None, "seq": 0, "intent": None, "withdrawing": None,
            "withdrawn": None, "live": None, "incarnations": {}, "commits": {}, "high": {}}


def read_head(private) -> tuple[dict, str | None]:
    """(head, its ETag). An absent head is the empty head with ETag None (its first write is create-only).
    An unreadable head makes history uncertain: PublishBlocked, nothing written or deleted."""
    got = private.get_versioned(HEAD_KEY)
    if got is None:
        return _empty_head(), None
    body, etag = got
    try:
        head = json.loads(body)
    except ValueError:
        head = None
    ok = (isinstance(head, dict) and head.get("schema_version") == HEAD_SCHEMA
          and all(isinstance(head.get(k), dict) for k in ("incarnations", "commits", "high"))
          and isinstance(head.get("seq"), int))
    if not ok:
        raise PublishBlocked(f"{HEAD_KEY} is unreadable or has the wrong shape; nothing written or deleted until "
                             "an operator settles it")
    return head, etag


def _write_head(private, head: dict, etag: str | None, intent: str) -> tuple[dict, str]:
    """One compare-and-swap of the head, sent once, with a fresh nonce in the body.

    Returns (the committed head, its ETag) only when a read after the write returns exactly these
    bytes. Otherwise PublishConflict, and the caller takes no public action for this intent:
      rejected   another actor changed the head first (412);
      ambiguous  the outcome is unknown and the committed head is not this document. The write may
                 still land later; the intents here are all safe to find in the head with no actor
                 behind them (a pin is an orphan, a recorded upload attempt quarantines its
                 incarnation, a prepared commit that was never sent stays open until the index
                 moves on, a retiring mark is finished by the next cleanup).
    """
    doc = copy.deepcopy(head)
    doc["nonce"] = _new_nonce()
    doc["seq"] = int(head.get("seq") or 0) + 1
    doc["intent"] = intent
    body = encode_json(doc)
    outcome = private.put_if_match(HEAD_KEY, body, etag, content_type="application/json", write_id=doc["nonce"])
    if outcome.rejected:
        raise PublishConflict(f"the head was changed by another actor before '{intent}' could be recorded (412); "
                              "nothing was done for it. Run again: the next run re-reads the head")
    got = private.get_versioned(HEAD_KEY)
    if got is None or got[0] != body:
        raise PublishConflict(f"the head write for '{intent}' was {outcome.status} and the committed head is not "
                              "that exact document; no public action is taken for it. Run again")
    return doc, got[1]


def _at(reading: dict) -> dict:
    """The times an event is stamped with: the high-water marks of the reading (never earlier than any
    earlier reading), so a clock that stepped back cannot date a retirement early. No store time for
    this reading means no store stamp, and an event without one is never old enough to delete."""
    stamp = reading.get("stamp") or reading
    return {"producer_at": iso(stamp["producer"]),
            "storage_at": iso(stamp["storage"]) if stamp["storage"] is not None else None}


def _epoch(entry: dict) -> dict | None:
    """The latest published/restored/ceased event of an incarnation. Protection follows the latest
    epoch, so an old restore is not a permanent exemption: a later cease starts a new retirement."""
    for e in reversed(entry["events"]):
        if e["kind"] in LIFECYCLE_EVENTS:
            return e
    return None


def _unsettled_uploads(entry: dict | None) -> list[str]:
    """Page-upload attempts into this incarnation that were recorded before their first PUT and never
    recorded as answered. Any of their PUTs may still land, at any time: a timed-out request, a process
    that exited, a lease that lapsed. Nothing in this module settles one: not an empty listing, elapsed
    time, a later identical upload, a local exit or a tombstone. While one is recorded the incarnation
    keeps its head entry and its full page reservation and is never retired."""
    return sorted((entry or {}).get("uploads") or {})


def _clock_reading(private, clock, state: dict) -> dict:
    """A paired reading: the producer's time and the store's own time for a heartbeat written now.
    Taken at the start of a run and again after each full observation that records lifecycle events,
    so a retirement is stamped after the replacement was seen live, never at run start and never
    from the time a commit was prepared.

    A clock that reads earlier than its recorded high-water mark, or a store that reports no
    modification time, is recorded as refused: no expiry deletion this run (retention only grows)."""
    producer = clock()
    private.put(HEARTBEAT_KEY, encode_json({"producer_at": iso(producer)}), "application/json")
    reader = getattr(private, "last_modified", None)
    storage = reader(HEARTBEAT_KEY) if reader is not None else None
    last = state.get("clock") or {}
    refused = []
    last_p = parse_iso(last["producer_at"]) if last.get("producer_at") else None
    last_s = parse_iso(last["storage_at"]) if last.get("storage_at") else None
    if last_p is not None and producer < last_p:
        refused.append("producer clock is earlier than its last recorded reading")
    if storage is None:
        refused.append("the store reports no modification time")
    elif last_s is not None and storage < last_s:
        refused.append("store clock is earlier than its last recorded reading")
    # High-water marks: a reading is never allowed to move them backwards.
    high_p = producer if last_p is None else max(producer, last_p)
    if storage is None:
        high_s = last_s
    else:
        high_s = storage if last_s is None else max(storage, last_s)
    state["clock"] = {
        "producer_at": iso(high_p),
        "storage_at": iso(high_s) if high_s is not None else None,
        "last_reading": {"producer": iso(producer), "storage": iso(storage) if storage is not None else None},
        "refused": refused,
    }
    return {"producer": producer, "storage": storage, "refused": refused,
            "stamp": {"producer": high_p, "storage": high_s if storage is not None else None}}


def _elapsed(event: dict, reading: dict) -> tuple[float | None, float | None]:
    p = (reading["producer"] - parse_iso(event["producer_at"])).total_seconds()
    s = None
    if reading["storage"] is not None and event.get("storage_at"):
        s = (reading["storage"] - parse_iso(event["storage_at"])).total_seconds()
    return p, s


def _time_passed(event: dict, reading: dict, need_s: float, policy: GenerationPolicy) -> tuple[bool, str]:
    """Both clocks show need_s since event, agree within the skew, neither went backwards."""
    if reading["refused"]:
        return False, "clock refused: " + "; ".join(reading["refused"])
    p, s = _elapsed(event, reading)
    if s is None:
        return False, "no store time for this event or run"
    if min(p, s) < need_s:
        return False, f"protected until both clocks show {int(need_s)}s (producer {int(p)}s, store {int(s)}s)"
    if abs(p - s) > policy.clock_skew_s:
        return False, f"producer and store elapsed disagree by {int(abs(p - s))}s (> {policy.clock_skew_s}s skew)"
    return True, ""


def _open_commits(head: dict) -> dict[str, dict]:
    """Prepared commits other than the live one. Each may still land, at any time, while the live
    ETag is the one it was conditioned on. A commit that can no longer land is removed by _settle."""
    live = (head.get("live") or {}).get("nonce")
    return {n: c for n, c in head["commits"].items() if n != live}


def classify(head: dict, listed: dict[str, list[str]], reading: dict, policy: GenerationPolicy) -> dict[str, dict]:
    """incarnation -> {"class", "why"} from the head and a listing of public pages.

      current    named by the live index
      open       a prepared index write naming it may still land: retained
      protected  ceased, inside the reader grace (or the clocks cannot show it has passed)
      expired    ceased and past the grace on both clocks: may be retired
      quarantined  a page upload into it was never answered and may still land: retained, with its
                 full reservation, until a real settlement mechanism exists (none in code)
      orphan     pinned, never offered, no open commit, every upload answered: may be retired
      retiring   a delete has been started in it: never written again; pages are deleted
      retired    no head entry and n <= the generation's high-water mark: stray pages are deleted
      uncertain  recorded current but not live (reconciliation ceases these first)
      unknown    public pages this publisher has no record of: retained
    Only orphan and expired incarnations are marked retiring; only retiring and retired pages are deleted."""
    live_inc = (head.get("live") or {}).get("inc")
    open_incs = {c.get("inc") for c in _open_commits(head).values() if c.get("inc")}
    out = {}
    for inc in sorted(set(head["incarnations"]) | set(listed)):
        entry = head["incarnations"].get(inc)
        if entry is None:
            parts = split_incarnation(inc)
            if parts is not None and parts[1] <= int(head["high"].get(parts[0], 0)):
                out[inc] = {"class": "retired", "why": "stray pages in a retired namespace: deleted, never reused"}
            else:
                out[inc] = {"class": "unknown", "why": "public pages with no head record: retained"}
            continue
        if entry["state"] == "retiring":
            out[inc] = {"class": "retiring", "why": "a delete was started in this namespace: never written again"}
            continue
        if inc == live_inc:
            out[inc] = {"class": "current", "why": "named by the live index"}
            continue
        if inc in open_incs:
            out[inc] = {"class": "open", "why": "a prepared index write naming it may still land (it is conditioned "
                                                "on the live index); retained until the index moves on"}
            continue
        epoch = _epoch(entry)
        if epoch is not None and epoch["kind"] != "ceased":
            out[inc] = {"class": "uncertain", "why": f"head says {epoch['kind']} but the live index differs"}
            continue
        if _unsettled_uploads(entry):
            # Would be orphan, protected or expired: the retirement path. Never on it while a PUT may land.
            out[inc] = {"class": "quarantined", "why": "a page upload into it has no recorded answer and may still "
                                                       "land: retained with its full reservation, never retired"}
            continue
        if epoch is None:
            out[inc] = {"class": "orphan", "why": "pinned, never offered to readers, no open index write"}
            continue
        ok, why = _time_passed(epoch, reading, policy.delete_after_s, policy)
        if ok and not policy.clock_bound_verified:
            ok, why = False, "clock bound unverified: expiry deletion refused"
        out[inc] = {"class": "expired" if ok else "protected", "why": why or "past the reader grace"}
    return out


def reserved_pages(head: dict, listed: dict[str, list[str]]) -> int:
    """Page-cap usage: the pages reserved by every incarnation in the head (pinned or retiring, written
    or not, with or without an open commit) plus public pages that have no head entry. The reservation
    is part of the head document, so it is taken and released only by a head compare-and-swap, and an
    entry with an unanswered upload attempt is never removed, so its reservation is never released."""
    known = sum(int(e["pages"]) for e in head["incarnations"].values())
    strays = sum(len(keys) for inc, keys in listed.items() if inc not in head["incarnations"])
    return known + strays


def _commit_key(nonce: str) -> str:
    return f"{COMMIT_PREFIX}{nonce}.json"


def read_commit(private, nonce: str) -> dict | None:
    rec = _read_json(private, _commit_key(nonce))
    if rec is None:
        return None
    if rec.get("schema_version") != COMMIT_SCHEMA or rec.get("nonce") != nonce or not isinstance(rec.get("page_set"), dict):
        raise PublishBlocked(f"{_commit_key(nonce)} has the wrong schema, nonce or shape")
    return rec


def _observe(private, public: "PublicViewStore", head: dict) -> dict:
    """One full, read-only observation of the live index.

    state: absent | foreign | tombstone | published. "problem" is set when the object cannot be
    matched in full to a commit this publisher prepared: its nonce is unknown, its bytes differ from
    the prepared ones, or (published) a page of the incarnation it names is missing or altered."""
    got = public.get_versioned(PUBLIC_INDEX)
    if got is None:
        return {"state": "absent", "etag": None, "nonce": None, "body": None, "commit": None, "record": None,
                "problem": None}
    body, etag = got
    nonce = commit_nonce_of(body)
    base = {"etag": etag, "nonce": nonce, "body": body, "commit": None, "record": None, "problem": None}
    known = head["commits"].get(nonce) if nonce else None
    if known is None:
        return dict(base, state="foreign", problem="the live index carries no commit nonce this publisher prepared "
                                                   "(a foreign or legacy write)")
    if sha256_hex(body) != known["body_sha256"]:
        return dict(base, state="foreign", commit=known,
                    problem=f"the live index carries commit {nonce} but its bytes differ from the prepared ones")
    if known["kind"] in TOMBSTONE_KINDS:
        return dict(base, state="tombstone", commit=known)
    record = read_commit(private, nonce)
    if record is None:
        return dict(base, state="published", commit=known, problem=f"commit record {nonce} is missing")
    for key, digest in record["page_set"].items():
        page = public.get(key)
        if page is None or sha256_hex(page) != digest:
            return dict(base, state="published", commit=known, record=record,
                        problem=f"the live index names incarnation {known.get('inc')} but its page {key} is "
                                "missing or altered")
    return dict(base, state="published", commit=known, record=record)


def _apply_observation(head: dict, obs: dict, stamp: dict) -> bool:
    """Record in `head` (in place) what one full observation proves. True if anything changed.

    Landed evidence and cessation are separate:
      - the observed commit has landed; its incarnation becomes current if it was not;
      - an incarnation ceases only here, because a *different* committed incarnation (or a
        tombstone) was just observed live in full. `stamp` is a reading taken after that
        observation. A prepared commit's own time is never used;
      - a prepared commit whose condition can no longer hold (its expected ETag is not the live
        one) is closed. If a later commit was conditioned on it, it did land once: its incarnation
        is recorded as offered and ceased at this observation (longer retention, never shorter).
    """
    changed = False
    nonce, etag = obs["nonce"], obs["etag"]
    live = head["commits"][nonce]
    seen = head.get("live") or {}
    if seen.get("nonce") != nonce or seen.get("etag") != etag:
        head["live"] = {"nonce": nonce, "etag": etag, "kind": live["kind"], "inc": live.get("inc")}
        changed = True
    if not live.get("landed"):
        live["landed"] = stamp
        changed = True
    live_inc = live.get("inc")
    entry = head["incarnations"].get(live_inc) if live_inc else None
    if entry is not None:
        epoch = _epoch(entry)
        if epoch is None or epoch["kind"] == "ceased":
            kind = "restored" if (epoch is not None or live["kind"] == "restore") else "published"
            entry["events"].append({"kind": kind, **stamp, "commit": nonce})
            changed = True
    for inc in sorted(head["incarnations"]):
        if inc == live_inc:
            continue
        other = head["incarnations"][inc]
        epoch = _epoch(other)
        if epoch is not None and epoch["kind"] in ("published", "restored"):
            other["events"].append({"kind": "ceased", **stamp, "observed": nonce})
            changed = True
    named = {c["expect"].get("nonce") for c in head["commits"].values()} - {None}
    for other_nonce in sorted(head["commits"]):
        if other_nonce == nonce:
            continue
        c = head["commits"][other_nonce]
        if c["expect"].get("etag") == etag:
            continue            # open: conditioned on the live ETag, it may still land
        inc = c.get("inc")
        if other_nonce in named and inc in head["incarnations"] and _epoch(head["incarnations"][inc]) is None:
            events = head["incarnations"][inc]["events"]
            events.append({"kind": "published", **stamp, "commit": other_nonce, "evidence": "named by a later commit"})
            events.append({"kind": "ceased", **stamp, "observed": nonce})
        del head["commits"][other_nonce]
        changed = True
    if live["kind"] == "tombstone" and head.get("withdrawing"):
        head["withdrawn"] = {"requested": head["withdrawing"], "confirmed": stamp, "tombstone": nonce}
        head["withdrawing"] = None
        changed = True
    if live["kind"] in INDEX_KINDS and head.get("withdrawn"):
        head["withdrawn"] = None
        changed = True
    return changed


def _committed_summary(obs: dict, head: dict, stamp: dict, previous: dict | None) -> dict | None:
    """state.json's informational copy of what is live (status reads it; no decision depends on it)."""
    if obs["state"] != "published":
        return None
    record, index = obs["record"], json.loads(obs["body"])
    if previous and previous.get("commit") == obs["nonce"]:
        return previous
    return {"gen_id": record["gen"], "incarnation": record["inc"], "commit": obs["nonce"], "action": obs["commit"]["kind"],
            "build_as_of": index["build_as_of"], "published_at": stamp["producer_at"],
            "index_sha256": sha256_hex(obs["body"]), "pages": len(record["page_set"]),
            "views_total": index["views_total"], "complete": index["complete"],
            "previous_gen_id": (previous or {}).get("gen_id")}


def _settle(private, public: "PublicViewStore", head: dict, hetag, state: dict, clock, reading: dict):
    """Observe the live index in full, then record what that proves. (head, hetag, observation, reading).

    An unmatched index blocks the run (nothing written or deleted). When something is recorded, the
    clock is read again first, so every stamp is later than the observation it comes from."""
    obs = _observe(private, public, head)
    if obs["problem"]:
        state["blocked"] = {"at": iso(reading["producer"]), "why": obs["problem"]}
        _write_state(private, state)
        raise PublishBlocked(f"{obs['problem']}; nothing written or deleted until an operator settles it")
    if obs["state"] == "absent":
        if head.get("live") is not None:
            why = "the index key is absent although the head records a live commit (this publisher never deletes it)"
            state["blocked"] = {"at": iso(reading["producer"]), "why": why}
            _write_state(private, state)
            raise PublishBlocked(f"{why}; nothing written or deleted until an operator settles it")
        return head, hetag, obs, reading
    probe = copy.deepcopy(head)
    if _apply_observation(probe, obs, _at(reading)):
        reading = _clock_reading(private, clock, state)       # after the full match, never before it
        new = copy.deepcopy(head)
        _apply_observation(new, obs, _at(reading))
        head, hetag = _write_head(private, new, hetag, f"observed commit {obs['nonce']} live")
    state["committed"] = _committed_summary(obs, head, _at(reading), state.get("committed"))
    if state.get("in_progress") and (state["in_progress"].get("commit") == obs["nonce"]):
        state["in_progress"] = None
    state["blocked"] = None
    _write_state(private, state)
    return head, hetag, obs, reading


def _prepare(private, head: dict, hetag, *, kind: str, inc: str | None, gen: str | None, body: bytes,
             page_set: dict, obs: dict, reading: dict) -> tuple[dict, str, str]:
    """Durable intent before the index write: the commit record (create-if-absent), then the commit in
    the head (one compare-and-swap). Returns (head, hetag, nonce). The index write is sent only after
    this returns, i.e. only once the committed head is known to hold exactly this commit.

    Refused, on the head document the compare-and-swap replaces:
      - while a withdrawal is in progress (any commit that names an incarnation);
      - if the incarnation is not pinned (a cleanup or a withdrawal marked it retiring first);
      - if the observation the write is conditioned on is not the head's own record of what is live.
    """
    nonce = commit_nonce_of(body)
    if nonce is None or nonce in head["commits"]:
        raise PublishIntegrityError("an index write needs a fresh commit nonce in its body")
    if len(body) > LIMITS.index_bytes:
        raise PublishError(f"index is {len(body)} bytes with its commit nonce, over its {LIMITS.index_bytes}-byte cap")
    expect = {"etag": obs["etag"], "nonce": obs["nonce"]}
    new = copy.deepcopy(head)
    if kind in INDEX_KINDS:
        if new.get("withdrawing"):
            raise PublishPaused("a withdrawal is in progress: no index write naming a generation is prepared until "
                                "its tombstone is confirmed (run publish-withdraw again)")
        entry = new["incarnations"].get(inc)
        if entry is None or entry["state"] != "pinned":
            raise PublishConflict(f"incarnation {inc} is no longer pinned (it was marked retiring); its index write "
                                  "is not sent. Run again: the build is published under a fresh incarnation")
        if (new.get("live") or {}).get("etag") != obs["etag"]:
            raise PublishConflict("the observation this index write is conditioned on is not the head's record of "
                                  "the live index; nothing sent. Run again")
    record = {"schema_version": COMMIT_SCHEMA, "nonce": nonce, "kind": kind, "inc": inc, "gen": gen, "expect": expect,
              "body_sha256": sha256_hex(body), "pages_sha256": files_digest_of_hashes(page_set), "page_set": page_set,
              "recorded": _at(reading)}
    data = encode_json(record)
    if not private.put_if_absent(_commit_key(nonce), data, "application/json") or private.get(_commit_key(nonce)) != data:
        raise PublishIntegrityError(f"commit record {nonce} could not be written as prepared")
    new["commits"][nonce] = {"kind": kind, "inc": inc, "expect": expect, "body_sha256": record["body_sha256"],
                             "prepared": _at(reading), "landed": None}
    head, hetag = _write_head(private, new, hetag, f"prepare {kind} commit {nonce}")
    return head, hetag, nonce


def files_digest_of_hashes(page_set: dict) -> str:
    """A digest of a page set (key -> sha256), for the commit record."""
    return sha256_hex(encode_json({"page_set": dict(sorted(page_set.items()))}))


def _drop_commit(private, head: dict, hetag, nonce: str, why: str) -> tuple[dict, str]:
    """A known rejection: this request did not apply and cannot apply later. Its commit is closed now."""
    new = copy.deepcopy(head)
    new["commits"].pop(nonce, None)
    return _write_head(private, new, hetag, f"commit {nonce} {why}")


def _send_index(private, public: "PublicViewStore", head: dict, hetag, state: dict, clock, reading: dict, *,
                nonce: str, body: bytes, expect_etag, what: str):
    """The commit point: one conditional index write, sent once, then a full observation.

      applied    observe; the commit must be live. Lifecycle events are recorded from that observation.
      rejected   known not applied (412): the commit is closed, the index is observed to record
                 whichever prepared commit is live, and PublishConflict is raised.
      ambiguous  unknown: the commit stays open in the head. No retry, no timer, no override. It can
                 land only while the index it was conditioned on is still live; a later run's
                 observation settles it either way. PublishIntegrityError is raised.
    """
    outcome = public.put_if_match(PUBLIC_INDEX, body, expect_etag, "application/json", INDEX_CACHE_CONTROL,
                                  write_id=nonce)
    if outcome.rejected:
        head, hetag = _drop_commit(private, head, hetag, nonce, "rejected (412)")
        head, hetag, obs, reading = _settle(private, public, head, hetag, state, clock, reading)
        raise PublishConflict(f"the index write for {what} (commit {nonce}) was rejected (412): the index changed "
                              f"after it was read; commit {obs['nonce']} is live. Known not applied. Run again")
    if outcome.ambiguous:
        raise PublishIntegrityError(f"the index write for {what} (commit {nonce}) has an unknown outcome "
                                    f"({outcome.detail or 'no answer'}). It stays open: it can land only while the "
                                    "index it was conditioned on is live. A later run observes and settles it")
    head, hetag, obs, reading = _settle(private, public, head, hetag, state, clock, reading)
    if obs["nonce"] != nonce:
        raise PublishIntegrityError(f"the index write for {what} (commit {nonce}) was applied but commit "
                                    f"{obs['nonce']} is live; the next run observes again")
    return head, hetag, obs, reading


def _genesis(private, public: "PublicViewStore", head: dict, hetag, state: dict, clock, reading: dict):
    """Create the index key, once, with an "unpublished" tombstone (If-None-Match: *). Every later
    write is a compare-and-swap on an existing object, and the key is never deleted, so no create-only
    request can succeed again. An ambiguous create is re-sent by the next run (the provider's
    documented reconciliation): while an object exists at most one create can have applied."""
    nonce = _new_nonce()
    body = tombstone_index(nonce, "unpublished")
    absent = {"etag": None, "nonce": None}
    head, hetag, nonce = _prepare(private, head, hetag, kind="genesis", inc=None, gen=None, body=body, page_set={},
                                  obs=absent, reading=reading)
    outcome = public.put_if_match(PUBLIC_INDEX, body, None, "application/json", INDEX_CACHE_CONTROL, write_id=nonce)
    if outcome.ambiguous:
        raise PublishIntegrityError(f"creating the index key (commit {nonce}) has an unknown outcome "
                                    f"({outcome.detail or 'no answer'}); the next run observes, and re-sends the "
                                    "create only if the key is still absent")
    if outcome.rejected:
        head, hetag = _drop_commit(private, head, hetag, nonce, "rejected (412): the key already exists")
    head, hetag, obs, reading = _settle(private, public, head, hetag, state, clock, reading)
    if obs["state"] == "absent":
        raise PublishIntegrityError("the index key is still absent after its create was answered; the next run observes")
    return head, hetag, obs, reading


def _finish_retiring(private, public: "PublicViewStore", head: dict, hetag, report: PublishReport, classes: dict):
    """Delete the pages of every retiring namespace and every stray in a retired one, then record the
    retiring namespaces a fresh listing shows empty. The generation's high-water mark stays, so the
    incarnation id is never issued again; only its page reservation is released, and only for an
    incarnation with no unanswered upload attempt (see _unsettled_uploads)."""
    listed = public.pages_by_incarnation()
    for inc in sorted(listed):
        entry = head["incarnations"].get(inc)
        parts = split_incarnation(inc)
        retired = entry is None and parts is not None and parts[1] <= int(head["high"].get(parts[0], 0))
        if not ((entry is not None and entry["state"] == "retiring") or retired):
            continue
        for key in listed[inc]:
            public.delete(key)
            report.deleted.append(key)
        report.retired[inc] = (classes.get(inc) or {}).get("class", "retiring")
    after = public.pages_by_incarnation()
    emptied = sorted(i for i, e in head["incarnations"].items() if e["state"] == "retiring" and i not in after)
    # An empty listing is not settlement: a page PUT recorded as unanswered may land after it. Such an
    # entry stays retiring in the head with its full reservation; every later run deletes what lands.
    report.quarantined = sorted(i for i in emptied if _unsettled_uploads(head["incarnations"][i]))
    empty = [i for i in emptied if i not in report.quarantined]
    if empty:
        new = copy.deepcopy(head)
        for inc in empty:
            del new["incarnations"][inc]
        head, hetag = _write_head(private, new, hetag, "retired " + ",".join(empty))
    return head, hetag


def _retire(private, public: "PublicViewStore", head: dict, hetag, reading: dict, policy: GenerationPolicy,
            report: PublishReport, keep: set):
    """Cleanup, before admission; needs no new commit. Returns (head, hetag, classes).

    1. Orphan and expired incarnations are marked retiring by one head compare-and-swap. The
       protections are revalidated on the document that compare-and-swap replaces (no commit names
       the incarnation, it is not live, it is still pinned, no upload into it is unanswered), so
       they are the shared protections in force when the mark commits, not an earlier read. From
       then the namespace is never written.
    2. Only then are pages deleted (retiring namespaces and strays in retired ones).
    3. Namespaces a fresh listing shows empty are recorded retired.
    """
    listed = public.pages_by_incarnation()
    classes = classify(head, listed, reading, policy)
    targets = sorted(i for i, c in classes.items() if c["class"] in ("orphan", "expired") and i not in keep)
    if targets:
        new = copy.deepcopy(head)
        stamp = _at(reading)
        protected = {c.get("inc") for c in new["commits"].values()} | {(new.get("live") or {}).get("inc")}
        for inc in targets:
            entry = new["incarnations"].get(inc)
            if entry is None or entry["state"] != "pinned" or inc in protected or _unsettled_uploads(entry):
                raise PublishConflict(f"incarnation {inc} is protected in the current head; it is not retired")
            entry["state"] = "retiring"
            entry["events"].append({"kind": "retiring", **stamp, "reason": classes[inc]["class"]})
        head, hetag = _write_head(private, new, hetag, "retire " + ",".join(targets))
    head, hetag = _finish_retiring(private, public, head, hetag, report, classes)
    return head, hetag, classes


def _pin(private, head: dict, hetag, gen: str, files: dict[str, bytes], action: str, reading: dict,
         policy: GenerationPolicy, listed: dict[str, list[str]]):
    """Admission. Chooses the incarnation, qualifies the build under it, and reserves its pages against
    the cap in the same head compare-and-swap that pins it. Returns (head, hetag, incarnation, files).

    An incarnation of this generation that is still pinned (no delete was ever started in it) is
    reused: its pages are immutable and identical. Otherwise the next number after the generation's
    high-water mark is taken; a retiring or retired incarnation is never reused."""
    if head.get("withdrawing"):
        raise PublishPaused("a withdrawal is in progress: nothing is admitted until its tombstone is confirmed "
                            "(run publish-withdraw again)")
    pinned = sorted((e["n"], i) for i, e in head["incarnations"].items() if e["gen"] == gen and e["state"] == "pinned")
    high = int(head["high"].get(gen, 0))
    n = pinned[-1][0] if pinned else high + 1
    for _ in range(2):
        if n > INCARNATION_MAX:
            raise PublishPaused(f"generation {gen} has used all {INCARNATION_MAX} incarnations")
        inc, qualified = qualify_generation(files, n)
        validate_qualified(inc, qualified)
        entry = head["incarnations"].get(inc)
        if entry is None or entry["pages_sha256"] == pages_digest(qualified):
            break
        n = high + 1            # same id, different bytes: never rewritten; take a fresh incarnation
    else:
        raise PublishIntegrityError(f"no incarnation of {gen} can be written consistently")
    if entry is not None:
        return head, hetag, inc, qualified
    pages = len(qualified) - 1
    used = reserved_pages(head, listed)
    if used + pages > policy.page_cap:
        raise PublishPaused(f"publication paused at the page cap: {used} reserved or present + {pages} needed > "
                            f"{policy.page_cap}; protected pages are never evicted, cleanup continues every run")
    new = copy.deepcopy(head)
    new["incarnations"][inc] = {"gen": gen, "n": n, "state": "pinned", "pages": pages,
                                "pages_sha256": pages_digest(qualified), "logical_sha256": files_digest(files),
                                "events": [{"kind": "pinned", **_at(reading), "action": action}]}
    new["high"][gen] = max(high, n)
    head, hetag = _write_head(private, new, hetag, f"pin {inc} for {action}")
    return head, hetag, inc, qualified


def _abandon(private, head: dict, hetag, inc: str, reading: dict, attempt: str) -> None:
    """A page of a never-offered incarnation holds other bytes. It is not deleted and rewritten (a
    delete with an unknown outcome could land on the rewrite): the incarnation is marked retiring and
    the next run uses a fresh one. Every PUT this attempt sent was answered (the last one too: it
    returned and was read back), so the same compare-and-swap records the attempt answered; an older
    unanswered attempt, if any, stays recorded and keeps the entry and its reservation."""
    entry = head["incarnations"].get(inc)
    named = any(c.get("inc") == inc for c in head["commits"].values())
    if entry is None or entry["state"] != "pinned" or named or _epoch(entry) is not None:
        return
    new = copy.deepcopy(head)
    new["incarnations"][inc]["state"] = "retiring"
    new["incarnations"][inc]["events"].append({"kind": "retiring", **_at(reading), "reason": "abandoned"})
    uploads = new["incarnations"][inc].get("uploads") or {}
    uploads.pop(attempt, None)
    if not uploads:
        new["incarnations"][inc].pop("uploads", None)
    _write_head(private, new, hetag, f"abandon {inc}")


def _record_upload(private, head: dict, hetag, inc: str, attempt: str, reading: dict, *, answered: bool):
    """Durable intent before the first page PUT, and its settlement after the last.

      started   one head compare-and-swap records the attempt before any PUT is sent. Refused unless
                the incarnation is still pinned on the document it replaces (a cleanup or withdrawal
                that marked it retiring first wins, and no page is written into that namespace).
      answered  sent only when every PUT of this attempt returned and its page read back as written.
                Removes this attempt only; any other unanswered attempt stays recorded.
    An attempt left recorded (a PUT timed out, the process exited, the answered write was ambiguous)
    quarantines the incarnation: see _unsettled_uploads. Returns (head, hetag)."""
    new = copy.deepcopy(head)
    entry = new["incarnations"].get(inc)
    if answered:
        if entry is None or attempt not in (entry.get("uploads") or {}):
            raise PublishIntegrityError(f"upload attempt {attempt} into {inc} is not recorded in the head")
        del entry["uploads"][attempt]
        if not entry["uploads"]:
            del entry["uploads"]
        return _write_head(private, new, hetag, f"uploads answered {inc} {attempt}")
    if entry is None or entry["state"] != "pinned":
        raise PublishConflict(f"incarnation {inc} is no longer pinned (it was marked retiring); no page is written "
                              "into it. Run again: the build is published under a fresh incarnation")
    entry.setdefault("uploads", {})[attempt] = {"started": _at(reading)}
    return _write_head(private, new, hetag, f"uploads started {inc} {attempt}")


def _pause(private, state: dict, reading: dict, why: str) -> None:
    state["paused"] = {"at": iso(reading["producer"]), "why": why}
    _write_state(private, state)


def _run(private, public: "PublicViewStore", gen: str, files: dict[str, bytes], clock, *, action: str,
         policy: GenerationPolicy, keep_snapshots: int, snapshot: bool,
         lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Observe, clean up, pin, write pages, prepare the commit, send the index, observe again.
    `files` is the unqualified build of generation `gen`."""
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        report = PublishReport(gen, action)
        if state.get("in_progress"):
            report.recovered_from = state["in_progress"].get("incarnation") or state["in_progress"].get("gen_id")
        stage = "clock"
        try:
            reading = _clock_reading(private, clock, state)
            _write_state(private, state)
            stage = "head"
            try:
                head, hetag = read_head(private)
            except PublishBlocked as exc:
                state["blocked"] = {"at": iso(reading["producer"]), "why": str(exc)}
                _write_state(private, state)
                raise
            if head.get("withdrawing"):
                _pause(private, state, reading, "withdrawal unconfirmed")
                raise PublishPaused("a withdrawal is in progress and its tombstone is not confirmed: nothing is "
                                    "published or restored. Run publish-withdraw again")
            stage = "observe"
            head, hetag, obs, reading = _settle(private, public, head, hetag, state, clock, reading)
            if obs["state"] == "absent":
                stage = "genesis"
                head, hetag, obs, reading = _genesis(private, public, head, hetag, state, clock, reading)
            stage = "cleanup"
            keep = {i for i, e in head["incarnations"].items() if e["gen"] == gen and e["state"] == "pinned"}
            head, hetag, _classes = _retire(private, public, head, hetag, reading, policy, report, keep)
            listed = public.pages_by_incarnation()
            report.public_pages = sum(len(v) for v in listed.values())
            live_inc = (head.get("live") or {}).get("inc")
            live_entry = head["incarnations"].get(live_inc) if live_inc else None
            if live_entry is not None and live_entry["gen"] == gen:
                state["paused"] = None
                _write_state(private, state)
                report.incarnation, report.commit = live_inc, head["live"]["nonce"]
                report.unchanged = len(files)
                _receipt(private, clock, report, {"note": "already current"})
                return report
            stage = "admission"
            try:
                head, hetag, inc, qualified = _pin(private, head, hetag, gen, files, action, reading, policy, listed)
            except PublishPaused as exc:
                _pause(private, state, reading, str(exc))
                raise
            report.incarnation = inc
            state["paused"] = None
            state["in_progress"] = {"gen_id": gen, "incarnation": inc, "action": action,
                                    "started_at": iso(reading["producer"]), "keys": len(qualified), "commit": None}
            _write_state(private, state)
            if snapshot:
                stage = "snapshot"
                _snapshot(private, gen, files, clock)
            stage = "upload-intent"     # durable, in the head, before the first page PUT
            attempt = _new_nonce()
            head, hetag = _record_upload(private, head, hetag, inc, attempt, reading, answered=False)
            stage = "pages"
            for key in page_write_order(qualified):
                body = qualified[key]
                created = public.put_if_absent(key, body, "application/json", PAGE_CACHE_CONTROL)
                if public.get(key) != body:
                    _abandon(private, head, hetag, inc, reading, attempt)
                    raise PublishIntegrityError(f"{key} exists with different bytes" if not created
                                                else f"{key} did not read back as written")
                if created:
                    report.written.append(key)
                else:
                    report.unchanged += 1
            stage = "upload-answered"   # every PUT above returned and read back: this attempt is settled
            head, hetag = _record_upload(private, head, hetag, inc, attempt, reading, answered=True)
            stage = "prepare"   # durable intent, in the head, before the public commit
            body = commit_index(qualified[PUBLIC_INDEX], _new_nonce())
            page_set = {k: sha256_hex(b) for k, b in sorted(qualified.items()) if k != PUBLIC_INDEX}
            try:
                head, hetag, nonce = _prepare(private, head, hetag, kind=action, inc=inc, gen=gen, body=body,
                                              page_set=page_set, obs=obs, reading=reading)
            except PublishPaused as exc:
                _pause(private, state, reading, str(exc))
                raise
            report.commit = nonce
            state["in_progress"]["commit"] = nonce
            _write_state(private, state)
            stage = "index"     # the commit point
            head, hetag, obs, reading = _send_index(private, public, head, hetag, state, clock, reading, nonce=nonce,
                                                    body=body, expect_etag=obs["etag"], what=f"{action} of {inc}")
            report.written.append(PUBLIC_INDEX)
            state["last_failure"] = None
            state["withdrawn"] = None
            state["in_progress"] = None
            _write_state(private, state)
            stage = "receipt"
            report.public_pages = sum(len(v) for v in public.pages_by_incarnation().values())
            if snapshot:
                previous = (state.get("committed") or {}).get("previous_gen_id")
                report.snapshots_pruned = _prune_snapshots(private, keep_snapshots, {gen, previous} - {None})
            _receipt(private, clock, report, {"build_as_of": (state.get("committed") or {}).get("build_as_of")})
        except (PublishBlocked, PublishPaused):
            raise
        except BaseException as exc:
            _record_failure(private, state, stage, exc, clock)
            raise
        return report


def publish(private, public, files: dict[str, bytes], clock, *, keep_snapshots: int = 3,
            policy: GenerationPolicy = DEFAULT_POLICY, lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Publish builder output: validate, then _run (which qualifies it under a safe incarnation)."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is not None:
        raise PublishStopped(f"publication is stopped ({stopped(private).get('reason', '')}); run `resume` first")
    validate_generation(files)
    return _run(private, public, generation_name(files), files, clock, action="publish", policy=policy,
                keep_snapshots=keep_snapshots, snapshot=True, lease_holder=lease_holder,
                break_stale_lease=break_stale_lease)


def restore(private, public, gen_id: str, clock, *, policy: GenerationPolicy = DEFAULT_POLICY,
            lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Republish a retained generation from its snapshot. Requires the stop marker.

    If an incarnation of it is still pinned (no delete ever started in it), its pages are reused and
    only a new index, with a fresh nonce, is written. Otherwise the build is re-qualified from the
    snapshot under a fresh incarnation. With no usable snapshot the restore is refused (the pause
    stays): a retiring or retired namespace is never written again. Counts against the page cap like
    a publication; the displaced generation's retirement starts when the restore is observed live."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is None:
        raise PublishError("restore requires `stop` first, so the next cycle does not republish over it")
    if not is_gen(gen_id):
        raise PublishError(f"{gen_id!r} is not a generation id")
    files = load_snapshot(private, gen_id)
    validate_generation(files)
    return _run(private, public, gen_id, files, clock, action="restore", policy=policy, keep_snapshots=0,
                snapshot=False, lease_holder=lease_holder, break_stale_lease=break_stale_lease)


def reseal(private, public, clock, *, lease_holder=None, break_stale_lease=False) -> dict:
    """Rewrite the live generation's index with a fresh nonce (same incarnation, same pages). Readers
    see no change. Once it lands, every prepared index write conditioned on the previous index is
    known dead: this is the operator's way to close open commits (an unwanted restore, say)."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        reading = _clock_reading(private, clock, state)
        _write_state(private, state)
        head, hetag = read_head(private)
        if head.get("withdrawing"):
            raise PublishPaused("a withdrawal is in progress: run publish-withdraw again, not reseal")
        head, hetag, obs, reading = _settle(private, public, head, hetag, state, clock, reading)
        if obs["state"] != "published":
            raise PublishError("nothing is published, so there is no index to reseal")
        record, inc = obs["record"], obs["commit"]["inc"]
        closes = sorted(_open_commits(head))
        body = commit_index(obs["body"], _new_nonce())
        head, hetag, nonce = _prepare(private, head, hetag, kind="reseal", inc=inc, gen=record["gen"], body=body,
                                      page_set=record["page_set"], obs=obs, reading=reading)
        head, hetag, obs, reading = _send_index(private, public, head, hetag, state, clock, reading, nonce=nonce,
                                                body=body, expect_etag=obs["etag"], what=f"reseal of {inc}")
        report = PublishReport(record["gen"], "reseal", written=[PUBLIC_INDEX], incarnation=inc, commit=nonce)
        _receipt(private, clock, report, {"closed_commits": closes})
        return {"resealed": inc, "commit": nonce, "closed_commits": closes, "open_commits": sorted(_open_commits(head))}


def withdraw(private, public, clock, *, lease_holder=None, break_stale_lease=False) -> dict:
    """Explicit withdrawal: immediate, no grace. Cached copies and downloads cannot be recalled.

    1. The head records `withdrawing` and marks every incarnation retiring (durable intent, one
       compare-and-swap, before any public action). From then nothing is admitted or prepared.
    2. A tombstone is written over the index by compare-and-swap. The key is never deleted. A 412
       means a prepared commit landed first: the index is read again and the next tombstone is
       conditioned on it (at most WITHDRAW_ROUNDS attempts, each with a recorded result).
    3. Every public page is deleted at once, whatever the tombstone's outcome.
    4. The withdrawal is confirmed only when a tombstone is read back live. Until then the result and
       `publish-status` say withdrawal_unconfirmed, and the public index may be the old index or an
       index from a commit prepared earlier that lands late; either names pages that no longer
       exist (readers see "refresh required"). That interval has no time bound: it ends when a
       tombstone is confirmed. Run withdraw again to send another tombstone.
    5. An incarnation with an unanswered page upload keeps its head entry and full reservation after
       its pages are deleted: a PUT sent before the withdrawal may land after it, and the page then
       reappears (named by no index). The tombstone confirms the index only. `uploads_unsettled`
       lists them; every later withdraw or cleanup run deletes what lands. No time bound either.
    """
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is None:
        raise PublishError("withdraw requires `stop` first, so the next cycle does not republish")
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        reading = _clock_reading(private, clock, state)
        _write_state(private, state)
        head, hetag = read_head(private)
        stamp = _at(reading)
        gen = (state.get("committed") or {}).get("gen_id")
        new = copy.deepcopy(head)
        if not new.get("withdrawing"):
            new["withdrawing"] = dict(stamp)
        new["withdrawn"] = None
        for entry in new["incarnations"].values():
            if entry["state"] != "retiring":
                entry["state"] = "retiring"
                entry["events"].append({"kind": "retiring", **stamp, "reason": "withdrawn"})
        head, hetag = _write_head(private, new, hetag, "withdraw")

        def live_tombstone():
            got = public.get_versioned(PUBLIC_INDEX)
            nonce = commit_nonce_of(got[0]) if got else None
            mine = nonce is not None and (head["commits"].get(nonce) or {}).get("kind") == "tombstone"
            return got, nonce, mine

        rounds = []
        for _ in range(WITHDRAW_ROUNDS):
            got, live_nonce, confirmed = live_tombstone()
            if confirmed:
                break
            body = tombstone_index(_new_nonce(), "withdrawn")
            seen = {"etag": got[1] if got else None, "nonce": live_nonce}
            head, hetag, nonce = _prepare(private, head, hetag, kind="tombstone", inc=None, gen=None, body=body,
                                          page_set={}, obs=seen, reading=reading)
            outcome = public.put_if_match(PUBLIC_INDEX, body, seen["etag"], "application/json", INDEX_CACHE_CONTROL,
                                          write_id=nonce)
            rounds.append(outcome.status)
            if outcome.rejected:
                head, hetag = _drop_commit(private, head, hetag, nonce, "rejected (412)")
            elif outcome.ambiguous:
                break               # unknown: it stays open; no blind resend in this call
        deleted = []
        for inc, keys in sorted(public.pages_by_incarnation().items()):
            for key in keys:
                public.delete(key)
                deleted.append(key)
        _got, _nonce, confirmed = live_tombstone()
        if confirmed:
            # The tombstone is observed live: record it (cessation stamped after this observation).
            head, hetag, _obs, reading = _settle(private, public, head, hetag, state, clock, reading)
        report = PublishReport(gen or "none", "withdraw", deleted=deleted)
        head, hetag = _finish_retiring(private, public, head, hetag, report, {})
        state["withdrawn"] = {"at": iso(clock()), "gen_id": gen, "deleted": len(deleted), "confirmed": confirmed}
        state["committed"] = None
        state["in_progress"] = None
        _write_state(private, state)
        unsettled = sorted(i for i, e in head["incarnations"].items() if _unsettled_uploads(e))
        _receipt(private, clock, report, {"note": WITHDRAW_NOTE, "withdrawal_unconfirmed": not confirmed,
                                          "tombstone_attempts": rounds, "uploads_unsettled": unsettled})
        return {"withdrawn_gen_id": gen, "deleted": len(deleted), "snapshots_kept": snapshot_ids(private),
                "withdrawal_unconfirmed": not confirmed, "tombstone_attempts": rounds,
                "open_commits": sorted(_open_commits(head)), "note": WITHDRAW_NOTE if confirmed else UNCONFIRMED_NOTE,
                "uploads_unsettled": unsettled, "uploads_note": UPLOADS_UNSETTLED_NOTE if unsettled else None}


# --- operating status -------------------------------------------------------------

def status(private, public, clock, *, interval_s: int = config.INTERVAL_S,
           budget_s: int = config.CYCLE_BUDGET_S, policy: GenerationPolicy = DEFAULT_POLICY) -> dict:
    """What an operator checks. Read-only: no heartbeat and no head write, so expiry is shown on the
    producer clock only and nothing an observation proves is recorded here."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    state = read_state(private)
    now = clock()
    committed = state.get("committed")
    age = None
    if committed:
        age = (now - parse_iso(committed["published_at"])).total_seconds()
    stale_after = 2 * interval_s + budget_s
    problems = []
    try:
        head, _etag = read_head(private)
    except PublishBlocked:
        head = _empty_head()
        problems.append("head_unreadable")
    if state.get("last_failure"):
        problems.append("last_publication_failed")
    if state.get("in_progress"):
        problems.append("publication_interrupted")
    if state.get("blocked"):
        problems.append("publication_blocked")
    if state.get("paused"):
        problems.append("publication_paused")
    open_commits = _open_commits(head)
    if open_commits:
        problems.append("open_index_commits")
    if head.get("withdrawing"):
        problems.append("withdrawal_unconfirmed")
    try:
        obs = _observe(private, public, head)
    except PublishBlocked as exc:
        obs = {"state": "foreign", "nonce": None, "commit": None, "problem": str(exc)}
    if obs["problem"]:
        problems.append("live_index_unmatched")
    if committed and age is not None and age > stale_after and not stopped(private):
        problems.append("published_views_stale")
    if committed and obs.get("nonce") != committed.get("commit"):
        problems.append("public_index_differs_from_state")
    if head.get("live") is None and not head.get("withdrawn") and not head.get("withdrawing") and not stopped(private):
        problems.append("never_published")
    listed = public.pages_by_incarnation()
    reading = {"producer": now, "storage": None, "refused": ["status is read-only (no store time)"]}
    classes = classify(head, listed, reading, policy)
    incarnations = {}
    waiting_on_clock_bound = False
    for inc, info in classes.items():
        entry = head["incarnations"].get(inc)
        item = dict(info, pages=len(listed.get(inc, [])), reserved=int(entry["pages"]) if entry else 0,
                    uploads_unsettled=_unsettled_uploads(entry))
        epoch = _epoch(entry) if entry else None
        if epoch is not None and epoch["kind"] == "ceased" and inc in listed:
            earliest = parse_iso(epoch["producer_at"]) + timedelta(seconds=policy.delete_after_s)
            item["earliest_deletion_producer_clock"] = iso(earliest)
            if now >= earliest and not policy.clock_bound_verified:
                waiting_on_clock_bound = True
        incarnations[inc] = item
    if waiting_on_clock_bound:
        problems.append("expiry_blocked_clock_bound_unverified")
    if any(c["class"] == "unknown" for c in classes.values()):
        problems.append("unknown_public_generations")
    if any(_unsettled_uploads(e) for e in head["incarnations"].values()):
        problems.append("page_uploads_unsettled")
    leases = {}
    for name, key in (("writer", LEASE_KEY), ("publish", PUBLISH_LEASE_KEY)):
        lease = _read_json(private, key)
        if lease is None:
            continue
        expires = parse_iso(lease.get("expires_at")) if lease.get("expires_at") else None
        expired = expires is None or expires <= now
        leases[name] = {"holder": lease.get("holder"), "acquired_at": lease.get("acquired_at"),
                        "expires_at": lease.get("expires_at"), "expired": expired}
        if expired:
            # Left behind by a process that did not exit cleanly. Never taken over automatically: the
            # operator confirms no writer or publisher runs, then passes --break-stale-lease.
            problems.append(f"stale_{name}_lease")
    live = head.get("live") or {}
    public_pages = sum(len(v) for v in listed.values())
    return {"now": iso(now), "stopped": stopped(private), "committed": committed, "leases": leases,
            "published_age_s": age, "stale_after_s": stale_after, "in_progress": state.get("in_progress"),
            "last_failure": state.get("last_failure"), "withdrawn": head.get("withdrawn") or state.get("withdrawn"),
            "withdrawing": head.get("withdrawing"), "withdrawal_unconfirmed": bool(head.get("withdrawing")),
            "blocked": state.get("blocked"), "paused": state.get("paused"),
            "clock": state.get("clock"), "head_seq": head.get("seq"), "live_index": obs["state"],
            "live_commit": obs.get("nonce"), "live_generation": live.get("inc"),
            "open_commits": {n: {"kind": c["kind"], "inc": c.get("inc"), "prepared": c.get("prepared")}
                             for n, c in sorted(open_commits.items())},
            "generations": incarnations, "public_pages": public_pages,
            "reserved_pages": reserved_pages(head, listed), "page_cap": policy.page_cap,
            "policy": {"reader_grace_s": policy.reader_grace_s, "delete_after_s": policy.delete_after_s,
                       "clock_bound_verified": policy.clock_bound_verified},
            "snapshots": snapshot_ids(private), "public_foreign_keys": public.foreign_keys(),
            "healthy": not problems, "problems": problems}


# --- the pilot cycle --------------------------------------------------------------

@dataclass(frozen=True)
class PilotConfig:
    raw: dict

    @property
    def active(self) -> bool:
        return self.raw["active"] is True

    def __getitem__(self, key):
        return self.raw[key]


LAUNCH_COMMAND = "python -m market_data.news.launch --config "


def _launcher_problems(cfg: dict) -> list[str]:
    """The whole-process bound (launch.py). The scheduled command must be the launcher; its deadline must
    leave room for the collection budget; its interrupt grace must outlast one source's worst fetch (so an
    interrupted cycle can unwind and release its leases); and deadline + grace + kill wait must end before
    the next tick and within the lease TTL (no self-overlap; a live cycle never outlives its lease)."""
    problems = []
    if not str((cfg.get("schedule") or {}).get("command") or "").startswith(LAUNCH_COMMAND):
        problems.append(f"schedule.command must run the bounded launcher ({LAUNCH_COMMAND}...)")
    bounds = cfg.get("launcher")
    if not isinstance(bounds, dict):
        return problems + ["launcher block (deadline_s, interrupt_grace_s, kill_wait_s) is required"]
    names = ("deadline_s", "interrupt_grace_s", "kill_wait_s")
    bad = [n for n in names if not isinstance(bounds.get(n), int) or isinstance(bounds.get(n), bool) or bounds[n] <= 0]
    if bad:
        return problems + [f"launcher.{n} must be a positive integer" for n in bad]
    deadline, grace, kill_wait = (bounds[n] for n in names)
    budget = cfg.get("cycle_budget_s") or 0
    if deadline <= budget:
        problems.append(f"launcher.deadline_s must exceed cycle_budget_s ({budget}): it covers collection and publication")
    worst_fetch = config.REQUEST_TIMEOUT_S * (config.MAX_RETRIES + 1) + sum(config.RETRY_BACKOFF_S)
    if grace < worst_fetch:
        problems.append(f"launcher.interrupt_grace_s must be >= {worst_fetch:g} (one source's worst fetch)")
    interval = (cfg.get("schedule") or {}).get("interval_s") or config.INTERVAL_S
    if deadline + grace + kill_wait >= min(interval, config.LEASE_TTL_S + 1):
        problems.append(f"launcher.deadline_s + interrupt_grace_s + kill_wait_s must be < interval_s ({interval}) "
                        f"and <= the lease TTL ({config.LEASE_TTL_S})")
    return problems


def load_pilot_config(path) -> PilotConfig:
    cfg = json.loads(Path(path).read_bytes())
    problems = []
    if cfg.get("schema_version") != PILOT_SCHEMA:
        problems.append(f"schema_version must be {PILOT_SCHEMA}")
    if cfg.get("active") not in (True, False):
        problems.append("active must be true or false")
    sched = cfg.get("schedule") or {}
    if sched.get("interval_s") != config.INTERVAL_S or sched.get("cron") != "*/15 * * * *":
        problems.append("the pilot cadence is 15 minutes (interval_s 900, cron */15 * * * *)")
    if sched.get("timezone") != "UTC":
        problems.append("schedule.timezone must be UTC")
    if sched.get("restart_policy") != "NEVER":
        problems.append("schedule.restart_policy must be NEVER (one-shot cycles)")
    if not 0 < (cfg.get("cycle_budget_s") or 0) <= config.CYCLE_BUDGET_S:
        problems.append(f"cycle_budget_s must be 1..{config.CYCLE_BUDGET_S}")
    problems += _launcher_problems(cfg)
    enabled = sorted(s.id for s in config.SOURCES.values() if s.enabled)
    if sorted(cfg.get("sources") or []) != enabled:
        problems.append(f"sources must be exactly the four enabled sources {enabled}")
    if sorted(cfg.get("streams") or []) != sorted(config.STREAMS):
        problems.append(f"streams must be {sorted(config.STREAMS)}")
    views = cfg.get("views") or {}
    if not 1 <= (views.get("days") or 0) <= 14:
        problems.append("views.days must be 1..14")
    if not (views.get("tolerance_s") or 0) > 0:
        problems.append("views.tolerance_s must be positive")
    if not views.get("data_label"):
        problems.append("views.data_label is required")
    if (cfg.get("prices") or {}).get("input") != "unavailable":
        problems.append("prices.input must be 'unavailable' until a real price input is connected")
    stores = cfg.get("storage") or {}
    priv, pub = stores.get("private") or {}, stores.get("public") or {}
    if not priv.get("env_prefix") or not pub.get("env_prefix") or priv.get("env_prefix") == pub.get("env_prefix"):
        problems.append("storage.private and storage.public need distinct env_prefix values")
    if pub.get("prefix") != PUBLIC_PREFIX:
        problems.append(f"storage.public.prefix must be {PUBLIC_PREFIX}")
    if not 1 <= ((cfg.get("retention") or {}).get("snapshots") or 0) <= 10:
        problems.append("retention.snapshots must be 1..10")
    gens = cfg.get("generations") or {}
    reader_floor = config.GEN_READER_DEADLINE_S + config.GEN_INDEX_LEASE_S + config.GEN_READER_DEADLINE_S
    if not isinstance(gens.get("reader_grace_s"), int) or gens["reader_grace_s"] < reader_floor:
        problems.append(f"generations.reader_grace_s must be an integer >= {reader_floor} "
                        "(index age + index lease + page request deadline)")
    for name in ("clock_epsilon_s", "clock_skew_s"):
        if not isinstance(gens.get(name), int) or gens[name] <= 0:
            problems.append(f"generations.{name} must be a positive integer")
    research = cfg.get("research") or {}
    if research.get("mode") != "research" or research.get("cutoff") != "build_as_of":
        problems.append("research must be pinned to mode 'research' at cutoff 'build_as_of' (the fixed view; "
                        "other modes or cutoffs are a separate decision)")
    if not research.get("data_label"):
        problems.append("research.data_label is required")
    # The pilot maximum: every view at its page limit, plus the one research page (R1).
    per_generation = 7 * len(STREAM_KEYS) * len(MODES) * LIMITS.pages_per_view + 1
    if not isinstance(gens.get("page_cap"), int) or gens["page_cap"] < per_generation:
        problems.append(f"generations.page_cap must be an integer >= {per_generation} (one pilot generation at its maximum)")
    if gens.get("clock_bound_verified") not in (True, False):
        problems.append("generations.clock_bound_verified must be true or false")
    if problems:
        raise PublishError("pilot config invalid: " + "; ".join(problems))
    return PilotConfig(cfg)


def policy_from(cfg: PilotConfig) -> GenerationPolicy:
    g = cfg["generations"]
    return GenerationPolicy(reader_grace_s=g["reader_grace_s"], clock_epsilon_s=g["clock_epsilon_s"],
                            clock_skew_s=g["clock_skew_s"], page_cap=g["page_cap"],
                            clock_bound_verified=g["clock_bound_verified"] is True)


def bucket_store_from_env(env, prefix: str) -> R2ObjectStore:
    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    return R2ObjectStore.from_env({n: env.get(prefix + n, "") for n in names})


def view_dates_for(now: datetime, days: int) -> list[str]:
    today = now.astimezone(timezone.utc).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def build_research(private, archive, now: datetime, data_label: str, limits=LIMITS) -> tuple[dict, bytes | None]:
    """R2: the research page of one build, from the private research ledger (news/v1/research/, local
    store only while the pilot is inactive). Pinned: research mode at this build's cutoff (now), with the
    collector archive's observations available as annotation targets. All time gates are research.py's
    (capture access + capture commit + annotation creation + annotation commit <= cutoff); nothing here
    chooses another mode or cutoff. Runs that fail verification are counted, never shown."""
    from .research import load_research, project, research_view   # local import: research imports archive
    ledger = load_research(private)
    if not ledger.runs:
        return research_page(None, as_of=now, limits=limits, problems=len(ledger.problems))
    view = research_view(ledger, now, "research", archive.observations)
    return research_page(project(view, data_label), as_of=now, limits=limits, runs=len(ledger.runs),
                         problems=len(ledger.problems))


def pilot_cycle(private, public, spool, fetch, clock, cfg: PilotConfig, *, commit_clock=None,
                lease_holder=None, break_stale_lease=False) -> dict:
    """One scheduled cycle: collect the four sources, build bounded views, publish. A stop marker no-ops."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    marker = stopped(private)
    if marker is not None:
        return {"status": "stopped", "stop": marker}
    report = collect_once(private, spool, fetch, clock, streams=list(cfg["streams"]),
                          only_sources=list(cfg["sources"]), budget_s=cfg["cycle_budget_s"],
                          commit_clock=commit_clock, lease_holder=lease_holder,
                          break_stale_lease=break_stale_lease)
    now = (commit_clock or clock)()
    v = cfg["views"]
    archive = load_archive(private)
    limits = qualified_limits()
    research = build_research(private, archive, now, cfg["research"]["data_label"], limits)
    # qualified_limits leaves room for the generation-qualified keys and fields added on publication.
    files = build_views(archive, unavailable_prices(), as_of=now, tolerance_s=v["tolerance_s"],
                        data_label=v["data_label"], dates=view_dates_for(now, v["days"]), limits=limits,
                        research=research)
    pub = publish(private, public, files, commit_clock or clock, keep_snapshots=cfg["retention"]["snapshots"],
                  policy=policy_from(cfg), lease_holder=lease_holder, break_stale_lease=break_stale_lease)
    m = report.manifest
    return {"status": "published", "run": {"run_id": report.run_id, "status": m["status"],
                                           "committed_at": m["committed_at"], "counts": m["counts"],
                                           "index_error": report.index_error, "history": report.checkpoint},
            "publication": pub.summary()}


__all__ = ["DEFAULT_POLICY", "GenerationPolicy", "MODES", "STREAM_KEYS", "PublicViewStore", "PublishBlocked",
           "PublishConflict", "PublishError", "PublishIntegrityError", "PublishPaused", "PublishStopped",
           "assert_separate", "classify", "incarnation_of", "is_builder_view_key", "is_public_view_key",
           "load_pilot_config", "pilot_cycle", "policy_from", "publish", "read_commit", "read_head", "read_state",
           "reseal", "reserved_pages", "restore", "resume", "status", "stop", "unavailable_prices",
           "validate_generation", "validate_qualified", "withdraw"]
