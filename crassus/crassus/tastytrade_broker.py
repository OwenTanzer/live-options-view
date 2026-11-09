"""Route one crassus bot's orders to a real tastytrade brokerage account.

Every other crassus account trades fake capital against the Worker's
`/api/paper-trade`. An account whose catalog entry sets
`"broker": "tastytrade"` instead gets the two classes in this module:

* `TastySession` -- stands in for `client.AccountSession`. Its `me()` reads
  the brokerage account's balances and positions and presents them as the
  same `AccountState` / trade-list shape the runner, `Book`, the strategies
  and `flatten.maybe_flatten` already consume.
* `TastyExecutionClient` -- stands in for `client.ExecutionClient`. Same
  persist-before-send intent file and the same `finalize()` contract, but
  orders go to `POST /accounts/{n}/orders` after a mandatory dry run, behind
  a `RiskGuard` that the strategy cannot override.

Real money changes the failure model the Worker client was built around, so
three things here deliberately differ from `ExecutionClient`:

1. tastytrade does not deduplicate retried submissions. A timeout or 5xx is
   reconciled by looking the order up by its `external-identifier` (the
   crassus `execution_request_id`), and is *never* resubmitted blind.
2. A crashed in-flight intent is never replayed. If the broker has no order
   with that id, the stale decision is recorded as rejected rather than sent
   minutes later against a different market.
3. Unfilled orders are cancelled after `fill_timeout_s` rather than left
   resting, so the bot never holds an order it is not watching.

The mode is fail-closed. Nothing trades unless `CRASSUS_TASTY_MODE` is
exactly `sandbox` or `live`; `live` additionally requires
`CRASSUS_TASTY_LIVE_APPROVAL=live:<account number>`, which only the account
owner sets. Credentials come only from the environment and are never logged.
See `docs/tastytrade-live-trading.md`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from . import clock
from .audit import Outcome
from .client import (
    AccountState,
    AmbiguousExecution,
    Book,
    ConfigError,
    ExecutionClient,
    ExecutionResult,
    RateLimiter,
    TransportError,
)
from .durability import durable_mkdir, sync_directory

LIVE_BASE = "https://api.tastyworks.com"
SANDBOX_BASE = "https://api.cert.tastyworks.com"
USER_AGENT = "crassus-tasty-router/1.0"
TOKEN_REFRESH_BUFFER_S = 60

# Hard ceilings. Account params may tighten these but never loosen them;
# raising one is a code change that goes through review.
HARD_MAX_CONTRACTS_PER_ORDER = 1
HARD_MAX_OPEN_CONTRACTS = 1
HARD_MAX_PREMIUM_PER_CONTRACT = 5.00  # option price, i.e. $500 per contract
HARD_MAX_DAILY_LOSS_USD = 500.0
HARD_MAX_OPENS_PER_DAY = 6

DEFAULT_LIMITS = {
    "max_contracts_per_order": 1,
    "max_open_contracts": 1,
    "max_premium_per_contract": 3.00,
    "max_daily_loss_usd": 250.0,
    "max_opens_per_day": 4,
}

# Only these strategies may *open* a position on the real account. Closing
# is always allowed (it can only reduce exposure), which keeps the mandatory
# EOD flatten (`eod_flatten`) working.
OPEN_ALLOWED_STRATEGIES = frozenset({"oi_skew_qqq"})
ALLOWED_UNDERLYINGS = frozenset({"QQQ"})

FILLED = "Filled"
WORKING_STATUSES = frozenset({"Received", "Routed", "In Flight", "Live", "Contingent", "Cancel Requested", "Replace Requested"})
DEAD_STATUSES = frozenset({"Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"})

_OCC_RE = re.compile(r"^([A-Z]{1,6})\s*(\d{6})([CP])(\d{8})$")


class LiveTradingNotApproved(ConfigError):
    """`live` mode without the owner's explicit approval for this account."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TastyConfig:
    mode: str  # "sandbox" | "live"
    base_url: str
    account_number: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TastyConfig":
        env = os.environ if env is None else env
        mode = env.get("CRASSUS_TASTY_MODE", "").strip()
        if mode not in ("sandbox", "live"):
            raise ConfigError(
                "CRASSUS_TASTY_MODE must be 'sandbox' or 'live' for a tastytrade-routed "
                f"account (got {mode!r}); refusing to trade."
            )
        missing = [
            name
            for name in ("CRASSUS_TASTY_ACCOUNT_NUMBER", "CRASSUS_TASTY_CLIENT_SECRET", "CRASSUS_TASTY_REFRESH_TOKEN")
            if not env.get(name)
        ]
        if missing:
            raise ConfigError(f"tastytrade routing is missing {', '.join(missing)}; refusing to trade.")
        account_number = env["CRASSUS_TASTY_ACCOUNT_NUMBER"].strip()
        if mode == "live":
            approval = env.get("CRASSUS_TASTY_LIVE_APPROVAL", "").strip()
            if approval != f"live:{account_number}":
                raise LiveTradingNotApproved(
                    "Live tastytrade trading needs CRASSUS_TASTY_LIVE_APPROVAL=live:<account number>, "
                    "set by the account owner. Refusing to trade."
                )
        return cls(
            mode=mode,
            base_url=LIVE_BASE if mode == "live" else SANDBOX_BASE,
            account_number=account_number,
            client_secret=env["CRASSUS_TASTY_CLIENT_SECRET"],
            refresh_token=env["CRASSUS_TASTY_REFRESH_TOKEN"],
        )

    def redact(self, text: str) -> str:
        for secret in (self.client_secret, self.refresh_token):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)(bearer\s+)[^\s\"',]+", r"\1[redacted]", text)
        return re.sub(r"\b[A-Za-z0-9_\-.]{40,}\b", "[redacted]", text)[:500]


