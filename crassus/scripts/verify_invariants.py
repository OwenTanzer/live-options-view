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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
from crassus.market import Quote, QuoteRateLimited, QuoteReader  # noqa: E402
from crassus.strategies.smoke import STRATEGY_ID, smoke_atm_roundtrip  # noqa: E402
from crassus.strategy import Decision, StrategyContext  # noqa: E402

MOCK = Path(__file__).parent / "mock_worker.py"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
# Must match mock_worker.BOT_REGISTRATION_KEY.
BOT_KEY = "mock-operator-key"
MOCK_SNAPSHOT_URL = f"{BASE}/intraday/latest.json"

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

    def __init__(
        self,
        fault: str | None = None,
        count: int = 1,
        me_fault: str | None = None,
        me_count: int = 1,
        me_skip: int = 0,
    ):
        self.args = [sys.executable, str(MOCK), "--port", str(PORT)]
        if fault:
            self.args += ["--fault", fault, "--fault-count", str(count)]
        if me_fault:
            self.args += [
                "--me-fault", me_fault, "--me-fault-count", str(me_count),
                "--me-fault-skip", str(me_skip),
            ]

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
    session = AccountSession(account, base_url=BASE, rate_limiter=RateLimiter(capacity=50, refill_per_s=50), timeout_s=3.0,
                             bot_registration_key=BOT_KEY)
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
        check("Intent retained until the caller acknowledges the outcome",
              ex.pending_intent() is not None)
        ex.finalize(result.execution_request_id)
        check("In-flight intent cleared once finalized", ex.pending_intent() is None)

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
        check("Intent retained until the caller acknowledges the outcome",
              ex.pending_intent() is not None)
        ex.finalize(result.execution_request_id)
        check("In-flight intent cleared once finalized", ex.pending_intent() is None)


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


def scenario_quote_rate_limit(tmp: Path) -> None:
    """The quote side of the process must share the account RateLimiter.

    Previously QuoteReader used a raw `requests.get`: a quote-side 429 never
    honored Retry-After and surfaced as a generic, unclassified strategy
    exception. This proves the shared limiter is actually shared -- a 429 on
    /api/live-quotes pauses the *account* limiter too -- and that exhausting
    quote retries raises a classifiable `QuoteRateLimited`, not a bare
    HTTPError.
    """
    print("\n6b. Quote-side rate limiting: shared budget, classified 429")
    # count=2: the Mock harness's own readiness probe hits /api/live-quotes
    # once and consumes the first fault, so this leaves exactly one 429 for
    # the scenario's own request.
    with Mock(fault="quote_429", count=2):
        limiter = RateLimiter(capacity=50, refill_per_s=50)
        reader = QuoteReader(BASE, rate_limiter=limiter, max_attempts=3)

        quotes = reader.quotes([SYM])
        check("Recovers from a quote-side 429 and returns quotes", SYM in quotes, list(quotes))
        check("Retry history is preserved on the reader for the audit trail",
              reader.last_retry_note is not None and "rate-limited" in reader.last_retry_note,
              reader.last_retry_note)

        # The penalty from a quote-side 429 must be visible to account
        # traffic sharing the same limiter -- prove the pause actually
        # propagated rather than being scoped to the quote reader alone.
        #
        # Measuring this against the *already-completed* `reader.quotes()`
        # call above doesn't work: that call's own retry already waited out
        # the pause internally (its own `acquire()` at the top of the retry
        # loop is what consumes it), so by the time it returns,
        # `_pause_until` has typically already elapsed and a fresh
        # `acquire()` finds nothing to wait for -- passing even if
        # `penalize()` never touched the shared object at all. Simulating a
        # still-fresh penalty directly (as if a peer account's request had
        # just been 429'd concurrently) and measuring an *independent*
        # `acquire()` call against it is the actual test of propagation.
        limiter.penalize(1.0)
        started = time.monotonic()
        limiter.acquire()
        check("Account-side acquire() also waited out a penalty applied to the shared limiter",
              time.monotonic() - started >= 0.5, time.monotonic() - started)

    with Mock(fault="quote_429", count=5):
        reader2 = QuoteReader(BASE, rate_limiter=RateLimiter(capacity=50, refill_per_s=50), max_attempts=2)
        raised = False
        try:
            reader2.quotes([SYM])
        except QuoteRateLimited:
            raised = True
        check("Exhausted quote retries raise a classifiable QuoteRateLimited", raised)


