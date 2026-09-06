"""Bounded, credential-free operational diagnostics for the runner."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class BelowError(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def configure_logging(verbose: bool = False) -> None:
    # Railway treats unstructured stderr as an error, even for INFO records.
    normal = logging.StreamHandler(sys.stdout)
    normal.addFilter(BelowError())
    errors = logging.StreamHandler(sys.stderr)
    errors.setLevel(logging.ERROR)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[normal, errors],
        force=True,
    )


# Exact Worker responses only. Never dump arbitrary response bodies, which
# could include credentials, cookies, HTML, or unrelated account data.
_REJECTION_REASONS = {
    "Insufficient balance": "insufficient_balance",
    "Cannot sell more shares than are held": "insufficient_position",
    "execution_request_id conflicts with a different trade intent": "intent_conflict",
    "Execution is already pending": "execution_pending",
}


def rejection_reason(response: Any) -> str:
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, str):
        return "unclassified_rejection"
    return _REJECTION_REASONS.get(error, "unclassified_rejection")


def event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Callers supply explicit operational fields; no raw network payloads."""
    logger.log(level, "%s", json.dumps({"event": name, **fields}, sort_keys=True))
