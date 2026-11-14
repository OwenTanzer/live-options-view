#!/usr/bin/env python3
"""Read-only check of the PUBLISHED OI skew meter projection (operator step).

Fetches the object over plain HTTPS, with no credentials. Checks that it is
oi_skew_meter.v1, carries only allowlisted keys, passes assert_sanitized,
and reports its status, provenance and age. The exit code is non-zero on
any failure.

    python scripts/check_published_projection.py \
        https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/crassus/oi-skew/v1/latest.json
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.oi_skew_projection import SCHEMA, assert_sanitized  # noqa: E402

ALLOWED = {"schema_version", "strategy_id", "strategy_version", "code_revision", "run_identity", "session_key",
           "anchor_observed_at", "session_start_imbalance", "imbalance_ratio", "delta_from_session_start",
           "band_strikes", "params", "session_phase", "observed_snapshot_timestamp", "decision_at",
           "published_at", "status"}


def main(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "crassus-projection-check/1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        status, ctype, body = r.status, r.headers.get("Content-Type", ""), r.read()
    doc = json.loads(body)
    problems = []
    if status != 200:
        problems.append(f"http {status}")
    if "json" not in ctype:
        problems.append(f"content-type {ctype!r}")
    if doc.get("schema_version") != SCHEMA:
        problems.append("schema")
    extra = set(doc) - ALLOWED
    if extra:
        problems.append(f"unexpected keys {sorted(extra)}")
    try:
        assert_sanitized(doc)
    except ValueError as exc:
        problems.append(str(exc))
    obs = doc.get("observed_snapshot_timestamp")
    age = None
    if obs:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(obs.replace("Z", "+00:00"))).total_seconds()
    print(json.dumps({"url": url, "status": doc.get("status"), "run_identity": doc.get("run_identity"),
                      "session_key": doc.get("session_key"), "anchor_observed_at": doc.get("anchor_observed_at"),
                      "code_revision": doc.get("code_revision"), "decision_at": doc.get("decision_at"),
                      "observed_age_s": None if age is None else round(age), "bytes": len(body),
                      "problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
