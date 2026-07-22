#!/usr/bin/env python3
"""Prove the integrity invariants against the mock Worker.

Each scenario targets one mandatory invariant from the MOO-24 contract. The
point is not coverage for its own sake -- it is that "the bot recovers
correctly from an ambiguous execution" is a claim that has to be demonstrated
before P1 can be called done, and the deployed Worker cannot demonstrate it.

    python scripts/verify_invariants.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from crassus import clock  # noqa: E402
from crassus.audit import MANDATORY_FIELDS, DecisionLedger, Outcome  # noqa: E402
from crassus.client import (  # noqa: E402
    AccountLiquidated,
    AccountSession,
    Book,
    ExecutionClient,
    RateLimiter,
)
from crassus.config import Account  # noqa: E402
from crassus.market import Quote  # noqa: E402
from crassus.strategies.smoke import STRATEGY_ID, smoke_atm_roundtrip  # noqa: E402
from crassus.strategy import Decision, StrategyContext  # noqa: E402

MOCK = Path(__file__).parent / "mock_worker.py"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [✓] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [✗] {name}" + (f" -- {detail}" if detail else ""))


class Mock:
    """Runs the mock Worker for the duration of one scenario."""

    def __init__(self, fault: str | None = None, count: int = 1):
        self.args = [sys.executable, str(MOCK), "--port", str(PORT)]
        if fault:
            self.args += ["--fault", fault, "--fault-count", str(count)]

    def __enter__(self):
        self.proc = subprocess.Popen(self.args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):  # poll until listening rather than sleeping blind
            try:
                urllib.request.urlopen(f"{BASE}/api/live-quotes?symbols=X", timeout=0.5).read()
                return self
            except urllib.error.HTTPError:
                return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("mock worker did not come up")

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=5)


def make_client(tmp: Path, alias: str = "TestAcct") -> tuple[AccountSession, ExecutionClient]:
    account = Account(alias=alias, username=f"u_{uuid.uuid4().hex[:8]}", password="pw", strategy_id=STRATEGY_ID)
    session = AccountSession(account, base_url=BASE, rate_limiter=RateLimiter(capacity=50, refill_per_s=50), timeout_s=3.0)
    session.ensure_session()
    return session, ExecutionClient(session, tmp, max_attempts=2, backoff_base_s=0.1)


SYM = "QQQ260722C00702000"


# ---------------------------------------------------------------------------


def scenario_happy_path(tmp: Path) -> None:
    print("\n1. Closed loop: submit, fill, reconcile")
    with Mock():
        session, ex = make_client(tmp)
        before = session.me()
        result = ex.submit(symbol=SYM, side="buy", quantity=1)
        check("Fill classified as `filled`", result.outcome_class == Outcome.FILLED, result.outcome_class)
        check("In-flight intent cleared after confirmed fill", ex.pending_intent() is None)

        after = session.me()
        check("Trade visible in /api/me", len(after.trades) == len(before.trades) + 1)
        check(
            "balance_cash moved by the cash cost, not by P&L",
            abs((before.balance_cash - after.balance_cash) - 834.0) < 0.01,
            f"{before.balance_cash} -> {after.balance_cash}",
        )
        book = Book(after.trades)
        check("Book derives the long position from server history", book.position(SYM).quantity == 1)


def scenario_idempotent_replay(tmp: Path) -> None:
    print("\n2. Idempotency: the same request id must not double the position")
    with Mock():
        session, ex = make_client(tmp)
        rid = str(uuid.uuid4())
        ex.submit(symbol=SYM, side="buy", quantity=1, execution_request_id=rid)
        ex.submit(symbol=SYM, side="buy", quantity=1, execution_request_id=rid)
        book = Book(session.me().trades)
        check("Replayed id yields one position, not two", book.position(SYM).quantity == 1,
              f"qty={book.position(SYM).quantity}")


def scenario_ambiguous_timeout(tmp: Path) -> None:
    print("\n3. Ambiguous execution: timeout after the server recorded the fill")
    with Mock(fault="timeout", count=1):
        session, ex = make_client(tmp)
        result = ex.submit(symbol=SYM, side="buy", quantity=1)
        check(
            "Timeout resolved by querying /api/me, not by blind retry",
            result.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY,
            result.outcome_class,
        )
        book = Book(session.me().trades)
        check("No duplicate fill after recovery", book.position(SYM).quantity == 1,
              f"qty={book.position(SYM).quantity}")
        check("In-flight intent cleared once reconciled", ex.pending_intent() is None)


def scenario_ambiguous_500(tmp: Path) -> None:
    print("\n4. Ambiguous execution: 5xx after the server recorded the fill")
    with Mock(fault="500", count=1):
        session, ex = make_client(tmp)
        result = ex.submit(symbol=SYM, side="buy", quantity=1)
        check("5xx reconciled against /api/me", result.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY,
              result.outcome_class)
        check("No duplicate fill", Book(session.me().trades).position(SYM).quantity == 1)


def scenario_restart_recovery(tmp: Path) -> None:
    print("\n5. Restart recovery: intent persisted, process dies, restart reconciles")
    with Mock(fault="timeout", count=1):
        session, ex = make_client(tmp)
        rid = str(uuid.uuid4())

        # Persist the intent exactly as submit() would, then abandon it --
        # standing in for a crash between the disk write and the response.
        ex._persist_intent(
            {"execution_request_id": rid, "symbol": SYM, "side": "buy", "quantity": 1,
             "decision_id": None, "created_utc": clock.iso_utc()}
        )
        check("Intent is on disk before any request is sent", ex.pending_intent() is not None)

        # The trade lands server-side, but the fault means the client never
        # hears back -- so the timeout here is the expected outcome, not a
        # failure. This is precisely the state a crash leaves behind.
        try:
            session.http.post(f"{BASE}/api/paper-trade",
                              json={"execution_request_id": rid, "sym": SYM, "side": "buy", "qty": 1},
                              timeout=2)
        except requests.exceptions.ReadTimeout:
            pass

        # Fresh client over the same account, as a restart would produce.
        ex2 = ExecutionClient(session, tmp, max_attempts=2, backoff_base_s=0.1)
        recovered = ex2.recover_pending()
        check("Restart finds the pending intent", recovered is not None)
        check("Existing trade treated as authoritative, not resubmitted",
              recovered and recovered.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY,
              recovered.outcome_class if recovered else "none")
        check("Position still 1 after restart", Book(session.me().trades).position(SYM).quantity == 1)


def scenario_rate_limit(tmp: Path) -> None:
    print("\n6. Rate limiting: 429 is retried, and Retry-After is honored")
    with Mock(fault="429", count=1):
        session, ex = make_client(tmp)
        started = time.monotonic()
        result = ex.submit(symbol=SYM, side="buy", quantity=1)
        elapsed = time.monotonic() - started
        check("Recovered from 429 and filled", result.outcome_class == Outcome.FILLED, result.outcome_class)
        check("Backed off before retrying", elapsed >= 0.1, f"{elapsed:.2f}s")


def scenario_liquidation(tmp: Path) -> None:
    print("\n7. Liquidation: 410 is terminal and recorded as a margin call")
    with Mock(fault="410", count=1):
        session, ex = make_client(tmp)
        raised = False
        try:
            ex.submit(symbol=SYM, side="buy", quantity=1)
        except AccountLiquidated:
            raised = True
        check("410 raises AccountLiquidated", raised)
        check("Session marked liquidated", session.liquidated)

        ledger = DecisionLedger(tmp, run_id="liq")
        rec = ledger.record(
            decision_id="d1", account_alias="TestAcct", strategy_id=STRATEGY_ID,
            strategy_version="1.0.0", outcome_class=Outcome.ACCOUNT_LIQUIDATED,
            reason="MARGIN CALL -- account liquidated and deleted by the Worker",
            http_status=410, margin_called=True,
        )
        check("Audit record carries an explicit margin-call annotation",
              rec["margin_called"] is True and "MARGIN CALL" in rec["reason"])


def scenario_ledger(tmp: Path) -> None:
    print("\n8. Decision ledger: schema completeness and durability")
    ledger = DecisionLedger(tmp, run_id="ledgertest")
    rec = ledger.record(
        decision_id="d1", account_alias="Bob", strategy_id=STRATEGY_ID, strategy_version="1.0.0",
        outcome_class=Outcome.NO_TRADE, reason="market premarket",
    )
    check("All 19 mandatory fields present", all(f in rec for f in MANDATORY_FIELDS))

    lines = ledger.paths.ledger.read_text().strip().split("\n")
    check("Record is on disk immediately (append-only JSONL)", len(lines) == 1)
    check("Line is valid JSON", json.loads(lines[0])["decision_id"] == "d1")

    rejected = False
    try:
        ledger.record(decision_id="d2", account_alias="Bob", strategy_id="s",
                      strategy_version="1", outcome_class="not_a_real_class")
    except ValueError:
        rejected = True
    check("Unknown outcome_class is rejected", rejected)

    # no_trade must be recorded, not filtered out -- evaluation needs it.
    ledger.record(decision_id="d3", account_alias="Bob", strategy_id=STRATEGY_ID,
                  strategy_version="1.0.0", outcome_class=Outcome.NO_TRADE, reason="declined")
    check("no_trade decisions are logged alongside fills",
          len(ledger.paths.ledger.read_text().strip().split("\n")) == 2)


def scenario_strategy_contract(tmp: Path) -> None:
    print("\n9. Strategy contract: validation and the smoke round trip")
    for bad, why in [
        (dict(action="frobnicate", reason="r", strategy_id="s", strategy_version="1"), "unknown action"),
        (dict(action="buy", reason="r", strategy_id="s", strategy_version="1"), "buy without symbol"),
        (dict(action="buy", symbol=SYM, quantity=0, reason="r", strategy_id="s", strategy_version="1"), "non-positive qty"),
        (dict(action="no_trade", symbol=SYM, reason="r", strategy_id="s", strategy_version="1"), "no_trade with symbol"),
        (dict(action="buy", symbol=SYM, quantity=1, reason="", strategy_id="s", strategy_version="1"), "empty reason"),
    ]:
        rejected = False
        try:
            Decision(**bad)
        except ValueError:
            rejected = True
        check(f"Rejects {why}", rejected)

    class FakeSnapshot:
        underlying_price = 702.0
        timestamp = clock.iso_utc()
        provenance = "test"

        def atm(self, t):
            return {"OptionSymbol": SYM, "Strike": 702.0, "Type": t, "Bid": 8.2, "Ask": 8.34}

    fresh = {SYM: Quote(SYM, 8.2, 8.34, quote_ts=clock.iso_utc(), server_ts=clock.iso_utc())}

    def ctx_for(trades):
        return StrategyContext(
            snapshot=FakeSnapshot(), account_state={}, book=Book(trades),
            now_et=clock.now_et(), session_phase="open", quotes=lambda syms: fresh,
        )

    flat = smoke_atm_roundtrip(ctx_for([]))
    check("Flat -> buys one contract", flat.action == "buy" and flat.quantity == 1, flat.action)

    held = [{"sym": SYM, "side": "buy", "qty": 1, "price": 8.34}]
    long_ = smoke_atm_roundtrip(ctx_for(held))
    check("Long -> sells to close, completing the round trip", long_.action == "sell", long_.action)

    stale = {SYM: Quote(SYM, 8.2, 8.34, quote_ts="2026-07-22T12:00:00+00:00", server_ts="2026-07-22T13:00:00+00:00")}
    ctx = StrategyContext(snapshot=FakeSnapshot(), account_state={}, book=Book([]), now_et=clock.now_et(),
                          session_phase="open", quotes=lambda syms: stale)
    check("Stale quote -> declines instead of buying a certain 409",
          smoke_atm_roundtrip(ctx).action == "no_trade")

    ctx_closed = StrategyContext(snapshot=FakeSnapshot(), account_state={}, book=Book([]), now_et=clock.now_et(),
                                 session_phase="premarket", quotes=lambda syms: fresh)
    check("Outside market hours -> declines", smoke_atm_roundtrip(ctx_closed).action == "no_trade")


def scenario_book_math(tmp: Path) -> None:
    print("\n10. Book: average cost derived from trade history")
    book = Book([
        {"sym": SYM, "side": "buy", "qty": 2, "price": 10.0},
        {"sym": SYM, "side": "buy", "qty": 2, "price": 12.0},
    ])
    pos = book.position(SYM)
    check("Averages cost across adds", pos.quantity == 4 and abs(pos.average_cost - 11.0) < 1e-9,
          f"qty={pos.quantity} avg={pos.average_cost}")

    closed = Book([
        {"sym": SYM, "side": "buy", "qty": 1, "price": 10.0},
        {"sym": SYM, "side": "sell", "qty": 1, "price": 12.0},
    ])
    check("Round trip leaves the book flat", closed.is_flat, closed.summary())
    check("Unparseable trades are skipped, not fatal", Book([{"garbage": True}]).is_flat)


def scenario_p1_closed_loop(tmp: Path) -> None:
    """The P1 exit criterion, driven through the real Runner.

    Market hours are supplied as an argument rather than read from the clock,
    so the loop can be exercised outside 9:30-16:00 ET without adding a
    test-only branch to the runtime. Everything else is the production path.
    """
    print("\n11. P1 exit criterion: full loop through the real Runner")
    from crassus.runner import Runner

    with Mock():
        account = Account(alias="Ankit", username=f"ankit_{uuid.uuid4().hex[:6]}",
                          password="pw", strategy_id=STRATEGY_ID)
        ledger_dir, state_dir = tmp / "p1-logs", tmp / "p1-state"

        runner = Runner([account], base_url=BASE, ledger_dir=ledger_dir, state_dir=state_dir)
        runner.startup()
        snapshot = runner.snapshots.read()

        runner._run_account(account, snapshot, phase="open")   # opens
        runner._run_account(account, snapshot, phase="open")   # closes

        records = [json.loads(l) for l in runner.ledger.paths.ledger.read_text().strip().split("\n")]
        actions = [r["decision"]["action"] for r in records if r.get("decision")]
        check("Two cycles produce a buy then a sell", actions == ["buy", "sell"], str(actions))
        check("Both cycles filled", [r["outcome_class"] for r in records] == ["filled", "filled"],
              str([r["outcome_class"] for r in records]))
        check("Every record carries full snapshot provenance",
              all(r["market_snapshot_url_or_hash"] and "sha256:" in r["market_snapshot_url_or_hash"] for r in records))
        check("Every record carries before/after account state",
              all(r["account_state_before"] and r["account_state_after"] for r in records))

        state = runner.sessions[account.alias].me()
        check("Round trip leaves the book flat", Book(state.trades).is_flat,
              str(Book(state.trades).summary()))

        # Restart: a fresh Runner over the same state and account.
        restarted = Runner([account], base_url=BASE, ledger_dir=ledger_dir, state_dir=state_dir)
        restarted.startup()
        rebuilt = Book(restarted.sessions[account.alias].me().trades)
        check("Restart reconstructs state from server history without duplicate fills",
              len(state.trades) == 2 and rebuilt.is_flat,
              f"{len(state.trades)} trades, flat={rebuilt.is_flat}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("Verifying Crassus integrity invariants against the mock Worker")
        for scenario in (
            scenario_happy_path, scenario_idempotent_replay, scenario_ambiguous_timeout,
            scenario_ambiguous_500, scenario_restart_recovery, scenario_rate_limit,
            scenario_liquidation, scenario_ledger, scenario_strategy_contract, scenario_book_math,
            scenario_p1_closed_loop,
        ):
            scenario(tmp)

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
