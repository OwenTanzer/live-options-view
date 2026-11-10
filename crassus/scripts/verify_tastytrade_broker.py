#!/usr/bin/env python3
"""Hermetic checks for the tastytrade brokerage route (crassus/tastytrade_broker.py).

No network and no credentials: every tastytrade call goes to `FakeTasty`, an
in-memory stand-in for the REST API that records what was sent. The checks
cover the fail-closed approval gate, limit validation, bot-ownership
isolation, order construction, exactly-once open counting, reconciliation
without blind resubmission, uncertain-state retention, crash recovery that
never replays, the end-of-day exit path through the real runner, secret
redaction, and per-account runner isolation.

These prove internal behaviour against a fake. Real broker statuses,
pagination, partial-fill fields and sandbox semantics still need a sandbox
session (see docs/tastytrade-live-trading.md).

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
from zoneinfo import ZoneInfo

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
CALL = "QQQ301220C00500000"
PUT = "QQQ301220P00495000"
OTHER = "SPY301220C00600000"
EXPIRED = "QQQ200117C00300000"
ACCT2 = "5WT00002"
ENV_SANDBOX = {
    "CRASSUS_TASTY_MODE": "sandbox",
    "CRASSUS_TASTY_ACCOUNT_NUMBER": ACCT,
    "CRASSUS_TASTY_CLIENT_SECRET": SECRET,
    "CRASSUS_TASTY_REFRESH_TOKEN": REFRESH,
}
ET = ZoneInfo("America/New_York")


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def tasty(symbol: str) -> str:
    return tb.to_tasty_symbol(symbol)


class FakeTasty:
    """Minimal in-memory tastytrade REST API."""

    def __init__(self, acct: str = ACCT):
        self.acct = acct
        self.calls: list[tuple[str, str, dict]] = []
        self.positions_lag = False         # fills do not reach /positions until catch_up()
        self._lagged: list[tuple[str, int, str]] = []
        self.orders_payload = None         # replace the GET /orders body (malformed history)
        self.positions: list[dict] = []
        self.nlv = 10_000.0
        self.orders: dict[int, dict] = {}
        self.next_id = 100
        self.fill_on_submit = True
        self.fill_on_poll = False
        self.fill_on_cancel = False        # cancel/fill race: the cancel loses
        self.partial_fill = 0              # fill this many, then rest
        self.submit_timeout = False
        self.record_on_timeout = True
        self.cancel_fails = False
        self.dry_run_status = 200
        self.token_401_once = False
        self.submit_error_echo: str | None = None
        self.status_override: str | None = None  # e.g. "Mystery" or "" (missing)
        self.hidden: set[str] = set()      # external-identifiers not yet listed
        self.orders_status = 200
        self.paginate = True               # include pagination metadata
        self.filler_orders = 0             # unrelated orders listed before ours

    def posts_to_orders(self) -> int:
        return sum(1 for m, u, _ in self.calls if m == "POST" and u.endswith(f"/accounts/{self.acct}/orders"))

    def hold(self, symbol: str, qty: int, direction: str = "Long", price: str = "1.25") -> None:
        for p in self.positions:
            if p["symbol"] == tasty(symbol) and p["quantity-direction"] == direction:
                p["quantity"] += qty
                return
        self.positions.append({"symbol": tasty(symbol), "instrument-type": "Equity Option", "quantity": qty,
                               "quantity-direction": direction, "average-open-price": price})

    def _new_order(self, body: dict, status: str) -> dict:
        oid = self.next_id
        self.next_id += 1
        legs = [dict(leg, **{"remaining-quantity": leg["quantity"]}) for leg in body["legs"]]
        order = {"id": oid, "status": status, "external-identifier": body.get("external-identifier"),
                 "order-type": body.get("order-type", "Limit"), "price": body.get("price"), "legs": legs,
                 "size": legs[0]["quantity"], "received-at": body.get("received-at") or tb.clock.iso_utc()}
        self.orders[oid] = order
        return order

    def _fill(self, order: dict, qty: int | None = None) -> None:
        leg = order["legs"][0]
        qty = leg["remaining-quantity"] if qty is None else qty
        leg["remaining-quantity"] -= qty
        leg.setdefault("fills", []).append({"quantity": qty, "fill-price": order.get("price") or "1.00"})
        if leg["remaining-quantity"] == 0:
            order["status"] = "Filled"
            order["terminal-at"] = tb.clock.iso_utc()
        sym = tb.to_crassus_symbol(leg["symbol"])
        if self.positions_lag:
            self._lagged.append((leg["action"], qty, sym))
            return
        self._move(leg["action"], qty, sym, order.get("price"))

    def catch_up(self) -> None:
        """The positions endpoint catches up with fills it had not shown."""
        lagged, self._lagged = self._lagged, []
        for action, qty, sym in lagged:
            self._move(action, qty, sym, "1.00")

    def _move(self, action: str, qty: int, sym: str, price) -> None:
        leg = {"action": action, "symbol": tasty(sym)}
        order = {"price": price}
        if leg["action"].endswith("Open"):
            self.hold(sym, qty, "Long" if leg["action"].startswith("Buy") else "Short", order.get("price") or "1.00")
        else:
            for p in self.positions:
                if p["symbol"] == leg["symbol"]:
                    p["quantity"] -= qty
            self.positions = [p for p in self.positions if p["quantity"] > 0]

    # back-compat name used by older scenarios
    _apply_fill = _fill

    def _view(self, order: dict) -> dict:
        view = json.loads(json.dumps(order))
        if self.status_override is not None:
            if self.status_override == "":
                view.pop("status", None)
            else:
                view["status"] = self.status_override
        return view

    def manual_order(self, symbol: str, action: str, qty: int, when: str | None = None) -> dict:
        """An order placed by hand (no external-identifier), filled now."""
        order = self._new_order({"legs": [{"instrument-type": "Equity Option", "symbol": tasty(symbol),
                                           "quantity": qty, "action": action}]}, "Live")
        order["external-identifier"] = None
        self._fill(order)
        if when:
            order["terminal-at"] = order["received-at"] = when
        return order

    def _list(self, params: dict) -> FakeResponse:
        if self.orders_status >= 400:
            return FakeResponse(self.orders_status, {"error": {"code": "unavailable"}})
        if self.orders_payload is not None:
            return FakeResponse(200, None if self.orders_payload == "invalid-json" else self.orders_payload)
        start = params.get("start-date")
        filler = [{"id": 9_000 + i, "status": "Filled", "external-identifier": f"other-{i}", "legs": []}
                  for i in range(self.filler_orders)]
        items = filler + [self._view(o) for o in self.orders.values()
                          if o["external-identifier"] not in self.hidden
                          and (not start or tb._et_date(o["received-at"]).isoformat() >= start)]
        per = int(params.get("per-page", 200))
        page = int(params.get("page-offset", 0))
        chunk = items[page * per:(page + 1) * per]
        payload: dict = {"data": {"items": chunk}}
        if self.paginate:
            payload["pagination"] = {"per-page": per, "page-offset": page, "total-items": len(items),
                                     "total-pages": max(1, -(-len(items) // per))}
        return FakeResponse(200, payload)

    def request(self, method, url, headers=None, timeout=None, **kw):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, {"headers": headers, **kw}))
        if path == "/oauth/token":
            return FakeResponse(200, {"access_token": "access-token-value", "expires_in": 900})
        if self.token_401_once and path != "/oauth/token":
            self.token_401_once = False
            return FakeResponse(401, {"error": {"code": "token_invalid"}})
        if path == "/customers/me/accounts":
            return FakeResponse(200, {"data": {"items": [{"account": {"account-number": self.acct}}]}})
        if path == f"/accounts/{self.acct}/balances":
            return FakeResponse(200, {"data": {"cash-balance": "5000.0", "net-liquidating-value": str(self.nlv)}})
        if path == f"/accounts/{self.acct}/positions":
            return FakeResponse(200, {"data": {"items": [dict(p) for p in self.positions]}})
        if path == f"/accounts/{self.acct}/orders/dry-run":
            if self.dry_run_status >= 400:
                return FakeResponse(self.dry_run_status, {"error": {"code": "preflight_check_failure", "message": "insufficient buying power"}})
            return FakeResponse(201, {"data": {"order": {"status": "Received"}, "warnings": []}})
        if path == f"/accounts/{self.acct}/orders" and method == "POST":
            body = kw["json"]
            if self.submit_error_echo:
                return FakeResponse(422, {"error": {"code": "bad", "message": f"echo {self.submit_error_echo}"}})
            if self.submit_timeout:
                if self.record_on_timeout:
                    self._fill(self._new_order(body, "Live"))
                raise requests.Timeout("read timed out")
            order = self._new_order(body, "Live")
            if self.partial_fill:
                self._fill(order, self.partial_fill)
            elif self.fill_on_submit:
                self._fill(order)
            return FakeResponse(201, {"data": {"order": self._view(order)}})
        if path == f"/accounts/{self.acct}/orders" and method == "GET":
            return self._list(kw.get("params") or {})
        if path.startswith(f"/accounts/{self.acct}/orders/"):
            oid = int(path.rsplit("/", 1)[1])
            order = self.orders[oid]
            if method == "DELETE":
                if self.cancel_fails:
                    return FakeResponse(500, {"error": {"code": "cancel_failed"}})
                if self.fill_on_cancel:
                    self._fill(order)
                    return FakeResponse(422, {"error": {"code": "order_not_cancellable"}})
                order["status"] = "Cancelled"
                return FakeResponse(200, {"data": self._view(order)})
            if self.fill_on_poll and order["status"] == "Live":
                self._fill(order)
            return FakeResponse(200, {"data": self._view(order)})
        return FakeResponse(404, {"error": {"code": "not_found"}})


def quote(symbol: str, bid: float | None, ask: float | None, fresh: bool = True) -> Quote:
    server = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    q_ts = server - timedelta(seconds=2 if fresh else 120)
    return Quote(symbol=symbol, bid=bid, ask=ask, quote_ts=q_ts.isoformat(), server_ts=server.isoformat())


def make_account(params: dict | None = None, alias: str = "OI Skew (tastytrade)") -> Account:
    return Account(alias=alias, username="tasty_oiskew", password="",
                   strategy_id="oi_skew_qqq", params=params or {}, broker="tastytrade")


def build(tmp: Path, fake: FakeTasty, quotes: dict | None = None, params: dict | None = None, env=None):
    env = dict(ENV_SANDBOX if env is None else env)
    config = tb.TastyConfig.from_env(env)
    account = make_account(params)
    qmap = quotes if quotes is not None else {CALL: quote(CALL, 1.20, 1.25), PUT: quote(PUT, 0.90, 0.95)}
    session, executor = tb.assemble(
        account, config, state_dir=tmp, quotes=lambda syms: {s: qmap[s] for s in syms if s in qmap},
        rate_limiter=RateLimiter(capacity=1000, refill_per_s=1000), http=fake,
        fill_timeout_s=0.05, poll_interval_s=0.0, sleep=lambda s: None,
    )
    return session, executor, executor.guard


def own(store: tb.OwnershipStore, symbol: str, qty: int, rid: str | None = None) -> None:
    """Seed ownership as if this bot's own order had filled."""
    rid = rid or f"seed-{symbol}-{qty}"
    store.register(rid, symbol, "Buy to Open")
    store.apply_fill(rid, qty, final=True)


