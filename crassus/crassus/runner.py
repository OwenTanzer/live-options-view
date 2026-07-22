"""The loop.

Single process, Eastern-Time clock, stoppable by hand. On every cycle each
account observes the market, asks its strategy for an intent, executes it if
there is one, reconciles against the server, and writes exactly one audit
record -- including when it decided to do nothing, and including when it
failed.

Accounts are processed sequentially and share one global rate limiter, since
the limit that binds is per-IP rather than per-account.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import clock, strategies  # noqa: F401  (import registers strategies)
from .audit import DecisionLedger, Outcome
from .client import (
    AccountLiquidated,
    AccountSession,
    Book,
    CrassusError,
    ExecutionClient,
    RateLimiter,
)
from .config import (
    BASE_URL,
    DEFAULT_LEDGER_DIR,
    DEFAULT_STATE_DIR,
    SNAPSHOT_URL,
    load_accounts,
)
from .market import QuoteReader, SnapshotReader
from .strategy import Decision, StrategyContext, get as get_strategy

log = logging.getLogger("crassus")


class Runner:
    def __init__(
        self,
        accounts: list[Any],
        *,
        base_url: str = BASE_URL,
        snapshot_url: str = SNAPSHOT_URL,
        ledger_dir: Path = DEFAULT_LEDGER_DIR,
        state_dir: Path = DEFAULT_STATE_DIR,
        interval_s: float = 300.0,
        dry_run: bool = False,
    ):
        self.accounts = accounts
        self.interval_s = interval_s
        self.dry_run = dry_run
        self.state_dir = Path(state_dir)
        self.stop_file = Path(state_dir) / "STOP"

        self.ledger = DecisionLedger(ledger_dir)
        self.snapshots = SnapshotReader(snapshot_url)
        self.quotes = QuoteReader(base_url)
        self.rate_limiter = RateLimiter()

        self.sessions: dict[str, AccountSession] = {}
        self.executors: dict[str, ExecutionClient] = {}
        for account in accounts:
            session = AccountSession(account, base_url=base_url, rate_limiter=self.rate_limiter)
            self.sessions[account.alias] = session
            self.executors[account.alias] = ExecutionClient(session, self.state_dir)

        self._stop = threading.Event()
        self.retired: set[str] = set()

    # -- lifecycle --------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.info("Received signal %s; finishing current cycle then stopping.", signum)
            self._stop.set()

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)

    @property
    def should_stop(self) -> bool:
        # A sentinel file is the kill switch that works when the process is
        # not attached to a terminal.
        return self._stop.is_set() or self.stop_file.exists()

    def startup(self) -> None:
        """Log in and resolve anything left in flight by a previous run.

        Recovery happens before the first new decision so a crashed cycle can
        never turn into a duplicate fill.
        """
        log.info("run_id=%s ledger=%s", self.ledger.run_id, self.ledger.paths.ledger)
        for account in self.accounts:
            session = self.sessions[account.alias]
            try:
                state = session.ensure_session()
                log.info(
                    "%s: session established (balance_cash=%.2f, trades=%d)",
                    account.alias,
                    state.balance_cash,
                    len(state.trades),
                )
            except AccountLiquidated:
                self._retire(account, "Account was already liquidated at startup.")
                continue
            except CrassusError as exc:
                log.error("%s: could not establish session: %s", account.alias, exc)
                self.ledger.record(
                    decision_id=self.ledger.new_decision_id(),
                    account_alias=account.alias,
                    strategy_id=account.strategy_id,
                    strategy_version="n/a",
                    outcome_class=Outcome.TRANSPORT_ERROR,
                    reason=f"Startup session failure: {exc}",
                )
                continue

            recovered = self.executors[account.alias].recover_pending()
            if recovered:
                log.warning("%s: %s", account.alias, recovered.note)
                self.ledger.record(
                    decision_id=self.ledger.new_decision_id(),
                    account_alias=account.alias,
                    strategy_id=account.strategy_id,
                    strategy_version="n/a",
                    outcome_class=recovered.outcome_class,
                    reason=recovered.note,
                    execution_request_id=recovered.execution_request_id,
                    http_status=recovered.http_status,
                    server_response=recovered.server_response,
                    latency_ms=recovered.latency_ms,
                    account_state_after=recovered.state_after.summary() if recovered.state_after else None,
                )

    def run(self) -> None:
        self.install_signal_handlers()
        self.startup()

        while not self.should_stop:
            started = time.monotonic()
            phase = clock.session_phase()
            log.info("--- cycle @ %s ET (%s) ---", clock.now_et().strftime("%H:%M:%S"), phase)

            try:
                snapshot = self.snapshots.read()
            except Exception as exc:  # snapshot is shared; one failure stalls the cycle
                log.error("Snapshot read failed: %s", exc)
                snapshot = None

            for account in self.accounts:
                if self.should_stop:
                    break
                if account.alias in self.retired:
                    continue
                self._run_account(account, snapshot, phase)

            if self.should_stop:
                break
            self._sleep(self.interval_s - (time.monotonic() - started))

        log.info("Stopped cleanly. Ledger: %s", self.ledger.paths.ledger)

    def _sleep(self, seconds: float) -> None:
        """Interruptible wait, so a stop signal does not have to sit through
        a full five-minute interval before being noticed."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline and not self.should_stop:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _retire(self, account: Any, reason: str) -> None:
        self.retired.add(account.alias)
        log.warning("%s retired: %s", account.alias, reason)

    # -- one account, one cycle -------------------------------------------

    def _run_account(self, account: Any, snapshot: Any, phase: str) -> None:
        alias = account.alias
        session = self.sessions[alias]
        executor = self.executors[alias]
        decision_id = self.ledger.new_decision_id()
        strategy = get_strategy(account.strategy_id)

        base = dict(
            decision_id=decision_id,
            account_alias=alias,
            strategy_id=account.strategy_id,
            strategy_version=getattr(strategy, "strategy_version", "unknown"),
            market_snapshot_timestamp=snapshot.timestamp if snapshot else None,
            market_snapshot_url_or_hash=snapshot.provenance if snapshot else None,
        )

        if snapshot is None:
            self.ledger.record(
                **base,
                outcome_class=Outcome.RUNNER_ERROR,
                reason="No market snapshot available this cycle.",
            )
            return

        # Reconcile first: the server's view of cash and trades is the only
        # authority, and the strategy should decide against current reality.
        try:
            pending = executor.recover_pending()
            if pending:
                log.warning("%s: %s", alias, pending.note)
                self.ledger.record(
                    **base,
                    outcome_class=pending.outcome_class,
                    reason=pending.note,
                    execution_request_id=pending.execution_request_id,
                    http_status=pending.http_status,
                    server_response=pending.server_response,
                    latency_ms=pending.latency_ms,
                    account_state_after=pending.state_after.summary() if pending.state_after else None,
                )
                return

            state = session.me()
        except AccountLiquidated as exc:
            self._record_liquidation(base, exc)
            self._retire(account, str(exc))
            return
        except CrassusError as exc:
            self.ledger.record(
                **base,
                outcome_class=exc.outcome_class,
                reason=f"Could not reconcile account state: {exc}",
            )
            return

        book = Book(state.trades)
        state_before = {**state.summary(), **book.summary()}

        ctx = StrategyContext(
            snapshot=snapshot,
            account_state=state.summary(),
            book=book,
            now_et=clock.now_et(),
            session_phase=phase,
            quotes=self.quotes.quotes,
        )

        try:
            decision: Decision = strategy(ctx)
        except Exception as exc:
            log.exception("%s: strategy raised", alias)
            self.ledger.record(
                **base,
                outcome_class=Outcome.RUNNER_ERROR,
                reason=f"Strategy {account.strategy_id} raised: {exc}",
                account_state_before=state_before,
            )
            return

        if not decision.is_trade:
            log.info("%s: no_trade -- %s", alias, decision.reason)
            self.ledger.record(
                **base,
                outcome_class=Outcome.NO_TRADE,
                decision=decision.to_dict(),
                reason=decision.reason,
                account_state_before=state_before,
                account_state_after=state_before,
            )
            return

        if self.dry_run:
            log.info("%s: DRY RUN would %s %s x%s -- %s", alias, decision.action, decision.symbol, decision.quantity, decision.reason)
            self.ledger.record(
                **base,
                outcome_class=Outcome.NO_TRADE,
                decision=decision.to_dict(),
                reason=f"[dry-run] {decision.reason}",
                account_state_before=state_before,
                account_state_after=state_before,
            )
            return

        log.info("%s: %s %s x%s -- %s", alias, decision.action, decision.symbol, decision.quantity, decision.reason)
        try:
            result = executor.submit(
                symbol=decision.symbol,
                side=decision.action,
                quantity=decision.quantity,
                decision_id=decision_id,
            )
        except AccountLiquidated as exc:
            self._record_liquidation(base, exc, decision=decision, state_before=state_before)
            self._retire(account, str(exc))
            return

        self.ledger.record(
            **base,
            outcome_class=result.outcome_class,
            decision=decision.to_dict(),
            reason=decision.reason,
            account_state_before=state_before,
            account_state_after=result.state_after.summary() if result.state_after else None,
            execution_request_id=result.execution_request_id,
            http_status=result.http_status,
            server_response=result.server_response,
            latency_ms=result.latency_ms,
            note=result.note,
        )
        log.info("%s: -> %s (http=%s)", alias, result.outcome_class, result.http_status)

    def _record_liquidation(self, base: dict, exc: Exception, decision: Decision | None = None, state_before: dict | None = None) -> None:
        """Record a 410 as an explicit margin call.

        The raw status alone would leave the record ambiguous later; the
        annotation says plainly that the account was margin called and that
        the Worker deleted it, which is the fact an evaluation needs.
        """
        self.ledger.record(
            **base,
            outcome_class=Outcome.ACCOUNT_LIQUIDATED,
            decision=decision.to_dict() if decision else None,
            reason=f"MARGIN CALL -- account liquidated and deleted by the Worker: {exc}",
            account_state_before=state_before,
            http_status=410,
            margin_called=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crassus / Golden Goose bot runner")
    parser.add_argument("--accounts", type=Path, default=None, help="Path to accounts JSON")
    parser.add_argument("--interval", type=float, default=300.0, help="Seconds between cycles (default 300)")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Decide and log, but never submit an order")
    parser.add_argument("--only", action="append", help="Limit to these account aliases")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    accounts = load_accounts(args.accounts)
    if args.only:
        accounts = [a for a in accounts if a.alias in set(args.only)]
        if not accounts:
            log.error("No accounts matched --only %s", args.only)
            return 2

    runner = Runner(
        accounts,
        base_url=args.base_url,
        interval_s=args.interval,
        dry_run=args.dry_run,
    )
    if args.once:
        runner.install_signal_handlers()
        runner.startup()
        snapshot = runner.snapshots.read()
        phase = clock.session_phase()
        for account in accounts:
            if account.alias not in runner.retired:
                runner._run_account(account, snapshot, phase)
        log.info("Single cycle complete. Ledger: %s", runner.ledger.paths.ledger)
        return 0

    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
