#!/usr/bin/env python3
"""Integration proof: looking_glass_straddle driven through the real
`Runner._run_account` loop at the deployed 300-second cadence, not just the
hermetic `_decide()` scenarios in `verify_looking_glass_straddle.py`.

Review on PR #60 asked for this repeatedly: unit scenarios that call the
strategy function directly never exercise the cross-cycle contract the
runner actually enforces -- `Book` reconstruction from `/api/me` trades,
`ExecutionClient`'s persist-before-send/idempotent-retry submission path,
and the decision ledger. Those are exactly the seams where "buys the exact
matching put" or "rolls back after the completion timeout" could still be
true of `_decide()` in isolation but false once wired to the real loop.

Fakes only the network boundary -- `requests.get` (snapshot + live-quotes,
both called at module level by `market.py`) and `AccountSession.http`
(swapped for a fake `Session`-shaped object) -- and the wall clock
(`clock.now_utc`). Everything above that boundary is the real
`Runner`, `AccountSession`, `ExecutionClient`, `Book`, and `DecisionLedger`
code, unmodified. Cadence is proven by advancing the frozen clock by exactly
`interval_s` (300s) between cycles and calling `Runner._run_account` once
per cycle -- not by sleeping in real time, the same "stub the clock, don't
wait on it" convention `verify_flatten.py` already uses.

    python scripts/verify_looking_glass_straddle_runner_integration.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus import clock  # noqa: E402
from crassus import market as market_mod  # noqa: E402
from crassus.client import Book  # noqa: E402
from crassus.config import Account  # noqa: E402
from crassus.runner import Runner  # noqa: E402
from crassus.strategies.looking_glass_straddle import STRATEGY_ID  # noqa: E402

ET = ZoneInfo("America/New_York")
BASE_URL = "https://fake.example"
SNAPSHOT_URL = "test://snapshot"

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


# -- fakes: only the network boundary and the wall clock ---------------------


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers: dict[str, str] = {}

    @property
    def content(self) -> bytes:
        return json.dumps(self._payload).encode()

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MarketState:
    """What the fake snapshot/live-quotes endpoints serve right now. The
    test mutates this once per simulated cycle before calling
    `Runner._run_account`, exactly like the real collector republishing a
    new snapshot and the Worker's live-quotes endpoint moving between polls.
    """

    def __init__(self):
        self.underlying_price = 400.0
        self.rows: list[dict] = []
        self.quotes: dict[str, tuple[float, float]] = {}

    def snapshot_payload(self) -> dict:
        return {
            "timestamp": clock.iso_utc(),
            "snapshot_time": clock.iso_utc(),
            "expiration": "2024-01-02",
            "underlying_price": self.underlying_price,
            "rows": self.rows,
        }

    def quotes_payload(self, symbols: list[str]) -> dict:
        server_ts = clock.iso_utc()
        quotes = []
        for s in symbols:
            if s in self.quotes:
                bid, ask = self.quotes[s]
                quotes.append({"symbol": s, "bid": bid, "ask": ask, "quote_ts": server_ts})
        return {"server_ts": server_ts, "quotes": quotes}


def make_fake_requests_get(market: MarketState):
    def fake_get(url, params=None, timeout=None):
        if url == SNAPSHOT_URL:
            return FakeResponse(200, market.snapshot_payload())
        if url == f"{BASE_URL}/api/live-quotes":
            symbols = (params or {}).get("symbols", "").split(",") if params else []
            return FakeResponse(200, market.quotes_payload(symbols))
        raise AssertionError(f"unexpected requests.get {url}")

    return fake_get


class FakeWorker:
    """The subset of the Worker's HTTP API the runner talks to for one
    account: register/login/bot-metadata/me/paper-trade. Orders fill against
    `MarketState`'s current quote for the symbol -- buys at the ask, sells at
    the bid -- so a fill price is never invented independently of what the
    strategy actually saw when it decided to trade.
    """

    def __init__(self, username: str, market: MarketState):
        self.username = username
        self.market = market
        self.balance_cash = 1_000_000.0
        self.trades: list[dict] = []

    def request(self, method: str, url: str, timeout=None, **kw):
        path = urlparse(url).path
        if path in ("/api/register", "/api/bot-metadata", "/api/login"):
            return FakeResponse(200, {"ok": True})
        if path == "/api/me":
            return FakeResponse(
                200,
                {"username": self.username, "balance_cash": self.balance_cash, "trades": list(self.trades)},
            )
        if path == "/api/paper-trade":
            body = kw.get("json") or {}
            symbol, side, qty = body["sym"], body["side"], int(body["qty"])
            request_id = body["execution_request_id"]
            existing = next((t for t in self.trades if t.get("execution_request_id") == request_id), None)
            if existing:  # idempotent replay, same as the real Worker's contract
                return FakeResponse(200, {"ok": True, "trade": existing})
            if symbol not in self.market.quotes:
                return FakeResponse(400, {"error": f"no quote for {symbol}"})
            bid, ask = self.market.quotes[symbol]
            price = ask if side == "buy" else bid
            self.balance_cash += -(price * qty * 100) if side == "buy" else (price * qty * 100)
            trade = {
                "sym": symbol, "side": side, "qty": qty, "price": price,
                "ts": clock.iso_utc(), "execution_request_id": request_id,
            }
            self.trades.append(trade)
            return FakeResponse(200, {"ok": True, "trade": trade})
        raise AssertionError(f"FakeWorker: unexpected request {method} {url}")


class FakeAccountHTTP:
    """Drop-in for `AccountSession.http` (an ordinary `requests.Session`)."""

    def __init__(self, worker: FakeWorker):
        self.worker = worker

    def request(self, method: str, url: str, timeout=None, **kw):
        return self.worker.request(method, url, timeout=timeout, **kw)


def et_to_utc(hour: int, minute: int, day: int = 2) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


class Harness:
    """One simulated account/day, wired through the real `Runner`."""

    def __init__(self, params: dict, tmp_path: Path):
        self.market = MarketState()
        self.worker = FakeWorker("tester", self.market)
        self.account = Account(alias="tester", username="tester", password="x", strategy_id=STRATEGY_ID, params=params)
        self.runner = Runner(
            [self.account],
            base_url=BASE_URL,
            snapshot_url=SNAPSHOT_URL,
            ledger_dir=tmp_path / "logs",
            state_dir=tmp_path / "state",
            interval_s=300.0,
            dry_run=False,
            bot_registration_key="test-key",
        )
        self.runner.sessions["tester"].http = FakeAccountHTTP(self.worker)

    def run_cycle(self, hour: int, minute: int, *, underlying_price: float, rows: list[dict], quotes: dict) -> dict:
        """Advance the frozen clock by exactly one 300s-cadence step (the
        caller picks hour:minute 5 minutes apart, matching `interval_s`),
        publish a new market state, and run one real `Runner._run_account`
        cycle -- returning the ledger record it wrote."""
        clock.now_utc = lambda: et_to_utc(hour, minute)
        self.market.underlying_price = underlying_price
        self.market.rows = rows
        self.market.quotes = quotes
        snapshot = self.runner.snapshots.read(force=True)
        phase = clock.session_phase()
        self.runner._run_account(self.account, snapshot, phase)
        return self._last_ledger_record()

    def _last_ledger_record(self) -> dict:
        lines = self.runner.ledger.paths.ledger.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    @property
    def book(self) -> Book:
        return Book(self.worker.trades)


CALL_SYM = "QQQ240102C00400000"
PUT_SYM_MATCH = "QQQ240102P00400000"  # the put implied by CALL_SYM's own OCC symbol
PUT_SYM_WRONG_ATM = "QQQ240102P00404000"  # what a naive ctx.snapshot.atm("put") would pick after the move

BASE_PARAMS = {
    "num_straddles": 4,
    "core_sell_count": 3,
    "entry_time_et": "09:30",
    "terminal_exit_time_et": "09:55",  # shortened so the test doesn't need ~75 five-minute cycles to reach it
    "core_profit_threshold_pct": 0.20,
    "max_entry_leg_spread_pct": 0.15,
    "max_leg_completion_wait_minutes": 10.0,
}


def scenario_full_sequence_through_real_runner_at_300s_cadence() -> None:
    print(
        "\n1. Entry -> matched leg 2 despite a strike-relevant underlying move -> core -> runner -> "
        "terminal -> flat, each step one real Runner._run_account cycle apart at 300s cadence"
    )
    original_get, original_now_utc = market_mod.requests.get, clock.now_utc
    try:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(BASE_PARAMS, Path(tmp))
            market_mod.requests.get = make_fake_requests_get(h.market)

            # Cycle 1 (09:30): flat -> buy N calls (leg 1/2). Underlying at 400.
            rec = h.run_cycle(
                9, 30, underlying_price=400.0,
                rows=[
                    {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.00, "Ask": 1.05},
                    {"OptionSymbol": PUT_SYM_MATCH, "Strike": 400.0, "Type": "put", "Bid": 1.00, "Ask": 1.05},
                ],
                quotes={CALL_SYM: (1.00, 1.05), PUT_SYM_MATCH: (1.00, 1.05)},
            )
            check("cycle 1: filled leg 1 (buy calls)", rec["outcome_class"] == "filled", rec.get("reason"))
            check("cycle 1: bought CALL_SYM x4", rec["decision"]["symbol"] == CALL_SYM and rec["decision"]["quantity"] == 4)

            # Cycle 2 (09:35, +300s): underlying has moved to 404 -- the *wrong*
            # ATM put (404-strike) is now quoted alongside the *matching*
            # 400-strike put. The real bug this PR was reviewed against would
            # buy the 404 put here; the fix must buy the 400 one.
            rec = h.run_cycle(
                9, 35, underlying_price=404.0,
                rows=[
                    {"OptionSymbol": PUT_SYM_MATCH, "Strike": 400.0, "Type": "put", "Bid": 1.10, "Ask": 1.15},
                    {"OptionSymbol": PUT_SYM_WRONG_ATM, "Strike": 404.0, "Type": "put", "Bid": 1.00, "Ask": 1.02},
                ],
                quotes={PUT_SYM_MATCH: (1.10, 1.15), PUT_SYM_WRONG_ATM: (1.00, 1.02)},
            )
            check("cycle 2: filled leg 2 (buy puts)", rec["outcome_class"] == "filled", rec.get("reason"))
            check(
                "cycle 2: bought the CALL_SYM-matching put (400), not the post-move ATM put (404)",
                rec["decision"]["symbol"] == PUT_SYM_MATCH and rec["decision"]["quantity"] == 4,
                f"got symbol={rec['decision']['symbol']}",
            )
            check("cycle 2: same-strike straddle now held", h.book.position(CALL_SYM).quantity == 4 and h.book.position(PUT_SYM_MATCH).quantity == 4)

            # Cycle 3 (09:40): full straddle held; quotes cross the core
            # profit threshold -- sells k=3 calls (puts to follow).
            rec = h.run_cycle(
                9, 40, underlying_price=404.0,
                rows=[
                    {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.40, "Ask": 1.45},
                    {"OptionSymbol": PUT_SYM_MATCH, "Strike": 400.0, "Type": "put", "Bid": 1.30, "Ask": 1.35},
                ],
                quotes={CALL_SYM: (1.40, 1.45), PUT_SYM_MATCH: (1.30, 1.35)},
            )
            check("cycle 3: core threshold sale (sell calls)", rec["outcome_class"] == "filled" and rec["decision"]["action"] == "sell")
            check("cycle 3: sold k=3 calls", rec["decision"]["symbol"] == CALL_SYM and rec["decision"]["quantity"] == 3)

            # Cycle 4 (09:45): calls at runner size (1), puts still at 4 --
            # sells k=3 puts to complete the core sale.
            rec = h.run_cycle(
                9, 45, underlying_price=404.0, rows=[],
                quotes={PUT_SYM_MATCH: (1.32, 1.38)},
            )
            check("cycle 4: core sale completed (sell puts)", rec["outcome_class"] == "filled" and rec["decision"]["action"] == "sell")
            check("cycle 4: sold k=3 puts", rec["decision"]["symbol"] == PUT_SYM_MATCH and rec["decision"]["quantity"] == 3)
            check(
                "cycle 4: bounded 1-straddle runner now held in both legs",
                h.book.position(CALL_SYM).quantity == 1 and h.book.position(PUT_SYM_MATCH).quantity == 1,
            )

            # Cycle 5 (09:50): runner-only -- nothing to do until terminal time.
            rec = h.run_cycle(9, 50, underlying_price=404.0, rows=[], quotes={})
            check("cycle 5: holds the runner, no trade", rec["outcome_class"] == "no_trade")

            # Cycle 6 (09:55, == terminal_exit_time_et): liquidate the runner
            # call (puts to follow next cycle).
            rec = h.run_cycle(9, 55, underlying_price=404.0, rows=[], quotes={CALL_SYM: (1.50, 1.55)})
            check("cycle 6: terminal exit sells the runner call", rec["outcome_class"] == "filled" and rec["decision"]["symbol"] == CALL_SYM and rec["decision"]["quantity"] == 1)

            # Cycle 7 (10:00): terminal exit continues -- sells the runner put.
            rec = h.run_cycle(10, 0, underlying_price=404.0, rows=[], quotes={PUT_SYM_MATCH: (1.20, 1.25)})
            check("cycle 7: terminal exit sells the runner put", rec["outcome_class"] == "filled" and rec["decision"]["symbol"] == PUT_SYM_MATCH and rec["decision"]["quantity"] == 1)
            check("cycle 7: book is flat before the close", h.book.is_flat, str(h.book.summary()))

            # Cycle 8 (10:05): flat, already traded today -- refuses to re-enter.
            rec = h.run_cycle(10, 5, underlying_price=404.0, rows=[], quotes={})
            check("cycle 8: no re-entry after today's sequence completed", rec["outcome_class"] == "no_trade" and "re-entry" in rec["reason"])

            check("exactly 6 real fills across the whole sequence (2 entry + 2 core + 2 terminal)", len(h.worker.trades) == 6, str(len(h.worker.trades)))
    finally:
        market_mod.requests.get = original_get
        clock.now_utc = original_now_utc


def scenario_rollback_bounds_naked_leg_exposure_through_real_runner() -> None:
    print(
        "\n2. Matching put never gets quoted -- the call leg's naked exposure is bounded by "
        "max_leg_completion_wait_minutes and rolled back, proven through the real Runner"
    )
    original_get, original_now_utc = market_mod.requests.get, clock.now_utc
    try:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(BASE_PARAMS, Path(tmp))
            market_mod.requests.get = make_fake_requests_get(h.market)

            # Cycle 1 (09:30): buy N calls. Entry evaluates both ATM legs
            # together (it needs a quoted put to enter with at all, even
            # though only the call order is placed this cycle) -- the put
            # leg's quote then vanishes starting next cycle, below.
            rec = h.run_cycle(
                9, 30, underlying_price=400.0,
                rows=[
                    {"OptionSymbol": CALL_SYM, "Strike": 400.0, "Type": "call", "Bid": 1.00, "Ask": 1.05},
                    {"OptionSymbol": PUT_SYM_MATCH, "Strike": 400.0, "Type": "put", "Bid": 1.00, "Ask": 1.05},
                ],
                quotes={CALL_SYM: (1.00, 1.05), PUT_SYM_MATCH: (1.00, 1.05)},
            )
            check("cycle 1: bought the call leg", rec["outcome_class"] == "filled" and rec["decision"]["symbol"] == CALL_SYM)

            # Cycle 2 (09:35, 5 min elapsed): matching put still isn't quoted
            # anywhere -- waits, does not substitute a different strike.
            rec = h.run_cycle(9, 35, underlying_price=400.0, rows=[], quotes={})
            check("cycle 2: waiting on the unquoted matching put, 5m elapsed (< 10m limit)", rec["outcome_class"] == "no_trade" and "isn't quoted" in rec["reason"])
            check("cycle 2: no rollback yet -- still holding the naked call", h.book.position(CALL_SYM).quantity == 4)

            # Cycle 3 (09:42, 12 min elapsed since the call's fill): past the
            # 10-minute completion window -- rolls back rather than riding
            # the naked call indefinitely.
            rec = h.run_cycle(9, 42, underlying_price=400.0, rows=[], quotes={CALL_SYM: (1.10, 1.15)})
            check("cycle 3: rollback fires past the completion timeout", rec["outcome_class"] == "filled" and rec["decision"]["action"] == "sell")
            check("cycle 3: rolls back the full N held calls", rec["decision"]["symbol"] == CALL_SYM and rec["decision"]["quantity"] == 4)
            check("cycle 3: decision metadata is tagged as a rollback", rec["decision"].get("metadata", {}).get("rollback") is True)
            check("cycle 3: book is flat again -- naked exposure bounded, not indefinite", h.book.is_flat, str(h.book.summary()))

            # Cycle 4 (09:47): already traded (and rolled back) today -- no re-entry.
            rec = h.run_cycle(9, 47, underlying_price=400.0, rows=[], quotes={})
            check("cycle 4: no re-entry after the rolled-back sequence", rec["outcome_class"] == "no_trade" and "re-entry" in rec["reason"])
    finally:
        market_mod.requests.get = original_get
        clock.now_utc = original_now_utc


def run() -> bool:
    scenario_full_sequence_through_real_runner_at_300s_cadence()
    scenario_rollback_bounds_naked_leg_exposure_through_real_runner()
    print(f"\n{passed}/{passed + failed} checks passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
