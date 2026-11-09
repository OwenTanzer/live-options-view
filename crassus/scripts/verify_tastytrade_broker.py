#!/usr/bin/env python3
"""Hermetic checks for the tastytrade brokerage route (crassus/tastytrade_broker.py).

No network and no credentials: every tastytrade call goes to `FakeTasty`, an
in-memory stand-in for the REST API that records what was sent. The checks
cover the fail-closed approval gate, the risk guard, order construction,
reconciliation without blind resubmission, cancel-on-no-fill, crash recovery
that never replays, secret redaction, and per-account runner isolation.

    python scripts/verify_tastytrade_broker.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import crassus.durability as durability  # noqa: E402

from crassus import runner as _runner  # noqa: E402,F401  (imports every crassus module)
from crassus import tastytrade_broker as tb  # noqa: E402
from crassus.audit import Outcome  # noqa: E402
from crassus.client import Book, ConfigError, RateLimiter  # noqa: E402
from crassus.config import Account, load_accounts  # noqa: E402
from crassus.market import Quote  # noqa: E402

if os.name == "nt":  # directory fsync is POSIX-only; CI runs on Linux unpatched
    _original_sync = durability.sync_directory
    for _name, _mod in list(sys.modules.items()):
        if _name.startswith("crassus") and getattr(_mod, "sync_directory", None) is _original_sync:
            _mod.sync_directory = lambda path: None

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


SECRET = "client-secret-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
REFRESH = "refresh-token-ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210"
ACCT = "5WT00001"
CALL = "QQQ261109C00500000"
PUT = "QQQ261109P00495000"
ENV_SANDBOX = {
    "CRASSUS_TASTY_MODE": "sandbox",
    "CRASSUS_TASTY_ACCOUNT_NUMBER": ACCT,
    "CRASSUS_TASTY_CLIENT_SECRET": SECRET,
    "CRASSUS_TASTY_REFRESH_TOKEN": REFRESH,
}


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeTasty:
    """Minimal in-memory tastytrade REST API."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.positions: list[dict] = []
        self.nlv = 10_000.0
        self.orders: dict[int, dict] = {}
        self.next_id = 100
        self.fill_on_submit = True
        self.fill_on_poll = False
        self.submit_timeout = False
        self.record_on_timeout = True
        self.cancel_fails = False
        self.dry_run_status = 200
        self.token_401_once = False
        self.submit_error_echo: str | None = None

    def posts_to_orders(self) -> int:
        return sum(1 for m, u, _ in self.calls if m == "POST" and u.endswith(f"/accounts/{ACCT}/orders"))

    def _new_order(self, body: dict, status: str) -> dict:
        oid = self.next_id
        self.next_id += 1
        order = {"id": oid, "status": status, "external-identifier": body.get("external-identifier"),
                 "order-type": body["order-type"], "price": body.get("price"), "legs": body["legs"]}
        self.orders[oid] = order
        return order

    def _apply_fill(self, order: dict) -> None:
        order["status"] = "Filled"
        leg = order["legs"][0]
        sym = leg["symbol"]
        if leg["action"].endswith("Open"):
            self.positions.append({"symbol": sym, "instrument-type": "Equity Option", "quantity": leg["quantity"],
                                   "quantity-direction": "Long", "average-open-price": order.get("price") or "1.00"})
        else:
            self.positions = [p for p in self.positions if p["symbol"] != sym]

    def request(self, method, url, headers=None, timeout=None, **kw):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, {"headers": headers, **kw}))
        if path == "/oauth/token":
            return FakeResponse(200, {"access_token": "access-token-value", "expires_in": 900})
        if self.token_401_once and path != "/oauth/token":
            self.token_401_once = False
            return FakeResponse(401, {"error": {"code": "token_invalid"}})
        if path == "/customers/me/accounts":
            return FakeResponse(200, {"data": {"items": [{"account": {"account-number": ACCT}}]}})
        if path == f"/accounts/{ACCT}/balances":
            return FakeResponse(200, {"data": {"cash-balance": "5000.0", "net-liquidating-value": str(self.nlv)}})
        if path == f"/accounts/{ACCT}/positions":
            return FakeResponse(200, {"data": {"items": list(self.positions)}})
        if path == f"/accounts/{ACCT}/orders/dry-run":
            if self.dry_run_status >= 400:
                return FakeResponse(self.dry_run_status, {"error": {"code": "preflight_check_failure", "message": "insufficient buying power"}})
            return FakeResponse(201, {"data": {"order": {"status": "Received"}, "warnings": []}})
        if path == f"/accounts/{ACCT}/orders" and method == "POST":
            body = kw["json"]
            if self.submit_error_echo:
                return FakeResponse(422, {"error": {"code": "bad", "message": f"echo {self.submit_error_echo}"}})
            if self.submit_timeout:
                if self.record_on_timeout:
                    order = self._new_order(body, "Live")
                    self._apply_fill(order)
                raise requests.Timeout("read timed out")
            order = self._new_order(body, "Live")
            if self.fill_on_submit:
                self._apply_fill(order)
            return FakeResponse(201, {"data": {"order": dict(order)}})
        if path == f"/accounts/{ACCT}/orders" and method == "GET":
            return FakeResponse(200, {"data": {"items": [dict(o) for o in self.orders.values()]}})
        if path.startswith(f"/accounts/{ACCT}/orders/"):
            oid = int(path.rsplit("/", 1)[1])
            order = self.orders[oid]
            if method == "DELETE":
                if self.cancel_fails:
                    return FakeResponse(500, {"error": {"code": "cancel_failed"}})
                order["status"] = "Cancelled"
                return FakeResponse(200, {"data": dict(order)})
            if self.fill_on_poll and order["status"] == "Live":
                self._apply_fill(order)
            return FakeResponse(200, {"data": dict(order)})
        return FakeResponse(404, {"error": {"code": "not_found"}})