def resolve_limits(params: dict[str, Any] | None) -> dict[str, float]:
    """Account params tighten the defaults; the hard ceilings always win."""
    ceilings = {
        "max_contracts_per_order": HARD_MAX_CONTRACTS_PER_ORDER,
        "max_open_contracts": HARD_MAX_OPEN_CONTRACTS,
        "max_premium_per_contract": HARD_MAX_PREMIUM_PER_CONTRACT,
        "max_daily_loss_usd": HARD_MAX_DAILY_LOSS_USD,
        "max_opens_per_day": HARD_MAX_OPENS_PER_DAY,
    }
    configured = (params or {}).get("tastytrade_limits") or {}
    limits: dict[str, float] = {}
    for key, default in DEFAULT_LIMITS.items():
        value = configured.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"tastytrade_limits.{key} must be a positive number, got {value!r}")
        limits[key] = min(value, ceilings[key])
    return limits


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


def to_tasty_symbol(symbol: str) -> str:
    """`QQQ260722C00702000` -> `QQQ   260722C00702000` (root padded to 6)."""
    match = _OCC_RE.match(symbol.strip())
    if not match:
        raise ValueError(f"Not an OCC equity option symbol: {symbol!r}")
    root, ymd, cp, strike = match.groups()
    return f"{root:<6}{ymd}{cp}{strike}"


def to_crassus_symbol(symbol: str) -> str:
    match = _OCC_RE.match(symbol.strip())
    if not match:
        raise ValueError(f"Not an OCC equity option symbol: {symbol!r}")
    return "".join(match.groups())


def underlying_of(symbol: str) -> str | None:
    match = _OCC_RE.match(symbol.strip())
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@dataclass
class TastyAccountState(AccountState):
    net_liquidating_value: float | None = None
    broker_mode: str = "sandbox"

    def summary(self) -> dict[str, Any]:
        return {
            **super().summary(),
            "broker": "tastytrade",
            "broker_mode": self.broker_mode,
            "net_liquidating_value": self.net_liquidating_value,
        }


