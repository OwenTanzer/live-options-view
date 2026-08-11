"""HTTP client for the Crassus AI override channel.

Mirrors `client.AccountSession`'s request pattern: short timeout, and every
failure mode -- timeout, connection error, non-200, malformed JSON -- is
swallowed and turned into the fail-closed value the caller should treat as
"no override, baseline only" (or, for the kill switch and freeze checks,
"assume the more restrictive state"). This module never raises; a broken
network path must degrade a bot to its own baseline parameters, not take
the cycle down.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import BOT_REGISTRATION_KEY, CRASSUS_AI_OVERRIDES_URL

log = logging.getLogger("crassus.overrides_client")

_TIMEOUT_S = 5.0


class OverridesClient:
    def __init__(
        self,
        base_url: str = CRASSUS_AI_OVERRIDES_URL,
        bot_registration_key: str | None = None,
        timeout_s: float = _TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self.bot_registration_key = (
            bot_registration_key if bot_registration_key is not None else BOT_REGISTRATION_KEY
        )
        self.timeout_s = timeout_s
        self.http = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"X-Bot-Registration-Key": self.bot_registration_key or ""}

    def fetch_override(self, account_alias: str) -> dict[str, Any] | None:
        """The latest `accepted` override envelope for one account, or None
        on any failure or if none exists. `None` is exactly the value
        `policy.OverridePolicy.evaluate` treats as "no override"."""
        try:
            resp = self.http.get(
                f"{self.base_url}/api/crassus/overrides/{account_alias}",
                headers=self._headers(),
                timeout=self.timeout_s,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                log.warning("fetch_override(%s): HTTP %s", account_alias, resp.status_code)
                return None
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            log.warning("fetch_override(%s) failed, treating as no override: %s", account_alias, exc)
            return None

    def fetch_kill_switch(self) -> bool | None:
        """True if globally disabled. None (unknown/unreachable) must be
        treated by the caller as equivalent to True -- fail-closed."""
        try:
            resp = self.http.get(
                f"{self.base_url}/api/crassus/kill-switch",
                headers=self._headers(),
                timeout=self.timeout_s,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            enabled = payload.get("enabled") if isinstance(payload, dict) else None
            return bool(enabled) if isinstance(enabled, bool) else None
        except Exception as exc:
            log.warning("fetch_kill_switch() failed, treating as engaged: %s", exc)
            return None

    def fetch_freeze(self, account_alias: str) -> bool | None:
        """True if this account is frozen. None (unknown/unreachable) must
        be treated by the caller as equivalent to True -- fail-closed."""
        try:
            resp = self.http.get(
                f"{self.base_url}/api/crassus/freeze/{account_alias}",
                headers=self._headers(),
                timeout=self.timeout_s,
            )
            if resp.status_code == 404:
                return False
            if resp.status_code != 200:
                return None
            payload = resp.json()
            frozen = payload.get("frozen") if isinstance(payload, dict) else None
            return bool(frozen) if isinstance(frozen, bool) else None
        except Exception as exc:
            log.warning("fetch_freeze(%s) failed, treating as frozen: %s", account_alias, exc)
            return None

    def post_ledger_mirror(self, record: dict[str, Any]) -> None:
        """Best-effort durability mirror of one decision-ledger record.

        Never raises and never blocks the caller on the outcome -- the local
        JSONL ledger (audit.py) remains the primary, authoritative record.
        This exists only so evidence survives a lost Railway volume too.
        """
        try:
            self.http.post(
                f"{self.base_url}/api/crassus/ledger",
                headers=self._headers(),
                json=record,
                timeout=self.timeout_s,
            )
        except Exception as exc:
            log.warning("post_ledger_mirror failed (non-fatal): %s", exc)
