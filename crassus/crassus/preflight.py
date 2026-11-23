"""The Worker's registration-key contract, in one place.

The preflight CLI (`crassus/scripts/preflight_accounts.py`) and the bounded
periodic key-health check (`crassus/key_health.py`) must agree on what counts
as proof that the operator key the runner carries is the one the deployed
Worker expects. Keeping that rule in a library module rather than in a script
is what makes the two checks the same check: a single accepted response
contract, tested once (see `scripts/verify_preflight.py` and
`tests/preflight_worker_contract.test.js`).

The accepted proof is deliberately narrow. An empty registration body with a
valid key passes the key guard and then fails username validation with a 400
carrying `Username must be 3-40 characters: letters, numbers, underscore`.
Nothing else proves the key: a 401, a different 403, a 404, a 429, an
unrelated 400, a 200, a malformed body or a transport failure all fail closed,
because treating any of them as acceptance is how a drifted key reaches
production as a silent wrong result. The empty body cannot create an account,
so probing is non-mutating.

No secret value is ever returned by `probe_registration_key`; the returned
detail is limited to a status, the HTTP status and the Worker's own short
error string.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests

ACCEPTED_ERROR = "Username must be 3-40 characters: letters, numbers, underscore"
REJECTED_ERROR = "Invalid bot registration key"

#: Coarse operational states. The detailed `status` from the probe is kept for
#: diagnosis; these three are what an operator or a dashboard should branch on.
STATES = ("accepted", "rejected", "unreachable")

_ACCEPTED_STATUSES = frozenset({"accepted"})
_REJECTED_STATUSES = frozenset({"rejected"})


def worker_error(response: Any) -> str | None:
    """The Worker's own `error` string, or None if the body is not that shape."""
    try:
        payload = json.loads(response.text or "")
    except (TypeError, ValueError):
        return None
    return payload.get("error") if isinstance(payload, dict) else None


def probe_registration_key(
    base_url: str,
    key: str,
    request: Callable[..., Any] = requests.post,
    *,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Classify the Worker's answer to a deliberately empty registration.

    Returns one of:

    * ``{"status": "accepted", "http_status": 400}`` -- the supplied header
      passed the key guard; the empty body then failed username validation.
    * ``{"status": "rejected", "http_status": 403}`` -- the exact key-mismatch
      response.
    * ``{"status": "unreachable", "detail": "<ExceptionName>"}`` -- the request
      never produced a usable answer.
    * A distinct named status for every other response (``unauthorized``,
      ``forbidden``, ``not_found``, ``rate_limited``, ``unexpected``), each
      with its HTTP status. `health_state` folds these into `unreachable`:
      they are not proof of a working key, and they are not proof of a
      mismatch either, so they must never read as ready.
    """
    try:
        response = request(
            base_url.rstrip("/") + "/api/register",
            headers={"X-Bot-Registration-Key": key},
            json={},
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        return {"status": "unreachable", "detail": type(exc).__name__}
    error = worker_error(response)
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


def health_state(probe: dict[str, Any]) -> str:
    """Fold a probe result into `accepted` | `rejected` | `unreachable`.

    Only the two exact contract responses are affirmative evidence. Everything
    else is `unreachable` -- "we could not establish the key's health" -- which
    is honest about a 429 or a 404 rather than mislabelling them as a key
    mismatch the operator would chase in the wrong place.
    """
    status = probe.get("status")
    if status in _ACCEPTED_STATUSES:
        return "accepted"
    if status in _REJECTED_STATUSES:
        return "rejected"
    return "unreachable"


def describe(probe: dict[str, Any]) -> str:
    """A short, secret-free description for a log line or a report."""
    status = probe.get("status")
    http_status = probe.get("http_status")
    detail = probe.get("detail")
    error = probe.get("error")
    if detail:
        return f"{status} ({detail})"
    if status in ("accepted", "rejected"):
        return f"{status} (HTTP {http_status})"
    if error:
        return f"{status} (HTTP {http_status}: {error})"
    return f"{status} (HTTP {http_status})"