class TastySession:
    """Drop-in for `AccountSession` backed by one tastytrade account."""

    def __init__(
        self,
        account: Any,
        config: TastyConfig,
        *,
        rate_limiter: RateLimiter | None = None,
        http: Any = None,
        timeout_s: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.account = account
        self.config = config
        self.base_url = config.base_url
        self.rate_limiter = rate_limiter or RateLimiter()
        self.http = http or requests.Session()
        self.timeout_s = timeout_s
        self.liquidated = False
        self._mutation_lock = threading.Lock()
        self._monotonic = monotonic
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    # -- auth -------------------------------------------------------------

    def _access_token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and self._monotonic() < self._token_expires_at:
                return self._token
            try:
                resp = self.http.request(
                    "POST",
                    f"{self.base_url}/oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": self.config.refresh_token,
                        "client_secret": self.config.client_secret,
                    },
                    headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                raise TransportError(f"tastytrade token exchange failed: {self.config.redact(str(exc))}") from exc
            if resp.status_code >= 400:
                raise ConfigError(
                    f"tastytrade token exchange rejected (HTTP {resp.status_code}): {self._error_text(resp)}"
                )
            payload = _json(resp) or {}
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise ConfigError("tastytrade token exchange returned no access_token")
            lifetime = float(payload.get("expires_in") or 900)
            self._token = token
            self._token_expires_at = self._monotonic() + max(lifetime - TOKEN_REFRESH_BUFFER_S, 30)
            return token

    def _error_text(self, resp: Any) -> str:
        payload = _json(resp)
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            detail = {k: err.get(k) for k in ("code", "message", "errors") if err.get(k) is not None}
            return self.config.redact(json.dumps(detail, default=str))
        return "unavailable"

    # -- plumbing ---------------------------------------------------------

    def request(self, method: str, path: str, *, mutation: bool = False, **kw: Any) -> Any:
        """One API call. A timeout on a mutation is ambiguous, never retried here."""
        for attempt in (1, 2):
            self.rate_limiter.acquire()
            headers = {
                "Authorization": f"Bearer {self._access_token(force=attempt == 2)}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
            if "json" in kw:
                headers["Content-Type"] = "application/json"
            try:
                resp = self.http.request(method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout_s, **kw)
            except requests.Timeout as exc:
                if mutation:
                    raise AmbiguousExecution(f"{method} {path} timed out") from exc
                raise TransportError(f"{method} {path} timed out") from exc
            except requests.RequestException as exc:
                if mutation:
                    raise AmbiguousExecution(f"{method} {path} failed in transit: {self.config.redact(str(exc))}") from exc
                raise TransportError(f"{method} {path} failed: {self.config.redact(str(exc))}") from exc
            if resp.status_code == 401 and attempt == 1:
                continue  # token expired early; refresh once
            if resp.status_code == 429:
                self.rate_limiter.penalize(float(resp.headers.get("Retry-After", 5)))
            return resp
        return resp

    def _get_data(self, path: str, **kw: Any) -> Any:
        resp = self.request("GET", path, **kw)
        if resp.status_code >= 400:
            raise TransportError(f"GET {path} -> {resp.status_code} {self._error_text(resp)}")
        return (_json(resp) or {}).get("data")

    # -- AccountSession interface ----------------------------------------

    def ensure_session(self) -> TastyAccountState:
        """Prove the grant can see the configured account before trading."""
        data = self._get_data("/customers/me/accounts") or {}
        numbers = {
            (item.get("account") or {}).get("account-number")
            for item in data.get("items", [])
            if isinstance(item, dict)
        }
        if self.config.account_number not in numbers:
            raise ConfigError(
                "The tastytrade grant does not include the configured account number; refusing to trade."
            )
        return self.me()

    def me(self) -> TastyAccountState:
        n = self.config.account_number
        balances = self._get_data(f"/accounts/{n}/balances") or {}
        positions = (self._get_data(f"/accounts/{n}/positions") or {}).get("items", [])
        trades = []
        for pos in positions:
            if pos.get("instrument-type") != "Equity Option":
                continue
            qty = int(float(pos.get("quantity") or 0))
            if qty == 0:
                continue
            try:
                symbol = to_crassus_symbol(pos["symbol"])
            except (KeyError, ValueError):
                continue
            trades.append(
                {
                    # One synthetic opening trade per held position, so
                    # `Book` reconstructs exactly the broker's positions.
                    "sym": symbol,
                    "side": "sell" if pos.get("quantity-direction") == "Short" else "buy",
                    "qty": qty,
                    "price": float(pos.get("average-open-price") or 0.0),
                    "source": "tastytrade_position",
                }
            )
        return TastyAccountState(
            username=self.account.username,
            balance_cash=_float(balances.get("cash-balance")) or 0.0,
            trades=trades,
            net_liquidating_value=_float(balances.get("net-liquidating-value")),
            broker_mode=self.config.mode,
        )


# --------------------------------------------------------------------------
# Risk guard
# --------------------------------------------------------------------------


class RiskGuard:
    """Per-day limits that the strategy layer cannot see or override.

    Day state (start-of-day net liquidating value, opens placed) is kept in
    `<state_dir>/<alias>.tasty_day.json` keyed by the ET session date, so a
    restart mid-session does not reset the daily loss cap.
    """

    def __init__(self, alias: str, state_dir: Path, limits: dict[str, float], halt_env: str = "CRASSUS_TASTY_HALT"):
        self.alias = alias
        self.state_dir = Path(state_dir)
        self.limits = limits
        self.halt_env = halt_env
        durable_mkdir(self.state_dir)

    @property
    def _path(self) -> Path:
        return self.state_dir / f"{self.alias}.tasty_day.json"

    @property
    def halt_file(self) -> Path:
        return self.state_dir / "TASTY_HALT"

    def _day(self) -> dict[str, Any]:
        today = clock.now_et().date().isoformat()
        try:
            state = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        if state.get("date") != today:
            state = {"date": today, "start_nlv": None, "opens": 0}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
        sync_directory(self.state_dir)

    def observe(self, state: TastyAccountState) -> dict[str, Any]:
        day = self._day()
        if day["start_nlv"] is None and state.net_liquidating_value is not None:
            day["start_nlv"] = state.net_liquidating_value
            self._save(day)
        return day

    def record_open(self) -> None:
        day = self._day()
        day["opens"] = int(day.get("opens", 0)) + 1
        self._save(day)

    def check_open(self, *, strategy_id: str | None, symbol: str, quantity: int,
                   book: Book, state: TastyAccountState, limit_price: float | None) -> str | None:
        """Return a block reason, or None if this opening order may go out."""
        if os.environ.get(self.halt_env, "").strip().lower() in ("1", "true", "yes", "on") or self.halt_file.exists():
            return "halted: new positions disabled by kill switch (closes still allowed)"
        if strategy_id not in OPEN_ALLOWED_STRATEGIES:
            return f"strategy {strategy_id!r} is not allowed to open positions on the brokerage account"
        if underlying_of(symbol) not in ALLOWED_UNDERLYINGS:
            return f"{symbol} is not an allowed underlying"
        if quantity > self.limits["max_contracts_per_order"]:
            return f"quantity {quantity} exceeds max_contracts_per_order {self.limits['max_contracts_per_order']:g}"
        held = sum(abs(p.quantity) for p in book.positions.values())
        if held + quantity > self.limits["max_open_contracts"]:
            return f"would hold {held + quantity} contracts; max_open_contracts is {self.limits['max_open_contracts']:g}"
        if limit_price is None:
            return "no executable quote to price the order"
        if limit_price > self.limits["max_premium_per_contract"]:
            return f"premium {limit_price:.2f} exceeds max_premium_per_contract {self.limits['max_premium_per_contract']:.2f}"
        day = self.observe(state)
        if day.get("opens", 0) >= self.limits["max_opens_per_day"]:
            return f"max_opens_per_day {self.limits['max_opens_per_day']:g} reached"
        start, now = day.get("start_nlv"), state.net_liquidating_value
        if start is None or now is None:
            return "net liquidating value unavailable; cannot enforce the daily loss cap"
        if start - now >= self.limits["max_daily_loss_usd"]:
            return f"daily loss cap hit: down {start - now:.2f} USD today (cap {self.limits['max_daily_loss_usd']:.2f})"
        return None


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


QuoteProvider = Callable[[list[str]], dict[str, Any]]


class TastyExecutionClient(ExecutionClient):
    """Drop-in for `ExecutionClient` that places real tastytrade orders."""

    def __init__(
        self,
        session: TastySession,
        state_dir: Path,
        quotes: QuoteProvider,
        guard: RiskGuard,
        *,
        fill_timeout_s: float = 20.0,
        poll_interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(session, state_dir, max_attempts=1, backoff_base_s=0.0)
        self.session: TastySession = session
        self.quotes = quotes
        self.guard = guard
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep

    @property
    def _orders_path(self) -> str:
        return f"/accounts/{self.session.config.account_number}/orders"

    # -- pricing ----------------------------------------------------------

    def _price(self, symbol: str, side: str) -> float | None:
        quote = (self.quotes([symbol]) or {}).get(symbol)
        if quote is None or not getattr(quote, "is_executable", False):
            return None
        price = quote.ask if side == "buy" else quote.bid
        if price is None or price <= 0:
            return None
        return round(float(price), 2)

    @staticmethod
    def _order_body(symbol: str, action: str, quantity: int, price: float | None,
                    request_id: str) -> dict[str, Any]:
        debit = action.startswith("Buy")
        body: dict[str, Any] = {
            "time-in-force": "Day",
            "order-type": "Limit" if price is not None else "Market",
            "external-identifier": request_id,
            "source": USER_AGENT,
            "legs": [
                {
                    "instrument-type": "Equity Option",
                    "symbol": to_tasty_symbol(symbol),
                    "quantity": quantity,
                    "action": action,
                }
            ],
        }
        if price is not None:
            body["price"] = f"{price:.2f}"
            body["price-effect"] = "Debit" if debit else "Credit"
        return body

    # -- lookup -----------------------------------------------------------

    def find_order(self, execution_request_id: str) -> dict[str, Any] | None:
        start = (clock.now_et().date() - timedelta(days=1)).isoformat()
        data = self.session._get_data(self._orders_path, params={"start-date": start, "per-page": 200}) or {}
        return next(
            (o for o in data.get("items", []) if o.get("external-identifier") == execution_request_id),
            None,
        )

    def find_trade(self, execution_request_id: str, state: AccountState | None = None) -> dict[str, Any] | None:
        order = self.find_order(execution_request_id)
        return order if order and order.get("status") == FILLED else None

    def _get_order(self, order_id: Any) -> dict[str, Any] | None:
        return self.session._get_data(f"{self._orders_path}/{order_id}")

    def _settle(self, order: dict[str, Any], request_id: str, latency_ms: float | None,
                *, reconciled: bool = False, note: str | None = None) -> ExecutionResult:
        """Wait for a working order to fill; cancel it if it doesn't."""
        deadline = time.monotonic() + self.fill_timeout_s
        while order.get("status") in WORKING_STATUSES and time.monotonic() < deadline:
            self._sleep(self.poll_interval_s)
            order = self._get_order(order["id"]) or order

        if order.get("status") in WORKING_STATUSES:
            try:
                cancel = self.session.request("DELETE", f"{self._orders_path}/{order['id']}", mutation=True)
            except AmbiguousExecution:
                cancel = None
            order = self._get_order(order["id"]) or order
            if order.get("status") in WORKING_STATUSES:
                return ExecutionResult(
                    outcome_class=Outcome.AMBIGUOUS,
                    execution_request_id=request_id,
                    http_status=getattr(cancel, "status_code", None),
                    server_response=_order_summary(order),
                    latency_ms=latency_ms,
                    note="Order still working after cancel attempt; intent retained for reconciliation.",
                )

        state_after = self._safe_me()
        status = order.get("status")
        if status == FILLED:
            return ExecutionResult(
                outcome_class=Outcome.RECONCILED_AFTER_AMBIGUITY if reconciled else Outcome.FILLED,
                execution_request_id=request_id,
                server_response=_order_summary(order),
                latency_ms=latency_ms,
                state_after=state_after,
                note=note,
            )
        return ExecutionResult(
            outcome_class=Outcome.REJECTED,
            execution_request_id=request_id,
            server_response=_order_summary(order),
            latency_ms=latency_ms,
            state_after=state_after,
            note=note or f"Order ended {status!r} without a fill.",
        )

    def _blocked(self, request_id: str, reason: str, state: AccountState | None = None,
                 http_status: int | None = None, response: Any = None) -> ExecutionResult:
        return ExecutionResult(
            outcome_class=Outcome.REJECTED,
            execution_request_id=request_id,
            http_status=http_status,
            server_response=response if response is not None else {"blocked_by": "tastytrade_router", "reason": reason},
            state_after=state,
            note=reason,
        )

    # -- submission -------------------------------------------------------

    def submit(self, *, symbol: str, side: str, quantity: int, execution_request_id: str | None = None,
               **envelope: Any) -> ExecutionResult:
        import uuid

        request_id = execution_request_id or str(uuid.uuid4())
        with self.session._mutation_lock:
            intent = {
                "execution_request_id": request_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                **{k: envelope.get(k) for k in (
                    "decision_id", "strategy_id", "strategy_version", "account_strategy_id", "reason",
                    "decision", "market_snapshot_timestamp", "market_snapshot_url_or_hash",
                    "market_snapshot_lineage", "account_state_before",
                )},
                "broker": "tastytrade",
                "broker_mode": self.session.config.mode,
                "created_utc": clock.iso_utc(),
            }
            self._persist_intent(intent)  # persist before anything is sent

            try:
                state = self.session.me()
            except TransportError as exc:
                return self._blocked(request_id, f"Could not read brokerage account before ordering: {exc}")
            book = Book(state.trades)
            self.guard.observe(state)
            held = book.position(symbol).quantity

            if side == "buy":
                action = "Buy to Close" if held < 0 else "Buy to Open"
            elif side == "sell":
                if held <= 0:
                    return self._blocked(request_id, "Refusing to sell an option the account does not hold (no naked shorts).", state)
                action = "Sell to Close"
            else:
                return self._blocked(request_id, f"Unsupported side {side!r}.", state)

            if action.endswith("Close") and quantity > abs(held):
                return self._blocked(request_id, f"Close quantity {quantity} exceeds held {abs(held)}.", state)

            price = self._price(symbol, side)
            if action.endswith("Open"):
                reason = self.guard.check_open(
                    strategy_id=envelope.get("strategy_id"), symbol=symbol, quantity=quantity,
                    book=book, state=state, limit_price=price,
                )
                if reason:
                    return self._blocked(request_id, reason, state)
            # A close with no quote goes out as a market order: getting flat
            # matters more than the last few cents.
            body = self._order_body(symbol, action, quantity, price, request_id)

            dry = self.session.request("POST", f"{self._orders_path}/dry-run", json=body)
            if dry.status_code >= 400:
                return self._blocked(
                    request_id, f"Dry run rejected (HTTP {dry.status_code}): {self.session._error_text(dry)}",
                    state, http_status=dry.status_code,
                )

            started = time.monotonic()
            try:
                resp = self.session.request("POST", self._orders_path, json=body, mutation=True)
            except AmbiguousExecution as exc:
                return self._resolve_ambiguous(request_id, (time.monotonic() - started) * 1000, str(exc))
            latency = (time.monotonic() - started) * 1000

            if resp.status_code >= 500:
                return self._resolve_ambiguous(request_id, latency, f"HTTP {resp.status_code}")
            if resp.status_code >= 400:
                return self._blocked(
                    request_id, f"Order rejected (HTTP {resp.status_code}): {self.session._error_text(resp)}",
                    state, http_status=resp.status_code,
                    response={"error": self.session._error_text(resp)},
                )

            order = ((_json(resp) or {}).get("data") or {}).get("order") or {}
            if action.endswith("Open"):
                self.guard.record_open()
            if not order.get("id"):
                return self._resolve_ambiguous(request_id, latency, "accepted without an order id")
            result = self._settle(order, request_id, latency)
            result.http_status = resp.status_code
            return result

    def _resolve_ambiguous(self, request_id: str, latency_ms: float, cause: str) -> ExecutionResult:
        try:
            order = self.find_order(request_id)
        except TransportError as exc:
            return ExecutionResult(
                outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id, latency_ms=latency_ms,
                note=f"{cause}; order lookup failed ({exc}); intent retained, not resubmitted.",
            )
        if order is None:
            return ExecutionResult(
                outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id, latency_ms=latency_ms,
                note=f"{cause}; no order with this external-identifier yet; intent retained, not resubmitted.",
            )
        return self._settle(order, request_id, latency_ms, reconciled=True,
                            note=f"{cause}; order found by external-identifier, not resubmitted.")

    def recover_pending(self) -> ExecutionResult | None:
        """Resolve a crashed intent from the broker's record. Never replays."""
        intent = self.pending_intent()
        if not intent:
            return None
        request_id = intent["execution_request_id"]
        order = self.find_order(request_id)
        if order is None:
            age_s = None
            try:
                from datetime import datetime
                age_s = (datetime.fromisoformat(clock.iso_utc()) - datetime.fromisoformat(intent["created_utc"])).total_seconds()
            except (KeyError, ValueError):
                pass
            if age_s is not None and age_s < 120:
                # The order may simply not be listed yet; look again next cycle.
                return ExecutionResult(
                    outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id,
                    note="In-flight intent not yet visible at the broker; retained for another pass.",
                )
            return self._blocked(
                request_id,
                "Recovered in-flight intent was never placed at the broker; stale decision not replayed.",
                self._safe_me(),
            )
        return self._settle(order, request_id, None, reconciled=True,
                            note="Recovered in-flight intent from the broker's order record; not resubmitted.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_summary(order: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "status", "external-identifier", "order-type", "price", "price-effect",
            "size", "filled-quantity", "received-at", "terminal-at", "reject-reason")
    summary = {k: order.get(k) for k in keep if order.get(k) is not None}
    legs = order.get("legs") or []
    summary["legs"] = [
        {k: leg.get(k) for k in ("symbol", "action", "quantity", "remaining-quantity", "fills") if leg.get(k) is not None}
        for leg in legs
    ]
    return summary


def build_for_account(account: Any, *, state_dir: Path, rate_limiter: RateLimiter,
                      quotes: QuoteProvider) -> tuple[TastySession, TastyExecutionClient]:
    """Runner hook. Raises ConfigError (fail closed) on any missing approval."""
    config = TastyConfig.from_env()
    session = TastySession(account, config, rate_limiter=rate_limiter)
    guard = RiskGuard(account.alias, state_dir, resolve_limits(account.params))
    return session, TastyExecutionClient(session, state_dir, quotes, guard)