def submit(executor, symbol=CALL, side="buy", qty=1, strategy_id="oi_skew_qqq"):
    return executor.submit(symbol=symbol, side=side, quantity=qty, strategy_id=strategy_id,
                           strategy_version="1.0.0", decision_id="d-1", reason="test")


class at:
    """Pin both clocks the route reads (UTC stamps and the ET date/time)."""

    def __init__(self, iso: str):
        self.iso = iso
        self.et = datetime.fromisoformat(iso).astimezone(ET)

    def __enter__(self):
        self._p = [patch.object(tb.clock, "iso_utc", return_value=self.iso),
                   patch.object(tb.clock, "now_et", return_value=self.et)]
        for p_ in self._p:
            p_.__enter__()
        return self

    def __exit__(self, *exc):
        for p_ in reversed(self._p):
            p_.__exit__(*exc)
        return False


def et_at(y: int, mo: int, d: int, h: int, mi: int):
    return at(datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc).isoformat())


# ---------------------------------------------------------------------------


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
    try:
        tb.TastySession(make_account(), sandbox, http=FakeTasty())
        check("a session without an ownership store is refused", False)
    except ConfigError:
        check("a session without an ownership store is refused", True)


def scenario_limits_and_symbols() -> None:
    print("\n-- F2 limits: validation, units, defaults vs hard ceilings")
    d = tb.resolve_limits({})
    check("defaults: 1/order, 1 held, 3.00 premium, $250 entry stop, 4 opens",
          d == {"max_contracts_per_order": 1, "max_open_contracts": 1, "max_premium_per_contract": 3.0,
                "max_daily_loss_usd": 250.0, "max_opens_per_day": 4}, str(d))
    check("hard ceilings: 1/order, 1 held, 5.00 premium, $500 entry stop, 6 opens",
          tb.HARD_CEILINGS == {"max_contracts_per_order": 1, "max_open_contracts": 1, "max_premium_per_contract": 5.0,
                               "max_daily_loss_usd": 500.0, "max_opens_per_day": 6}, str(tb.HARD_CEILINGS))
    limits = tb.resolve_limits({"tastytrade_limits": {"max_contracts_per_order": 50, "max_daily_loss_usd": 10_000,
                                                      "max_premium_per_contract": 1.5}})
    check("params cannot raise contracts above the hard ceiling", limits["max_contracts_per_order"] == 1)
    check("params cannot raise the entry stop above the hard ceiling", limits["max_daily_loss_usd"] == tb.HARD_MAX_DAILY_LOSS_USD)
    check("params can tighten a limit", limits["max_premium_per_contract"] == 1.5)
    loose = tb.resolve_limits({"tastytrade_limits": {"max_premium_per_contract": 4.0, "max_opens_per_day": 6}})
    check("params may also set a limit above its default, up to the ceiling (not 'tighten only')",
          loose["max_premium_per_contract"] == 4.0 and loose["max_opens_per_day"] == 6, str(loose))
    for bad, label in (({"max_opens_per_day": 0}, "zero"), ({"max_opens_per_day": 2.5}, "fractional count"),
                       ({"max_premium_per_contract": float("nan")}, "NaN"), ({"max_premium_per_contract": "3"}, "string"),
                       ({"max_loss": 100}, "unknown key"), ({"max_open_contracts": True}, "boolean")):
        try:
            tb.resolve_limits({"tastytrade_limits": bad})
            check(f"{label} limit is a config error", False)
        except ConfigError:
            check(f"{label} limit is a config error", True)
    check("bot-loss bound with a bot-basis stop at defaults = $250 + one $300 order = $550",
          tb.worst_case_daily_loss(d, "both") == 550.0 and tb.worst_case_daily_loss(d, "bot") == 550.0)
    check("bot-loss bound at the hard ceilings = $500 + $500 = $1,000",
          tb.worst_case_daily_loss(dict(tb.HARD_CEILINGS), "both") == 1000.0)
    check("whole-account basis alone gives no bot-loss bound (external gains can mask bot losses)",
          tb.worst_case_daily_loss(d, "account") is None)
    check("entry_stop_basis defaults to both", tb.resolve_entry_stop_basis({}) == "both")
    try:
        tb.resolve_entry_stop_basis({"entry_stop_basis": "vibes"})
        check("unknown entry_stop_basis is a config error", False)
    except ConfigError:
        check("unknown entry_stop_basis is a config error", True)
    for bad in ({"eod_unquoted_exit": "yolo"}, {"eod_unquoted_exit": "market", "eod_unquoted_exit_minutes": -1}):
        try:
            tb.resolve_exit_policy(bad)
            check(f"invalid exit policy {bad} is a config error", False)
        except ConfigError:
            check(f"invalid exit policy {bad} is a config error", True)
    check("exit policy defaults to retry (owner must opt in to market)", tb.resolve_exit_policy({}) == ("retry", 5.0))
    check("OCC symbol pads root to 6", tb.to_tasty_symbol(CALL) == "QQQ   301220C00500000")
    check("symbol round-trips", tb.to_crassus_symbol(tb.to_tasty_symbol(CALL)) == CALL)


