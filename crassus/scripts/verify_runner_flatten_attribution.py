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

    def me(self) -> AccountState:
        return self._state


class FakeExecutor:
    """Records the kwargs `_run_account` submits with, without any I/O."""

    def __init__(self, result: ExecutionResult):
        self._result = result
        self.submit_calls: list[dict[str, Any]] = []

    def pending_intent(self):
        return None

    def submit(self, **kwargs: Any) -> ExecutionResult:
        self.submit_calls.append(kwargs)
        return self._result

    def finalize(self, execution_request_id: str) -> None:
        pass


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


def make_runner(account: FakeAccount, *, state: AccountState, ledger_dir: Path) -> tuple[Runner, FakeExecutor]:
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
    executor = FakeExecutor(ExecutionResult(outcome_class=Outcome.FILLED, execution_request_id="req-1"))
    runner.executors = {account.alias: executor}
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


def main() -> int:
    for scenario in (scenario_flatten_close_attributed_to_eod_flatten,):
        scenario()

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