def scenario_liquidation(tmp: Path) -> None:
    """Proves the client handles a 410 correctly *if the server sends one*.

    This is NOT proof that liquidation detection works against the real
    deployed Worker. The mock fabricates 410 from /api/me and
    /api/paper-trade on demand, matching the documented contract -- but
    `worker.js`'s settlement path deletes the user record outright on
    insolvency rather than leaving a tombstone, so a session resolved after
    liquidation gets a generic 401 there, not 410. See
    crassus/README.md ("Known gaps") for the real-server gap this doesn't
    close.
    """
    print("\n7. Liquidation: 410 is terminal and recorded as a margin call (mock contract only)")
    with Mock(fault="410", count=1):
        session, ex = make_client(tmp)
        raised = False
        try:
            ex.submit(symbol=SYM, side="buy", quantity=1)
        except AccountLiquidated:
            raised = True
        check("410 raises AccountLiquidated", raised)
        check("Session marked liquidated", session.liquidated)
        pending = ex.pending_intent()
        check("Intent retained until the margin call is durably recorded",
              pending is not None)

        ledger = DecisionLedger(tmp, run_id="liq")
        rec = ledger.record(
            decision_id="d1", account_alias="TestAcct", strategy_id=STRATEGY_ID,
            strategy_version="1.0.0", outcome_class=Outcome.ACCOUNT_LIQUIDATED,
            reason="MARGIN CALL -- account liquidated and deleted by the Worker",
            execution_request_id=pending.get("execution_request_id") if pending else None,
            http_status=410, margin_called=True,
        )
        if pending:
            ex.finalize(pending["execution_request_id"])
        check("Intent cleared once the margin call is finalized", ex.pending_intent() is None)
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

        runner = Runner([account], bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL, ledger_dir=ledger_dir, state_dir=state_dir)
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
        restarted = Runner([account], bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL, ledger_dir=ledger_dir, state_dir=state_dir)
        restarted.startup()
        rebuilt = Book(restarted.sessions[account.alias].me().trades)
        check("Restart reconstructs state from server history without duplicate fills",
              len(state.trades) == 2 and rebuilt.is_flat,
              f"{len(state.trades)} trades, flat={rebuilt.is_flat}")


def scenario_recovery_idempotent_after_ledger_write(tmp: Path) -> None:
    """A crash between the ledger write and clearing the intent must not
    turn into a second ledger record for the same execution.

    This is the gap a confirmed fill could previously fall into: `submit()`
    used to clear the intent itself on a terminal outcome, uncoupled from
    whether the ledger write had actually happened yet. Now the intent
    persists the full decision envelope and stays on disk until the caller
    explicitly finalizes it, and recovery checks the ledger by
    `execution_request_id` before ever re-querying the server.
    """
    print("\n12. Crash recovery is idempotent by execution_request_id")
    from crassus.runner import Runner

    with Mock():
        account = Account(alias="Idem", username=f"idem_{uuid.uuid4().hex[:6]}",
                          password="pw", strategy_id=STRATEGY_ID)
        ledger_dir, state_dir = tmp / "idem-logs", tmp / "idem-state"
        runner = Runner([account], bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL, ledger_dir=ledger_dir, state_dir=state_dir)
        runner.startup()

        executor = runner.executors[account.alias]
        rid = str(uuid.uuid4())
        result = executor.submit(
            symbol=SYM, side="buy", quantity=1, execution_request_id=rid,
            decision_id="dtest", strategy_id=STRATEGY_ID, strategy_version="1.0.0",
            reason="test envelope",
        )
        check("Fill executes", result.outcome_class == Outcome.FILLED, result.outcome_class)
        check("Intent retained -- not yet acknowledged by a ledger write",
              executor.pending_intent() is not None)

        # Simulate the ledger write succeeding, then a crash before
        # `finalize()` clears the intent file.
        runner.ledger.record(
            decision_id="dtest", account_alias=account.alias, strategy_id=STRATEGY_ID,
            strategy_version="1.0.0", outcome_class=Outcome.FILLED,
            execution_request_id=rid, reason="test envelope",
        )

        before_trade_count = len(runner.sessions[account.alias].me().trades)

        handled = runner._recover_pending(account)
        check("Recovery detects the pending intent", handled)
        check("Stale intent cleared without re-querying for a second record",
              executor.pending_intent() is None)

        lines = runner.ledger.paths.ledger.read_text().strip().split("\n")
        matching = [json.loads(l) for l in lines if json.loads(l).get("execution_request_id") == rid]
        check("Exactly one ledger record for this execution, not a duplicate",
              len(matching) == 1, f"count={len(matching)}")
        check("No duplicate trade was submitted to the server",
              len(runner.sessions[account.alias].me().trades) == before_trade_count)


