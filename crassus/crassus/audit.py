"""The decision ledger -- the fossil record.

Every pass through the loop writes exactly one append-only JSONL record,
whether it traded, declined to trade, or failed. Fills are not privileged:
an evaluation built only from executions is survivorship-filtered and cannot
explain why the bot did nothing.

The 19 mandatory fields and 9 outcome classes below are fixed by
`crassus_golden_goose_guidance.v1` (MOO-24). `record()` refuses to write a
line that is missing any of them, so a schema drift fails loudly at write
time rather than silently producing unusable evidence.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import clock

SCHEMA_VERSION = "crassus_audit.v1"


class Outcome:
    """The 9 permitted outcome_class values."""

    NO_TRADE = "no_trade"
    FILLED = "filled"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    AMBIGUOUS = "ambiguous"
    RECONCILED_AFTER_AMBIGUITY = "reconciled_after_ambiguity"
    ACCOUNT_LIQUIDATED = "account_liquidated"
    TRANSPORT_ERROR = "transport_error"
    RUNNER_ERROR = "runner_error"

    ALL = frozenset(
        {
            NO_TRADE,
            FILLED,
            REJECTED,
            RATE_LIMITED,
            AMBIGUOUS,
            RECONCILED_AFTER_AMBIGUITY,
            ACCOUNT_LIQUIDATED,
            TRANSPORT_ERROR,
            RUNNER_ERROR,
        }
    )


MANDATORY_FIELDS = (
    "schema_version",
    "run_id",
    "decision_id",
    "timestamp_utc",
    "timestamp_et",
    "account_alias",
    "strategy_id",
    "strategy_version",
    "market_snapshot_timestamp",
    "market_snapshot_url_or_hash",
    "account_state_before",
    "decision",
    "reason",
    "execution_request_id",
    "http_status",
    "server_response",
    "account_state_after",
    "latency_ms",
    "outcome_class",
)


@dataclass
class LedgerPaths:
    ledger: Path

    @classmethod
    def for_run(cls, ledger_dir: Path, run_id: str) -> "LedgerPaths":
        ledger_dir.mkdir(parents=True, exist_ok=True)
        return cls(ledger=ledger_dir / f"decisions-{run_id}.jsonl")


class DecisionLedger:
    """Append-only JSONL writer.

    Every line is flushed and fsynced before `record()` returns. The loop is
    allowed to crash immediately afterwards; what it already claimed to have
    written must actually be on disk.
    """

    def __init__(self, ledger_dir: Path, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.paths = LedgerPaths.for_run(Path(ledger_dir), self.run_id)
        self._lock = threading.Lock()

    def new_decision_id(self) -> str:
        return str(uuid.uuid4())

    def find_by_execution_request_id(self, execution_request_id: str) -> dict[str, Any] | None:
        """Idempotency check for crash recovery -- returns the *most recent*
        record for this execution, not just any record.

        A crash can land between a durable ledger write and clearing the
        in-flight intent file on disk. Without this, the next recovery pass
        would see the stale intent, find the trade again via `/api/me`, and
        write a second ledger record for something already recorded once.
        Scans every run's ledger file, not just the current run's, since the
        crash may have happened in a previous process lifetime.

        An `ambiguous` outcome deliberately does *not* finalize its intent,
        so the same execution_request_id can legitimately accumulate more
        than one record across recovery attempts before it resolves --
        returning the first match found would keep reporting a stale
        `ambiguous` record forever and never notice the later `filled`. Scan
        every file (oldest to newest by mtime, an approximation of run
        order across restarts) and every line within it, keeping the last
        match seen.
        """
        found: dict[str, Any] | None = None
        paths = sorted(
            self.paths.ledger.parent.glob("decisions-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
        )
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("execution_request_id") == execution_request_id:
                            found = rec
            except OSError:
                continue
        return found

    def record(
        self,
        *,
        decision_id: str,
        account_alias: str,
        strategy_id: str,
        strategy_version: str,
        outcome_class: str,
        decision: dict[str, Any] | None = None,
        reason: str | None = None,
        market_snapshot_timestamp: str | None = None,
        market_snapshot_url_or_hash: str | None = None,
        account_state_before: dict[str, Any] | None = None,
        account_state_after: dict[str, Any] | None = None,
        execution_request_id: str | None = None,
        http_status: int | None = None,
        server_response: Any = None,
        latency_ms: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if outcome_class not in Outcome.ALL:
            raise ValueError(f"Unknown outcome_class {outcome_class!r}")

        now = clock.now_utc()
        rec: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "decision_id": decision_id,
            "timestamp_utc": clock.iso_utc(now),
            "timestamp_et": clock.iso_et(now),
            "account_alias": account_alias,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "market_snapshot_timestamp": market_snapshot_timestamp,
            "market_snapshot_url_or_hash": market_snapshot_url_or_hash,
            "account_state_before": account_state_before,
            "decision": decision,
            "reason": reason,
            "execution_request_id": execution_request_id,
            "http_status": http_status,
            "server_response": server_response,
            "account_state_after": account_state_after,
            "latency_ms": latency_ms,
            "outcome_class": outcome_class,
        }
        rec.update(extra)

        missing = [f for f in MANDATORY_FIELDS if f not in rec]
        if missing:
            raise ValueError(f"Audit record missing mandatory fields: {missing}")

        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._lock:
            with open(self.paths.ledger, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return rec
