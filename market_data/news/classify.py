"""Versioned, deterministic enrichment: stream membership and category.

A classification is its own event tied to one exact input revision. It never
alters the observation and carries its own classified_at, so an as-of reader
can tell what was known when. Model enrichment is a later, optional method
behind the same record shape; it is not implemented in this slice.
"""

from __future__ import annotations

import re

from . import config
from .normalize import iso, sha256_hex


def _relevance_text(obs: dict) -> str:
    # Prototype is_relevant(): title + summary + tag terms.
    return " ".join([obs.get("title", ""), obs.get("excerpt", ""), " ".join(obs.get("categories") or [])])


def _arc_text(obs: dict) -> str:
    # Prototype fallback_arc(): source + title + summary, lowercased.
    return f"{obs.get('source_name', '')} {obs.get('title', '')} {obs.get('excerpt', '')}".lower()


def stream_label(stream: config.Stream, obs: dict) -> dict:
    text = _relevance_text(obs)
    if stream.exclude_re.search(text):
        return {"member": False, "arc": None, "basis": "excluded"}
    if not stream.keyword_re.search(text):
        return {"member": False, "arc": None, "basis": "no_keyword"}
    arc_text = _arc_text(obs)
    for arc, pattern in stream.arc_rules:
        if re.search(pattern, arc_text):
            return {"member": True, "arc": arc, "basis": "rule"}
    return {"member": True, "arc": "UNMAPPED", "basis": "no_rule"}


def classify_rules(obs: dict, run_id: str, classified_at, streams=config.STREAMS) -> dict:
    """Rules classification of one observation revision.

    Membership is evaluated for each stream whose configuration includes the
    observation's source, so one item can belong to several streams.
    """
    digest = config.rules_digest(streams)
    labels = {sid: stream_label(streams[sid], obs) for sid in obs["source_streams"] if sid in streams}
    return {
        "schema_version": config.SCHEMA_VERSION,
        "record_type": "classification",
        "classification_id": sha256_hex(f"{obs['revision_id']}\x00rules\x00{config.RULES_VERSION}\x00{digest}"),
        "run_id": run_id,
        "item_id": obs["item_id"],
        "input_revision_id": obs["revision_id"],
        "classified_at": iso(classified_at),
        "method": "rules",
        "version": config.RULES_VERSION,
        "rules_digest": digest,
        "labels": labels,
        "stream_memberships": sorted(sid for sid, lab in labels.items() if lab["member"]),
        # No entity/instrument extraction in this slice: links stay unknown,
        # never a guessed ticker.
        "entities": [],
        "instruments": [],
        "instrument_mapping": "unknown",
        "conflict": "unknown",
    }


def classify_all(observations: list[dict], run_id: str, clock, classifier=classify_rules) -> tuple[list[dict], list[str], str]:
    """(classifications, failed revision ids, error). Failure never blocks archival."""
    out, failed, error = [], [], ""
    for obs in observations:
        try:
            out.append(classifier(obs, run_id, clock()))
        except Exception as exc:
            failed.append(obs["revision_id"])
            error = error or f"{type(exc).__name__}: {exc}"[:300]
    return out, failed, error
