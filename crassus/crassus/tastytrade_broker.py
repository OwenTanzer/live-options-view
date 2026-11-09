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
4. The brokerage account may hold positions the bot did not open (manual
   trades, hedges). `OwnershipStore` records the contracts this bot's own
   fills produced; only those reach the `Book`, so neither the strategy nor
   the EOD flatten can trade anything else. Other holdings are reported as
   `external_positions` but never managed.

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
    # An *entry stop*, not a loss cap: once account net liq is down this much
    # from the session's first reading, no new position may be opened. It
    # cannot stop out a position already held. See `worst_case_daily_loss`.
    "max_daily_loss_usd": 250.0,
    "max_opens_per_day": 4,
}
HARD_CEILINGS = {
    "max_contracts_per_order": HARD_MAX_CONTRACTS_PER_ORDER,
    "max_open_contracts": HARD_MAX_OPEN_CONTRACTS,
    "max_premium_per_contract": HARD_MAX_PREMIUM_PER_CONTRACT,
    "max_daily_loss_usd": HARD_MAX_DAILY_LOSS_USD,
    "max_opens_per_day": HARD_MAX_OPENS_PER_DAY,
}
INTEGER_LIMITS = frozenset({"max_contracts_per_order", "max_open_contracts", "max_opens_per_day"})
CONTRACT_MULTIPLIER = 100

# What to do in the EOD flatten window when a held contract has no
# executable quote. "retry" (default) keeps waiting for a quote and may
# therefore carry the position into expiration; "market" sends a market
# sell-to-close in the final `eod_unquoted_exit_minutes`. This is an owner
# risk decision, set in account params, never assumed.
EXIT_POLICIES = frozenset({"retry", "market"})
DEFAULT_EXIT_POLICY = "retry"

# Declaring an in-flight order "never placed" needs repeated, complete
# order-history scans spread over time, not one first-page miss.
ABSENCE_MIN_AGE_S = 15 * 60
ABSENCE_MIN_SCANS = 3
ORDERS_PER_PAGE = 200
LATE_FILL_WATCH_S = 2 * 24 * 3600

# Only these strategies may *open* a position on the real account. Closing
# is always allowed (it can only reduce exposure), which keeps the mandatory
# EOD flatten (`eod_flatten`) working.
OPEN_ALLOWED_STRATEGIES = frozenset({"oi_skew_qqq"})
ALLOWED_UNDERLYINGS = frozenset({"QQQ"})

FILLED = "Filled"
WORKING_STATUSES = frozenset({"Received", "Routed", "In Flight", "Live", "Contingent", "Cancel Requested", "Replace Requested"})
DEAD_STATUSES = frozenset({"Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"})
# Anything else (a missing status, or one not listed above) is *unknown*:
# the order is kept ambiguous and its intent retained, never read as a
# rejection.

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
    """Account params tighten the defaults; the hard ceilings always win.

    Units: `max_premium_per_contract` is the option *price* (3.00 means $300
    for one 100-share contract); `max_daily_loss_usd` is dollars of account
    net liquidating value; the other three are whole contracts / orders.
    Unknown keys, non-numbers, non-integers for counts and non-positive
    values are configuration errors, never silently ignored.
    """
    configured = (params or {}).get("tastytrade_limits") or {}
    if not isinstance(configured, dict):
        raise ConfigError("tastytrade_limits must be an object")
    unknown = sorted(set(configured) - set(DEFAULT_LIMITS))
    if unknown:
        raise ConfigError(f"tastytrade_limits has unknown key(s) {unknown}; known: {sorted(DEFAULT_LIMITS)}")
    limits: dict[str, float] = {}
    for key, default in DEFAULT_LIMITS.items():
        value = configured.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0 or value != value:
            raise ConfigError(f"tastytrade_limits.{key} must be a positive number, got {value!r}")
        if key in INTEGER_LIMITS and float(value) != int(value):
            raise ConfigError(f"tastytrade_limits.{key} must be a whole number, got {value!r}")
        limits[key] = min(value, HARD_CEILINGS[key])
    return limits