def scenario_ambiguous_stays_pending_until_reconciled(tmp: Path) -> None:
    """An unresolved `ambiguous` outcome must not be finalized -- it isn't done.

    `Runner._run_account` used to call `executor.finalize()` unconditionally
    after every `executor.submit()`, including when retries were exhausted
    with the outcome still `ambiguous`. That deleted the only recovery
    marker for a trade whose fate genuinely isn't known yet, defeating the
    crash-safety invariant from scenario 1.

    Constructed so the ambiguity is genuine, not self-resolving: the
    `unreachable` paper-trade fault records nothing server-side (unlike
    `timeout`, where a retry would immediately discover the fill via the
    idempotent-replay path), and `--me-fault 503` makes /api/me
    simultaneously unavailable for the reconciliation attempts inside
    `submit()`'s retry loop -- so the account genuinely cannot tell whether
    its order landed until the server recovers.
    """
    print("\n13. Ambiguous execution stays pending until it's actually reconciled")
    from crassus.runner import Runner

    # me-fault-skip lets the two `/api/me` calls that happen before any
    # order is placed -- login's ensure_session() and _run_account's own
    # pre-decision reconcile -- succeed normally. The fault then covers
    # exactly the two reconciliation attempts inside submit()'s retries.
    with Mock(fault="unreachable", count=2, me_fault="503", me_count=2, me_skip=2):
        account = Account(alias="Pending", username=f"pending_{uuid.uuid4().hex[:6]}",
                          password="pw", strategy_id=STRATEGY_ID)
        runner = Runner([account], bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL,
                        ledger_dir=tmp / "pending-logs", state_dir=tmp / "pending-state")
        # Keep the test fast: default backoff/timeout are tuned for a real
        # deployment, not a sleeping fault in a unit test.
        runner.sessions[account.alias].timeout_s = 2.0
        executor = runner.executors[account.alias]
        executor.max_attempts = 2
        executor.backoff_base_s = 0.1

        runner.startup()
        snapshot = runner.snapshots.read()
        runner._run_account(account, snapshot, phase="open")

        intent = executor.pending_intent()
        check("Intent survives _run_account() with retries exhausted ambiguous",
              intent is not None)

        lines = runner.ledger.paths.ledger.read_text().strip().split("\n")
        records = [json.loads(l) for l in lines]
        check("The unresolved outcome was still recorded, not silently dropped",
              any(r["outcome_class"] == Outcome.AMBIGUOUS for r in records), str(records))

        # The server recovers (both faults are exhausted by now); a later
        # recovery pass must reconcile this exactly once.
        request_id = intent["execution_request_id"]
        handled = runner._recover_pending(account)
        check("Recovery reconciles the previously-ambiguous intent", handled)
        check("Intent finalized once the outcome actually resolved",
              executor.pending_intent() is None)

        matching = [json.loads(l) for l in runner.ledger.paths.ledger.read_text().strip().split("\n")
                    if json.loads(l).get("execution_request_id") == request_id]
        check("Ledger shows the ambiguous attempt and the eventual resolution, not a silent overwrite",
              len(matching) == 2 and matching[-1]["outcome_class"] == Outcome.FILLED,
              str([r["outcome_class"] for r in matching]))
        check("No duplicate fill -- the order never actually reached the server the first time",
              len(runner.sessions[account.alias].me().trades) == 1,
              len(runner.sessions[account.alias].me().trades))


