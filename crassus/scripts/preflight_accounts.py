#!/usr/bin/env python3
"""Check bot provisioning before the runner enters a restart loop.

Reports every active account whose password environment variable is absent,
every strategy_id the runner does not register, and whether
BOT_REGISTRATION_KEY is present. With ``--probe`` it sends an intentionally
empty registration request to the Worker. Only HTTP 400 with
``{"error":"Username must be 3-40 characters: letters, numbers, underscore"}``
proves the supplied header passed the key guard; that exact 403
``Invalid bot registration key`` proves a mismatch. Any other 400, a different
403, 401, 404, 429, 5xx, 200, malformed body or network failure fails closed.
The empty body cannot create an account.

    python scripts/preflight_accounts.py
    python scripts/preflight_accounts.py --probe --base-url https://...

Exit codes: 0 accepted, 2 missing account or key configuration, 3 Worker
rejected the key, 4 Worker unreachable or returned an unaccepted response. No
secret value is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

from crassus import strategies  # noqa: F401,E402  (import registers strategies)
from crassus.config import (  # noqa: E402
    BOT_REGISTRATION_KEY, DEFAULT_ACCOUNTS_CATALOG, DEFAULT_ACCOUNTS_OVERRIDE,
    _entries_for, account_gaps,
)
from crassus.strategy import REGISTRY  # noqa: E402


ACCEPTED_ERROR = "Username must be 3-40 characters: letters, numbers, underscore"
REJECTED_ERROR = "Invalid bot registration key"


def _worker_error(response) -> str | None:
    try:
        payload = json.loads(response.text or "")
    except (TypeError, ValueError):
        return None
    return payload.get("error") if isinstance(payload, dict) else None


def probe_registration_key(base_url: str, key: str, request=requests.post) -> dict:
    """Classify the Worker's answer without mutating an account.

    The only accepted proof is the real handleRegister contract: the supplied
    header passed the key guard, and the intentionally empty body then failed
    username validation with this exact 400 error. Any other 4xx (401, a
    different 403, 404, 429, an unrelated 400) fails closed rather than
    reporting readiness.
    """
    try:
        response = request(
            base_url.rstrip("/") + "/api/register",
            headers={"X-Bot-Registration-Key": key},
            json={},
            timeout=15,
        )
    except requests.RequestException as exc:
        return {"status": "unreachable", "detail": type(exc).__name__}
    error = _worker_error(response)
    status = response.status_code
    if status == 400 and error == ACCEPTED_ERROR:
        return {"status": "accepted", "http_status": status}
    if status == 403 and error == REJECTED_ERROR:
        return {"status": "rejected", "http_status": status}
    if status == 401:
        return {"status": "unauthorized", "http_status": status}
    if status == 403:
        return {"status": "forbidden", "http_status": status, "error": (error or "")[:160]}
    if status == 404:
        return {"status": "not_found", "http_status": status}
    if status == 429:
        return {"status": "rate_limited", "http_status": status}
    return {"status": "unexpected", "http_status": status, "error": (error or "")[:160]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="Worker base URL for --probe")
    parser.add_argument("--probe", action="store_true", help="check the key against the Worker")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    gaps = account_gaps()
    entries = _entries_for(
        None,
        DEFAULT_ACCOUNTS_CATALOG,
        DEFAULT_ACCOUNTS_OVERRIDE,
    )
    unregistered = [
        {"alias": entry.get("alias"), "strategy_id": entry.get("strategy_id")}
        for entry in entries
        if entry.get("active", True) is not False
        and entry.get("strategy_id") not in REGISTRY
    ]
    report = {
        "accounts_file": "CRASSUS_ACCOUNTS_FILE" if os.environ.get("CRASSUS_ACCOUNTS_FILE") else "catalog+override",
        "missing_env": gaps,
        "unregistered_strategies": unregistered,
        "registration_key": "present" if BOT_REGISTRATION_KEY else "missing",
        "worker_probe": None,
    }
    if not BOT_REGISTRATION_KEY:
        report["worker_probe"] = {"status": "not_attempted", "detail": "registration key missing"}
    elif args.probe:
        base = args.base_url or "https://options.moopertonic.net"
        report["worker_probe"] = probe_registration_key(base, BOT_REGISTRATION_KEY)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"accounts: {report['accounts_file']}")
        print(f"registration key: {report['registration_key']}")
        for gap in gaps:
            print(f"MISSING  {gap['alias']} ({gap['username']}): {gap['missing']}")
        for item in unregistered:
            print(f"UNKNOWN STRATEGY  {item['alias']}: {item['strategy_id']}")
        if report["worker_probe"]:
            print(f"worker probe: {report['worker_probe']['status']}")
        if not gaps and not unregistered and BOT_REGISTRATION_KEY:
            print("account provisioning: ready")

    if gaps or unregistered or not BOT_REGISTRATION_KEY:
        return 2
    probe = report["worker_probe"]
    if probe and probe["status"] == "rejected":
        return 3
    if probe and probe["status"] != "accepted":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