def worst_case_daily_loss(limits: dict[str, float]) -> float:
    """Upper bound, in dollars, on what the bot's own trades can lose in one
    session under these limits, before fees and before gap/assignment risk.

    The bot only ever holds long options (no naked shorts), so a held
    position can lose at most the premium paid. The entry stop lets a new
    position open while the account is down by just under
    `max_daily_loss_usd`, and that position can then lose its whole
    premium. So the bound is the entry stop plus one full position at the
    premium cap. It is *not* a stop-loss on any single position.
    """
    position_cap = limits["max_premium_per_contract"] * CONTRACT_MULTIPLIER * limits["max_open_contracts"]
    return round(limits["max_daily_loss_usd"] + position_cap, 2)


def resolve_exit_policy(params: dict[str, Any] | None) -> tuple[str, float]:
    params = params or {}
    policy = params.get("eod_unquoted_exit", DEFAULT_EXIT_POLICY)
    if policy not in EXIT_POLICIES:
        raise ConfigError(f"eod_unquoted_exit must be one of {sorted(EXIT_POLICIES)}, got {policy!r}")
    minutes = params.get("eod_unquoted_exit_minutes", 5)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not minutes > 0:
        raise ConfigError(f"eod_unquoted_exit_minutes must be a positive number, got {minutes!r}")
    return policy, float(minutes)


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
# Ownership
# --------------------------------------------------------------------------


class OwnershipError(TransportError):
    """Bot ownership cannot be established; fail closed for this cycle."""


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    sync_directory(path.parent)


