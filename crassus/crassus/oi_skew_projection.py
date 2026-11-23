"""Sanitized, read-only `oi_skew_meter.v1` projection of one OI Skew decision.

INACTIVE. This module only *builds* the document from a private ledger record
(`crassus_audit.v1`); nothing here writes to a provider, and no runner path
calls it. Publishing it (a new writer), allowlisting its key (API lane) and
fetching it (asset lane) are separate, unapproved releases -- see
docs/2026-11-oi-skew-meter-projection.md.

Privacy is enforced by construction: the output is assembled from an explicit
allowlist of fields, never by copying and deleting. The account alias and
username, cash, positions, trades, symbols, quantities, decision/execution IDs,
server responses and the raw run_id never appear; the run identity is a
salted-free one-way digest prefix, enough to tell two runner processes apart
on the same session key (a restart reset) without exposing the ledger name.

Missing evidence stays missing: if any provenance field the display needs is
absent (e.g. a ledger row written before the additive metadata existed), the
document says `status.code = "unavailable"` with the exact missing fields, and
the browser shows the bot signal as unavailable rather than guessing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "oi_skew_meter.v1"
STRATEGY_ID = "oi_skew_qqq"
PARAM_KEYS = ("near_money_pct", "imbalance_threshold", "min_change_from_session_start", "min_band_strikes")
#: Fields the display needs. `imbalance_ratio` may legitimately be null (no
#: OI in band) and `session_phase` is optional, so neither is listed.
REQUIRED = ("strategy_version", "code_revision", "run_identity", "session_key", "anchor_observed_at",
            "session_start_imbalance", "band_strikes", "params", "observed_snapshot_timestamp", "decision_at")
_REVISION_RE = re.compile(r"[0-9a-f]{7,40}")
_SESSION_KEY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: Keys that must never appear anywhere in a projection (checked by tests).
FORBIDDEN_KEYS = frozenset({
    "account_alias", "username", "account_state_before", "account_state_after", "balance_cash",
    "open_positions", "trades", "symbol", "quantity", "decision_id", "execution_request_id",
    "server_response", "run_id", "http_status", "closing_symbol", "held_quantity",
})


def run_identity(run_id: str | None) -> str | None:
    if not run_id:
        return None
    return "run:" + hashlib.sha256(f"oi_skew_meter.v1|{run_id}".encode()).hexdigest()[:16]


def _finite(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _timestamp(value: Any) -> str | None:
    """A timezone-aware ISO-8601 instant, kept verbatim; anything else is absent.

    Empty strings, unparseable text and naive times are not provenance."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _session_key(value: Any) -> str | None:
    """The tracker's reset key: a real UTC calendar date, YYYY-MM-DD."""
    if not isinstance(value, str) or not _SESSION_KEY_RE.fullmatch(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _revision(value: Any) -> str | None:
    """A git commit SHA (7-40 lowercase hex); anything else is unknown."""
    return value if isinstance(value, str) and _REVISION_RE.fullmatch(value) else None


def _params(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out = {}
    for k in PARAM_KEYS:
        v = raw.get(k)
        if k == "min_band_strikes":
            if not (isinstance(v, int) and not isinstance(v, bool)):
                return None
            out[k] = v
        else:
            v = _finite(v)
            if v is None:
                return None
            out[k] = v
    return out


def build_projection(record: dict[str, Any], *, published_at: datetime | None = None) -> dict[str, Any]:
    """One ledger record -> one sanitized projection (never raises on bad input)."""
    decision = record.get("decision") if isinstance(record, dict) else None
    meta = (decision or {}).get("metadata") if isinstance(decision, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    band = meta.get("band_strikes")
    band = band if isinstance(band, int) and not isinstance(band, bool) and band >= 0 else None
    doc: dict[str, Any] = {
        "schema_version": SCHEMA,
        "strategy_id": STRATEGY_ID,
        "strategy_version": record.get("strategy_version")
        if isinstance(record.get("strategy_version"), str) and record.get("strategy_version").strip() else None,
        "code_revision": _revision(record.get("code_revision")),
        "run_identity": run_identity(record.get("run_id") if isinstance(record.get("run_id"), str) else None),
        "session_key": _session_key(meta.get("session_key")),
        "anchor_observed_at": _timestamp(meta.get("anchor_observed_at")),
        "session_start_imbalance": _finite(meta.get("session_start_imbalance")),
        "imbalance_ratio": _finite(meta.get("imbalance_ratio")),
        "delta_from_session_start": _finite(meta.get("delta_from_session_start")),
        "band_strikes": band if isinstance(band, int) and not isinstance(band, bool) else None,
        "params": _params(meta.get("params")),
        # The strategy records the phase only on its market-closed branch;
        # when absent it stays null rather than being inferred.
        "session_phase": meta.get("session_phase") if isinstance(meta.get("session_phase"), str) else None,
        "observed_snapshot_timestamp": _timestamp(record.get("market_snapshot_timestamp")),
        "decision_at": _timestamp(record.get("timestamp_utc")),
        "published_at": (published_at or datetime.now(timezone.utc)).isoformat(),
    }
    problems = []
    if record.get("strategy_id") != STRATEGY_ID:
        problems.append("not_an_oi_skew_decision")
    missing = [k for k in REQUIRED if doc.get(k) is None]
    if missing:
        # Absent and malformed are the same for the display: not evidence.
        problems.append("missing_or_invalid:" + ",".join(missing))
    doc["status"] = ({"code": "ok", "reason": None} if not problems
                     else {"code": "unavailable", "reason": ";".join(problems)})
    return doc


def latest_oi_skew_record(records: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent OI Skew decision with a strategy decision attached."""
    best = None
    for r in records:
        if not isinstance(r, dict) or r.get("strategy_id") != STRATEGY_ID or not isinstance(r.get("decision"), dict):
            continue
        if best is None or str(r.get("timestamp_utc")) > str(best.get("timestamp_utc")):
            best = r
    return best


def assert_sanitized(doc: Any, path: str = "$") -> None:
    """Raise if any forbidden key appears anywhere in the document."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden key {k!r} at {path}")
            assert_sanitized(v, f"{path}.{k}")
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            assert_sanitized(v, f"{path}[{i}]")


def main(argv=None) -> int:
    """Local only: read ledger JSONL file(s), print/write the projection."""
    ap = argparse.ArgumentParser(description="Build an oi_skew_meter.v1 projection locally (no provider writes).")
    ap.add_argument("ledger", nargs="+", help="decisions-*.jsonl files (local copies)")
    ap.add_argument("--out", help="local output file (default: stdout)")
    a = ap.parse_args(argv)
    records = []
    for p in a.ledger:
        with open(p, encoding="utf-8") as fh:
            records.extend(json.loads(line) for line in fh if line.strip())
    rec = latest_oi_skew_record(records)
    doc = build_projection(rec or {})
    if rec is None:
        doc["status"] = {"code": "unavailable", "reason": "no_oi_skew_decision_in_input"}
    assert_sanitized(doc)
    text = json.dumps(doc, indent=1, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
