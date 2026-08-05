#!/usr/bin/env python3
"""Prove the runner attributes a flatten-produced trade to the flatten
module, not the account's own assigned strategy (see crassus/flatten.py and
the wiring in `Runner._run_account`).

`AccountSession`/`ExecutionClient` do real HTTP; there's no existing fixture
for exercising `_run_account` in isolation, so this builds a `Runner`
without running `__init__` (which would try to construct real sessions) and
hand-populates only the attributes `_run_account` touches, with small fakes
standing in for the session and executor. `_recover_pending` is stubbed to
skip the crash-recovery path entirely, which isn't what this test is about.

    python scripts/verify_runner_flatten_attribution.py
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.audit import DecisionLedger, Outcome  # noqa: E402
from crassus.client import AccountState, ExecutionResult  # noqa: E402
from crassus.flatten import STRATEGY_ID as FLATTEN_STRATEGY_ID  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.runner import Runner  # noqa: E402
from crassus.strategies import momentum_qqq  # noqa: E402, F401 (registers momentum_qqq)

ET = ZoneInfo("America/New_York")

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


@dataclass
class FakeAccount:
    alias: str
    username: str
    strategy_id: str
    params: dict[str, Any] = field(default_factory=dict)


class FakeSession:
    def __init__(self, state: AccountState):
        self._state = state
        self.me_calls = 0

    def me(self) -> AccountState:
        self.me_calls += 1
        return self._state


class FakeExecutor:
    """Records the kwargs `_run_account` submits with, without any I/O."""

    def __init__(self, result: ExecutionResult, pending: dict[str, Any] | None = None):
        self._result = result
        self.submit_calls: list[dict[str, Any]] = []
        self._pending = pending
        self.finalized: list[str] = []

    def pending_intent(self):
        return self._pending

    def submit(self, **kwargs: Any) -> ExecutionResult:
        self.submit_calls.append(kwargs)
        return self._result

    def recover_pending(self) -> ExecutionResult | None:
        if not self._pending:
            return None
        return self._result

    def finalize(self, execution_request_id: str) -> None:
        self.finalized.append(execution_request_id)


def make_snapshot() -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://snapshot",
        payload={
            "timestamp": "2024-01-01T15:00:00+00:00",
            "snapshot_time": "2024-01-01T15:00:00+00:00",
            "expiration": "2024-01-01",
            "underlying_price": 400.0,
            "rows": [],
        },
        raw=b"{}",
    )


LONG_CALL = {
    "sym": "QQQ240101C00400000", "side": "buy", "qty": 1, "price": 1.0,
    "strike": 400.0, "type": "call", "exp": "2024-01-01",
    "instrument_type": "option", "multiplier": 100, "ts": "2024-01-01T10:00:00Z",
}


def make_runner(
    account: FakeAccount,
    *,
    state: AccountState,
    ledger_dir: Path,
    pending: dict[str, Any] | None = None,
    stub_recover: bool = True,
) -> tuple[Runner, FakeExecutor]:
    runner = object.__new__(Runner)  # skip __init__: no real HTTP sessions
    runner.accounts = [account]
    runner.dry_run = False
    runner.ledger = DecisionLedger(ledger_dir)
    runner.retired = set()
    runner._stop = None
    runner.quotes = type(
        "FakeQuotes", (), {
            "quotes": staticmethod(lambda symbols: {s: fresh_quote(s) for s in symbols}),
            "last_retry_note": None,
        },
    )()
    runner.sessions = {account.alias: FakeSession(state)}
    executor = FakeExecutor(
        ExecutionResult(outcome_class=Outcome.FILLED, execution_request_id="req-1"), pending=pending
    )
    runner.executors = {account.alias: executor}
    if stub_recover:
        runner._recover_pending = lambda acct: False
    return runner, executor


def fresh_quote(symbol: str) -> Quote:
    return Quote(symbol=symbol, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00", server_ts="2024-01-01T15:00:05")


def last_ledger_record(ledger_dir: Path) -> dict[str, Any]:
    import json

    files = sorted(ledger_dir.glob("decisions-*.jsonl"), key=lambda p: p.stat().st_mtime)
    lines = files[-1].read_text().strip().splitlines()
    return json.loads(lines[-1])


def scenario_flatten_close_attributed_to_eod_flatten() -> None:
    print("\n1. A flatten-produced close is attributed to eod_flatten in both the ledger and the submitted intent, not the account's own strategy_id")
    account = FakeAccount(alias="Newton", username="crassus_newton", strategy_id="momentum_qqq")
    state = AccountState(username="crassus_newton", balance_cash=50000.0, trades=[LONG_CALL])

    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp)
        runner, executor = make_runner(account, state=state, ledger_dir=ledger_dir)
        # 10 minutes to the close -- inside the mandatory 15-minute default window.
        now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)
        import crassus.clock as clock_module

        original_now_et = clock_module.now_et
        clock_module.now_et = lambda: now
        try:
            runner._run_account(account, make_snapshot(), "open")
        finally:
            clock_module.now_et = original_now_et

        check("the executor was called exactly once", len(executor.submit_calls) == 1)
        submitted = executor.submit_calls[0] if executor.submit_calls else {}
        check(
            "the submitted intent's strategy_id is eod_flatten, not momentum_qqq",
            submitted.get("strategy_id") == FLATTEN_STRATEGY_ID,
            submitted.get("strategy_id"),
        )

        record = last_ledger_record(ledger_dir)
        check(
            "the ledger record's top-level strategy_id is eod_flatten",
            record.get("strategy_id") == FLATTEN_STRATEGY_ID,
            record.get("strategy_id"),
        )
        check(
            "the ledger record still preserves the account's own assigned strategy separately",
            record.get("account_strategy_id") == "momentum_qqq",
            record.get("account_strategy_id"),
        )
        check("the outcome is filled", record.get("outcome_class") == Outcome.FILLED)


def scenario_flatten_runs_without_a_snapshot() -> None:
    print("\n2. A snapshot-service outage does not strand a held position inside the flatten window -- flatten still reconciles and closes it")
    account = FakeAccount(alias="Newton", username="crassus_newton", strategy_id="momentum_qqq")
    state = AccountState(username="crassus_newton", balance_cash=50000.0, trades=[LONG_CALL])

    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp)
        runner, executor = make_runner(account, state=state, ledger_dir=ledger_dir)
        session: FakeSession = runner.sessions[account.alias]
        now = datetime(2024, 1, 1, 15, 50, tzinfo=ET)  # 10 min to close -- inside the window
        import crassus.clock as clock_module

        original_now_et = clock_module.now_et
        clock_module.now_et = lambda: now
        try:
            runner._run_account(account, None, "open")  # snapshot=None: outage
        finally:
            clock_module.now_et = original_now_et

        check("the account was still reconciled via /api/me despite no snapshot", session.me_calls == 1)
        check("the executor was still called exactly once", len(executor.submit_calls) == 1)
        submitted = executor.submit_calls[0] if executor.submit_calls else {}
        check(
            "the submitted intent is attributed to eod_flatten",
            submitted.get("strategy_id") == FLATTEN_STRATEGY_ID,
            submitted.get("strategy_id"),
        )

        record = last_ledger_record(ledger_dir)
        check("the ledger record is not a runner_error", record.get("outcome_class") != Outcome.RUNNER_ERROR, record.get("outcome_class"))
        check(
            "the ledger record's strategy_id is eod_flatten",
            record.get("strategy_id") == FLATTEN_STRATEGY_ID,
            record.get("strategy_id"),
        )


def scenario_no_snapshot_no_flatten_still_errors() -> None:
    print("\n3. A snapshot-service outage with nothing for flatten to do still blocks ordinary strategy evaluation (which does need the snapshot)")
    account = FakeAccount(alias="Newton", username="crassus_newton", strategy_id="momentum_qqq")
    state = AccountState(username="crassus_newton", balance_cash=50000.0, trades=[])  # flat book

    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp)
        runner, executor = make_runner(account, state=state, ledger_dir=ledger_dir)
        now = datetime(2024, 1, 1, 12, 0, tzinfo=ET)  # well outside any flatten window
        import crassus.clock as clock_module

        original_now_et = clock_module.now_et
        clock_module.now_et = lambda: now
        try:
            runner._run_account(account, None, "open")
        finally:
            clock_module.now_et = original_now_et

        check("the executor was never called", len(executor.submit_calls) == 0)
        record = last_ledger_record(ledger_dir)
        check("the ledger record is a runner_error", record.get("outcome_class") == Outcome.RUNNER_ERROR, record.get("outcome_class"))


def scenario_account_strategy_id_survives_crash_recovery() -> None:
    print("\n4. account_strategy_id persisted on the intent is restored during crash recovery, not silently dropped")
    account = FakeAccount(alias="Newton", username="crassus_newton", strategy_id="momentum_qqq")
    state = AccountState(username="crassus_newton", balance_cash=50000.0, trades=[])

    pending_intent = {
        "execution_request_id": "req-crash-1",
        "symbol": "QQQ240101C00400000",
        "side": "sell",
        "quantity": 1,
        "decision_id": "dec-1",
        "strategy_id": FLATTEN_STRATEGY_ID,
        "strategy_version": "1.0.0",
        "account_strategy_id": "momentum_qqq",
        "reason": "End-of-day flatten",
        "decision": {},
        "market_snapshot_timestamp": None,
        "market_snapshot_url_or_hash": None,
        "account_state_before": None,
    }

    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp)
        runner, executor = make_runner(
            account, state=state, ledger_dir=ledger_dir, pending=pending_intent, stub_recover=False
        )
        handled = runner._recover_pending(account)
        check("a pending intent was found and handled", handled)

        record = last_ledger_record(ledger_dir)
        check(
            "the recovered record's strategy_id is eod_flatten",
            record.get("strategy_id") == FLATTEN_STRATEGY_ID,
            record.get("strategy_id"),
        )
        check(
            "the recovered record preserves account_strategy_id from the persisted intent",
            record.get("account_strategy_id") == "momentum_qqq",
            record.get("account_strategy_id"),
        )


def main() -> int:
    for scenario in (
        scenario_flatten_close_attributed_to_eod_flatten,
        scenario_flatten_runs_without_a_snapshot,
        scenario_no_snapshot_no_flatten_still_errors,
        scenario_account_strategy_id_survives_crash_recovery,
    ):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