class OwnershipStore:
    """Which contracts on the brokerage account belong to this bot.

    Persisted at `<state_dir>/<alias>.tasty_owned.json`:

    * `owned`: crassus symbol -> signed contracts this bot holds.
    * `orders`: execution_request_id -> {symbol, action, applied, final,
      created_utc}. `applied` is the filled quantity already credited, so
      crediting the same fill twice (reconcile, recovery, restart) is a
      no-op. Non-final entries younger than `LATE_FILL_WATCH_S` are
      re-checked against the broker's order history each cycle, so a fill
      that only becomes visible late is still attributed.

    Only this bot's own fills ever increase `owned`. A missing file means
    "owns nothing" (a fresh deployment). An unreadable or malformed file
    raises `OwnershipError`, which stops this bot's cycle rather than
    guessing.
    """

    def __init__(self, alias: str, state_dir: Path):
        self.state_dir = Path(state_dir)
        durable_mkdir(self.state_dir)
        self.path = self.state_dir / f"{alias}.tasty_owned.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {"owned": {}, "orders": {}}
            except OSError as exc:
                raise OwnershipError(f"cannot read ownership file {self.path.name}: {exc}") from exc
            try:
                data = json.loads(raw)
                owned = {str(k): int(v) for k, v in (data.get("owned") or {}).items()}
                orders = dict(data.get("orders") or {})
            except (ValueError, TypeError, AttributeError) as exc:
                raise OwnershipError(
                    f"ownership file {self.path.name} is malformed ({exc}); refusing to trade until it is repaired"
                ) from exc
            return {"owned": owned, "orders": orders}

    def _save(self, data: dict[str, Any]) -> None:
        data["owned"] = {k: v for k, v in data["owned"].items() if v != 0}
        _atomic_write_json(self.path, data)

    def register(self, request_id: str, symbol: str, action: str) -> None:
        """Record a submission before it is sent, so a late fill can be found."""
        with self._lock:
            data = self.load()
            data["orders"].setdefault(request_id, {
                "symbol": symbol, "action": action, "applied": 0, "final": False,
                "created_utc": clock.iso_utc(),
            })
            self._save(data)

    def apply_fill(self, request_id: str, filled_quantity: int, *, final: bool) -> int:
        """Credit the not-yet-credited part of a fill. Returns the delta."""
        with self._lock:
            data = self.load()
            entry = data["orders"].get(request_id)
            if entry is None:
                return 0
            delta = max(int(filled_quantity) - int(entry.get("applied", 0)), 0)
            if delta:
                sign = 1 if entry["action"] in ("Buy to Open", "Buy to Close") else -1
                sym = entry["symbol"]
                data["owned"][sym] = data["owned"].get(sym, 0) + sign * delta
                entry["applied"] = int(entry.get("applied", 0)) + delta
            if final:
                entry["final"] = True
            self._save(data)
            return delta

    def watch_ids(self) -> list[str]:
        """Non-final submissions still young enough to watch for a late fill."""
        with self._lock:
            ids = []
            for rid, entry in self.load()["orders"].items():
                if entry.get("final"):
                    continue
                age = _age_s(entry.get("created_utc"))
                if age is None or age <= LATE_FILL_WATCH_S:
                    ids.append(rid)
            return ids

    def reconcile_with_broker(self, broker_qty: dict[str, int]) -> dict[str, int]:
        """Shrink ownership to what the broker still holds.

        A position that expired, was assigned or was closed by hand is no
        longer the bot's to manage. Ownership never grows here.
        """
        with self._lock:
            data = self.load()
            changed = False
            for sym, qty in list(data["owned"].items()):
                held = broker_qty.get(sym, 0)
                if qty > 0:
                    new = max(min(qty, held), 0)
                elif qty < 0:
                    new = min(max(qty, held), 0)
                else:
                    new = 0
                if new != qty:
                    data["owned"][sym] = new
                    changed = True
            if changed:
                self._save(data)
            return {k: v for k, v in data["owned"].items() if v != 0}


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@dataclass
class TastyAccountState(AccountState):
    net_liquidating_value: float | None = None
    broker_mode: str = "sandbox"
    # Option positions on the account that this bot did not open. Visible
    # for audit, never put in the Book, never traded.
    external_positions: dict[str, int] = field(default_factory=dict)
    late_fills: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = {
            **super().summary(),
            "broker": "tastytrade",
            "broker_mode": self.broker_mode,
            "net_liquidating_value": self.net_liquidating_value,
            "external_positions": dict(self.external_positions),
        }
        if self.late_fills:
            out["late_fills"] = list(self.late_fills)
        return out


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
        ownership: OwnershipStore | None = None,
    ):
        self.account = account
        self.config = config
        if ownership is None:
            raise ConfigError("TastySession needs an OwnershipStore; refusing to trade without bot attribution")
        self.ownership = ownership
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

    def sync_metadata(self, **_: Any) -> None:
        """Paper-roster metadata has no brokerage equivalent; nothing to sync."""
        return None

    def list_orders(self) -> list[dict[str, Any]]:
        """Every order since yesterday (ET), across all pages.

        Raises `TransportError` if the history cannot be shown to be
        complete, so a caller can never mistake "not on page one" for
        "never placed".
        """
        start = (clock.now_et().date() - timedelta(days=1)).isoformat()
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            resp = self.request("GET", f"/accounts/{self.config.account_number}/orders",
                                params={"start-date": start, "per-page": ORDERS_PER_PAGE, "page-offset": page})
            if resp.status_code >= 400:
                raise TransportError(f"GET orders -> {resp.status_code} {self._error_text(resp)}")
            payload = _json(resp) or {}
            batch = (payload.get("data") or {}).get("items") or []
            items.extend(o for o in batch if isinstance(o, dict))
            pagination = payload.get("pagination") or {}
            total_pages = pagination.get("total-pages")
            if isinstance(total_pages, int) and not isinstance(total_pages, bool):
                page += 1
                if page >= total_pages:
                    return items
                if page >= 50:
                    raise TransportError("order history exceeds 50 pages; refusing to treat it as complete")
                continue
            if len(batch) < ORDERS_PER_PAGE:
                return items
            raise TransportError("order history is a full page with no pagination metadata; cannot prove completeness")

    def find_order(self, execution_request_id: str) -> dict[str, Any] | None:
        return next((o for o in self.list_orders() if o.get("external-identifier") == execution_request_id), None)

    def _credit_late_fills(self) -> list[str]:
        """Attribute fills of this bot's own orders that became visible late."""
        watch = self.ownership.watch_ids()
        if not watch:
            return []
        orders = {o.get("external-identifier"): o for o in self.list_orders()}
        credited = []
        for rid in watch:
            order = orders.get(rid)
            if order is None:
                continue
            filled = _filled_quantity(order)
            if filled is None:
                continue
            status = order.get("status")
            terminal = status == FILLED or status in DEAD_STATUSES
            if self.ownership.apply_fill(rid, filled, final=terminal):
                credited.append(rid)
        return credited

    def me(self) -> TastyAccountState:
        n = self.config.account_number
        balances = self._get_data(f"/accounts/{n}/balances") or {}
        positions = (self._get_data(f"/accounts/{n}/positions") or {}).get("items", [])
        late = self._credit_late_fills()

        broker_qty: dict[str, int] = {}
        avg_price: dict[str, float] = {}
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
            signed = -abs(qty) if pos.get("quantity-direction") == "Short" else abs(qty)
            broker_qty[symbol] = broker_qty.get(symbol, 0) + signed
            avg_price[symbol] = float(pos.get("average-open-price") or 0.0)

        owned = self.ownership.reconcile_with_broker(broker_qty)
        trades = []
        for symbol, qty in sorted(owned.items()):
            # One synthetic opening trade per *owned* position, so `Book`,
            # the strategy and the EOD flatten only ever see the bot's own
            # contracts.
            trades.append({
                "sym": symbol,
                "side": "buy" if qty > 0 else "sell",
                "qty": abs(qty),
                "price": avg_price.get(symbol, 0.0),
                "source": "tastytrade_position",
            })
        external = {
            sym: qty - owned.get(sym, 0)
            for sym, qty in broker_qty.items()
            if qty - owned.get(sym, 0) != 0
        }
        return TastyAccountState(
            username=self.account.username,
            balance_cash=_float(balances.get("cash-balance")) or 0.0,
            trades=trades,
            net_liquidating_value=_float(balances.get("net-liquidating-value")),
            broker_mode=self.config.mode,
            external_positions=external,
            late_fills=late,
        )