def quote(symbol: str, bid: float | None, ask: float | None, fresh: bool = True) -> Quote:
    server = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    q_ts = server - timedelta(seconds=2 if fresh else 120)
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts=q_ts.isoformat(), server_ts=server.isoformat())


def build(tmp: Path, fake: FakeTasty, quotes: dict | None = None, params: dict | None = None, env=None):
    env = dict(ENV_SANDBOX if env is None else env)
    with patch.dict(os.environ, env, clear=False):
        config = tb.TastyConfig.from_env(env)
    account = Account(alias="OI Skew (tastytrade)", username="tasty_oiskew", password="",
                      strategy_id="oi_skew_qqq", params=params or {}, broker="tastytrade")
    session = tb.TastySession(account, config, rate_limiter=RateLimiter(capacity=1000, refill_per_s=1000), http=fake)
    guard = tb.RiskGuard(account.alias, tmp, tb.resolve_limits(account.params))
    qmap = quotes if quotes is not None else {CALL: quote(CALL, 1.20, 1.25), PUT: quote(PUT, 0.90, 0.95)}
    executor = tb.TastyExecutionClient(session, tmp, lambda syms: {s: qmap[s] for s in syms if s in qmap},
                                       guard, fill_timeout_s=0.05, poll_interval_s=0.0, sleep=lambda s: None)
    return session, executor, guard


def submit(executor, symbol=CALL, side="buy", qty=1, strategy_id="oi_skew_qqq"):
    return executor.submit(symbol=symbol, side=side, quantity=qty, strategy_id=strategy_id,
                           strategy_version="1.0.0", decision_id="d-1", reason="test")


def scenario_config() -> None:
    print("\n-- approval gate (fail closed)")
    for env, label in (({}, "no mode"), ({**ENV_SANDBOX, "CRASSUS_TASTY_MODE": "paper"}, "unknown mode"),
                       ({**ENV_SANDBOX, "CRASSUS_TASTY_REFRESH_TOKEN": ""}, "missing secret")):
        try:
            tb.TastyConfig.from_env(env)
            check(f"{label} refuses to trade", False)
        except ConfigError:
            check(f"{label} refuses to trade", True)

    live = {**ENV_SANDBOX, "CRASSUS_TASTY_MODE": "live"}
    for approval, label in ((None, "live without approval"), ("yes", "live with a vague approval"),
                            ("live:9ZZ99999", "live approved for a different account")):
        env = dict(live)
        if approval:
            env["CRASSUS_TASTY_LIVE_APPROVAL"] = approval
        try:
            tb.TastyConfig.from_env(env)
            check(f"{label} is refused", False)
        except tb.LiveTradingNotApproved:
            check(f"{label} is refused", True)

    approved = tb.TastyConfig.from_env({**live, "CRASSUS_TASTY_LIVE_APPROVAL": f"live:{ACCT}"})
    check("owner approval for this exact account enables live", approved.base_url == tb.LIVE_BASE)
    sandbox = tb.TastyConfig.from_env(ENV_SANDBOX)
    check("sandbox mode never points at the production host", sandbox.base_url == tb.SANDBOX_BASE)
    check("config repr hides secrets", SECRET not in repr(sandbox) and REFRESH not in repr(sandbox))


