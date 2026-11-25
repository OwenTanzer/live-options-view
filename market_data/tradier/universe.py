"""The day's contract universe: select it once, then reload it.

Selection is persisted before the stream opens and reloaded on a same-day
restart, so a restart never re-selects against a moved spot price.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ._probe import DEFAULT_UNDERLYING, Tradier, select_symbols
from .archive import universe_key


def load_or_select_universe(
    client: Tradier,
    r2: Any,
    bucket: str,
    prefix: str,
    strike_count: int | None,
    run_date: str,
    now_et: datetime,
    session_open: datetime | None = None,
    *,
    underlying: str = DEFAULT_UNDERLYING,
    expiration_policy: str = "same_day",
) -> tuple[list[str], dict[str, Any]]:
    """Load the day's already-selected contract universe, or select and
    persist it once. A same-day restart must never re-select against a
    (possibly moved) current spot price -- MOO-169 requires the universe
    stay fixed for the whole session.
    """
    loaded = load_persisted_universe(r2, bucket, prefix)
    full_chain = strike_count is None
    if loaded is not None and (not full_chain or loaded[1].get("selection_policy") == "full_chain"):
        return loaded
    symbols, universe = select_symbols(
        client, strike_count, 0, run_date, now_et, session_open=session_open,
        underlying=underlying, expiration_policy=expiration_policy,
    )
    if loaded is not None:
        # Read the value and its ETag together. Keep the opening selection as
        # provenance and atomically widen it under the existing collector lease.
        key = universe_key(prefix)
        previous = r2.get_object(Bucket=bucket, Key=key)
        payload = json.loads(previous["Body"].read())
        if payload["universe"].get("selection_policy") == "full_chain":
            return payload["symbols"], payload["universe"]
        if not set(payload["symbols"]).issubset(symbols):
            raise RuntimeError("Full chain would drop previously subscribed contracts")
        universe["previous_selection"] = payload["universe"]
        body = json.dumps({"symbols": symbols, "universe": universe}).encode()
        r2.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=previous["ETag"],
                      ContentType="application/json")
        return symbols, universe
    saved = persist_universe(r2, bucket, prefix, symbols, universe)
    if full_chain and saved[1].get("selection_policy") != "full_chain":
        raise RuntimeError("Persisted QQQ universe does not satisfy full-chain selection")
    return saved


def load_persisted_universe(r2: Any, bucket: str, prefix: str) -> tuple[list[str], dict[str, Any]] | None:
    try:
        body = r2.get_object(Bucket=bucket, Key=universe_key(prefix))["Body"].read()
        payload = json.loads(body)
        return payload["symbols"], payload["universe"]
    except r2.exceptions.NoSuchKey:
        return None
    except r2.exceptions.ClientError:
        return None


def persist_universe(
    r2: Any, bucket: str, prefix: str, symbols: list[str], universe: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Persist a freshly selected universe once; an existing one wins."""
    key = universe_key(prefix)
    payload = {"symbols": symbols, "universe": universe}
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        r2.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", IfNoneMatch="*")
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            # Another concurrent starter won the race -- use their selection.
            existing = json.loads(r2.get_object(Bucket=bucket, Key=key)["Body"].read())
            return existing["symbols"], existing["universe"]
        raise
    return symbols, universe