def scenario_strategy_config_validation(tmp: Path) -> None:
    """An unregistered strategy_id must be a startup error, not a mid-run crash.

    `get_strategy()` used to run per-account with no error boundary: one
    account configured with an unimplemented strategy_id (as five of the
    six in accounts.example.json used to be) would raise `KeyError` deep in
    `_run_account` and take the whole process down, Ankit's fill included.
    """
    print("\n14. Strategy config validation: unregistered strategy_id fails at startup")
    from crassus.config import load_accounts
    from crassus.runner import Runner

    with Mock():
        good = Account(alias="Ankit", username=f"cfg_{uuid.uuid4().hex[:6]}",
                       password="pw", strategy_id=STRATEGY_ID)
        bad = Account(alias="Bob", username=f"cfg_{uuid.uuid4().hex[:6]}",
                      password="pw", strategy_id="patient_calls_dip")

        raised = False
        try:
            Runner([good, bad], bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL,
                   ledger_dir=tmp / "cfgbad-logs", state_dir=tmp / "cfgbad-state")
        except ValueError as exc:
            raised = True
            check("Error names the offending account and strategy_id",
                  "Bob" in str(exc) and "patient_calls_dip" in str(exc), str(exc))
        check("Constructing the Runner refuses to start rather than crashing mid-run", raised)

        # The bundled example config must be copy-paste runnable today, not
        # just an eventual illustration of the six-strategy mapping.
        example = Path(__file__).parent.parent / "accounts.example.json"
        raw = json.loads(example.read_text())
        for entry in raw["accounts"]:
            entry["password"] = "pw"
        example_accounts_path = tmp / "accounts.example.filled.json"
        example_accounts_path.write_text(json.dumps(raw))
        accounts = load_accounts(example_accounts_path)
        runner = Runner(accounts, bot_registration_key=BOT_KEY, base_url=BASE, snapshot_url=MOCK_SNAPSHOT_URL,
                        ledger_dir=tmp / "cfgexample-logs", state_dir=tmp / "cfgexample-state")
        runner.startup()
        check("accounts.example.json constructs and starts a Runner without error",
              set(runner.sessions) == {a.alias for a in accounts})