def scenario_limits_and_symbols() -> None:
    print("\n-- limits and symbols")
    limits = tb.resolve_limits({"tastytrade_limits": {"max_contracts_per_order": 50, "max_daily_loss_usd": 10_000,
                                                      "max_premium_per_contract": 1.5}})
    check("params cannot raise contracts above the hard ceiling", limits["max_contracts_per_order"] == 1)
    check("params cannot raise the daily loss cap above the hard ceiling", limits["max_daily_loss_usd"] == tb.HARD_MAX_DAILY_LOSS_USD)
    check("params can tighten a limit", limits["max_premium_per_contract"] == 1.5)
    try:
        tb.resolve_limits({"tastytrade_limits": {"max_opens_per_day": 0}})
        check("non-positive limit is a config error", False)
    except ConfigError:
        check("non-positive limit is a config error", True)
    check("OCC symbol pads root to 6", tb.to_tasty_symbol(CALL) == "QQQ   261109C00500000")
    check("symbol round-trips", tb.to_crassus_symbol(tb.to_tasty_symbol(CALL)) == CALL)


def scenario_open_and_close(tmp: Path) -> None:
    print("\n-- open then close")
    fake = FakeTasty()
    session, ex, _ = build(tmp / "oc", fake)
    session.ensure_session()
    r = submit(ex)
    body = next(kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders"))
    check("buy to open fills", r.outcome_class == Outcome.FILLED, r.note or "")
    check("dry run precedes the live order",
          [u for m, u, _ in fake.calls if m == "POST" and "/orders" in u] == [f"/accounts/{ACCT}/orders/dry-run", f"/accounts/{ACCT}/orders"])
    check("order is a Day limit debit at the ask",
          body["order-type"] == "Limit" and body["price"] == "1.25" and body["price-effect"] == "Debit" and body["time-in-force"] == "Day")
    check("leg uses tastytrade symbol and Buy to Open",
          body["legs"][0] == {"instrument-type": "Equity Option", "symbol": "QQQ   261109C00500000", "quantity": 1, "action": "Buy to Open"})
    check("external-identifier is the crassus execution_request_id", body["external-identifier"] == r.execution_request_id)
    check("intent persisted and left for the runner to finalize", ex.pending_intent() is not None)
    ex.finalize(r.execution_request_id)
    book = Book(session.me().trades)
    check("Book reconstructs the broker position", book.position(CALL).quantity == 1)

    r2 = submit(ex, side="sell", strategy_id="eod_flatten")
    body2 = [kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders")][-1]
    check("eod_flatten may close even though it may not open", r2.outcome_class == Outcome.FILLED, r2.note or "")
    check("close is Sell to Close credit at the bid",
          body2["legs"][0]["action"] == "Sell to Close" and body2["price"] == "1.20" and body2["price-effect"] == "Credit")
    ex.finalize(r2.execution_request_id)
    check("account flat after close", Book(session.me().trades).is_flat)


def scenario_guard(tmp: Path) -> None:
    print("\n-- risk guard")
    fake = FakeTasty()
    _, ex, _ = build(tmp / "g1", fake)
    r = submit(ex, strategy_id="momentum_qqq")
    check("non-allowlisted strategy cannot open", r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    r = submit(ex, side="sell")
    check("no naked short: sell while flat is refused", r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    with patch.dict(os.environ, {"CRASSUS_TASTY_HALT": "1"}):
        r = submit(ex)
    check("kill switch env blocks opens", r.outcome_class == Outcome.REJECTED and "halted" in (r.note or ""))
    (tmp / "g1" / "TASTY_HALT").write_text("")
    r = submit(ex)
    check("kill switch file blocks opens", r.outcome_class == Outcome.REJECTED and "halted" in (r.note or ""))
    (tmp / "g1" / "TASTY_HALT").unlink()

    fake = FakeTasty()
    _, ex, _ = build(tmp / "g2", fake, quotes={CALL: quote(CALL, 3.40, 3.50)})
    r = submit(ex)
    check("premium above cap is refused", r.outcome_class == Outcome.REJECTED and "premium" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    _, ex, _ = build(tmp / "g3", fake, quotes={CALL: quote(CALL, 1.2, 1.25, fresh=False)})
    r = submit(ex)
    check("stale quote cannot open", r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    fake = FakeTasty()
    _, ex, _ = build(tmp / "g4", fake)
    ex.finalize(submit(ex).execution_request_id)
    r = submit(ex, symbol=PUT)
    check("max_open_contracts blocks a second position", r.outcome_class == Outcome.REJECTED and "max_open_contracts" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    _, ex, guard = build(tmp / "g5", fake)
    guard.observe(ex.session.me())  # start-of-day NLV 10,000
    fake.nlv = 9_740.0
    r = submit(ex)
    check("daily loss cap blocks opens after a $260 drawdown", r.outcome_class == Outcome.REJECTED and "daily loss" in (r.note or ""), r.note or "")
    fake.positions.append({"symbol": "QQQ   261109C00500000", "instrument-type": "Equity Option", "quantity": 1,
                           "quantity-direction": "Long", "average-open-price": "1.25"})
    r = submit(ex, side="sell", strategy_id="eod_flatten")
    check("daily loss cap still allows closing", r.outcome_class == Outcome.FILLED, r.note or "")

    fake = FakeTasty()
    _, ex, _ = build(tmp / "g6", fake, params={"tastytrade_limits": {"max_opens_per_day": 1}})
    ex.finalize(submit(ex).execution_request_id)
    ex.finalize(submit(ex, side="sell").execution_request_id)
    r = submit(ex)
    check("max_opens_per_day is enforced across the session", r.outcome_class == Outcome.REJECTED and "max_opens_per_day" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    fake.dry_run_status = 422
    _, ex, _ = build(tmp / "g7", fake)
    r = submit(ex)
    check("dry-run rejection stops the order", r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")


def scenario_close_without_quote(tmp: Path) -> None:
    print("\n-- close without a quote")
    fake = FakeTasty()
    fake.positions.append({"symbol": "QQQ   261109C00500000", "instrument-type": "Equity Option", "quantity": 1,
                           "quantity-direction": "Long", "average-open-price": "1.25"})
    _, ex, _ = build(tmp / "cq", fake, quotes={})
    r = submit(ex, side="sell", strategy_id="eod_flatten")
    body = [kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders")][-1]
    check("close with no quote goes out as a market order", body["order-type"] == "Market" and "price" not in body)
    check("market close fills", r.outcome_class == Outcome.FILLED)


def scenario_ambiguity(tmp: Path) -> None:
    print("\n-- ambiguity and cancel")
    fake = FakeTasty()
    fake.submit_timeout = True
    _, ex, _ = build(tmp / "a1", fake)
    r = submit(ex)
    check("timeout reconciles by external-identifier", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY, r.note or "")
    check("timeout never resubmits", fake.posts_to_orders() == 1)

    fake = FakeTasty()
    fake.submit_timeout = True
    fake.record_on_timeout = False
    _, ex, _ = build(tmp / "a2", fake)
    r = submit(ex)
    check("timeout with no order found stays ambiguous", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")
    check("...and is not resubmitted", fake.posts_to_orders() == 1 and ex.pending_intent() is not None)

    fake = FakeTasty()
    fake.fill_on_submit = False
    _, ex, _ = build(tmp / "a3", fake)
    r = submit(ex)
    check("unfilled order is cancelled and recorded rejected", r.outcome_class == Outcome.REJECTED and fake.orders[100]["status"] == "Cancelled", r.note or "")

    fake = FakeTasty()
    fake.fill_on_submit = False
    fake.fill_on_poll = True
    _, ex, _ = build(tmp / "a4", fake)
    r = submit(ex)
    check("order that fills while polling is filled", r.outcome_class == Outcome.FILLED)

    fake = FakeTasty()
    fake.fill_on_submit = False
    fake.cancel_fails = True
    _, ex, _ = build(tmp / "a5", fake)
    r = submit(ex)
    check("failed cancel leaves the outcome ambiguous for reconciliation", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")


def scenario_recovery(tmp: Path) -> None:
    print("\n-- crash recovery never replays")
    fake = FakeTasty()
    _, ex, _ = build(tmp / "r1", fake)
    ex._persist_intent({"execution_request_id": "stale-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "created_utc": "2026-11-09T14:00:00+00:00"})
    with patch.object(tb.clock, "iso_utc", return_value="2026-11-09T15:00:00+00:00"):
        r = ex.recover_pending()
    check("stale intent absent at broker is rejected, not replayed",
          r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    ex._persist_intent({"execution_request_id": "fresh-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "created_utc": "2026-11-09T14:59:30+00:00"})
    with patch.object(tb.clock, "iso_utc", return_value="2026-11-09T15:00:00+00:00"):
        r = ex.recover_pending()
    check("very recent intent not yet listed stays ambiguous", r.outcome_class == Outcome.AMBIGUOUS)

    fake = FakeTasty()
    _, ex, _ = build(tmp / "r2", fake)
    order = fake._new_order({"external-identifier": "known-1", "order-type": "Limit", "price": "1.25",
                             "legs": [{"symbol": "QQQ   261109C00500000", "action": "Buy to Open", "quantity": 1}]}, "Live")
    fake._apply_fill(order)
    ex._persist_intent({"execution_request_id": "known-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "created_utc": "2026-11-09T14:00:00+00:00"})
    r = ex.recover_pending()
    check("intent found filled at broker is reconciled", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY)


def scenario_auth_and_redaction(tmp: Path) -> None:
    print("\n-- auth and redaction")
    fake = FakeTasty()
    fake.token_401_once = True
    session, _, _ = build(tmp / "s1", fake)
    session.me()
    check("a 401 triggers exactly one token refresh", sum(1 for m, u, _ in fake.calls if u == "/oauth/token") == 2)
    token_call = next(kw for m, u, kw in fake.calls if u == "/oauth/token")
    check("token exchange uses the refresh_token grant", token_call["json"]["grant_type"] == "refresh_token")
    api_call = next(kw for m, u, kw in fake.calls if u.endswith("/balances"))
    check("API calls carry a bearer token and User-Agent",
          api_call["headers"]["Authorization"].startswith("Bearer ") and api_call["headers"]["User-Agent"] == tb.USER_AGENT)

    fake = FakeTasty()
    fake.submit_error_echo = SECRET
    _, ex, _ = build(tmp / "s2", fake)
    r = submit(ex)
    blob = json.dumps({"note": r.note, "resp": r.server_response}, default=str)
    check("a broker error echoing a secret is redacted", SECRET not in blob and r.outcome_class == Outcome.REJECTED, r.note or "")


def scenario_config_loading_and_runner(tmp: Path) -> None:
    print("\n-- catalog and runner isolation")
    catalog = tmp / "catalog.json"
    catalog.write_text(json.dumps({"accounts": [
        {"alias": "Paper", "username": "paper_bot", "password_env": "PW_PAPER", "strategy_id": "oi_skew_qqq"},
        {"alias": "Tasty", "username": "tasty_bot", "broker": "tastytrade", "strategy_id": "oi_skew_qqq"},
    ]}))
    with patch.dict(os.environ, {"PW_PAPER": "pw"}, clear=True):
        accounts = load_accounts(catalog_path=catalog, override_path=tmp / "none.json")
    check("tastytrade entry loads without a Worker password",
          [a.broker for a in accounts] == ["worker", "tastytrade"])

    bad = tmp / "bad.json"
    bad.write_text(json.dumps({"accounts": [
        {"alias": "Tasty", "username": "tasty_bot", "broker": "tastytrade", "password_env": "X", "strategy_id": "oi_skew_qqq"}]}))
    try:
        load_accounts(catalog_path=bad, override_path=tmp / "none.json")
        check("tastytrade entry with a Worker password is refused", False)
    except ValueError:
        check("tastytrade entry with a Worker password is refused", True)

    from crassus.runner import Runner

    with patch.dict(os.environ, {"PW_PAPER": "pw"}, clear=True):
        runner = Runner(accounts, bot_registration_key="k" * 32, ledger_dir=tmp / "logs", state_dir=tmp / "state")
    check("missing tastytrade config disables only that bot", [a.alias for a in runner.accounts] == ["Paper"])

    with patch.dict(os.environ, {"PW_PAPER": "pw", **ENV_SANDBOX}, clear=True):
        runner = Runner(accounts, bot_registration_key="k" * 32, ledger_dir=tmp / "logs2", state_dir=tmp / "state2")
    check("configured tastytrade bot gets the broker executor",
          isinstance(runner.executors.get("Tasty"), tb.TastyExecutionClient) and len(runner.accounts) == 2)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        scenario_config()
        scenario_limits_and_symbols()
        scenario_open_and_close(tmp)
        scenario_guard(tmp)
        scenario_close_without_quote(tmp)
        scenario_ambiguity(tmp)
        scenario_recovery(tmp)
        scenario_auth_and_redaction(tmp)
        scenario_config_loading_and_runner(tmp)
    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
