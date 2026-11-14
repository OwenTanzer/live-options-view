"""P1: positioning / institutional-pressure research captures and annotations (local, inactive).

Implements the accepted design (POSITIONING_ANNOTATIONS_DESIGN_v2.md) as a separate research ledger
next to the collector archive. Nothing here collects, fetches or publishes; records come from a
prepared bundle imported by a person (`research-import`).

Records
  research_capture   a source a person read (or tried to read). An ordinary revision (item_id,
                     revision chain, payload digest) with publication/update times and our
                     `accessed_at`, and NO collector known-at (no observed_at / first_seen_at).
                     Never holds third-party text: no excerpt, body or quote is accepted.
  annotation         a versioned judgment on one exact revision (a capture or a collected
                     observation), shaped like a classification: input_revision_id, annotated_at
                     (stamped by the import, never supplied), method, version, its own run. Body:
                     claims[] (one subject, one action each), context[] (stated figures that are
                     not pressure quantities) and an optional hypothesis. An annotation lineage is
                     revised by a later annotation that names the previous one.

Ledger: news/v1/research/runs/<date>/<run_id>/{captures,annotations}.jsonl.gz + manifest.json +
commit.json (the archive's commit-record primitive: written once, stamped after the manifest reads
back). A run without commit.json is not available in archive/research modes until a later
`research-settle` stamps it, at that later time. Its own lease, so it never blocks the collector.

Times (never filled in from one another): measured_at per quantity (text, may be "unknown");
publication/update on the capture (kind: publication | edgar_acceptance | sent, with precision);
collector known-at on observations only; accessed_at on captures only; annotated_at and the
annotation run's commit. A time with date or minute precision is gated by its LATEST possible
instant, so nothing is shown earlier than it could have been known.

Visibility per cutoff c (the accepted table):
  collector    observations with observed_at <= c; captures never; annotations on visible
               observations with annotated_at <= c
  archive      as collector, plus observation and annotation commits <= c
  publication  hindsight by design: captures with publication time <= c, every annotation known
               now, each flagged made_after_cutoff when made (or committed) after c. RETROSPECTIVE
  research     observations as archive mode, or captures with accessed_at <= c and capture commit
               <= c; annotations with annotated_at <= c and commit <= c. RESEARCH
Annotations are shown only on the exact revision shown. A hypothesis is contemporaneous only if its
annotation was made and committed before the horizon start; otherwise it is retrospective, is never
a prediction, and is withheld in collector/archive modes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .archive import (ArchiveError, ArchiveIntegrityError, WriterLease, _read_commit_record, _record_commit,
                      decode_jsonl_gz, encode_json, encode_jsonl_gz, new_run_id)
from .normalize import canonical_json, canonical_url, iso, item_identity, parse_iso, revision_id, sha256_hex

RESEARCH_ROOT = f"{config.NAMESPACE}/research/"
RESEARCH_RUNS_PREFIX = f"{RESEARCH_ROOT}runs/"
RESEARCH_LEASE_KEY = f"{RESEARCH_ROOT}_writer/lease.json"
OBJECT_NAMES = ("captures", "annotations")
BUNDLE_SCHEMA = "news-research-bundle/v1"
CAPTURE_SCHEMA = "news-research-capture/v1"
ANNOTATION_SCHEMA = "news-annotation/v1"
RUN_SCHEMA = "news-research-run/v1"
VIEW_SCHEMA = "news-research-view/v1"
CAPTURE_SOURCE_ID = "research"

MODES = ("collector", "archive", "publication", "research")
EVIDENCE_KINDS = ("primary_disclosure", "attributed_report", "model_estimate", "mechanism_hypothesis",
                  "thesis_assertion")
ROLES = ("publisher", "cites", "origin")
ACCESS_RESULTS = ("read", "http_401", "http_403", "bot_blocked", "login_required", "not_attempted",
                  "not_available")
ACTION_STATUSES = ("reported", "offered", "executed", "unverified", "contradicted")
DIRECTIONS = ("long", "short", "gross", "net", "unknown")
QUANTITY_BASES = ("reported", "estimated", "modelled")
METHODS = ("manual", "rules", "model")
PUBLISHED_KINDS = ("publication", "edgar_acceptance", "sent", "unknown")
# Keys that would carry third-party text. Refused anywhere in a bundle.
TEXT_KEYS = frozenset({"excerpt", "body", "text", "quote", "content", "html", "summary", "snippet"})
MAX_TEXT = 400
MAX_TITLE = 160

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")
_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_URL = re.compile(r"^https?://[^\s<>\"']+$")


class ResearchError(ArchiveError):
    """A bundle or ledger the research module refuses."""


# ── time with precision ─────────────────────────────────────────────────────

def time_bounds(value) -> dict | None:
    """{"text", "precision", "earliest", "latest"} for a stated time, or None for "unknown"/null.

    date      the day anywhere on Earth: [D-1 10:00Z, D+1 12:00Z) (no zone is stated)
    minute    [t, t+59s]
    second    [t, t]
    """
    if value is None or value == "unknown":
        return None
    if not isinstance(value, str):
        raise ResearchError(f"time {value!r} is not a string")
    if _DATE.match(value):
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return {"text": value, "precision": "date", "earliest": day - timedelta(hours=14),
                "latest": day + timedelta(hours=35, minutes=59, seconds=59)}
    if _MINUTE.match(value):
        t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return {"text": value, "precision": "minute", "earliest": t, "latest": t + timedelta(seconds=59)}
    if _SECOND.match(value):
        t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return {"text": value, "precision": "second", "earliest": t, "latest": t}
    raise ResearchError(f"time {value!r}: use YYYY-MM-DD, a minute or second time with a zone, or \"unknown\"")


def _latest(value) -> datetime | None:
    b = time_bounds(value)
    return None if b is None else b["latest"]


def _earliest(value) -> datetime | None:
    b = time_bounds(value)
    return None if b is None else b["earliest"]


# ── bundle validation ───────────────────────────────────────────────────────

def _walk_keys(node, path, problems):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in TEXT_KEYS:
                problems.append(f"{path}.{k}: third-party text is not stored (no excerpts)")
            _walk_keys(v, f"{path}.{k}", problems)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_keys(v, f"{path}[{i}]", problems)
    elif isinstance(node, str) and len(node) > MAX_TEXT:
        problems.append(f"{path}: {len(node)} characters (max {MAX_TEXT}); keep our own short wording")


def _one_of(value, allowed, path, problems):
    if value not in allowed:
        problems.append(f"{path}: {value!r} is not one of {list(allowed)}")


def _time(value, path, problems, allow_unknown=True):
    if value in (None, "unknown"):
        if not allow_unknown:
            problems.append(f"{path}: a time is required")
        return
    try:
        time_bounds(value)
    except ResearchError as exc:
        problems.append(f"{path}: {exc}")


def _url(value, path, problems):
    if value is not None and (not isinstance(value, str) or not _URL.match(value)):
        problems.append(f"{path}: {value!r} is not an http(s) URL")


def _exact_keys(obj, required, optional, path, problems) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{path}: must be an object")
        return False
    missing = [k for k in required if k not in obj]
    extra = [k for k in obj if k not in required and k not in optional]
    if missing:
        problems.append(f"{path}: missing {missing}")
    if extra:
        problems.append(f"{path}: unexpected {extra}")
    return not missing


def _validate_access(access, path, problems):
    if not _exact_keys(access, ("result",), ("accessed_at", "via", "basis", "attempts"), path, problems):
        return
    _one_of(access["result"], ACCESS_RESULTS, f"{path}.result", problems)
    _time(access.get("accessed_at"), f"{path}.accessed_at", problems)
    for i, a in enumerate(access.get("attempts") or []):
        if _exact_keys(a, ("requested_at", "result"), (), f"{path}.attempts[{i}]", problems):
            _time(a["requested_at"], f"{path}.attempts[{i}].requested_at", problems, allow_unknown=False)
            _one_of(a["result"], ACCESS_RESULTS, f"{path}.attempts[{i}].result", problems)


def _validate_capture(c, i, problems):
    p = f"captures[{i}]"
    if not _exact_keys(c, ("key", "party", "title", "url", "published", "access"), ("updated", "note"), p, problems):
        return
    if not isinstance(c["title"], str) or not c["title"] or len(c["title"]) > MAX_TITLE:
        problems.append(f"{p}.title: our own short descriptor, 1-{MAX_TITLE} characters")
    _url(c["url"], f"{p}.url", problems)
    if _exact_keys(c["published"], ("at", "kind"), ("basis",), f"{p}.published", problems):
        _time(c["published"]["at"], f"{p}.published.at", problems)
        _one_of(c["published"]["kind"], PUBLISHED_KINDS, f"{p}.published.kind", problems)
    if c.get("updated") is not None:
        _time(c["updated"], f"{p}.updated", problems)
    _validate_access(c["access"], f"{p}.access", problems)
    if c["access"].get("accessed_at") in (None, "unknown"):
        problems.append(f"{p}.access.accessed_at: a capture needs our access time (it gates research mode)")


def _validate_claim(cl, path, problems):
    if not _exact_keys(cl, ("id", "evidence_kind", "chain", "subject", "action"),
                       ("exposure", "quantities", "dates", "note"), path, problems):
        return
    _one_of(cl["evidence_kind"], EVIDENCE_KINDS, f"{path}.evidence_kind", problems)
    if not isinstance(cl["chain"], list) or not cl["chain"]:
        problems.append(f"{path}.chain: at least one hop (what we read)")
    for j, hop in enumerate(cl["chain"] if isinstance(cl["chain"], list) else []):
        hp = f"{path}.chain[{j}]"
        if _exact_keys(hop, ("party", "role", "access"), ("url", "capture", "note"), hp, problems):
            _one_of(hop["role"], ROLES, f"{hp}.role", problems)
            _url(hop.get("url"), f"{hp}.url", problems)
            _validate_access(hop["access"], f"{hp}.access", problems)
    subj = cl["subject"]
    if _exact_keys(subj, (), ("party", "population"), f"{path}.subject", problems):
        if bool(subj.get("party")) == bool(subj.get("population")):
            problems.append(f"{path}.subject: exactly one of party or population (one subject per claim)")
    act = cl["action"]
    if isinstance(act, list):
        problems.append(f"{path}.action: one action per claim (split it into separate claims)")
    elif _exact_keys(act, ("kind", "status"), ("detail",), f"{path}.action", problems):
        _one_of(act["status"], ACTION_STATUSES, f"{path}.action.status", problems)
    if cl.get("exposure") is not None and _exact_keys(cl["exposure"], ("direction",), ("to", "instrument_ids"),
                                                      f"{path}.exposure", problems):
        _one_of(cl["exposure"]["direction"], DIRECTIONS, f"{path}.exposure.direction", problems)
    for j, q in enumerate(cl.get("quantities") or []):
        _validate_quantity(q, f"{path}.quantities[{j}]", problems)
    for j, d in enumerate(cl.get("dates") or []):
        if _exact_keys(d, ("kind", "at"), (), f"{path}.dates[{j}]", problems):
            _time(d["at"], f"{path}.dates[{j}].at", problems, allow_unknown=False)


def _validate_quantity(q, path, problems):
    if _exact_keys(q, ("value", "unit", "of", "measured_at", "basis"), (), path, problems):
        if not isinstance(q["of"], str) or not q["of"]:
            problems.append(f"{path}.of: say what the figure is of (never merged across different 'of')")
        if not isinstance(q["measured_at"], str) or not q["measured_at"]:
            problems.append(f"{path}.measured_at: a time, an interval in words, or \"unknown\"")
        _one_of(q["basis"], QUANTITY_BASES, f"{path}.basis", problems)


def _validate_annotation(a, i, keys, problems):
    p = f"annotations[{i}]"
    if "annotated_at" in (a if isinstance(a, dict) else {}):
        problems.append(f"{p}.annotated_at: stamped by the import, never supplied (no backdating)")
    if not _exact_keys(a, ("lineage", "method", "version", "claims"),
                       ("capture", "input_revision_id", "context", "hypothesis", "note", "annotated_at"), p, problems):
        return
    if bool(a.get("capture")) == bool(a.get("input_revision_id")):
        problems.append(f"{p}: exactly one of capture (a bundle capture key) or input_revision_id")
    if a.get("capture") and a["capture"] not in keys:
        problems.append(f"{p}.capture: {a['capture']!r} is not a capture in this bundle")
    _one_of(a["method"], METHODS, f"{p}.method", problems)
    if not isinstance(a["claims"], list) or not a["claims"]:
        problems.append(f"{p}.claims: at least one claim")
    ids = [cl.get("id") for cl in a["claims"] if isinstance(cl, dict)] if isinstance(a["claims"], list) else []
    if len(ids) != len(set(ids)):
        problems.append(f"{p}.claims: claim ids repeat")
    for j, cl in enumerate(a["claims"] if isinstance(a["claims"], list) else []):
        _validate_claim(cl, f"{p}.claims[{j}]", problems)
        for hop in (cl.get("chain") or []) if isinstance(cl, dict) else []:
            if isinstance(hop, dict) and hop.get("capture") and hop["capture"] not in keys:
                problems.append(f"{p}.claims[{j}]: hop capture {hop['capture']!r} is not in this bundle")
    for j, q in enumerate(a.get("context") or []):
        _validate_quantity(q, f"{p}.context[{j}]", problems)
    h = a.get("hypothesis")
    if h is not None and _exact_keys(h, ("statement", "horizon", "disconfirmation"),
                                     ("instruments", "missing_evidence"), f"{p}.hypothesis", problems):
        if _exact_keys(h["horizon"], ("start", "end"), (), f"{p}.hypothesis.horizon", problems):
            _time(h["horizon"]["start"], f"{p}.hypothesis.horizon.start", problems)
            _time(h["horizon"]["end"], f"{p}.hypothesis.horizon.end", problems)


def validate_bundle(bundle: dict) -> None:
    problems: list[str] = []
    if not isinstance(bundle, dict) or bundle.get("schema_version") != BUNDLE_SCHEMA:
        raise ResearchError(f"bundle schema_version must be {BUNDLE_SCHEMA}")
    if not _exact_keys(bundle, ("schema_version", "label", "captures", "annotations"), ("provenance", "note"),
                       "bundle", problems):
        raise ResearchError("; ".join(problems))
    _walk_keys(bundle, "bundle", problems)
    keys = [c.get("key") for c in bundle["captures"] if isinstance(c, dict)]
    if len(keys) != len(set(keys)):
        problems.append("captures: keys repeat")
    for i, c in enumerate(bundle["captures"]):
        _validate_capture(c, i, problems)
    for i, a in enumerate(bundle["annotations"]):
        _validate_annotation(a, i, set(keys), problems)
    if problems:
        raise ResearchError("bundle refused: " + "; ".join(problems[:20]))


# ── ledger ──────────────────────────────────────────────────────────────────

@dataclass
class ResearchRun:
    manifest: dict
    captures: list[dict]
    annotations: list[dict]

    @property
    def committed_at(self) -> str | None:
        return self.manifest.get("committed_at")


@dataclass
class Research:
    runs: list[ResearchRun] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)

    @property
    def captures(self) -> list[dict]:
        return [dict(c, committed_at=r.committed_at) for r in self.runs for c in r.captures]

    @property
    def annotations(self) -> list[dict]:
        return [dict(a, committed_at=r.committed_at) for r in self.runs for a in r.annotations]


def _run_prefix(date: str, run_id: str) -> str:
    return f"{RESEARCH_RUNS_PREFIX}{date}/{run_id}/"


def _verify(store, prefix: str) -> tuple[ResearchRun | None, str]:
    raw = store.get(prefix + "manifest.json")
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError):
        return None, "manifest unreadable"
    if manifest.get("schema_version") != RUN_SCHEMA or "committed_at" in manifest:
        return None, "manifest schema invalid"
    loaded = {}
    for name in OBJECT_NAMES:
        meta = manifest.get("objects", {}).get(name) or {}
        if meta.get("key") != f"{prefix}{name}.jsonl.gz":
            return None, f"{name}: manifest entry missing or outside run prefix"
        data = store.get(meta["key"])
        if data is None or len(data) != meta["bytes"] or sha256_hex(data) != meta["sha256"]:
            return None, f"{name}: missing or size/digest mismatch"
        records = decode_jsonl_gz(data)
        if len(records) != meta["records"]:
            return None, f"{name}: record count mismatch"
        loaded[name] = records
    committed_at, problem = _read_commit_record(store, prefix, manifest.get("run_id"), sha256_hex(raw))
    if problem:
        return None, problem
    return ResearchRun(dict(manifest, committed_at=committed_at), loaded["captures"], loaded["annotations"]), ""


def load_research(store) -> Research:
    """Every research run with a manifest, verified. Read-only. Runs without commit.json have
    committed_at None (not available in archive/research modes)."""
    out = Research()
    prefixes = sorted({k[:k.rindex("/") + 1] for k in store.list(RESEARCH_RUNS_PREFIX) if k.endswith("manifest.json")})
    for prefix in prefixes:
        run, problem = _verify(store, prefix)
        if run is None:
            out.problems.append({"prefix": prefix, "detail": problem})
        else:
            out.runs.append(run)
    out.runs.sort(key=lambda r: (r.manifest["started_at"], r.manifest["run_id"]))
    return out


def _capture_payload(c: dict) -> dict:
    return {"party": c["party"], "title": c["title"], "url": c["url"], "published": c["published"],
            "updated": c.get("updated"), "access": c["access"], "note": c.get("note")}


def _resolve_hops(claims, cap_ids):
    out = []
    for cl in claims:
        chain = []
        for hop in cl["chain"]:
            h = dict(hop)
            key = h.pop("capture", None)
            h["revision"] = cap_ids.get(key) if key else None   # None: not captured (not ingested)
            chain.append(h)
        out.append(dict(cl, chain=chain))
    return out


def import_bundle(store, bundle: dict, clock, *, commit_clock=None, defer_commit=False, run_id=None,
                  lease_holder=None, break_stale_lease=False) -> dict:
    """One research run from a prepared bundle. annotated_at = this import's clock (never earlier).

    Captures: a capture whose payload is unchanged from its latest revision adds nothing; a changed
    one is a new revision (previous_revision_id). Annotations: a lineage whose latest annotation has
    the same payload on the same input revision adds nothing; otherwise a new annotation that names
    the previous one. defer_commit leaves the run without commit.json (a delayed commit)."""
    validate_bundle(bundle)
    commit_clock = commit_clock or clock
    with WriterLease(store, clock, lease_holder, break_stale=break_stale_lease, key=RESEARCH_LEASE_KEY):
        ledger = load_research(store)
        if ledger.problems:
            raise ArchiveIntegrityError(f"research ledger has corrupt runs: {ledger.problems}")
        started = clock()
        run_id = run_id or new_run_id(started)
        heads: dict[str, dict] = {}
        for c in ledger.captures:
            heads[c["capture_key"]] = c
        lineages: dict[str, dict] = {}
        for a in ledger.annotations:
            lineages[a["lineage"]] = a
        captures, cap_ids = [], {}
        for c in bundle["captures"]:
            if _earliest(c["access"]["accessed_at"]) > started:
                raise ResearchError(f"capture {c['key']}: accessed_at {c['access']['accessed_at']} is after this "
                                    f"import ({iso(started)}); research knowledge is never backdated or forward-dated")
            canon = canonical_url(c["url"]) if c["url"] else ""
            item_id, quality = item_identity(CAPTURE_SOURCE_ID, canon, None if canon else c["key"], c["title"])
            payload = _capture_payload(c)
            digest = sha256_hex(canonical_json(payload))
            head = heads.get(c["key"])
            if head is not None and head["payload_digest"] == digest:
                cap_ids[c["key"]] = {"item_id": head["item_id"], "revision_id": head["revision_id"]}
                continue
            previous = head["revision_id"] if head else None
            rev = revision_id(item_id, digest, previous)
            acc = time_bounds(c["access"]["accessed_at"])
            rec = {"schema_version": CAPTURE_SCHEMA, "content_kind": "research_capture", "capture_key": c["key"],
                   "item_id": item_id, "identity_quality": quality, "revision_id": rev,
                   "previous_revision_id": previous, "payload_digest": digest, "run_id": run_id,
                   "canonical_url": canon or None, "accessed_precision": acc["precision"], **payload}
            captures.append(rec)
            heads[c["key"]] = rec
            cap_ids[c["key"]] = {"item_id": item_id, "revision_id": rev}
        known_revisions = {c["revision_id"] for c in heads.values()} | {c["revision_id"] for c in ledger.captures}
        annotations = []
        for a in bundle["annotations"]:
            if a.get("capture"):
                input_rev, input_kind = cap_ids[a["capture"]]["revision_id"], "research_capture"
            else:
                input_rev = a["input_revision_id"]
                input_kind = "research_capture" if input_rev in known_revisions else "observation"
            body = {"input_revision_id": input_rev, "input_kind": input_kind, "method": a["method"],
                    "version": a["version"], "claims": _resolve_hops(a["claims"], cap_ids),
                    "context": a.get("context") or [], "hypothesis": a.get("hypothesis"), "note": a.get("note")}
            digest = sha256_hex(canonical_json(body))
            head = lineages.get(a["lineage"])
            if head is not None and head["payload_digest"] == digest:
                continue
            previous = head["annotation_id"] if head else None
            ann_id = sha256_hex(f"annotation\x00{a['lineage']}\x00{digest}\x00{previous or ''}")
            rec = {"schema_version": ANNOTATION_SCHEMA, "record_type": "annotation", "annotation_id": ann_id,
                   "lineage": a["lineage"], "previous_annotation_id": previous, "payload_digest": digest,
                   "annotated_at": iso(started), "run_id": run_id, **body}
            annotations.append(rec)
            lineages[a["lineage"]] = rec
        finished = clock()
        date = started.strftime("%Y-%m-%d")
        prefix = _run_prefix(date, run_id)
        blobs = {"captures": encode_jsonl_gz(captures), "annotations": encode_jsonl_gz(annotations)}
        counts = {"captures": len(captures), "annotations": len(annotations)}
        manifest = {"schema_version": RUN_SCHEMA, "run_id": run_id, "run_kind": "research_import",
                    "namespace": config.NAMESPACE, "code_version": config.CODE_VERSION,
                    "bundle_label": bundle["label"], "bundle_provenance": bundle.get("provenance"),
                    "started_at": iso(started), "finished_at": iso(finished), "date": date, "counts": counts,
                    "objects": {n: {"key": f"{prefix}{n}.jsonl.gz", "bytes": len(blobs[n]),
                                    "sha256": sha256_hex(blobs[n]), "records": counts[n]} for n in OBJECT_NAMES}}
        for n in OBJECT_NAMES:
            store.put(manifest["objects"][n]["key"], blobs[n], "application/gzip")
            if store.get(manifest["objects"][n]["key"]) != blobs[n]:
                raise ArchiveIntegrityError(f"{prefix}{n}.jsonl.gz did not read back as written")
        body = encode_json(manifest)
        if not store.put_if_absent(prefix + "manifest.json", body) or store.get(prefix + "manifest.json") != body:
            raise ArchiveIntegrityError(f"{prefix}manifest.json could not be created as written")
        committed_at = None if defer_commit else _record_commit(store, prefix, run_id, sha256_hex(body), commit_clock)
        return {"run_id": run_id, "started_at": iso(started), "committed_at": committed_at, "counts": counts,
                "captures": {c["capture_key"]: c["revision_id"] for c in captures},
                "annotations": {a["lineage"]: a["annotation_id"] for a in annotations}}


def settle_research(store, commit_clock, *, lease_holder=None, break_stale_lease=False) -> list[str]:
    """Stamp commit.json for research runs that have a manifest but none, at commit_clock (never earlier)."""
    with WriterLease(store, commit_clock, lease_holder, break_stale=break_stale_lease, key=RESEARCH_LEASE_KEY):
        ledger = load_research(store)
        if ledger.problems:
            raise ArchiveIntegrityError(f"research ledger has corrupt runs: {ledger.problems}")
        settled = []
        for run in ledger.runs:
            if run.committed_at is None:
                m = run.manifest
                prefix = _run_prefix(m["date"], m["run_id"])
                _record_commit(store, prefix, m["run_id"], sha256_hex(store.get(prefix + "manifest.json")), commit_clock)
                settled.append(m["run_id"])
        return settled


# ── as-of view ──────────────────────────────────────────────────────────────

def _le(t: datetime | None, cutoff: datetime) -> bool:
    return t is not None and t <= cutoff


def hypothesis_timing(annotation: dict) -> str | None:
    """contemporaneous | retrospective (None without a hypothesis). Contemporaneous only if the
    annotation was made AND committed before the earliest possible horizon start."""
    h = annotation.get("hypothesis")
    if not h:
        return None
    start = _earliest(h["horizon"]["start"])
    made, committed = parse_iso(annotation["annotated_at"]), parse_iso(annotation.get("committed_at"))
    if start is None or committed is None:
        return "retrospective"
    return "contemporaneous" if made < start and committed < start else "retrospective"


def _annotation_visible(a: dict, cutoff, mode) -> bool:
    if mode == "publication":
        return True
    made = parse_iso(a["annotated_at"])
    if not _le(made, cutoff):
        return False
    if mode == "collector":
        return True
    return _le(parse_iso(a.get("committed_at")), cutoff)


def _capture_visible(c: dict, cutoff, mode) -> bool:
    if mode in ("collector", "archive"):
        return False
    if mode == "publication":
        return _le(_latest(c["published"]["at"]), cutoff)
    return _le(_latest(c["access"]["accessed_at"]), cutoff) and _le(parse_iso(c.get("committed_at")), cutoff)


def _observation_visible(o: dict, cutoff, mode) -> bool:
    if mode == "publication":
        return _le(parse_iso(o.get("published_at")), cutoff)
    if not _le(parse_iso(o["observed_at"]), cutoff):
        return False
    if mode == "collector":
        return True
    return _le(parse_iso(o.get("archived_at")), cutoff)


def research_view(research: Research, cutoff, mode: str = "research", observations=()) -> dict:
    """What the research ledger shows at a cutoff in one mode (see the module docstring).

    observations: collected revisions (reader.Archive.observations) that annotations may target."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cutoff = parse_iso(cutoff) if isinstance(cutoff, str) else cutoff
    sources = []      # (kind, revisions known now, visible revisions)
    by_item: dict[str, list[dict]] = {}
    for c in research.captures:
        by_item.setdefault(c["item_id"], []).append(c)
    for item_id, revs in by_item.items():
        sources.append(("research_capture", revs, [r for r in revs if _capture_visible(r, cutoff, mode)]))
    # A collected revision appears here only through an annotation visible at this cutoff: whether it
    # will be annotated later is not known at the cutoff (publication mode is hindsight by design).
    annotated_obs = {a["input_revision_id"] for a in research.annotations
                     if a["input_kind"] == "observation" and _annotation_visible(a, cutoff, mode)}
    obs_by_item: dict[str, list[dict]] = {}
    for o in observations:
        obs_by_item.setdefault(o["item_id"], []).append(o)
    for item_id, revs in obs_by_item.items():
        if not any(r["revision_id"] in annotated_obs for r in revs):
            continue
        revs = sorted(revs, key=lambda r: (r["observed_at"], r.get("archived_at") or ""))
        sources.append(("observation", revs, [r for r in revs if _observation_visible(r, cutoff, mode)]))
    excluded = {"no_publication_time": 0, "not_yet_visible": 0}
    items = []
    for kind, revs, visible in sources:
        if not visible:
            if mode == "publication" and kind == "research_capture" and _latest(revs[-1]["published"]["at"]) is None:
                excluded["no_publication_time"] += 1
            else:
                excluded["not_yet_visible"] += 1
            continue
        shown = revs[-1] if mode == "publication" else visible[-1]
        anns = [a for a in research.annotations if a["input_revision_id"] == shown["revision_id"]
                and _annotation_visible(a, cutoff, mode)]
        other = {a["lineage"] for a in research.annotations if a["input_revision_id"] in
                 {r["revision_id"] for r in revs} - {shown["revision_id"]} and _annotation_visible(a, cutoff, mode)}
        lineage_items = []
        for lineage in sorted({a["lineage"] for a in anns}):
            chain = [a for a in anns if a["lineage"] == lineage]
            head = chain[-1]
            timing = hypothesis_timing(head)
            entry = {"annotation": head, "revision_count": len(chain),
                     "previous": [a["annotation_id"] for a in chain[:-1]], "hypothesis_timing": timing,
                     "made_after_cutoff": (mode == "publication" and not (
                         _le(parse_iso(head["annotated_at"]), cutoff) and _le(parse_iso(head.get("committed_at")), cutoff)))}
            if timing == "retrospective" and mode in ("collector", "archive"):
                entry["hypothesis_withheld"] = "retrospective hypotheses appear only in publication and research modes"
            lineage_items.append(entry)
        items.append({"kind": kind, "source": shown, "revision_count": len(revs if mode == "publication" else visible),
                      "revisions": [r["revision_id"] for r in (revs if mode == "publication" else visible)],
                      "annotations": lineage_items, "annotations_on_other_revisions": sorted(other)})
    labels = {"collector": "collector-as-of view: what our collector had observed by the cutoff",
              "archive": "archive-as-of view: only what was durably committed by the cutoff",
              "publication": "RETROSPECTIVE publication-time view: uses everything known now (hindsight)",
              "research": "RESEARCH view: captures we had accessed and committed, and annotations made "
                          "and committed, by the cutoff"}
    return {"mode": mode, "cutoff": iso(cutoff), "label": labels[mode], "hindsight": mode == "publication",
            "items": items, "excluded": excluded,
            "runs": [{"run_id": r.manifest["run_id"], "started_at": r.manifest["started_at"],
                      "committed_at": r.committed_at, "bundle_label": r.manifest["bundle_label"]}
                     for r in research.runs]}


