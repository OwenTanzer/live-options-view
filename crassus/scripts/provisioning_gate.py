#!/usr/bin/env python3
"""Pre-merge gate for newly active bot credentials (#41 / B2).

Compares the catalog against a base revision and requires redacted provisioning
evidence for every entry that becomes active with a `password_env` variable for
the first time. Entries landed as `active: false` need no evidence and remain
valid. Secret values are never read, printed or committed.

    python scripts/provisioning_gate.py --base-ref origin/master
    python scripts/provisioning_gate.py --base-ref "$BASE_SHA" --json
    python scripts/provisioning_gate.py --base-ref none          # no comparison
    python scripts/provisioning_gate.py --base-ref origin/master --check-env

`--check-env` adds the release-time half of the check: each newly active
variable must also have a value in the current process environment (the
operator exports the Railway variables for that run). Without it the gate is
purely a review control over the evidence file.

Exit codes: 0 pass, 1 gate findings, 2 unusable input (unreadable catalog or
evidence, unresolvable base revision).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

from crassus.provisioning import (  # noqa: E402
    evaluate, load_catalog, load_evidence,
)


def _base_catalog(repo_root: Path, ref: str, catalog_rel: str) -> list[dict] | None:
    """The catalog at `ref`, or None when the caller explicitly asked for no base."""
    if ref.strip().lower() in ("", "none") or set(ref.strip()) == {"0"}:
        # An all-zero revision is what GitHub sends as `github.event.before`
        # for a new branch; it is not a revision to compare against.
        return None
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{catalog_rel}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot read {catalog_rel} at base revision {ref!r}: "
            f"{(result.stderr or '').strip().splitlines()[-1] if result.stderr.strip() else 'unknown error'}. "
            "Fetch the base revision (actions/checkout fetch-depth: 0) or pass "
            "--base-ref none to skip the comparison deliberately."
        )
    payload = json.loads(result.stdout)
    entries = payload.get("accounts")
    if not isinstance(entries, list):
        raise ValueError(f"{ref}:{catalog_rel} has no 'accounts' list")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="origin/master",
                        help="git revision to compare against; 'none' skips the comparison")
    parser.add_argument("--catalog", type=Path, default=ROOT / "crassus" / "accounts.example.json")
    parser.add_argument("--base-catalog", type=Path, default=None,
                        help="read the base catalog from this file instead of a git revision")
    parser.add_argument("--evidence", type=Path, default=ROOT / "crassus" / "provisioning_evidence.json")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--check-env", action="store_true",
                        help="also require each newly active variable to be set here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        head_entries = load_catalog(args.catalog)
        if args.base_catalog is not None:
            base_entries = load_catalog(args.base_catalog)
        else:
            base_entries = _base_catalog(
                args.repo_root, args.base_ref, "crassus/accounts.example.json"
            )
        evidence = load_evidence(args.evidence) if Path(args.evidence).exists() else {}
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        message = str(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"provisioning gate: unusable input -- {message}")
        return 2

    result = evaluate(
        head_entries,
        base_entries=base_entries,
        evidence=evidence,
        environ=dict(os.environ),
        require_environment=args.check_env,
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        for line in result.summary_lines():
            print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