def scenario_limit_boundaries(tmp: Path) -> None:
    print("\n-- F2 limits: boundaries")
    fake = FakeTasty()
    _, ex, _ = build(tmp / "b1", fake, quotes={CALL: quote(CALL, 2.95, 3.00)})
    r = submit(ex)
    check("premium exactly at the cap (3.00) is allowed", r.outcome_class == Outcome.FILLED, r.note or "")

    fake = FakeTasty()
    _, ex, _ = build(tmp / "b2", fake, quotes={CALL: quote(CALL, 2.96, 3.01)})
    r = submit(ex)
    check("premium one cent over the cap (3.01) is refused",
          r.outcome_class == Outcome.REJECTED and "premium" in (r.note or "") and fake.posts_to_orders() == 0, r.note or "")

    fake = FakeTasty()
    _, ex, guard = build(tmp / "b3", fake)
    guard.observe(ex.session.me())
    fake.nlv = 10_000 - 249.99
    r = submit(ex)
    check("down $249.99 (just under the entry stop) may still open", r.outcome_class == Outcome.FILLED, r.note or "")

    fake = FakeTasty()
    _, ex, guard = build(tmp / "b4", fake)
    guard.observe(ex.session.me())
    fake.nlv = 10_000 - 250.00
    r = submit(ex)
    check("down exactly $250 hits the entry stop", r.outcome_class == Outcome.REJECTED and "entry stop" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    fake.hold(OTHER, 3)  # pre-existing external exposure in the account
    _, ex, guard = build(tmp / "b5", fake)
    guard.observe(ex.session.me())
    fake.nlv = 9_700.0   # the account falls because of the external holding
    r = submit(ex)
    check("entry stop uses whole-account net liq, so external losses also stop new entries",
          r.outcome_class == Outcome.REJECTED and "entry stop" in (r.note or ""), r.note or "")
    check("...and external exposure does not count against the bot's own max_open_contracts",
          "max_open_contracts" not in (r.note or ""))


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
          body["legs"][0] == {"instrument-type": "Equity Option", "symbol": "QQQ   301220C00500000", "quantity": 1, "action": "Buy to Open"})
    check("external-identifier is the crassus execution_request_id", body["external-identifier"] == r.execution_request_id)
    check("intent persisted and left for the runner to finalize", ex.pending_intent() is not None)
    ex.finalize(r.execution_request_id)
    book = Book(session.me().trades)
    check("Book reconstructs the bot-owned position", book.position(CALL).quantity == 1)

    r2 = submit(ex, side="sell", strategy_id="eod_flatten")
    body2 = [kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders")][-1]
    check("eod_flatten may close even though it may not open", r2.outcome_class == Outcome.FILLED, r2.note or "")
    check("close is Sell to Close credit at the bid",
          body2["legs"][0]["action"] == "Sell to Close" and body2["price"] == "1.20" and body2["price-effect"] == "Credit")
    ex.finalize(r2.execution_request_id)
    check("account flat after close", Book(session.me().trades).is_flat)


def scenario_ownership(tmp: Path) -> None:
    print("\n-- F1 ownership: only the bot's own contracts are managed")
    fake = FakeTasty()
    session, ex, _ = build(tmp / "own", fake)
    own(session.ownership, CALL, 1)
    fake.hold(CALL, 1)                  # the bot's contract
    fake.hold(CALL, 2)                  # + 2 manual contracts of the same series
    fake.hold(PUT, 1)                   # a manual hedge
    fake.hold(OTHER, 4)                 # another underlying entirely
    state = session.me()
    book = Book(state.trades)
    check("mixed account: Book holds only the bot's 1 CALL",
          {s: p.quantity for s, p in book.positions.items()} == {CALL: 1}, str(book.summary()))
    check("other holdings stay visible as external_positions",
          state.external_positions == {CALL: 2, PUT: 1, OTHER: 4}, str(state.external_positions))
    check("external_positions appear in the ledger summary", state.summary()["external_positions"] == state.external_positions)

    # Restart: a fresh store and session on the same state dir.
    session2, ex2, _ = build(tmp / "own", fake)
    check("ownership survives restart", {s: p.quantity for s, p in Book(session2.me().trades).positions.items()} == {CALL: 1})

    r = submit(ex2, symbol=PUT, side="sell", strategy_id="eod_flatten")
    check("selling a manual hedge is refused and says why",
          r.outcome_class == Outcome.REJECTED and "did not open" in (r.note or "") and fake.posts_to_orders() == 0, r.note or "")
    r = submit(ex2, symbol=CALL, side="sell", qty=3, strategy_id="eod_flatten")
    check("closing more than the bot owns is refused even when the account holds more",
          r.outcome_class == Outcome.REJECTED and "bot-held 1" in (r.note or ""), r.note or "")
    r = submit(ex2, symbol=CALL, side="sell", qty=1, strategy_id="eod_flatten")
    sent = [kw["json"]["legs"][0] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders")]
    check("closing the bot's contract sends exactly its quantity",
          r.outcome_class == Outcome.FILLED and sent == [{"instrument-type": "Equity Option", "symbol": tasty(CALL), "quantity": 1, "action": "Sell to Close"}],
          str(sent))
    ex2.finalize(r.execution_request_id)
    after = session2.me()
    check("after the close the bot is flat and the manual contracts are untouched",
          Book(after.trades).is_flat and after.external_positions == {CALL: 2, PUT: 1, OTHER: 4}, str(after.external_positions))

    # A confirmed manual close of the bot's contract shrinks ownership; a
    # later manual purchase of the same series does not re-grow it.
    fake2 = FakeTasty()
    s3, _, _ = build(tmp / "own2", fake2)
    own(s3.ownership, CALL, 1)
    fake2.hold(CALL, 1)
    fake2.manual_order(CALL, "Sell to Close", 1)
    st = s3.me()
    check("a confirmed manual close (non-bot closing fill after the bot's fill) shrinks ownership",
          Book(st.trades).is_flat and st.ownership_resolved and st.ownership_resolved[0]["reason"] == "manual_close",
          str(st.ownership_resolved))
    fake2.hold(CALL, 1)
    st = s3.me()
    check("...and a later manual buy of the same series is external, never re-acquired",
          Book(st.trades).is_flat and st.external_positions == {CALL: 1})

    # Unknown ownership fails closed.
    fake3 = FakeTasty()
    s4, ex4, _ = build(tmp / "own3", fake3)
    s4.ownership.path.write_text("{not json", encoding="utf-8")
    try:
        s4.me()
        check("a corrupt ownership file fails closed", False)
    except tb.OwnershipError as exc:
        check("a corrupt ownership file fails closed", "malformed" in str(exc), str(exc))
    r = submit(ex4)
    check("...and no order is sent while ownership is unknown",
          r.outcome_class == Outcome.REJECTED and fake3.posts_to_orders() == 0 and "malformed" in (r.note or ""), r.note or "")


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
    _, ex, _ = build(tmp / "g3", fake, quotes={CALL: quote(CALL, 1.2, 1.25, fresh=False)})
    r = submit(ex)
    check("stale quote cannot open", r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    fake = FakeTasty()
    _, ex, _ = build(tmp / "g4", fake)
    ex.finalize(submit(ex).execution_request_id)
    r = submit(ex, symbol=PUT)
    check("max_open_contracts blocks a second position", r.outcome_class == Outcome.REJECTED and "max_open_contracts" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    session, ex, guard = build(tmp / "g5", fake)
    guard.observe(ex.session.me())  # start-of-day NLV 10,000
    fake.nlv = 9_740.0
    r = submit(ex, symbol=PUT)
    check("entry stop blocks opens after a $260 drawdown", r.outcome_class == Outcome.REJECTED and "entry stop" in (r.note or ""), r.note or "")
    own(session.ownership, CALL, 1)
    fake.hold(CALL, 1)
    r = submit(ex, side="sell", strategy_id="eod_flatten")
    check("entry stop still allows closing the bot's position", r.outcome_class == Outcome.FILLED, r.note or "")

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


def scenario_open_counting(tmp: Path) -> None:
    print("\n-- F3 opens counted exactly once, uncertainty reserves capacity")
    fake = FakeTasty()
    fake.submit_timeout = True
    session, ex, guard = build(tmp / "c1", fake)
    r = submit(ex)
    check("timeout then found filled: reconciled", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY, r.note or "")
    check("...counted as one open", guard.opens_today() == 1, str(guard._day()))
    check("...and owned by the bot", Book(session.me().trades).position(CALL).quantity == 1)
    rid = r.execution_request_id
    ex._account_for(rid, "Buy to Open", 1, final=True)  # a second reconcile of the same fill
    with patch.object(ex, "pending_intent", return_value={"execution_request_id": rid, "broker_action": "Buy to Open",
                                                          "created_utc": tb.clock.iso_utc()}):
        ex.recover_pending()  # and a recovery pass over it
    _, _, guard_restart = build(tmp / "c1", fake)
    check("repeated reconcile, recovery and restart still count it once",
          guard_restart.opens_today() == 1 and Book(session.me().trades).position(CALL).quantity == 1, str(guard_restart._day()))
    check("no duplicate submission", fake.posts_to_orders() == 1)

    fake = FakeTasty()
    fake.submit_timeout = True
    fake.record_on_timeout = False
    _, ex, guard = build(tmp / "c2", fake, params={"tastytrade_limits": {"max_opens_per_day": 1}})
    r = submit(ex)
    check("timeout with no order visible: ambiguous, slot reserved",
          r.outcome_class == Outcome.AMBIGUOUS and guard.opens_today() == 1, r.note or "")
    ex.finalize(r.execution_request_id)  # even if the intent were cleared...
    r2 = submit(ex)
    check("...the uncertain open still blocks another open (its contract stays reserved as exposure)",
          r2.outcome_class == Outcome.REJECTED and "unresolved opening orders" in (r2.note or "")
          and fake.posts_to_orders() == 1, r2.note or "")
    guard.limits = dict(guard.limits, max_open_contracts=2)
    r3 = submit(ex, symbol=PUT)
    check("...and with room for the exposure, its daily slot still counts",
          r3.outcome_class == Outcome.REJECTED and "max_opens_per_day" in (r3.note or ""), r3.note or "")

    fake = FakeTasty()
    fake.fill_on_submit = False
    _, ex, guard = build(tmp / "c3", fake)
    r = submit(ex)
    check("an open that is cancelled unfilled releases its slot",
          r.outcome_class == Outcome.REJECTED and guard.opens_today() == 0, r.note or "")

    fake = FakeTasty()
    fake.submit_error_echo = "nope"
    _, ex, guard = build(tmp / "c4", fake)
    r = submit(ex)
    check("an open the broker rejects outright releases its slot", r.outcome_class == Outcome.REJECTED and guard.opens_today() == 0)

    # Partial fill: 2 owned, close 2, broker fills 1 then the rest is cancelled.
    fake = FakeTasty()
    fake.partial_fill = 1
    session, ex, guard = build(tmp / "c5", fake)
    own(session.ownership, CALL, 2)
    fake.hold(CALL, 2)
    r = submit(ex, side="sell", qty=2, strategy_id="eod_flatten")
    check("partial fill then cancel is recorded filled with a partial note",
          r.outcome_class == Outcome.FILLED and "Partially filled (1)" in (r.note or ""), r.note or "")
    check("...and ownership reflects exactly the filled part", Book(session.me().trades).position(CALL).quantity == 1)


def scenario_uncertain_state(tmp: Path) -> None:
    print("\n-- F4 unknown or incomplete order state is never read as a rejection")
    for status, label in (("Mystery", "unrecognised status"), ("", "missing status")):
        fake = FakeTasty()
        fake.fill_on_submit = False
        fake.status_override = status
        _, ex, guard = build(tmp / f"u-{label[:4]}", fake)
        r = submit(ex)
        check(f"{label} stays ambiguous", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")
        check(f"...{label}: intent retained, slot reserved, no resubmission",
              ex.pending_intent() is not None and guard.opens_today() == 1 and fake.posts_to_orders() == 1)

    fake = FakeTasty()
    fake.fill_on_submit = False
    _, ex, _ = build(tmp / "u-term", fake)
    order = fake._new_order({"external-identifier": "x", "legs": [{"symbol": tasty(CALL), "action": "Buy to Open", "quantity": 1}]}, "Cancelled")
    del order["legs"][0]["remaining-quantity"]
    r = ex._settle(dict(order), "x", None, action="Buy to Open")
    check("a terminal status without fill data stays ambiguous", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")

    # Fill that only becomes visible in the order history after 120 seconds:
    # the broker accepted and filled the order, but the submit timed out and
    # the order is not listed yet.
    fake2 = FakeTasty()
    fake2.submit_timeout = True
    session, ex, guard = build(tmp / "u-late", fake2)
    t0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    original_new = fake2._new_order

    def hide_new(body, status):
        o = original_new(body, status)
        fake2.hidden.add(o["external-identifier"])
        return o

    fake2._new_order = hide_new
    with at(t0.isoformat()):
        r = submit(ex)
    rid = r.execution_request_id
    check("order invisible at submit time: ambiguous", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")
    with at((t0 + timedelta(seconds=200)).isoformat()):
        r1 = ex.recover_pending()
    check("still invisible at 200s: still ambiguous (no 120s cutoff)", r1.outcome_class == Outcome.AMBIGUOUS, r1.note or "")
    fake2.hidden.clear()
    with at((t0 + timedelta(seconds=260)).isoformat()):
        r2 = ex.recover_pending()
    check("fill that appears after 120s is reconciled, not discarded",
          r2.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY and r2.execution_request_id == rid, r2.note or "")
    check("...owned and counted once, never resubmitted",
          Book(session.me().trades).position(CALL).quantity == 1 and guard.opens_today() == 1 and fake2.posts_to_orders() == 1)

    # Our order on page two of the order history.
    fake = FakeTasty()
    fake.submit_timeout = True
    fake.filler_orders = 250
    _, ex, _ = build(tmp / "u-page", fake)
    r = submit(ex)
    pages = [kw.get("params", {}).get("page-offset") for m, u, kw in fake.calls if m == "GET" and u.endswith("/orders")]
    check("an order on page two of history is found", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY and 1 in pages, f"pages={pages}")

    fake = FakeTasty()
    fake.submit_timeout = True
    fake.record_on_timeout = False
    fake.filler_orders = 200
    fake.paginate = False
    _, ex, _ = build(tmp / "u-nopage", fake)
    r = submit(ex)
    check("a full page with no pagination metadata is not treated as complete",
          r.outcome_class == Outcome.AMBIGUOUS and "completeness" in (r.note or ""), r.note or "")

    fake = FakeTasty()
    fake.submit_timeout = True
    _, ex, _ = build(tmp / "u-outage", fake)
    fake.orders_status = 503
    r = submit(ex)
    check("order-history outage keeps the outcome ambiguous", r.outcome_class == Outcome.AMBIGUOUS, r.note or "")
    with at(tb.clock.iso_utc()):
        rr = ex.recover_pending()
    check("...and recovery during the outage stays ambiguous", rr.outcome_class == Outcome.AMBIGUOUS, rr.note or "")

    fake = FakeTasty()
    fake.fill_on_submit = False
    fake.fill_on_cancel = True
    session, ex, guard = build(tmp / "u-race", fake)
    r = submit(ex)
    check("cancel/fill race: the fill wins and is recorded filled",
          r.outcome_class == Outcome.FILLED and Book(session.me().trades).position(CALL).quantity == 1 and guard.opens_today() == 1,
          r.note or "")


def scenario_recovery(tmp: Path) -> None:
    print("\n-- crash recovery never replays")
    fake = FakeTasty()
    _, ex, _ = build(tmp / "r1", fake)
    ex._persist_intent({"execution_request_id": "never-sent", "symbol": CALL, "side": "buy", "quantity": 1,
                        "created_utc": "2026-11-09T14:59:59+00:00"})
    r = ex.recover_pending()
    check("an intent that crashed before submission is rejected immediately, not replayed",
          r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")

    ex._persist_intent({"execution_request_id": "sent-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "broker_action": "Buy to Open", "created_utc": "2026-11-09T14:00:00+00:00"})
    outcomes = []
    for minute in (16, 17, 18):
        with at(f"2026-11-09T14:{minute:02d}:00+00:00"):
            outcomes.append(ex.recover_pending().outcome_class)
    check("R4 three rapid scans (minutes 16-18, a 2-minute span) do not establish absence",
          outcomes == [Outcome.AMBIGUOUS] * 3, str(outcomes))
    with at("2026-11-09T14:30:00+00:00"):
        o4 = ex.recover_pending().outcome_class
    with at("2026-11-09T14:31:00+00:00"):
        o5 = ex.recover_pending()
    check("...absence is declared only once complete scans span 15 minutes (16 -> 31)",
          o4 == Outcome.AMBIGUOUS and o5.outcome_class == Outcome.REJECTED and "900s" in (o5.note or ""), o5.note or "")
    check("...and is never resubmitted", fake.posts_to_orders() == 0)

    # Late fill after absence was declared: still attributed via the watch list.
    fake = FakeTasty()
    session, ex, guard = build(tmp / "r3", fake)
    session.ownership.register("late-1", CALL, "Buy to Open")
    guard.reserve_open("late-1")
    order = fake._new_order({"external-identifier": "late-1", "legs": [{"symbol": tasty(CALL), "action": "Buy to Open", "quantity": 1}]}, "Live")
    fake._fill(order)
    state = session.me()
    check("a fill that surfaces after absence was declared is still attributed to the bot",
          Book(state.trades).position(CALL).quantity == 1 and state.late_fills == ["late-1"], str(state.summary()))
    check("...and its open stayed counted", guard.opens_today() == 1)

    fake = FakeTasty()
    session, ex, _ = build(tmp / "r2", fake)
    session.ownership.register("known-1", CALL, "Buy to Open")
    order = fake._new_order({"external-identifier": "known-1", "legs": [{"symbol": tasty(CALL), "action": "Buy to Open", "quantity": 1}]}, "Live")
    fake._fill(order)
    ex._persist_intent({"execution_request_id": "known-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "broker_action": "Buy to Open", "created_utc": "2026-11-09T14:00:00+00:00"})
    r = ex.recover_pending()
    check("intent found filled at broker is reconciled", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY)


def scenario_ambiguity(tmp: Path) -> None:
    print("\n-- ambiguity and cancel")
    fake = FakeTasty()
    fake.submit_timeout = True
    _, ex, _ = build(tmp / "a1", fake)
    r = submit(ex)
    check("timeout reconciles by external-identifier", r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY, r.note or "")
    check("timeout never resubmits", fake.posts_to_orders() == 1)

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


def scenario_close_without_quote(tmp: Path) -> None:
    print("\n-- F5 close without a usable quote (executor level)")
    for qmap, label in (({}, "missing"), ({CALL: quote(CALL, 1.30, 1.20)}, "crossed/malformed"),
                        ({CALL: quote(CALL, 1.20, 1.25, fresh=False)}, "stale")):
        fake = FakeTasty()
        session, ex, _ = build(tmp / f"cq-{label[:4]}", fake, quotes=qmap)
        own(session.ownership, CALL, 1)
        fake.hold(CALL, 1)
        r = submit(ex, side="sell", strategy_id="eod_flatten")
        check(f"{label} quote, policy retry: no order sent, reason logged",
              r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0 and "eod_unquoted_exit=retry" in (r.note or ""),
              r.note or "")

    fake = FakeTasty()
    session, ex, _ = build(tmp / "cq-mkt", fake, quotes={}, params={"eod_unquoted_exit": "market"})
    own(session.ownership, CALL, 1)
    fake.hold(CALL, 1)
    r = submit(ex, side="sell", strategy_id="oi_skew_qqq")
    check("policy market still refuses an unquoted close from the strategy itself",
          r.outcome_class == Outcome.REJECTED and fake.posts_to_orders() == 0, r.note or "")
    with et_at(2026, 11, 9, 15, 57):
        r = submit(ex, side="sell", strategy_id="eod_flatten")
    body = [kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith("/orders")][-1]
    check("policy market lets eod_flatten close at market inside the final window",
          r.outcome_class == Outcome.FILLED and body["order-type"] == "Market" and "price" not in body, r.note or "")


def scenario_runner_eod(tmp: Path) -> None:
    print("\n-- F5 end-of-day exit through the real Runner (flatten -> broker)")
    from crassus import clock
    from crassus.runner import Runner

    def run_case(name: str, qmap: dict, minutes_to_close: float, params: dict | None = None,
                 manual: bool = False, exec_qmap: dict | None = None,
                 day: tuple[int, int, int] = (2026, 11, 9), close_hour: int = 16) -> tuple[list[dict], FakeTasty]:
        fake = FakeTasty()
        base = tmp / f"run-{name}"
        params = params or {}
        account = make_account(params, alias="Tasty")
        with patch.dict(os.environ, ENV_SANDBOX, clear=False), patch.object(tb.requests, "Session", return_value=fake):
            runner = Runner([account], bot_registration_key="k" * 32, ledger_dir=base / "logs", state_dir=base / "state")
        own(runner.sessions["Tasty"].ownership, CALL, 1)
        fake.hold(CALL, 1)
        if manual:
            fake.hold(PUT, 2)
        runner.quotes.quotes = lambda syms: {s: qmap[s] for s in syms if s in qmap}
        runner.executors["Tasty"].quotes = runner.quotes.quotes
        if exec_qmap is not None:  # the quote seen at execution differs (deteriorated)
            runner.executors["Tasty"].quotes = lambda syms: {s: exec_qmap[s] for s in syms if s in exec_qmap}
        runner.executors["Tasty"].fill_timeout_s = 0.05
        runner.executors["Tasty"].poll_interval_s = 0.0
        runner.executors["Tasty"]._sleep = lambda s: None
        now = datetime(*day, close_hour, 0, tzinfo=ET) - timedelta(minutes=minutes_to_close)
        with patch.object(clock, "now_et", return_value=now):
            runner._run_account(account, None, "open")
        records = [json.loads(line) for line in runner.ledger.paths.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        return records, fake

    def orders(fake: FakeTasty) -> list[dict]:
        return [kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith(f"/accounts/{fake.acct}/orders")]

    recs, fake = run_case("valid", {CALL: quote(CALL, 1.20, 1.25)}, 10)
    last = recs[-1]
    check("valid quote: runner flattens with a limit sell at the bid",
          last["strategy_id"] == "eod_flatten" and last["outcome_class"] == "filled"
          and [o["price"] for o in orders(fake)] == ["1.20"], f"{last['outcome_class']} {orders(fake)}")

    recs, fake = run_case("stale", {CALL: quote(CALL, 1.20, 1.25, fresh=False)}, 10)
    last = recs[-1]
    check("stale quote, policy retry: no order, explicit no_trade reason in the ledger",
          last["outcome_class"] == "no_trade" and "no live/fresh quote" in last["reason"] and not orders(fake), last["reason"][:90])

    recs, fake = run_case("missing-early", {}, 10, {"eod_unquoted_exit": "market"})
    check("missing quote, policy market, 10 min out: still waits (outside the final 5 min)",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])

    recs, fake = run_case("missing-late", {}, 3, {"eod_unquoted_exit": "market"})
    last = recs[-1]
    check("missing quote, policy market, 3 min out: runner reaches the broker's market close",
          last["outcome_class"] == "filled" and [o["order-type"] for o in orders(fake)] == ["Market"]
          and (last["decision"]["metadata"] or {}).get("unquoted_exit") is True, f"{last['outcome_class']} {orders(fake)}")

    recs, fake = run_case("missing-retry", {}, 3)
    check("missing quote, policy retry, 3 min out: no order, reason says why",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])

    recs, fake = run_case("malformed", {CALL: quote(CALL, 1.30, 1.20)}, 10)
    last = recs[-1]
    check("malformed (crossed) quote, policy retry: refused at the broker with a logged reason",
          last["outcome_class"] == "rejected" and "eod_unquoted_exit=retry" in (last.get("note") or "") and not orders(fake),
          (last.get("note") or "")[:90])

    recs, fake = run_case("malformed-mkt", {CALL: quote(CALL, 1.30, 1.20)}, 3, {"eod_unquoted_exit": "market"})
    check("malformed quote, policy market: closes at market",
          recs[-1]["outcome_class"] == "filled" and [o["order-type"] for o in orders(fake)] == ["Market"])

    mkt = {"eod_unquoted_exit": "market"}
    recs, fake = run_case("crossed-mkt-early", {CALL: quote(CALL, 1.30, 1.20)}, 10, mkt)
    check("R5 crossed quote, policy market, 10 min out: no market order before the final 5 minutes",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])
    recs, fake = run_case("zero-bid-mkt-early", {CALL: quote(CALL, 0.0, 0.05)}, 10, mkt)
    check("R5 zero-bid quote, policy market, 10 min out: no order",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])
    recs, fake = run_case("zero-bid-mkt-late", {CALL: quote(CALL, 0.0, 0.05)}, 3, mkt)
    check("R5 zero-bid quote, policy market, 3 min out: market close",
          recs[-1]["outcome_class"] == "filled" and [o["order-type"] for o in orders(fake)] == ["Market"])
    recs, fake = run_case("crossed-retry-early", {CALL: quote(CALL, 1.30, 1.20)}, 10, {"eod_unquoted_exit": "retry"})
    check("R5 crossed quote, policy retry (brokerage params): flatten waits, nothing sent",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])
    recs, fake = run_case("deteriorate-early", {CALL: quote(CALL, 1.20, 1.25)}, 10, mkt, exec_qmap={})
    last = recs[-1]
    check("R5 quote good at decision but gone at execution, 10 min out: executor refuses the market order",
          last["outcome_class"] == "rejected" and "outside the final 5" in (last.get("note") or "") and not orders(fake),
          (last.get("note") or "")[:100])
    recs, fake = run_case("deteriorate-crossed", {CALL: quote(CALL, 1.20, 1.25)}, 10, mkt,
                          exec_qmap={CALL: quote(CALL, 1.30, 1.20)})
    check("R5 quote crosses between decision and execution, 10 min out: no order",
          recs[-1]["outcome_class"] == "rejected" and not orders(fake), (recs[-1].get("note") or "")[:100])
    recs, fake = run_case("deteriorate-late", {CALL: quote(CALL, 1.20, 1.25)}, 3, mkt, exec_qmap={})
    check("R5 quote gone at execution inside the final window: market close",
          recs[-1]["outcome_class"] == "filled" and [o["order-type"] for o in orders(fake)] == ["Market"])
    recs, fake = run_case("early-close-late", {}, 3, mkt, day=(2026, 11, 27), close_hour=13)
    check("R5 early-close day (13:00 close), 3 min out: market close allowed",
          recs[-1]["outcome_class"] == "filled" and [o["order-type"] for o in orders(fake)] == ["Market"],
          recs[-1]["outcome_class"])
    recs, fake = run_case("early-close-early", {}, 10, mkt, day=(2026, 11, 27), close_hour=13)
    check("R5 early-close day, 10 min before 13:00: waits",
          recs[-1]["outcome_class"] == "no_trade" and not orders(fake), recs[-1]["reason"][:90])

    recs, fake = run_case("mixed", {CALL: quote(CALL, 1.20, 1.25), PUT: quote(PUT, 0.90, 0.95)}, 10, manual=True)
    sent = [o["legs"][0]["symbol"] for o in orders(fake)]
    check("mixed account through the runner: only the bot's contract is flattened",
          sent == [tasty(CALL)] and recs[-1]["account_state_before"]["external_positions"] == {PUT: 2}, str(sent))


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
    from crassus.config import account_gaps

    catalog = tmp / "catalog.json"
    catalog.write_text(json.dumps({"accounts": [
        {"alias": "Paper", "username": "paper_bot", "password_env": "PW_PAPER", "strategy_id": "oi_skew_qqq"},
        {"alias": "Tasty", "username": "tasty_bot", "broker": "tastytrade", "strategy_id": "oi_skew_qqq"},
    ]}))
    with patch.dict(os.environ, {"PW_PAPER": "pw"}, clear=True):
        accounts = load_accounts(catalog_path=catalog, override_path=tmp / "none.json")
    check("tastytrade entry loads without a Worker password",
          [a.broker for a in accounts] == ["worker", "tastytrade"])
    gaps = account_gaps(catalog_path=catalog, override_path=tmp / "none.json", environ={"PW_PAPER": "pw"})
    check("preflight reports the missing CRASSUS_TASTY_* names, not a Worker password",
          sorted(g["missing"] for g in gaps) == sorted(["CRASSUS_TASTY_MODE", "CRASSUS_TASTY_ACCOUNT_NUMBER",
                                                         "CRASSUS_TASTY_CLIENT_SECRET", "CRASSUS_TASTY_REFRESH_TOKEN"]),
          str(gaps))
    gaps = account_gaps(catalog_path=catalog, override_path=tmp / "none.json",
                        environ={"PW_PAPER": "pw", **ENV_SANDBOX, "CRASSUS_TASTY_MODE": "live"})
    check("preflight in live mode also requires the owner's approval variable",
          [g["missing"] for g in gaps] == ["CRASSUS_TASTY_LIVE_APPROVAL"], str(gaps))

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

    example = json.loads((Path(__file__).resolve().parent.parent / "accounts.example.json").read_text(encoding="utf-8"))
    entry = next(e for e in example["accounts"] if e.get("broker") == "tastytrade")
    check("catalog entry is inactive with the default retry exit policy",
          entry["active"] is False and entry["params"]["eod_unquoted_exit"] == "retry")
    check("catalog limits validate", tb.resolve_limits(entry["params"]) == tb.resolve_limits({}))


def scenario_unquoted_window(tmp: Path) -> None:
    print("\n-- R5 unquoted market exit: window enforced at the executor")

    def attempt(name: str, when, qmap: dict | None = None, params: dict | None = None):
        fake = FakeTasty()
        session, ex, _ = build(tmp / f"uw-{name}", fake, quotes={} if qmap is None else qmap,
                               params={"eod_unquoted_exit": "market", **(params or {})})
        with when:
            own(session.ownership, CALL, 1)
            fake.hold(CALL, 1)
            r = submit(ex, side="sell", strategy_id="eod_flatten")
        sent = [kw["json"]["order-type"] for m, u, kw in fake.calls if m == "POST" and u.endswith(f"/accounts/{fake.acct}/orders")]
        return r, sent

    r, sent = attempt("10min", et_at(2026, 11, 9, 15, 50))
    check("missing quote, market policy, 10 min out: executor refuses (outside the final 5 minutes)",
          r.outcome_class == Outcome.REJECTED and not sent and "outside the final 5" in (r.note or ""), r.note or "")
    r, sent = attempt("crossed10", et_at(2026, 11, 9, 15, 50), {CALL: quote(CALL, 1.30, 1.20)})
    check("crossed quote sent straight to the executor at 10 min out: refused, no early market order",
          r.outcome_class == Outcome.REJECTED and not sent, r.note or "")
    r, sent = attempt("zerobid10", et_at(2026, 11, 9, 15, 50), {CALL: quote(CALL, 0.0, 0.05)})
    check("zero-bid quote at 10 min out: refused", r.outcome_class == Outcome.REJECTED and not sent, r.note or "")
    r, sent = attempt("zerobid3", et_at(2026, 11, 9, 15, 57), {CALL: quote(CALL, 0.0, 0.05)})
    check("zero-bid quote at 3 min out: market close", r.outcome_class == Outcome.FILLED and sent == ["Market"], r.note or "")
    r, sent = attempt("after", et_at(2026, 11, 9, 16, 1))
    check("after the close: refused (session closed)", r.outcome_class == Outcome.REJECTED and not sent
          and "closed" in (r.note or ""), r.note or "")
    r, sent = attempt("early-ok", et_at(2026, 11, 27, 12, 57))
    check("early-close day, 12:57 (3 min before 13:00): market close", r.outcome_class == Outcome.FILLED and sent == ["Market"],
          r.note or "")
    r, sent = attempt("early-late", et_at(2026, 11, 27, 15, 57))
    check("early-close day, 15:57 (ordinary-day window, after the 13:00 close): refused",
          r.outcome_class == Outcome.REJECTED and not sent, r.note or "")
    r, sent = attempt("holiday", et_at(2026, 11, 26, 15, 57))
    check("exchange holiday: refused", r.outcome_class == Outcome.REJECTED and not sent and "closed today" in (r.note or ""),
          r.note or "")
    r, sent = attempt("wider", et_at(2026, 11, 9, 15, 50), params={"eod_unquoted_exit_minutes": 15})
    check("the window is the owner's eod_unquoted_exit_minutes (15 here), not a hard-coded 5",
          r.outcome_class == Outcome.FILLED and sent == ["Market"], r.note or "")


def scenario_state_identity(tmp: Path) -> None:
    print("\n-- R2 state bound to broker environment and account")
    base = tmp / "ident"
    sandbox_fake = FakeTasty()
    s_sb, ex_sb, g_sb = build(base, sandbox_fake)
    r = submit(ex_sb)
    check("sandbox bot owns one CALL", r.outcome_class == Outcome.FILLED and Book(s_sb.me().trades).position(CALL).quantity == 1)
    ex_sb._persist_intent({"execution_request_id": "sb-pending", "symbol": CALL, "side": "buy", "quantity": 1,
                           "broker_action": "Buy to Open", "created_utc": tb.clock.iso_utc()})

    live_env = {**ENV_SANDBOX, "CRASSUS_TASTY_MODE": "live", "CRASSUS_TASTY_ACCOUNT_NUMBER": ACCT2,
                "CRASSUS_TASTY_LIVE_APPROVAL": f"live:{ACCT2}"}
    live_fake = FakeTasty(acct=ACCT2)
    live_fake.hold(CALL, 1)  # the owner's own matching manual contract in the live account
    s_lv, ex_lv, g_lv = build(base, live_fake, env=live_env)
    st = s_lv.me()
    check("same alias, live account: the sandbox-owned CALL is not the bot's; the live manual CALL is external",
          Book(st.trades).is_flat and st.external_positions == {CALL: 1}, str(st.summary()))
    check("state directories differ by environment and account",
          s_sb.ownership.state_dir != s_lv.ownership.state_dir
          and s_lv.ownership.state_dir.parent == base / "tastytrade", str(s_lv.ownership.state_dir))
    check("the sandbox in-flight intent is not the live executor's", ex_lv.pending_intent() is None)
    check("sandbox risk-day opens do not count against live", g_sb.opens_today() == 1 and g_lv.opens_today() == 0)
    r = submit(ex_lv, side="sell", strategy_id="eod_flatten")
    check("...so the live flatten cannot sell the manual CALL",
          r.outcome_class == Outcome.REJECTED and live_fake.posts_to_orders() == 0, r.note or "")
    check("the account number is stored only as a digest",
          ACCT2 not in (s_lv.ownership.state_dir / "identity.json").read_text()
          and ACCT2 not in str(s_lv.ownership.state_dir))

    # A sandbox ownership file copied into the live directory is refused.
    s_lv.ownership.path.write_text(s_sb.ownership.path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        s_lv.me()
        check("a copied ownership file from another environment is refused", False)
    except tb.OwnershipError as exc:
        check("a copied ownership file from another environment is refused", "different broker account" in str(exc), str(exc))
    r = submit(ex_lv)
    check("...and nothing is sent", r.outcome_class == Outcome.REJECTED and live_fake.posts_to_orders() == 0, r.note or "")

    # A copied risk-day file is refused too.
    fake3 = FakeTasty(acct=ACCT2)
    env3 = {**live_env, "CRASSUS_TASTY_ACCOUNT_NUMBER": "5WT00003", "CRASSUS_TASTY_LIVE_APPROVAL": "live:5WT00003"}
    fake3.acct = "5WT00003"
    s3, ex3, g3 = build(base, fake3, env=env3)
    g3._path.write_text(g_sb._path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        g3.opens_today()
        check("a copied risk-day file from another account is refused", False)
    except tb.OwnershipError:
        check("a copied risk-day file from another account is refused", True)
    check("an account change (live 5WT00002 -> 5WT00003) gets its own empty state",
          s3.ownership.state_dir not in (s_lv.ownership.state_dir, s_sb.ownership.state_dir) and not s3.ownership.load()["owned"])

    # A tampered identity marker fails closed at wiring time.
    marker = s_lv.ownership.state_dir / "identity.json"
    marker.write_text(json.dumps({"alias": "someone else", "mode": "live", "account_sha256": "0" * 64}))
    try:
        build(base, FakeTasty(acct=ACCT2), env=live_env)
        check("a state directory whose identity does not match is refused", False)
    except ConfigError:
        check("a state directory whose identity does not match is refused", True)

    # Unscoped state from the previous build has no known account.
    legacy_base = tmp / "legacy"
    legacy_base.mkdir()
    (legacy_base / "OI Skew (tastytrade).tasty_owned.json").write_text(json.dumps({"owned": {CALL: 1}, "orders": {}}))
    try:
        build(legacy_base, FakeTasty())
        check("unscoped state from an earlier build needs an explicit migration", False)
    except ConfigError as exc:
        check("unscoped state from an earlier build needs an explicit migration", "unscoped" in str(exc), str(exc))

    # The runner hook uses the same scoping.
    from crassus.runner import Runner
    with patch.dict(os.environ, ENV_SANDBOX, clear=False), patch.object(tb.requests, "Session", return_value=FakeTasty()):
        runner = Runner([make_account(alias="Tasty")], bot_registration_key="k" * 32,
                        ledger_dir=tmp / "ident-run" / "logs", state_dir=tmp / "ident-run" / "state")
    ex_run = runner.executors["Tasty"]
    check("runner wiring: ownership, risk day and in-flight intent all live in the scoped directory",
          ex_run.state_dir == ex_run.ownership.state_dir == ex_run.guard.state_dir
          and ex_run.state_dir.parent == tmp / "ident-run" / "state" / "tastytrade"
          and ex_run.guard.halt_file == tmp / "ident-run" / "state" / "TASTY_HALT", str(ex_run.state_dir))


def scenario_position_lag(tmp: Path) -> None:
    print("\n-- R1 ownership survives a positions snapshot that lags or drops the bot's fill")
    fake = FakeTasty()
    fake.positions_lag = True
    session, ex, guard = build(tmp / "lag1", fake)
    r = submit(ex)
    after = r.state_after
    check("fill credited while /positions still shows nothing: ownership kept (the _settle -> me() ordering)",
          r.outcome_class == Outcome.FILLED and Book(after.trades).position(CALL).quantity == 1
          and CALL in after.ownership_discrepancies, str(after.summary()))
    ex.finalize(r.execution_request_id)
    st = session.me()
    check("a later snapshot still lagging keeps the exposure and reports the discrepancy",
          Book(st.trades).position(CALL).quantity == 1 and st.ownership_discrepancies[CALL]["snapshots"] >= 2
          and st.external_positions == {}, str(st.summary()))
    guard.limits = dict(guard.limits, max_open_contracts=2)
    r2 = submit(ex, symbol=PUT)
    check("while the discrepancy is unexplained, new opens are blocked",
          r2.outcome_class == Outcome.REJECTED and "ownership discrepancy" in (r2.note or "") and fake.posts_to_orders() == 1,
          r2.note or "")
    fake.catch_up()
    st = session.me()
    check("when the position appears it is the bot's (not external) and the discrepancy clears",
          Book(st.trades).position(CALL).quantity == 1 and not st.ownership_discrepancies and st.external_positions == {},
          str(st.summary()))
    guard.limits = dict(guard.limits, max_open_contracts=1)
    r3 = submit(ex, symbol=PUT)
    check("...and the real holding still blocks a second contract (no double exposure)",
          r3.outcome_class == Outcome.REJECTED and "max_open_contracts" in (r3.note or "") and fake.posts_to_orders() == 1,
          r3.note or "")

    # Disappearance and reappearance of a held position.
    fake = FakeTasty()
    session, ex, _ = build(tmp / "lag2", fake)
    own(session.ownership, CALL, 1)
    fake.hold(CALL, 1)
    saved, fake.positions = fake.positions, []
    st = session.me()
    check("a held position missing from one snapshot stays owned, flagged as a discrepancy",
          Book(st.trades).position(CALL).quantity == 1 and CALL in st.ownership_discrepancies)
    r = submit(ex, side="sell", strategy_id="eod_flatten")
    check("...the EOD close is held back (never sent against a position the broker does not show)",
          r.outcome_class == Outcome.REJECTED and "Broker shows 0" in (r.note or "") and fake.posts_to_orders() == 0,
          r.note or "")
    fake.positions = saved
    r = submit(ex, side="sell", strategy_id="eod_flatten")
    check("when it reappears, the bot closes its contract normally",
          r.outcome_class == Outcome.FILLED and fake.posts_to_orders() == 1, r.note or "")

    # Mixed account: the bot's contract lags but a manual one of the same
    # series is visible. The close must not take the manual contract.
    fake = FakeTasty()
    fake.positions_lag = True
    session, ex, _ = build(tmp / "lag3", fake)
    fake.positions_lag = False
    fake.hold(CALL, 1)  # manual contract, visible
    own(session.ownership, CALL, 1)  # the bot's own fill, not yet in /positions
    st = session.me()
    check("bot fill lagging beside one manual contract of the same series: the visible one is treated as the "
          "bot's (indistinguishable by position), so exposure is never undercounted",
          Book(st.trades).position(CALL).quantity == 1 and st.external_positions == {}, str(st.summary()))

    # Evidence-based shrink: expiry.
    fake = FakeTasty()
    session, _, _ = build(tmp / "lag4", fake)
    own(session.ownership, EXPIRED, 1)
    st = session.me()
    check("an owned contract past its expiry date and gone from the broker is released as expired",
          Book(st.trades).is_flat and st.ownership_resolved and st.ownership_resolved[0]["reason"] == "expired"
          and not st.ownership_discrepancies, str(st.ownership_resolved))

    # A manual close from *before* the bot's fill is not evidence.
    fake = FakeTasty()
    session, _, _ = build(tmp / "lag5", fake)
    fake.manual_order(CALL, "Buy to Open", 1, when="2026-01-01T15:00:00+00:00")
    fake.manual_order(CALL, "Sell to Close", 1, when="2026-01-01T16:00:00+00:00")
    own(session.ownership, CALL, 1)
    with patch.object(tb.clock, "now_et", return_value=datetime(2026, 1, 2, 10, 0, tzinfo=ET)):
        st = session.me()
    check("a non-bot close older than the bot's fill does not explain the gap (stays a discrepancy)",
          Book(st.trades).position(CALL).quantity == 1 and CALL in st.ownership_discrepancies, str(st.summary()))


def scenario_loss_basis(tmp: Path) -> None:
    print("\n-- R3 entry stop basis: bot-attributed vs whole-account loss")

    def round_trip(ex, qmap, sell_bid: float) -> None:
        qmap[CALL] = quote(CALL, 2.95, 3.00)
        r = submit(ex)
        assert r.outcome_class == Outcome.FILLED, r.note
        ex.finalize(r.execution_request_id)
        qmap[CALL] = quote(CALL, sell_bid, sell_bid + 0.05)
        r = submit(ex, side="sell")
        assert r.outcome_class == Outcome.FILLED, r.note
        ex.finalize(r.execution_request_id)

    # External gains mask bot losses on the account basis.
    for basis in ("account", "both", "bot"):
        fake = FakeTasty()
        qmap: dict = {}
        _, ex, guard = build(tmp / f"mask-{basis}", fake, quotes=qmap, params={"entry_stop_basis": basis})
        opens = 0
        for _ in range(4):
            qmap[CALL] = quote(CALL, 2.95, 3.00)
            r = submit(ex)
            if r.outcome_class != Outcome.FILLED:
                break
            opens += 1
            ex.finalize(r.execution_request_id)
            qmap[CALL] = quote(CALL, 0.00 + 0.05, 0.10)
            ex.finalize(submit(ex, side="sell").execution_request_id)
            fake.nlv = 10_000.0  # manual holdings gained what the bot lost
        loss = guard.bot_loss_today()
        bound = tb.worst_case_daily_loss(guard.limits, basis)
        if basis == "account":
            check("account basis: four $295 bot losses offset by manual gains all pass (the $1,180 counterexample)",
                  opens == 4 and loss == 1180.0 and bound is None, f"opens={opens} loss={loss}")
        else:
            check(f"{basis} basis: the bot stops after its own loss reaches the stop, whatever the account does",
                  opens == 1 and loss == 295.0 and loss <= bound and "bot entry stop" in (r.note or "" if opens < 4 else ""),
                  f"opens={opens} loss={loss} bound={bound}")

    # The bound with a sub-stop first loss: second open allowed, then stop.
    fake = FakeTasty()
    qmap = {}
    _, ex, guard = build(tmp / "bound", fake, quotes=qmap)
    round_trip(ex, qmap, 0.60)   # -240
    round_trip(ex, qmap, 0.05)   # -295
    qmap[CALL] = quote(CALL, 2.95, 3.00)
    r = submit(ex)
    loss = guard.bot_loss_today()
    check("defaults: a $240 loss leaves room for one more open; after it the total ($535) stays within the $550 bound",
          r.outcome_class == Outcome.REJECTED and "bot entry stop" in (r.note or "") and loss == 535.0
          and loss <= tb.worst_case_daily_loss(guard.limits), f"loss={loss} {r.note}")

    # Cash flows.
    fake = FakeTasty()
    _, ex, guard = build(tmp / "wd-bot", fake, params={"entry_stop_basis": "bot"})
    guard.observe(ex.session.me())
    fake.nlv = 9_000.0  # a $1,000 withdrawal
    r = submit(ex)
    check("withdrawal: bot basis ignores it (the bot lost nothing)", r.outcome_class == Outcome.FILLED, r.note or "")
    fake = FakeTasty()
    _, ex, guard = build(tmp / "wd-both", fake)
    guard.observe(ex.session.me())
    fake.nlv = 9_000.0
    r = submit(ex)
    check("withdrawal: the default (both) treats it as an account loss and stops entries",
          r.outcome_class == Outcome.REJECTED and "account entry stop" in (r.note or ""), r.note or "")
    for basis in ("account", "both"):
        fake = FakeTasty()
        qmap = {}
        _, ex, guard = build(tmp / f"dep-{basis}", fake, quotes=qmap, params={"entry_stop_basis": basis})
        guard.observe(ex.session.me())
        round_trip(ex, qmap, 0.05)       # bot -295
        fake.nlv = 10_000.0 - 295 + 1_000  # deposit masks it
        qmap[CALL] = quote(CALL, 2.95, 3.00)
        r = submit(ex)
        if basis == "account":
            check("deposit: account basis is masked and allows another open", r.outcome_class == Outcome.FILLED, r.note or "")
        else:
            check("deposit: both basis still stops on the bot's own loss",
                  r.outcome_class == Outcome.REJECTED and "bot entry stop" in (r.note or ""), r.note or "")

    # Baseline timing: day state first observed after the bot already lost.
    fake = FakeTasty()
    qmap = {}
    _, ex, guard = build(tmp / "late-base", fake, quotes=qmap)
    round_trip(ex, qmap, 0.05)
    fake.nlv = 10_000.0 - 295
    guard._path.unlink()  # e.g. day state lost; the first NLV seen is already down
    _, ex2, guard2 = build(tmp / "late-base", fake, quotes=qmap)
    qmap[CALL] = quote(CALL, 2.95, 3.00)
    r = submit(ex2)
    check("late baseline: the account basis cannot see the earlier loss, the bot basis still counts it from fills",
          r.outcome_class == Outcome.REJECTED and "bot entry stop" in (r.note or "")
          and guard2._day()["start_nlv"] == 10_000.0 - 295, r.note or "")

    # A position carried in from yesterday counts at its cost.
    fake = FakeTasty()
    qmap = {CALL: quote(CALL, 0.05, 0.10)}
    session, ex, guard = build(tmp / "carry", fake, quotes=qmap)
    session.ownership.register("carried", CALL, "Buy to Open", 1)
    session.ownership.apply_fill("carried", 1, final=True, notional=300.0)
    data = json.loads(session.ownership.path.read_text())
    data["orders"]["carried"]["fill_et_date"] = "2000-01-01"
    data["orders"]["carried"]["filled_utc"] = "2000-01-01T15:00:00+00:00"
    session.ownership.path.write_text(json.dumps(data))
    fake.hold(CALL, 1)
    ex.finalize(submit(ex, side="sell", strategy_id="eod_flatten").execution_request_id)
    qmap[CALL] = quote(CALL, 2.95, 3.00)
    r = submit(ex)
    check("a position carried in at $300 and sold for $5 counts as a $295 bot loss today",
          r.outcome_class == Outcome.REJECTED and guard.bot_loss_today() == 295.0, f"{guard.bot_loss_today()} {r.note}")

    # Unknown fill prices err high.
    order = {"status": "Filled", "legs": [{"fills": [{"quantity": 1}]}]}
    check("a fill with no price and no limit is unknown (counted at the premium cap for buys)",
          tb._fill_notional(order, 1) is None)
    check("fill prices are used when they cover the fill",
          tb._fill_notional({"legs": [{"fills": [{"quantity": "1", "fill-price": "2.10"}]}]}, 1) == 210.0)


def scenario_finite_nlv(tmp: Path) -> None:
    print("\n-- finite account values")
    for bad in ("NaN", "inf", "-inf", "garbage"):
        fake = FakeTasty()
        fake.nlv = bad
        session, ex, guard = build(tmp / f"nlv-{bad}", fake)
        st = session.me()
        r = submit(ex)
        check(f"broker NLV {bad!r} is not a value: opening refused",
              st.net_liquidating_value is None and r.outcome_class == Outcome.REJECTED
              and "finite" in (r.note or "") and fake.posts_to_orders() == 0, r.note or "")
        check(f"...and {bad!r} never becomes the day's baseline", guard._day()["start_nlv"] is None)


def scenario_history_validation(tmp: Path) -> None:
    print("\n-- R4 malformed or incomplete order history is never evidence of absence")
    malformed = {
        "invalid JSON": "invalid-json",
        "empty object": {},
        "data missing": {"pagination": {"total-pages": 1}},
        "items missing": {"data": {}},
        "items not a list": {"data": {"items": {"id": 1}}},
        "non-object item": {"data": {"items": ["x"]}},
        "pagination not an object": {"data": {"items": []}, "pagination": "1"},
        "total-pages not a number": {"data": {"items": []}, "pagination": {"total-pages": "1"}},
    }
    for label, payload in malformed.items():
        fake = FakeTasty()
        _, ex, _ = build(tmp / f"hv-{abs(hash(label)) % 10**6}", fake)
        ex._persist_intent({"execution_request_id": "gone", "symbol": CALL, "side": "buy", "quantity": 1,
                            "broker_action": "Buy to Open", "created_utc": "2026-11-09T14:00:00+00:00"})
        fake.orders_payload = payload
        outcomes = []
        for minute in (16, 31, 46, 59):
            with at(f"2026-11-09T14:{minute:02d}:00+00:00"):
                outcomes.append(ex.recover_pending().outcome_class)
        check(f"HTTP 200 with {label}: stays ambiguous across scans spanning 43 minutes",
              outcomes == [Outcome.AMBIGUOUS] * 4, str(outcomes))

    # Long outage: an intent from three days ago is looked up from its own date.
    fake = FakeTasty()
    session, ex, _ = build(tmp / "hv-outage", fake)
    session.ownership.register("old-1", CALL, "Buy to Open", 1)
    order = fake._new_order({"external-identifier": "old-1", "received-at": "2026-11-06T15:00:00+00:00",
                             "legs": [{"symbol": tasty(CALL), "action": "Buy to Open", "quantity": 1}]}, "Live")
    with at("2026-11-06T15:00:05+00:00"):
        fake._fill(order)
    ex._persist_intent({"execution_request_id": "old-1", "symbol": CALL, "side": "buy", "quantity": 1,
                        "broker_action": "Buy to Open", "created_utc": "2026-11-06T15:00:00+00:00"})
    with at("2026-11-09T15:00:00+00:00"):
        r = ex.recover_pending()
    starts = [kw.get("params", {}).get("start-date") for m, u, kw in fake.calls if m == "GET" and u.endswith("/orders")]
    check("after a multi-day outage the history search starts before the intent's own date and finds the fill",
          r.outcome_class == Outcome.RECONCILED_AFTER_AMBIGUITY and min(starts) <= "2026-11-05", f"{r.note} starts={starts}")

    # Fill appearing after absence was declared: exposure held, no double.
    fake = FakeTasty()
    fake.submit_timeout = True
    session, ex, guard = build(tmp / "hv-late", fake)
    original_new = fake._new_order

    def hide_new(body, status):
        o = original_new(body, status)
        fake.hidden.add(o["external-identifier"])
        return o

    fake._new_order = hide_new
    fake.record_on_timeout = False
    t0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    with at(t0.isoformat()):
        r = submit(ex)
    rid = r.execution_request_id
    fake.record_on_timeout = True
    body = next(kw["json"] for m, u, kw in fake.calls if m == "POST" and u.endswith(f"/accounts/{fake.acct}/orders"))
    late = original_new(body, "Live")  # the broker did accept it; not visible yet
    fake.hidden.add(rid)
    outs = []
    for minutes in (16, 24, 32):
        with at((t0 + timedelta(minutes=minutes)).isoformat()):
            outs.append(ex.recover_pending())
    check("absent across complete scans spanning 16 minutes: recorded not placed",
          [o.outcome_class for o in outs] == [Outcome.AMBIGUOUS, Outcome.AMBIGUOUS, Outcome.REJECTED],
          str([o.outcome_class for o in outs]))
    ex.finalize(rid)
    with at((t0 + timedelta(minutes=33)).isoformat()):
        r2 = submit(ex, symbol=PUT)
    check("...but its exposure stays reserved: a new open is refused (the original may still fill)",
          r2.outcome_class == Outcome.REJECTED and "unresolved opening orders" in (r2.note or ""), r2.note or "")
    fake.hidden.clear()
    with at((t0 + timedelta(minutes=40)).isoformat()):
        fake._fill(late)
        st = session.me()
    check("the late fill is attributed to the bot, counted once, never resubmitted",
          Book(st.trades).position(CALL).quantity == 1 and st.late_fills == [rid] and st.pending_open_contracts == 0
          and guard.opens_today() == 1 and fake.posts_to_orders() == 1, str(st.summary()))


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        scenario_config()
        scenario_limits_and_symbols()
        scenario_limit_boundaries(tmp)
        scenario_open_and_close(tmp)
        scenario_ownership(tmp)
        scenario_guard(tmp)
        scenario_open_counting(tmp)
        scenario_uncertain_state(tmp)
        scenario_recovery(tmp)
        scenario_ambiguity(tmp)
        scenario_close_without_quote(tmp)
        scenario_unquoted_window(tmp)
        scenario_runner_eod(tmp)
        scenario_state_identity(tmp)
        scenario_position_lag(tmp)
        scenario_loss_basis(tmp)
        scenario_finite_nlv(tmp)
        scenario_history_validation(tmp)
        scenario_auth_and_redaction(tmp)
        scenario_config_loading_and_runner(tmp)
    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