# ── sanitized projection (what the interface reads) ─────────────────────────

def _clip(s, n=MAX_TEXT):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _safe_url(u):
    return u if isinstance(u, str) and _URL.match(u) and len(u) <= 500 else None


def _time_view(value, kind=None, basis=None):
    b = time_bounds(value) if value not in (None, "unknown") else None
    return {"value": value if value is not None else "unknown", "precision": b["precision"] if b else "unknown",
            "kind": kind, "basis": _clip(basis)}


def _access_view(acc):
    return {"result": acc["result"], "at": _time_view(acc.get("accessed_at"), basis=acc.get("basis")),
            "via": _clip(acc.get("via")),
            "attempts": [{"requested_at": a["requested_at"], "result": a["result"]} for a in acc.get("attempts") or []]}


def _quantity_view(q):
    return {k: _clip(q[k]) if isinstance(q[k], str) else q[k] for k in ("value", "unit", "of", "measured_at", "basis")}


def project(view: dict, data_label: str) -> dict:
    """The sanitized view the interface renders: allowed fields only, http(s) URLs only, bounded text,
    no third-party text, every time with its precision, and every record with its provenance."""
    items = []
    for it in view["items"]:
        s = it["source"]
        if it["kind"] == "research_capture":
            source = {"kind": "research_capture", "party": _clip(s["party"], MAX_TITLE),
                      "title": _clip(s["title"], MAX_TITLE), "url": _safe_url(s["url"]),
                      "published": _time_view(s["published"]["at"], s["published"]["kind"], s["published"].get("basis")),
                      "updated": _time_view(s.get("updated")) if s.get("updated") else None,
                      "access": _access_view(s["access"]), "committed_at": s.get("committed_at"),
                      "item_id": s["item_id"], "revision_id": s["revision_id"],
                      "previous_revision_id": s["previous_revision_id"], "run_id": s["run_id"],
                      "note": _clip(s.get("note"))}
        else:
            source = {"kind": "observation", "party": _clip(s.get("outlet") or s.get("source_name"), MAX_TITLE),
                      "title": _clip(s.get("title"), MAX_TITLE), "url": _safe_url(s.get("canonical_url")),
                      "published": _time_view(s.get("published_at"), "publication"),
                      "observed_at": s["observed_at"], "committed_at": s.get("archived_at"),
                      "item_id": s["item_id"], "revision_id": s["revision_id"],
                      "previous_revision_id": s.get("previous_revision_id"), "run_id": s.get("run_id")}
        anns = []
        for e in it["annotations"]:
            a = e["annotation"]
            claims = []
            for cl in a["claims"]:
                claims.append({
                    "id": _clip(cl["id"], 20), "evidence_kind": cl["evidence_kind"],
                    "subject": {"party": _clip(cl["subject"].get("party"), MAX_TITLE),
                                "population": _clip(cl["subject"].get("population"))},
                    "exposure": None if not cl.get("exposure") else {
                        "direction": cl["exposure"]["direction"], "to": _clip(cl["exposure"].get("to")),
                        "instrument_ids": list(cl["exposure"].get("instrument_ids") or [])[:10]},
                    "action": {"kind": _clip(cl["action"]["kind"], 60), "status": cl["action"]["status"],
                               "detail": _clip(cl["action"].get("detail"))},
                    "quantities": [_quantity_view(q) for q in cl.get("quantities") or []],
                    "dates": [{"kind": _clip(d["kind"], 60), "at": _time_view(d["at"])} for d in cl.get("dates") or []],
                    "chain": [{"party": _clip(h["party"], MAX_TITLE), "role": h["role"], "url": _safe_url(h.get("url")),
                               "revision": h.get("revision"), "access": _access_view(h["access"]),
                               "note": _clip(h.get("note"))} for h in cl["chain"]],
                    "note": _clip(cl.get("note"))})
            hyp = None
            if a.get("hypothesis") and not e.get("hypothesis_withheld"):
                h = a["hypothesis"]
                hyp = {"timing": e["hypothesis_timing"], "statement": _clip(h["statement"]),
                       "instruments": list(h.get("instruments") or [])[:10],
                       "horizon": {"start": _time_view(h["horizon"]["start"]), "end": _time_view(h["horizon"]["end"])},
                       "disconfirmation": _clip(h["disconfirmation"]),
                       "missing_evidence": [_clip(m) for m in h.get("missing_evidence") or []][:10]}
            anns.append({"annotation_id": a["annotation_id"], "lineage": _clip(a["lineage"], 40),
                         "method": a["method"], "version": _clip(a["version"], 40),
                         "annotated_at": a["annotated_at"], "committed_at": a.get("committed_at"),
                         "run_id": a["run_id"], "input_revision_id": a["input_revision_id"],
                         "revision_count": e["revision_count"], "previous": e["previous"],
                         "made_after_cutoff": e["made_after_cutoff"], "claims": claims,
                         "context": [_quantity_view(q) for q in a.get("context") or []],
                         "hypothesis": hyp, "hypothesis_withheld": e.get("hypothesis_withheld"),
                         "note": _clip(a.get("note"))})
        items.append({"source": source, "revision_count": it["revision_count"], "annotations": anns,
                      "annotations_on_other_revisions": it["annotations_on_other_revisions"]})
    return {"schema_version": VIEW_SCHEMA, "data_label": _clip(data_label, 200), "mode": view["mode"],
            "cutoff": view["cutoff"], "label": view["label"], "hindsight": view["hindsight"],
            "not_proof": "Research records: they say who reported what, how we know it and when. "
                         "They are not evidence that the underlying market claims or any thesis are true.",
            "excluded": view["excluded"], "items": items}
