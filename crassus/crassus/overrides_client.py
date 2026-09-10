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

Follow-up review found the circuit above never actually engaged for a
Worker returning slow HTTP error responses: `_get`/`_post` called
`_note_success()` as soon as the HTTP round trip completed without
raising, before anything looked at the status code, so a stream of 503s
reset the consecutive-failure counter on every single request. Success is
now decided by each `fetch_*` method after validating status code and
payload -- `_get`/`_post` only hand back the raw response (or `None` on a
transport-level exception, which they still classify as a failure
themselves). A 404 that a given endpoint documents as a legitimate
"nothing there" is still counted as a healthy round trip, not a failure.

A consecutive-failure count is also not the same thing as a cycle-wide
time budget: failures from different accounts interleaved with occasional
slow successes could still burn well past the runner's cadence without
ever stringing together `_FAILURE_CIRCUIT_THRESHOLD` failures in a row.
`begin_cycle()` (called once per runner cycle, before any account is
processed) resets a wall-clock budget (`_CONTROL_READ_BUDGET_S`,
independent of the consecutive-failure circuit); every real GET/POST
attempt's duration counts against it, and once it's exhausted no further
real request is attempted until the next cycle, regardless of how those
attempts were distributed across successes and failures.
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
# Shared control-read time budget per runner cycle, well under the 300s
# cadence -- bounds cumulative real request time even when failures never
# string together enough consecutive hits to trip the circuit above.
_CONTROL_READ_BUDGET_S = 30.0


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
        control_read_budget_s: float = _CONTROL_READ_BUDGET_S,
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

        self._control_read_budget_s = control_read_budget_s
        self._cycle_elapsed_s = 0.0
        self._budget_exhausted = False

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

    # -- cycle budget --------------------------------------------------------

    def begin_cycle(self) -> None:
        """Reset the shared control-read time budget for a new runner cycle.

        Deliberately does not touch `_consecutive_failures` or
        `_circuit_open_until` -- the consecutive-failure circuit is a health
        signal about the Worker that should persist across cycle
        boundaries, not something a new cycle should forgive.
        """
        self._cycle_elapsed_s = 0.0
        self._budget_exhausted = False

    def _note_cycle_time(self, elapsed_s: float) -> None:
        self._cycle_elapsed_s += elapsed_s
        if self._cycle_elapsed_s >= self._control_read_budget_s:
            self._budget_exhausted = True

    def _get(self, path: str) -> requests.Response | None:
        """Returns the raw response, or `None` if this client is disabled,
        the circuit is open, this cycle's control-read time budget is
        exhausted, or the request itself raised (which this method treats
        as a failure on the caller's behalf). A returned response is NOT
        itself proof of a healthy round trip -- e.g. a 503, or a 200 with a
        malformed body; the caller must validate it and call
        `_note_success()`/`_note_failure()` accordingly.
        """
        if not self.enabled or self._circuit_is_open() or self._budget_exhausted:
            return None
        started = self._monotonic()
        try:
            return self.http.get(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout_s)
        except Exception as exc:
            log.warning("GET %s failed: %s", path, exc)
            self._note_failure()
            return None
        finally:
            self._note_cycle_time(self._monotonic() - started)

    def _post(self, path: str, json_body: dict[str, Any]) -> None:
        """Fire-and-forget, circuit- and budget-aware. Called only from
        `_mirror_worker` (see `post_ledger_mirror`) -- never on the caller's
        own thread."""
        if not self.enabled or self._circuit_is_open() or self._budget_exhausted:
            return
        started = self._monotonic()
        try:
            resp = self.http.post(f"{self.base_url}{path}", headers=self._headers(), json=json_body, timeout=self.timeout_s)
        except Exception as exc:
            log.warning("POST %s failed (non-fatal): %s", path, exc)
            self._note_failure()
            return
        finally:
            self._note_cycle_time(self._monotonic() - started)
        if resp.status_code != 200:
            log.warning("POST %s failed (non-fatal): HTTP %s", path, resp.status_code)
            self._note_failure()
            return
        self._note_success()

    # -- reads ---------------------------------------------------------------

    def fetch_override(self, account_alias: str) -> dict[str, Any] | None:
        """The latest `accepted` override envelope for one account, or None
        on any failure, if disabled, or if none exists. `None` is exactly
        the value `policy.OverridePolicy.evaluate` treats as "no override".

        A 404 (no accepted override for this account) is a legitimate,
        healthy answer and counts as a circuit success; a non-200/404
        status or a malformed body counts as a failure -- the Worker
        answered, just not usably.
        """
        resp = self._get(f"/api/crassus/overrides/{account_alias}")
        if resp is None:
            return None
        if resp.status_code == 404:
            self._note_success()
            return None
        if resp.status_code != 200:
            log.warning("fetch_override(%s): HTTP %s", account_alias, resp.status_code)
            self._note_failure()
            return None
        try:
            payload = resp.json()
        except Exception as exc:
            log.warning("fetch_override(%s): malformed JSON: %s", account_alias, exc)
            self._note_failure()
            return None
        if not isinstance(payload, dict):
            log.warning("fetch_override(%s): malformed payload: %r", account_alias, payload)
            self._note_failure()
            return None
        self._note_success()
        return payload

    def fetch_kill_switch(self) -> bool | None:
        """True if globally disabled. None (unknown/unreachable/disabled)
        must be treated by the caller as equivalent to True -- fail-closed.

        Only a 200 with a well-formed boolean `enabled` field counts as a
        circuit success; anything else -- bad status, bad JSON, a missing
        or non-boolean field -- counts as a failure.
        """
        resp = self._get("/api/crassus/kill-switch")
        if resp is None:
            return None
        if resp.status_code != 200:
            log.warning("fetch_kill_switch: HTTP %s", resp.status_code)
            self._note_failure()
            return None
        try:
            payload = resp.json()
        except Exception as exc:
            log.warning("fetch_kill_switch: malformed JSON: %s", exc)
            self._note_failure()
            return None
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            log.warning("fetch_kill_switch: malformed payload: %r", payload)
            self._note_failure()
            return None
        self._note_success()
        return enabled

    def fetch_freeze(self, account_alias: str) -> bool | None:
        """True if this account is frozen. None (unknown/unreachable/
        disabled) must be treated by the caller as equivalent to True --
        fail-closed.

        A 404 (never frozen) is a legitimate, healthy answer and counts as
        a circuit success, same as `fetch_override`'s 404 case.
        """
        resp = self._get(f"/api/crassus/freeze/{account_alias}")
        if resp is None:
            return None
        if resp.status_code == 404:
            self._note_success()
            return False
        if resp.status_code != 200:
            log.warning("fetch_freeze(%s): HTTP %s", account_alias, resp.status_code)
            self._note_failure()
            return None
        try:
            payload = resp.json()
        except Exception as exc:
            log.warning("fetch_freeze(%s): malformed JSON: %s", account_alias, exc)
            self._note_failure()
            return None
        frozen = payload.get("frozen") if isinstance(payload, dict) else None
        if not isinstance(frozen, bool):
            log.warning("fetch_freeze(%s): malformed payload: %r", account_alias, payload)
            self._note_failure()
            return None
        self._note_success()
        return frozen

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
