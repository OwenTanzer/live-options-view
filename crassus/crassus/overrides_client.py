"""HTTP client for the Crassus AI override channel.

Mirrors `client.AccountSession`'s request pattern: short timeout, and every
failure mode -- timeout, connection error, non-200, malformed JSON -- is
swallowed and turned into the fail-closed value the caller should treat as
"no override, baseline only" (or, for the kill switch and freeze checks,
"assume the more restrictive state"). This module never raises; a broken
network path must degrade a bot to its own baseline parameters, not take
the cycle down.

Bounded across a whole run, not just per request. Flagged in review: an
earlier version made three independent, unbounded-by-anything-but-their-own-
timeout GET requests per account per cycle (override, kill switch, freeze)
plus a synchronous ledger-mirror POST per decision, with no shared circuit
between accounts. A Worker outage meant every account paid its own full
timeout on every one of those calls -- 22 accounts x 4 calls x a
`_TIMEOUT_S` that times out is 88 attempts and, worst case, 88 x timeout
seconds, comfortably past the runner's 300s cadence and the supervisor's
600s watchdog before a single account's strategy ever ran.

Two changes close that gap:

1. A consecutive-failure circuit shared across every read (`fetch_override`,
   `fetch_kill_switch`, `fetch_freeze`) on this client: once
   `_FAILURE_CIRCUIT_THRESHOLD` requests in a row fail, no further attempt
   is made for `_CIRCUIT_COOLDOWN_S` -- every call in that window returns
   its fail-closed default immediately, with no network I/O at all. This
   bounds one Worker outage to a handful of real timeouts total across an
   entire run, not one per account.
2. `post_ledger_mirror` never blocks the caller: it enqueues onto a bounded
   background queue drained by one dedicated worker thread, so a slow or
   down Worker cannot add latency to the runner's own cycle. A full queue
   (the Worker has been down long enough that puts have outpaced drains)
   drops the oldest-pending record rather than blocking -- the local JSONL
   ledger (audit.py) remains the authoritative record regardless.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import requests

from .config import BOT_REGISTRATION_KEY, CRASSUS_AI_ENABLED, CRASSUS_AI_OVERRIDES_URL

log = logging.getLogger("crassus.overrides_client")

_TIMEOUT_S = 5.0
_FAILURE_CIRCUIT_THRESHOLD = 3
_CIRCUIT_COOLDOWN_S = 60.0
_MIRROR_QUEUE_MAXSIZE = 200


class OverridesClient:
    def __init__(
        self,
        base_url: str = CRASSUS_AI_OVERRIDES_URL,
        bot_registration_key: str | None = None,
        timeout_s: float = _TIMEOUT_S,
        *,
        enabled: bool = CRASSUS_AI_ENABLED,
        failure_circuit_threshold: int = _FAILURE_CIRCUIT_THRESHOLD,
        circuit_cooldown_s: float = _CIRCUIT_COOLDOWN_S,
        monotonic: Callable[[], float] = time.monotonic,
        start_mirror_thread: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.bot_registration_key = (
            bot_registration_key if bot_registration_key is not None else BOT_REGISTRATION_KEY
        )
        self.timeout_s = timeout_s
        self.enabled = enabled
        self.http = requests.Session()

        self._failure_circuit_threshold = failure_circuit_threshold
        self._circuit_cooldown_s = circuit_cooldown_s
        self._monotonic = monotonic
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        # One dedicated worker thread drains this; post_ledger_mirror only
        # ever enqueues, never sends. Daemon so it never blocks process
        # shutdown -- an unsent mirror record is not worth delaying exit
        # for, since the local ledger already has it.
        self._mirror_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_MIRROR_QUEUE_MAXSIZE)
        self._mirror_thread: threading.Thread | None = None
        if start_mirror_thread:
            self._mirror_thread = threading.Thread(
                target=self._mirror_worker, name="crassus-ledger-mirror", daemon=True,
            )
            self._mirror_thread.start()

    def _headers(self) -> dict[str, str]:
        return {"X-Bot-Registration-Key": self.bot_registration_key or ""}

    # -- circuit -----------------------------------------------------------

    def _circuit_is_open(self) -> bool:
        return self._monotonic() < self._circuit_open_until

    def _note_success(self) -> None:
        self._consecutive_failures = 0

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_circuit_threshold:
            self._circuit_open_until = self._monotonic() + self._circuit_cooldown_s

    def _get(self, path: str) -> requests.Response | None:
        """`None` means "treat as unreachable" to every caller below --
        either this client is disabled, the circuit is open (bounding a
        whole run's worth of accounts after enough consecutive failures,
        not just this one request), or the request itself failed. Every
        failure here also feeds the circuit; every success resets it.
        """
        if not self.enabled or self._circuit_is_open():
            return None
        try:
            resp = self.http.get(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout_s)
            self._note_success()
            return resp
        except Exception as exc:
            log.warning("GET %s failed: %s", path, exc)
            self._note_failure()
            return None

    def _post(self, path: str, json_body: dict[str, Any]) -> None:
        """Fire-and-forget, circuit-aware. Called only from `_mirror_worker`
        (see `post_ledger_mirror`) -- never on the caller's own thread."""
        if not self.enabled or self._circuit_is_open():
            return
        try:
            self.http.post(f"{self.base_url}{path}", headers=self._headers(), json=json_body, timeout=self.timeout_s)
            self._note_success()
        except Exception as exc:
            log.warning("POST %s failed (non-fatal): %s", path, exc)
            self._note_failure()

    # -- reads ---------------------------------------------------------------

    def fetch_override(self, account_alias: str) -> dict[str, Any] | None:
        """The latest `accepted` override envelope for one account, or None
        on any failure, if disabled, or if none exists. `None` is exactly
        the value `policy.OverridePolicy.evaluate` treats as "no override"."""
        resp = self._get(f"/api/crassus/overrides/{account_alias}")
        if resp is None:
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            log.warning("fetch_override(%s): HTTP %s", account_alias, resp.status_code)
            return None
        try:
            payload = resp.json()
        except Exception as exc:
            log.warning("fetch_override(%s): malformed JSON: %s", account_alias, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def fetch_kill_switch(self) -> bool | None:
        """True if globally disabled. None (unknown/unreachable/disabled)
        must be treated by the caller as equivalent to True -- fail-closed."""
        resp = self._get("/api/crassus/kill-switch")
        if resp is None or resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        return bool(enabled) if isinstance(enabled, bool) else None

    def fetch_freeze(self, account_alias: str) -> bool | None:
        """True if this account is frozen. None (unknown/unreachable/
        disabled) must be treated by the caller as equivalent to True --
        fail-closed."""
        resp = self._get(f"/api/crassus/freeze/{account_alias}")
        if resp is None:
            return None
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        frozen = payload.get("frozen") if isinstance(payload, dict) else None
        return bool(frozen) if isinstance(frozen, bool) else None

    # -- ledger mirror ---------------------------------------------------------

    def post_ledger_mirror(self, record: dict[str, Any]) -> None:
        """Best-effort durability mirror of one decision-ledger record.

        Never raises and never blocks the caller on the network outcome --
        enqueues onto a bounded background queue and returns immediately.
        The local JSONL ledger (audit.py) remains the primary, authoritative
        record; this exists only so evidence survives a lost Railway volume
        too. If the queue is already full (the Worker has been down long
        enough that puts have outpaced one worker thread's drains), the
        oldest queued record is dropped to make room -- recent decisions
        are more useful to have mirrored than very old ones by the time a
        queue has backed up that far, and either way the local ledger is
        unaffected.
        """
        if not self.enabled:
            return
        try:
            self._mirror_queue.put_nowait(record)
        except queue.Full:
            try:
                self._mirror_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._mirror_queue.put_nowait(record)
            except queue.Full:
                log.warning("ledger mirror queue full; dropping record %s", record.get("decision_id"))

    def _mirror_worker(self) -> None:
        while True:
            record = self._mirror_queue.get()
            try:
                self._post("/api/crassus/ledger", record)
            except Exception:
                log.exception("ledger mirror worker: unexpected error")
            finally:
                self._mirror_queue.task_done()