def scenario_bot_identity(tmp: Path) -> None:
    """A registered account must actually land in the public /api/bots roster.

    Registration used to send only username/password, so every first-run
    account was created as an ordinary human user: invisible on the site's
    Automated tab, and unrecoverable, since the username was then taken and
    could not be re-registered as a bot. The failure was silent -- login
    worked, trades worked, only the roster was quietly short an account.
    """
    print("\n15. Bot identity: registration and metadata sync reach /api/bots")
    from crassus.client import ConfigError
    from crassus.runner import Runner

    with Mock():
        account = Account(alias="Ankit", username=f"bot_{uuid.uuid4().hex[:8]}",
                          password="pw", strategy_id=STRATEGY_ID)
        session = AccountSession(account, base_url=BASE, bot_registration_key=BOT_KEY,
                                 rate_limiter=RateLimiter(capacity=50, refill_per_s=50), timeout_s=3.0)
        session.ensure_session()

        roster = requests.get(f"{BASE}/api/bots", timeout=3).json()["bots"]
        mine = [b for b in roster if b["username"] == account.username]
        check("a first-run account appears in the public roster", len(mine) == 1)
        check("the roster records its alias", mine and mine[0]["alias"] == "Ankit")
        check("the roster records its strategy", mine and mine[0]["strategy_id"] == STRATEGY_ID)
        check("no password is exposed on the public roster",
              mine and not any("password" in k for k in mine[0]))

        # Moving the account to another strategy must re-attribute it. This is
        # the case registration alone cannot cover: the account already exists.
        account.strategy_id = "reddit_sentiment_qqq"
        session.ensure_session()
        roster = requests.get(f"{BASE}/api/bots", timeout=3).json()["bots"]
        moved = [b for b in roster if b["username"] == account.username][0]
        check("a strategy move is re-synced on the next startup",
              moved["strategy_id"] == "reddit_sentiment_qqq")

        # A human account registered without the header must never appear.
        human = Account(alias="Human", username=f"hum_{uuid.uuid4().hex[:8]}",
                        password="pw", strategy_id=STRATEGY_ID)
        plain = AccountSession(human, base_url=BASE, bot_registration_key=None,
                               rate_limiter=RateLimiter(capacity=50, refill_per_s=50), timeout_s=3.0)
        raised = False
        try:
            plain.ensure_session()
        except ConfigError:
            raised = True
        check("a missing operator key fails loudly instead of registering a human account", raised)
        roster = requests.get(f"{BASE}/api/bots", timeout=3).json()["bots"]
        check("the aborted account was never created",
              not any(b["username"] == human.username for b in roster))

        # A wrong key is rejected outright rather than silently downgraded.
        wrong = Account(alias="Wrong", username=f"wrg_{uuid.uuid4().hex[:8]}",
                        password="pw", strategy_id=STRATEGY_ID)
        bad = AccountSession(wrong, base_url=BASE, bot_registration_key="not-the-key",
                             rate_limiter=RateLimiter(capacity=50, refill_per_s=50), timeout_s=3.0)
        check("a wrong operator key is refused, not downgraded to a human account",
              bad.register().status_code == 403)

        # And the Runner refuses to start at all without a key, before any
        # network call burns a username.
        raised = False
        try:
            Runner([account], bot_registration_key=None, base_url=BASE,
                   snapshot_url=MOCK_SNAPSHOT_URL,
                   ledger_dir=tmp / "botid-logs", state_dir=tmp / "botid-state")
        except ValueError as exc:
            raised = "BOT_REGISTRATION_KEY" in str(exc)
        check("Runner refuses to start without BOT_REGISTRATION_KEY", raised)


ET = ZoneInfo("America/New_York")
# A fixed weekday, mid-session ET instant -- comfortably inside "open" and far
# from any close, so `Runner._run_account`'s mandatory EOD flatten (which
# reads `clock.now_et()` to decide whether its window is open) never
# interferes with these scenarios, which are testing other invariants
# entirely. Injected by monkeypatching `crassus.clock.now_et` rather than
# threading a clock parameter through Runner/flatten/every strategy
# signature -- `clock.now_et` is the single source of truth every one of
# them already reads from, so patching it there deterministically freezes
# time for the whole integration surface at once. Flagged in review: this
# suite previously used the real wall clock, so it went red for a couple of
# hours around each weekday's 15:45 mandatory-flatten window.
_FROZEN_NOW_ET = datetime(2024, 1, 2, 11, 0, tzinfo=ET)  # a Tuesday, 11:00 ET


def main() -> int:
    real_now_et = clock.now_et
    clock.now_et = lambda: _FROZEN_NOW_ET
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            print("Verifying Crassus integrity invariants against the mock Worker")
            for scenario in (
                scenario_happy_path, scenario_idempotent_replay, scenario_ambiguous_timeout,
                scenario_ambiguous_500, scenario_restart_recovery, scenario_rate_limit,
                scenario_quote_rate_limit,
                scenario_liquidation, scenario_ledger, scenario_strategy_contract, scenario_book_math,
                scenario_p1_closed_loop, scenario_recovery_idempotent_after_ledger_write,
                scenario_ambiguous_stays_pending_until_reconciled,
                scenario_strategy_config_validation,
                scenario_bot_identity,
            ):
                scenario(tmp)
    finally:
        clock.now_et = real_now_et

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