# --------------------------------------------------------------------------
# Risk guard
# --------------------------------------------------------------------------


class RiskGuard:
    """Per-day limits that the strategy layer cannot see or override.

    Day state is kept in `<state_dir>/<alias>.tasty_day.json`, keyed by the
    ET session date, so a restart mid-session resets nothing:

    * `start_nlv`: the first net liquidating value seen this session.
    * `opens`: execution_request_id -> "reserved" | "filled". An opening
      order reserves its slot *before* it is sent and keeps it through any
      uncertainty (timeout, unknown status, crash). The slot is released
      only when the broker definitively reports the order ended unfilled.
      Keyed by request id, so reconciling the same order twice, or after a
      restart, never counts it twice.
    """

    def __init__(self, alias: str, state_dir: Path, limits: dict[str, float], halt_env: str = "CRASSUS_TASTY_HALT"):
        self.alias = alias
        self.state_dir = Path(state_dir)
        self.limits = limits
        self.halt_env = halt_env
        self._lock = threading.RLock()
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
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError) as exc:
            raise OwnershipError(f"cannot read risk day state {self._path.name}: {exc}") from exc
        if state.get("date") != today or not isinstance(state.get("opens"), dict):
            state = {"date": today, "start_nlv": None, "opens": {}}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self._path, state)

    def observe(self, state: TastyAccountState) -> dict[str, Any]:
        with self._lock:
            day = self._day()
            if day["start_nlv"] is None and state.net_liquidating_value is not None:
                day["start_nlv"] = state.net_liquidating_value
                self._save(day)
            return day

    def opens_today(self) -> int:
        return len(self._day()["opens"])

    def reserve_open(self, request_id: str) -> None:
        with self._lock:
            day = self._day()
            day["opens"].setdefault(request_id, "reserved")
            self._save(day)

    def confirm_open(self, request_id: str) -> None:
        with self._lock:
            day = self._day()
            day["opens"][request_id] = "filled"
            self._save(day)

    def release_open(self, request_id: str) -> None:
        with self._lock:
            day = self._day()
            if day["opens"].get(request_id) == "reserved":
                del day["opens"][request_id]
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
        if len(day["opens"]) >= self.limits["max_opens_per_day"]:
            return f"max_opens_per_day {self.limits['max_opens_per_day']:g} reached (filled or still uncertain)"
        start, now = day.get("start_nlv"), state.net_liquidating_value
        if start is None or now is None:
            return "net liquidating value unavailable; cannot enforce the daily entry stop"
        if start - now >= self.limits["max_daily_loss_usd"]:
            return (f"daily entry stop hit: account down {start - now:.2f} USD today "
                    f"(entry stop {self.limits['max_daily_loss_usd']:.2f}); no new positions")
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
        exit_policy: str = DEFAULT_EXIT_POLICY,
        fill_timeout_s: float = 20.0,
        poll_interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(session, state_dir, max_attempts=1, backoff_base_s=0.0)
        if exit_policy not in EXIT_POLICIES:
            raise ConfigError(f"unknown exit policy {exit_policy!r}")
        self.session: TastySession = session
        self.ownership = session.ownership
        self.quotes = quotes
        self.guard = guard
        self.exit_policy = exit_policy
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep

    @property
    def _orders_path(self) -> str:
        return f"/accounts/{self.session.config.account_number}/orders"

    # -- pricing ----------------------------------------------------------

    def _price(self, symbol: str, side: str) -> float | None:
        """Limit price from a fresh, well-formed quote; None otherwise."""
        quote = (self.quotes([symbol]) or {}).get(symbol)
        if quote is None or not getattr(quote, "is_executable", False):
            return None
        bid, ask = quote.bid, quote.ask
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
            return None
        if bid < 0 or ask <= 0 or bid > ask:
            return None  # malformed / crossed quote is not a price
        price = ask if side == "buy" else bid
        if price <= 0:
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
        return self.session.find_order(execution_request_id)

    def find_trade(self, execution_request_id: str, state: AccountState | None = None) -> dict[str, Any] | None:
        order = self.find_order(execution_request_id)
        return order if order and order.get("status") == FILLED else None

    def _get_order(self, order_id: Any) -> dict[str, Any] | None:
        return self.session._get_data(f"{self._orders_path}/{order_id}")

    # -- bookkeeping ------------------------------------------------------

    def _account_for(self, request_id: str, action: str | None, filled: int, *, final: bool) -> None:
        """Credit ownership and settle the open reservation, idempotently."""
        self.ownership.apply_fill(request_id, filled, final=final)
        if action and action.endswith("Open"):
            if filled > 0:
                self.guard.confirm_open(request_id)
            elif final:
                self.guard.release_open(request_id)

    def _settle(self, order: dict[str, Any], request_id: str, latency_ms: float | None,
                *, action: str | None, reconciled: bool = False, note: str | None = None) -> ExecutionResult:
        """Wait for a working order to fill; cancel it if it doesn't.

        Only a broker-reported terminal status with known fill quantity is
        conclusive. A missing or unrecognised status, or a terminal one
        without fill data, stays `ambiguous` so the runner keeps the intent.
        """
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
                partial = _filled_quantity(order)
                if partial:
                    self._account_for(request_id, action, partial, final=False)
                return ExecutionResult(
                    outcome_class=Outcome.AMBIGUOUS,
                    execution_request_id=request_id,
                    http_status=getattr(cancel, "status_code", None),
                    server_response=_order_summary(order),
                    latency_ms=latency_ms,
                    note="Order still working after cancel attempt; intent retained for reconciliation.",
                )

        status = order.get("status")
        filled = _filled_quantity(order)
        conclusive = (status == FILLED or status in DEAD_STATUSES) and filled is not None
        if not conclusive:
            return ExecutionResult(
                outcome_class=Outcome.AMBIGUOUS,
                execution_request_id=request_id,
                server_response=_order_summary(order),
                latency_ms=latency_ms,
                note=(f"Order status {status!r} with fill quantity {filled!r} is not conclusive; "
                      "intent retained, not resubmitted."),
            )

        self._account_for(request_id, action, filled, final=True)
        state_after = self._safe_me()
        if filled > 0:
            partial_note = None if status == FILLED else f"Partially filled ({filled}) then ended {status!r}."
            return ExecutionResult(
                outcome_class=Outcome.RECONCILED_AFTER_AMBIGUITY if reconciled else Outcome.FILLED,
                execution_request_id=request_id,
                server_response=_order_summary(order),
                latency_ms=latency_ms,
                state_after=state_after,
                note="; ".join(n for n in (note, partial_note) if n) or None,
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
            book = Book(state.trades)  # owned positions only
            self.guard.observe(state)
            held = book.position(symbol).quantity

            if side == "buy":
                action = "Buy to Close" if held < 0 else "Buy to Open"
            elif side == "sell":
                if held <= 0:
                    external = state.external_positions.get(symbol)
                    why = (f" (the account holds {external} that this bot did not open; not managed)"
                           if external else "")
                    return self._blocked(
                        request_id,
                        "Refusing to sell an option this bot does not hold (no naked shorts)" + why + ".",
                        state,
                    )
                action = "Sell to Close"
            else:
                return self._blocked(request_id, f"Unsupported side {side!r}.", state)

            if action.endswith("Close") and quantity > abs(held):
                return self._blocked(request_id, f"Close quantity {quantity} exceeds bot-held {abs(held)}.", state)

            price = self._price(symbol, side)
            if action.endswith("Open"):
                reason = self.guard.check_open(
                    strategy_id=envelope.get("strategy_id"), symbol=symbol, quantity=quantity,
                    book=book, state=state, limit_price=price,
                )
                if reason:
                    return self._blocked(request_id, reason, state)
            elif price is None:
                # A close with no usable quote. Whether to send it at market
                # is the owner's choice (`eod_unquoted_exit`), and only the
                # mandatory EOD flatten may use it.
                if not (self.exit_policy == "market" and envelope.get("strategy_id") == "eod_flatten"):
                    return self._blocked(
                        request_id,
                        f"No executable quote to close {symbol}; unquoted market exit not enabled "
                        f"(eod_unquoted_exit={self.exit_policy}, strategy={envelope.get('strategy_id')}). "
                        "Will retry next cycle.",
                        state,
                    )
            body = self._order_body(symbol, action, quantity, price, request_id)

            dry = self.session.request("POST", f"{self._orders_path}/dry-run", json=body)
            if dry.status_code >= 400:
                return self._blocked(
                    request_id, f"Dry run rejected (HTTP {dry.status_code}): {self.session._error_text(dry)}",
                    state, http_status=dry.status_code,
                )

            # Last durable writes before the order leaves: the intent learns
            # its broker action (its presence means "may have been sent"),
            # the ownership store can attribute a late fill, and an opening
            # order reserves its daily slot.
            intent["broker_action"] = action
            self._persist_intent(intent)
            self.ownership.register(request_id, symbol, action)
            if action.endswith("Open"):
                self.guard.reserve_open(request_id)

            started = time.monotonic()
            try:
                resp = self.session.request("POST", self._orders_path, json=body, mutation=True)
            except AmbiguousExecution as exc:
                return self._resolve_ambiguous(request_id, (time.monotonic() - started) * 1000, str(exc), action)
            latency = (time.monotonic() - started) * 1000

            if resp.status_code >= 500:
                return self._resolve_ambiguous(request_id, latency, f"HTTP {resp.status_code}", action)
            if resp.status_code >= 400:
                self._account_for(request_id, action, 0, final=True)
                return self._blocked(
                    request_id, f"Order rejected (HTTP {resp.status_code}): {self.session._error_text(resp)}",
                    state, http_status=resp.status_code,
                    response={"error": self.session._error_text(resp)},
                )

            order = ((_json(resp) or {}).get("data") or {}).get("order") or {}
            if not order.get("id"):
                return self._resolve_ambiguous(request_id, latency, "accepted without an order id", action)
            result = self._settle(order, request_id, latency, action=action)
            result.http_status = resp.status_code
            return result

    def _resolve_ambiguous(self, request_id: str, latency_ms: float, cause: str, action: str | None) -> ExecutionResult:
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
        return self._settle(order, request_id, latency_ms, action=action, reconciled=True,
                            note=f"{cause}; order found by external-identifier, not resubmitted.")

    def recover_pending(self) -> ExecutionResult | None:
        """Resolve a crashed or uncertain intent from the broker's record. Never replays."""
        intent = self.pending_intent()
        if not intent:
            return None
        request_id = intent["execution_request_id"]
        action = intent.get("broker_action")
        if action is None:
            # `broker_action` is written immediately before the order is
            # sent, so an intent without it never reached the broker.
            return self._blocked(
                request_id,
                "Recovered intent was never sent to the broker (crash before submission); stale decision not replayed.",
                self._safe_me(),
            )
        try:
            order = self.find_order(request_id)
        except TransportError as exc:
            return ExecutionResult(
                outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id,
                note=f"Order history unavailable or incomplete ({exc}); intent retained.",
            )
        if order is None:
            scans = int(intent.get("absent_scans", 0)) + 1
            intent["absent_scans"] = scans
            self._persist_intent(intent)
            age_s = _age_s(intent.get("created_utc"))
            if age_s is None or age_s < ABSENCE_MIN_AGE_S or scans < ABSENCE_MIN_SCANS:
                return ExecutionResult(
                    outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id,
                    note=(f"In-flight order not in the broker's complete order history "
                          f"(scan {scans}, age {age_s if age_s is None else round(age_s)}s); "
                          "retained until absence is established."),
                )
            # Absent from every complete scan for long enough to call it
            # never placed. The id stays on the ownership late-fill watch and
            # its open slot stays reserved for the day, so a fill that
            # surfaces later is still attributed and still counted.
            return self._blocked(
                request_id,
                f"Order not found in {scans} complete order-history scans over {round(age_s)}s; "
                "recorded as not placed (not replayed). Still watched for a late fill.",
                self._safe_me(),
            )
        action = action or ((order.get("legs") or [{}])[0].get("action"))
        return self._settle(order, request_id, None, action=action, reconciled=True,
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


def _age_s(created_utc: Any) -> float | None:
    from datetime import datetime

    try:
        return (datetime.fromisoformat(clock.iso_utc()) - datetime.fromisoformat(str(created_utc))).total_seconds()
    except (TypeError, ValueError):
        return None


def _filled_quantity(order: dict[str, Any]) -> int | None:
    """Contracts filled so far, or None when the order doesn't say.

    `Filled` means the full size. Otherwise fills are read from each leg's
    `fills` list or `quantity - remaining-quantity`. A broker-`Rejected`
    order never executed. Anything else without fill data is unknown.
    """
    status = order.get("status")
    legs = [leg for leg in (order.get("legs") or []) if isinstance(leg, dict)]
    try:
        if status == FILLED:
            size = order.get("size")
            if size is None and legs:
                size = legs[0].get("quantity")
            return int(float(size)) if size is not None else None
        total, known = 0, False
        for leg in legs:
            fills = leg.get("fills")
            if isinstance(fills, list) and fills:
                total += sum(int(float(f.get("quantity") or 0)) for f in fills if isinstance(f, dict))
                known = True
            elif leg.get("remaining-quantity") is not None and leg.get("quantity") is not None:
                total += int(float(leg["quantity"])) - int(float(leg["remaining-quantity"]))
                known = True
    except (TypeError, ValueError):
        return None
    if known:
        return max(total, 0)
    return 0 if status == "Rejected" else None


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
                      quotes: QuoteProvider, http: Any = None) -> tuple[TastySession, TastyExecutionClient]:
    """Runner hook. Raises ConfigError (fail closed) on any missing approval."""
    config = TastyConfig.from_env()
    limits = resolve_limits(account.params)
    exit_policy, _ = resolve_exit_policy(account.params)
    ownership = OwnershipStore(account.alias, state_dir)
    session = TastySession(account, config, rate_limiter=rate_limiter, http=http, ownership=ownership)
    guard = RiskGuard(account.alias, state_dir, limits)
    return session, TastyExecutionClient(session, state_dir, quotes, guard, exit_policy=exit_policy)
