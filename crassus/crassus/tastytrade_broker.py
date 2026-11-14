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
   `external_positions` but never managed. Ownership only shrinks on
   evidence (expiry, or a confirmed non-bot closing fill); an unexplained
   gap between ownership and the broker's positions is kept as attributed
   exposure and blocks new opens until it resolves.
5. All persistent state (ownership, risk day, in-flight intent) lives in a
   directory bound to the broker environment and account (`bind_state`), so
   sandbox state can never be read as live state or vice versa.

The mode is fail-closed. Nothing trades unless `CRASSUS_TASTY_MODE` is
exactly `sandbox` or `live`; `live` additionally requires
`CRASSUS_TASTY_LIVE_APPROVAL=live:<account number>`, which only the account
owner sets. Credentials come only from the environment and are never logged.
See `docs/tastytrade-live-trading.md`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from . import clock, exchange_calendar
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
from .market import executable_price

LIVE_BASE = "https://api.tastyworks.com"
SANDBOX_BASE = "https://api.cert.tastyworks.com"
USER_AGENT = "crassus-tasty-router/1.0"
TOKEN_REFRESH_BUFFER_S = 60

# Hard ceilings. Account params may set each limit anywhere above zero up
# to its ceiling, including above the default; nothing in config can exceed
# a ceiling. Raising a ceiling is a code change that goes through review.
HARD_MAX_CONTRACTS_PER_ORDER = 1
HARD_MAX_OPEN_CONTRACTS = 1
HARD_MAX_PREMIUM_PER_CONTRACT = 5.00  # option price, i.e. $500 per contract
HARD_MAX_DAILY_LOSS_USD = 500.0
HARD_MAX_OPENS_PER_DAY = 6

DEFAULT_LIMITS = {
    "max_contracts_per_order": 1,
    "max_open_contracts": 1,
    "max_premium_per_contract": 3.00,
    # An *entry stop*, not a stop-loss: once the day's loss on the chosen
    # basis (`entry_stop_basis`) reaches this, no new position may open. It
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

# Which loss the daily entry stop measures (an owner decision):
# * "bot": the bot's own fills today (plus positions it carried in at cost),
#   unaffected by manual trades, deposits or withdrawals.
# * "account": whole-account net liquidating value against the first value
#   seen this session. Manual gains and deposits can mask bot losses.
# * "both" (default): either one stops new entries.
ENTRY_STOP_BASES = frozenset({"bot", "account", "both"})
DEFAULT_ENTRY_STOP_BASIS = "both"

# Declaring an in-flight order "never placed" needs repeated, complete
# order-history scans spread over time, not one first-page miss.
ABSENCE_MIN_AGE_S = 15 * 60
ABSENCE_MIN_SCANS = 3
ABSENCE_MIN_SPAN_S = 15 * 60  # first to last absent scan, not intent age
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
    """Defaults, overridden by account params, capped by the hard ceilings.

    A param may set a limit anywhere above zero up to its ceiling, so it can
    be looser than the default; a value above the ceiling is clamped to it.

    Units: `max_premium_per_contract` is the option *price* (3.00 means $300
    for one 100-share contract); `max_daily_loss_usd` is dollars of loss on
    the `entry_stop_basis`; the other three are whole contracts / orders.
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


def worst_case_daily_loss(limits: dict[str, float], basis: str = DEFAULT_ENTRY_STOP_BASIS) -> float | None:
    """Bound, in dollars, on the bot's own trading loss in one ET session,
    or None when the configured basis gives no such bound.

    Only the "bot" component bounds the bot's own loss. With basis
    "account" alone, gains elsewhere in the account (or a deposit) can hide
    bot losses from the stop, so there is no bot-loss bound at all; that is
    why this returns None for it.

    With "bot" or "both": the bot-attributed loss counts every open bought
    today at its cost (premium paid; a still-held position is valued at
    zero) plus positions carried in at cost, minus today's sale proceeds. A
    new open is allowed only while that loss is below `max_daily_loss_usd`,
    and the open adds at most `max_contracts_per_order` contracts at the
    premium cap. So the bound is the entry stop plus one order at the
    premium cap.

    Excluded, and reported separately: fees and commissions; fill prices
    beyond the limit (opens are limit orders priced within the cap); and
    exposure after expiry, exercise or assignment, where a long option can
    turn into a stock position whose loss is not limited to the premium.
    """
    if basis not in ENTRY_STOP_BASES:
        raise ConfigError(f"unknown entry_stop_basis {basis!r}")
    if basis == "account":
        return None
    order_cap = limits["max_premium_per_contract"] * CONTRACT_MULTIPLIER * limits["max_contracts_per_order"]
    return round(limits["max_daily_loss_usd"] + order_cap, 2)


def resolve_entry_stop_basis(params: dict[str, Any] | None) -> str:
    basis = (params or {}).get("entry_stop_basis", DEFAULT_ENTRY_STOP_BASIS)
    if basis not in ENTRY_STOP_BASES:
        raise ConfigError(f"entry_stop_basis must be one of {sorted(ENTRY_STOP_BASES)}, got {basis!r}")
    return basis


def resolve_exit_policy(params: dict[str, Any] | None) -> tuple[str, float]:
    params = params or {}
    policy = params.get("eod_unquoted_exit", DEFAULT_EXIT_POLICY)
    if policy not in EXIT_POLICIES:
        raise ConfigError(f"eod_unquoted_exit must be one of {sorted(EXIT_POLICIES)}, got {policy!r}")
    minutes = params.get("eod_unquoted_exit_minutes", 5)
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not math.isfinite(minutes) or not minutes > 0):
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


def expiry_of(symbol: str) -> date | None:
    match = _OCC_RE.match(symbol.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), "%y%m%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# State identity
# --------------------------------------------------------------------------

LEGACY_STATE_SUFFIXES = (".tasty_owned.json", ".tasty_day.json")


def bind_state(base_dir: Path, alias: str, config: TastyConfig) -> tuple[Path, str]:
    """The state directory for this alias on this broker environment and
    account, and the identity tag every state file inside it carries.

    Ownership, the risk day and the in-flight intent all live in
    `<base>/tastytrade/<alias>.<mode>.<account hash>/`. A different mode or
    account number is a different directory, so sandbox state can never be
    read as live state, nor one live account's state as another's. Nothing
    migrates automatically: a new environment or account starts owning
    nothing, and the old directory is left untouched for audit.

    Fails closed (ConfigError) when:
    * the directory's `identity.json` names a different alias, mode or
      account (a copied or hand-edited directory), or is unreadable;
    * unscoped state from an earlier build exists for this alias
      (`<base>/<alias>.tasty_owned.json` / `.tasty_day.json`). Its account is
      unknown, so it needs an explicit operator decision (move it into the
      right scoped directory, or archive it) before this bot trades.
    The account number itself is stored only as a SHA-256 digest.
    """
    base_dir = Path(base_dir)
    for suffix in LEGACY_STATE_SUFFIXES:
        legacy = base_dir / f"{alias}{suffix}"
        if legacy.exists():
            raise ConfigError(
                f"unscoped tastytrade state {legacy.name} found; its broker account is unknown. "
                "Move it into the scoped state directory for the right account or archive it, "
                "then restart. Refusing to trade."
            )
    digest = hashlib.sha256(f"{config.mode}:{config.account_number}".encode()).hexdigest()
    safe_alias = re.sub(r"[^A-Za-z0-9_.-]+", "_", alias).strip("._") or "account"
    scoped = base_dir / "tastytrade" / f"{safe_alias}.{config.mode}.{digest[:16]}"
    durable_mkdir(scoped)
    expected = {"alias": alias, "mode": config.mode, "account_sha256": digest}
    marker = scoped / "identity.json"
    if marker.exists():
        try:
            found = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"unreadable state identity {marker}: {exc}; refusing to trade") from exc
        if found != expected:
            raise ConfigError(
                f"state directory {scoped.name} belongs to a different alias, environment or account; "
                "refusing to trade."
            )
    else:
        _atomic_write_json(marker, expected)
    return scoped, digest[:16]


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

    Persisted at `<scoped state dir>/<alias>.tasty_owned.json`:

    * `identity`: the `bind_state` tag; a file carrying another tag is
      refused, so state cannot cross environments or accounts.
    * `owned`: crassus symbol -> signed contracts this bot holds.
    * `orders`: execution_request_id -> {symbol, action, quantity, applied,
      final, created_utc, notional, filled_utc, fill_et_date}. `applied` is
      the filled quantity already credited, so crediting the same fill twice
      (reconcile, recovery, restart) is a no-op. `notional` is the dollar
      value of those fills (conservative when the broker gives no fill
      price). Non-final entries younger than `LATE_FILL_WATCH_S` are
      re-checked against the order history each cycle.
    * `discrepancies`: symbol -> details, while the broker shows fewer
      contracts than the bot owns and nothing explains it yet.

    Only this bot's own fills ever increase `owned`. Ownership shrinks only
    on evidence: the contract has expired, or a non-bot closing fill on that
    symbol after the bot's fill accounts for the gap. A position that is
    merely missing from a snapshot (the positions endpoint lagging the
    fill, or a transient disappearance) is kept as owned and recorded as a
    discrepancy, which blocks new opens until it resolves.

    A missing file means "owns nothing" (a fresh deployment). An unreadable
    or malformed file raises `OwnershipError`, which stops this bot's cycle
    rather than guessing.
    """

    def __init__(self, alias: str, state_dir: Path, *, identity: str | None = None):
        self.state_dir = Path(state_dir)
        durable_mkdir(self.state_dir)
        self.path = self.state_dir / f"{alias}.tasty_owned.json"
        self.identity = identity
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {"identity": self.identity, "owned": {}, "orders": {}, "discrepancies": {}}
            except OSError as exc:
                raise OwnershipError(f"cannot read ownership file {self.path.name}: {exc}") from exc
            try:
                data = json.loads(raw)
                owned = {str(k): int(v) for k, v in (data.get("owned") or {}).items()}
                orders = dict(data.get("orders") or {})
                discrepancies = dict(data.get("discrepancies") or {})
                found = data.get("identity")
            except (ValueError, TypeError, AttributeError) as exc:
                raise OwnershipError(
                    f"ownership file {self.path.name} is malformed ({exc}); refusing to trade until it is repaired"
                ) from exc
            if self.identity is not None and found != self.identity:
                raise OwnershipError(
                    f"ownership file {self.path.name} belongs to a different broker account or environment; "
                    "refusing to trade"
                )
            return {"identity": self.identity, "owned": owned, "orders": orders, "discrepancies": discrepancies}

    def _save(self, data: dict[str, Any]) -> None:
        data["owned"] = {k: v for k, v in data["owned"].items() if v != 0}
        data["identity"] = self.identity
        _atomic_write_json(self.path, data)

    def register(self, request_id: str, symbol: str, action: str, quantity: int = 1) -> None:
        """Record a submission before it is sent, so a late fill can be found."""
        with self._lock:
            data = self.load()
            data["orders"].setdefault(request_id, {
                "symbol": symbol, "action": action, "quantity": int(quantity), "applied": 0, "final": False,
                "created_utc": clock.iso_utc(),
            })
            self._save(data)

    def apply_fill(self, request_id: str, filled_quantity: int, *, final: bool,
                   notional: float | None = None) -> int:
        """Credit the not-yet-credited part of a fill. Returns the delta.

        `notional` is the dollar value of all `filled_quantity` contracts so
        far, when known.
        """
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
                entry["filled_utc"] = clock.iso_utc()
                entry.setdefault("fill_et_date", clock.now_et().date().isoformat())
            if notional is not None and filled_quantity:
                entry["notional"] = round(float(notional), 2)
            if final:
                entry["final"] = True
            self._save(data)
            return delta

    def watch_ids(self) -> list[str]:
        """Non-final submissions still young enough to watch for a late fill."""
        with self._lock:
            return [rid for rid, _ in self._watched(self.load())]

    @staticmethod
    def _watched(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for rid, entry in data["orders"].items():
            if entry.get("final"):
                continue
            age = _age_s(entry.get("created_utc"))
            if age is None or age <= LATE_FILL_WATCH_S:
                out.append((rid, entry))
        return out

    def oldest_watch_date(self) -> date | None:
        with self._lock:
            dates = [_et_date(e.get("created_utc")) for _, e in self._watched(self.load())]
            dates = [d for d in dates if d is not None]
            return min(dates) if dates else None

    def pending_open_contracts(self, today: date) -> int:
        """Contracts that opening orders sent today could still add.

        Day orders cannot fill after their session, so only orders created
        in today's ET session count. A non-final order counts its unfilled
        remainder until the broker reports it terminal; that includes an
        order already recorded as "not placed", because absence from the
        history is not proof.
        """
        with self._lock:
            pending = 0
            for entry in self.load()["orders"].values():
                if entry.get("final") or not str(entry.get("action", "")).endswith("Open"):
                    continue
                if _et_date(entry.get("created_utc")) != today:
                    continue
                pending += max(int(entry.get("quantity", 1)) - int(entry.get("applied", 0)), 0)
            return pending

    def reconcile_with_broker(self, broker_qty: dict[str, int], *, manual_closes: dict[str, list[tuple[str, int]]],
                              today: date) -> tuple[dict[str, int], dict[str, Any], list[dict[str, Any]]]:
        """Compare ownership with the broker's positions.

        Returns (owned, open discrepancies, resolutions this call). Ownership
        never grows here, and only shrinks on evidence:

        * `expired`: the contract's expiry date is before today (ET);
        * `manual_close`: non-bot closing fills on that symbol, after the
          bot's latest opening fill, cover the shortfall.

        Anything else is a discrepancy: ownership is kept (the bot still
        treats the exposure as its own) and the caller blocks new opens.
        """
        with self._lock:
            data = self.load()
            now = clock.iso_utc()
            changed = False
            resolved: list[dict[str, Any]] = []
            discrepancies = data["discrepancies"]
            for sym, qty in list(data["owned"].items()):
                held = broker_qty.get(sym, 0)
                covered = max(min(qty, held), 0) if qty > 0 else min(max(qty, held), 0)
                if covered == qty:
                    if discrepancies.pop(sym, None) is not None:
                        changed = True
                    continue
                shortfall = abs(qty - covered)
                last_fill = max((e.get("filled_utc") or "" for e in data["orders"].values()
                                 if e.get("symbol") == sym and str(e.get("action", "")).endswith("Open")
                                 and int(e.get("applied", 0)) > 0), default="")
                expiry = expiry_of(sym)
                after_fill = sum(q for ts, q in manual_closes.get(sym, []) if ts and _ts(ts) >= _ts(last_fill))
                if expiry is not None and expiry < today:
                    reason = "expired"
                elif last_fill and after_fill >= shortfall:
                    reason = "manual_close"
                else:
                    entry = discrepancies.setdefault(sym, {"first_seen_utc": now, "snapshots": 0})
                    entry.update(owned=qty, broker_held=held, last_seen_utc=now,
                                 snapshots=int(entry.get("snapshots", 0)) + 1)
                    changed = True
                    continue
                data["owned"][sym] = covered
                discrepancies.pop(sym, None)
                resolved.append({"symbol": sym, "from": qty, "to": covered, "reason": reason})
                changed = True
            for sym in [s for s in discrepancies if s not in data["owned"]]:
                discrepancies.pop(sym)
                changed = True
            if changed:
                self._save(data)
            owned = {k: v for k, v in data["owned"].items() if v != 0}
            return owned, dict(discrepancies), resolved


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
    # Every option position the broker reports (bot and external).
    broker_positions: dict[str, int] = field(default_factory=dict)
    # Owned contracts the broker does not currently show, with no evidence
    # yet of why. Kept as the bot's exposure; new opens are blocked.
    ownership_discrepancies: dict[str, Any] = field(default_factory=dict)
    ownership_resolved: list[dict[str, Any]] = field(default_factory=list)
    # Contracts that today's unresolved opening orders could still add.
    pending_open_contracts: int = 0
    late_fills: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = {
            **super().summary(),
            "broker": "tastytrade",
            "broker_mode": self.broker_mode,
            "net_liquidating_value": self.net_liquidating_value,
            "external_positions": dict(self.external_positions),
        }
        if self.ownership_discrepancies:
            out["ownership_discrepancies"] = dict(self.ownership_discrepancies)
        if self.ownership_resolved:
            out["ownership_resolved"] = list(self.ownership_resolved)
        if self.pending_open_contracts:
            out["pending_open_contracts"] = self.pending_open_contracts
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

    def list_orders(self, since: date | None = None) -> list[dict[str, Any]]:
        """Every order from the day before `since` (default: yesterday, ET),
        across all pages.

        Raises `TransportError` unless every page is a well-formed success
        (`data.items` a list of objects, valid pagination metadata), so a
        caller can never mistake a malformed, empty-bodied or truncated
        response for "never placed".
        """
        start = clock.now_et().date() - timedelta(days=1)
        if since is not None and since - timedelta(days=1) < start:
            start = since - timedelta(days=1)
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            resp = self.request("GET", f"/accounts/{self.config.account_number}/orders",
                                params={"start-date": start.isoformat(), "per-page": ORDERS_PER_PAGE,
                                        "page-offset": page})
            if resp.status_code >= 400:
                raise TransportError(f"GET orders -> {resp.status_code} {self._error_text(resp)}")
            payload = _json(resp)
            data = payload.get("data") if isinstance(payload, dict) else None
            batch = data.get("items") if isinstance(data, dict) else None
            if not isinstance(batch, list) or not all(isinstance(o, dict) for o in batch):
                raise TransportError("order history response is malformed (no data.items list of orders); "
                                     "cannot treat it as complete")
            items.extend(batch)
            pagination = payload.get("pagination")
            if pagination is not None and not isinstance(pagination, dict):
                raise TransportError("order history pagination is malformed; cannot treat it as complete")
            total_pages = (pagination or {}).get("total-pages")
            if total_pages is not None:
                if isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < 0:
                    raise TransportError("order history total-pages is malformed; cannot treat it as complete")
                page += 1
                if page >= total_pages:
                    return items
                if page >= 50:
                    raise TransportError("order history exceeds 50 pages; refusing to treat it as complete")
                continue
            if len(batch) < ORDERS_PER_PAGE:
                return items
            raise TransportError("order history is a full page with no pagination metadata; cannot prove completeness")

    def find_order(self, execution_request_id: str, since: date | None = None) -> dict[str, Any] | None:
        return next((o for o in self.list_orders(since) if o.get("external-identifier") == execution_request_id), None)

    def _credit_late_fills(self, orders: list[dict[str, Any]]) -> list[str]:
        """Attribute fills of this bot's own orders that became visible late."""
        watch = self.ownership.watch_ids()
        if not watch:
            return []
        by_id = {o.get("external-identifier"): o for o in orders}
        credited = []
        for rid in watch:
            order = by_id.get(rid)
            if order is None:
                continue
            filled = _filled_quantity(order)
            if filled is None:
                continue
            status = order.get("status")
            terminal = status == FILLED or status in DEAD_STATUSES
            if self.ownership.apply_fill(rid, filled, final=terminal, notional=_fill_notional(order, filled)):
                credited.append(rid)
        return credited

    def me(self) -> TastyAccountState:
        n = self.config.account_number
        today = clock.now_et().date()
        balances = self._get_data(f"/accounts/{n}/balances") or {}
        if not isinstance(balances, dict):
            raise TransportError("balances response is malformed")
        data = self.ownership.load()
        orders: list[dict[str, Any]] = []
        if data["owned"] or self.ownership.watch_ids():
            # Needed both for late fills and as evidence for any ownership
            # gap. Incomplete history raises, failing this cycle closed.
            orders = self.list_orders(self.ownership.oldest_watch_date())
        late = self._credit_late_fills(orders)
        positions = (self._get_data(f"/accounts/{n}/positions") or {}).get("items", [])

        broker_qty: dict[str, int] = {}
        avg_price: dict[str, float] = {}
        for pos in positions:
            if not isinstance(pos, dict) or pos.get("instrument-type") != "Equity Option":
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
            avg_price[symbol] = _float(pos.get("average-open-price")) or 0.0

        bot_ids = set(self.ownership.load()["orders"])
        owned, discrepancies, resolved = self.ownership.reconcile_with_broker(
            broker_qty, manual_closes=_non_bot_closes(orders, bot_ids), today=today,
        )
        trades = []
        for symbol, qty in sorted(owned.items()):
            # One synthetic opening trade per *owned* position, so `Book`,
            # the strategy and the EOD flatten only ever see the bot's own
            # contracts, including ones the positions endpoint has not
            # caught up with yet.
            trades.append({
                "sym": symbol,
                "side": "buy" if qty > 0 else "sell",
                "qty": abs(qty),
                "price": avg_price.get(symbol, 0.0),
                "source": "tastytrade_position",
            })
        external = {}
        for sym, qty in broker_qty.items():
            mine = owned.get(sym, 0)
            covered = max(min(mine, qty), 0) if mine > 0 else min(max(mine, qty), 0)
            if qty - covered != 0:
                external[sym] = qty - covered
        return TastyAccountState(
            username=self.account.username,
            balance_cash=_float(balances.get("cash-balance")) or 0.0,
            trades=trades,
            net_liquidating_value=_float(balances.get("net-liquidating-value")),
            broker_mode=self.config.mode,
            external_positions=external,
            broker_positions=dict(broker_qty),
            ownership_discrepancies=discrepancies,
            ownership_resolved=resolved,
            pending_open_contracts=self.ownership.pending_open_contracts(today),
            late_fills=late,
        )


# --------------------------------------------------------------------------
# Risk guard
# --------------------------------------------------------------------------


class RiskGuard:
    """Per-day limits that the strategy layer cannot see or override.

    Day state is kept in `<scoped state dir>/<alias>.tasty_day.json`, keyed by
    the ET session date, so a restart mid-session resets nothing:

    * `start_nlv`: the first finite net liquidating value seen this session
      (the "account" basis compares against it).
    * `carry_cost`: dollars paid for bot positions already held when the
      session was first observed (the "bot" basis counts them at cost).
    * `opens`: execution_request_id -> "reserved" | "filled". An opening
      order reserves its slot *before* it is sent and keeps it through any
      uncertainty (timeout, unknown status, crash). The slot is released
      only when the broker definitively reports the order ended unfilled.
      Keyed by request id, so reconciling the same order twice, or after a
      restart, never counts it twice.

    The kill-switch file stays in the unscoped base state directory
    (`halt_dir`), where an operator expects it.
    """

    def __init__(self, alias: str, state_dir: Path, limits: dict[str, float], halt_env: str = "CRASSUS_TASTY_HALT",
                 *, ownership: OwnershipStore | None = None, basis: str = DEFAULT_ENTRY_STOP_BASIS,
                 identity: str | None = None, halt_dir: Path | None = None):
        if basis not in ENTRY_STOP_BASES:
            raise ConfigError(f"unknown entry_stop_basis {basis!r}")
        if basis != "account" and ownership is None:
            raise ConfigError("the bot-loss entry stop needs the ownership store")
        self.alias = alias
        self.state_dir = Path(state_dir)
        self.limits = limits
        self.halt_env = halt_env
        self.ownership = ownership
        self.basis = basis
        self.identity = identity
        self.halt_dir = Path(halt_dir) if halt_dir is not None else self.state_dir
        self._lock = threading.RLock()
        durable_mkdir(self.state_dir)

    @property
    def _path(self) -> Path:
        return self.state_dir / f"{self.alias}.tasty_day.json"

    @property
    def halt_file(self) -> Path:
        return self.halt_dir / "TASTY_HALT"

    def _day(self) -> dict[str, Any]:
        today = clock.now_et().date().isoformat()
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError) as exc:
            raise OwnershipError(f"cannot read risk day state {self._path.name}: {exc}") from exc
        if state and self.identity is not None and state.get("identity") != self.identity:
            raise OwnershipError(f"risk day state {self._path.name} belongs to a different broker account; refusing")
        if state.get("date") != today or not isinstance(state.get("opens"), dict):
            state = {"date": today, "start_nlv": None, "carry_cost": None, "opens": {}}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["identity"] = self.identity
        _atomic_write_json(self._path, state)

    def observe(self, state: TastyAccountState) -> dict[str, Any]:
        with self._lock:
            day = self._day()
            changed = False
            nlv = state.net_liquidating_value
            if day.get("start_nlv") is None and nlv is not None and math.isfinite(nlv):
                day["start_nlv"] = nlv
                changed = True
            if day.get("carry_cost") is None and self.ownership is not None:
                day["carry_cost"] = self._carry_cost(day["date"])
                changed = True
            if changed:
                self._save(day)
            return day

    def _carry_cost(self, today: str) -> float:
        """Cost of bot positions held into this session from an earlier one."""
        data = self.ownership.load()
        cap = self.limits["max_premium_per_contract"] * CONTRACT_MULTIPLIER
        total = 0.0
        for sym, qty in data["owned"].items():
            if qty <= 0:
                continue
            opens = [e for e in data["orders"].values()
                     if e.get("symbol") == sym and e.get("action") == "Buy to Open" and int(e.get("applied", 0)) > 0]
            if opens and all(e.get("fill_et_date") == today for e in opens):
                continue  # opened today: counted in today's buys instead
            latest = max(opens, key=lambda e: e.get("filled_utc") or "", default=None)
            per_contract = cap
            if latest and latest.get("notional") is not None and int(latest["applied"]) > 0:
                per_contract = float(latest["notional"]) / int(latest["applied"])
            total += per_contract * qty
        return round(total, 2)

    def bot_loss_today(self) -> float:
        """The bot's own loss this ET session, in dollars (positive = loss).

        carry_cost + cost of today's bot buys - proceeds of today's bot sells.
        A still-held position is valued at zero and a buy with no known fill
        price at the premium cap; a sale with no known price adds nothing.
        Every unknown errs towards a larger loss.
        """
        day = self.observe_day_only()
        today = day["date"]
        cap = self.limits["max_premium_per_contract"] * CONTRACT_MULTIPLIER
        cost = float(day.get("carry_cost") or 0.0)
        proceeds = 0.0
        for e in self.ownership.load()["orders"].values():
            applied = int(e.get("applied", 0))
            if not applied or e.get("fill_et_date") != today:
                continue
            notional = e.get("notional")
            if str(e.get("action", "")).startswith("Buy"):
                cost += float(notional) if notional is not None else applied * cap
            elif notional is not None:
                proceeds += float(notional)
        return round(cost - proceeds, 2)

    def observe_day_only(self) -> dict[str, Any]:
        with self._lock:
            day = self._day()
            if day.get("carry_cost") is None and self.ownership is not None:
                day["carry_cost"] = self._carry_cost(day["date"])
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
        if state.ownership_discrepancies:
            return ("ownership discrepancy unresolved for "
                    f"{sorted(state.ownership_discrepancies)} (bot owns more than the broker shows, unexplained); "
                    "no new positions until it resolves")
        if quantity > self.limits["max_contracts_per_order"]:
            return f"quantity {quantity} exceeds max_contracts_per_order {self.limits['max_contracts_per_order']:g}"
        held = sum(abs(p.quantity) for p in book.positions.values())
        pending = int(state.pending_open_contracts or 0)
        if held + pending + quantity > self.limits["max_open_contracts"]:
            return (f"would hold {held + pending + quantity} contracts ({held} held, {pending} from unresolved "
                    f"opening orders); max_open_contracts is {self.limits['max_open_contracts']:g}")
        if limit_price is None:
            return "no executable quote to price the order"
        if limit_price > self.limits["max_premium_per_contract"]:
            return f"premium {limit_price:.2f} exceeds max_premium_per_contract {self.limits['max_premium_per_contract']:.2f}"
        day = self.observe(state)
        if len(day["opens"]) >= self.limits["max_opens_per_day"]:
            return f"max_opens_per_day {self.limits['max_opens_per_day']:g} reached (filled or still uncertain)"
        stop = self.limits["max_daily_loss_usd"]
        start, now = day.get("start_nlv"), state.net_liquidating_value
        if start is None or now is None or not math.isfinite(start) or not math.isfinite(now):
            return "net liquidating value unavailable or not a finite number; no new positions"
        if self.basis in ("bot", "both"):
            loss = self.bot_loss_today()
            if loss >= stop:
                return (f"bot entry stop hit: the bot's own loss today is {loss:.2f} USD "
                        f"(entry stop {stop:.2f}); no new positions")
        if self.basis in ("account", "both") and start - now >= stop:
            return (f"account entry stop hit: account net liq down {start - now:.2f} USD today "
                    f"(entry stop {stop:.2f}); no new positions")
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
        exit_minutes: float = 5.0,
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
        self.exit_minutes = float(exit_minutes)
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep

    @property
    def _orders_path(self) -> str:
        return f"/accounts/{self.session.config.account_number}/orders"

    # -- pricing ----------------------------------------------------------

    def _price(self, symbol: str, side: str) -> float | None:
        """Limit price from a fresh, well-formed quote; None otherwise.

        Same predicate (`market.executable_price`) the brokerage EOD flatten
        uses to decide whether a quote exists at all.
        """
        return executable_price((self.quotes([symbol]) or {}).get(symbol), side)

    def _unquoted_exit_blocker(self) -> str | None:
        """Why an unquoted market close may not go out *now*, or None.

        Enforced here, at the last step before the order leaves, from the
        exchange calendar (holidays and early closes) and the current time,
        whatever the caller decided earlier with an older quote.
        """
        now = clock.now_et()
        today = now.date()
        if now.weekday() >= 5 or exchange_calendar.is_holiday(today):
            return "the market is closed today"
        close = exchange_calendar.session_close(today)
        close_at = now.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
        minutes = (close_at - now).total_seconds() / 60.0
        if minutes <= 0:
            return f"the session closed {-minutes:.1f} minute(s) ago"
        if minutes > self.exit_minutes:
            return (f"{minutes:.1f} minute(s) to the {close.strftime('%H:%M')} close is outside the final "
                    f"{self.exit_minutes:g} minute(s) allowed for an unquoted market exit")
        return None

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

    def find_order(self, execution_request_id: str, since: date | None = None) -> dict[str, Any] | None:
        return self.session.find_order(execution_request_id, since)

    def find_trade(self, execution_request_id: str, state: AccountState | None = None) -> dict[str, Any] | None:
        order = self.find_order(execution_request_id)
        return order if order and order.get("status") == FILLED else None

    def _get_order(self, order_id: Any) -> dict[str, Any] | None:
        return self.session._get_data(f"{self._orders_path}/{order_id}")

    # -- bookkeeping ------------------------------------------------------

    def _account_for(self, request_id: str, action: str | None, filled: int, *, final: bool,
                     order: dict[str, Any] | None = None) -> None:
        """Credit ownership and settle the open reservation, idempotently."""
        notional = _fill_notional(order, filled) if order is not None else None
        self.ownership.apply_fill(request_id, filled, final=final, notional=notional)
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
                    self._account_for(request_id, action, partial, final=False, order=order)
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

        self._account_for(request_id, action, filled, final=True, order=order)
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
            broker_held = max(state.broker_positions.get(symbol, 0), 0)
            if action == "Sell to Close" and quantity > broker_held:
                # The bot owns it but the broker does not show it (yet):
                # never send a close the broker cannot match to a position,
                # and never let it consume a manual contract.
                return self._blocked(
                    request_id,
                    f"Broker shows {broker_held} {symbol} held; the bot owns {abs(held)} "
                    "(ownership discrepancy unresolved). Not closing until they agree.",
                    state,
                )

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
                too_early = self._unquoted_exit_blocker()
                if too_early:
                    return self._blocked(
                        request_id,
                        f"No executable quote to close {symbol}, and {too_early}. Will retry next cycle.",
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
            self.ownership.register(request_id, symbol, action, quantity)
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
            order = self.find_order(request_id, since=_et_date(intent.get("created_utc")))
        except TransportError as exc:
            return ExecutionResult(
                outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id,
                note=f"Order history unavailable or incomplete ({exc}); intent retained.",
            )
        if order is None:
            times = [t for t in (intent.get("absent_scan_utc") or []) if isinstance(t, str)] + [clock.iso_utc()]
            intent["absent_scan_utc"] = times
            intent["absent_scans"] = len(times)
            self._persist_intent(intent)
            age_s = _age_s(intent.get("created_utc"))
            span_s = (_ts(times[-1]) - _ts(times[0])).total_seconds()
            if (age_s is None or age_s < ABSENCE_MIN_AGE_S or len(times) < ABSENCE_MIN_SCANS
                    or span_s < ABSENCE_MIN_SPAN_S):
                return ExecutionResult(
                    outcome_class=Outcome.AMBIGUOUS, execution_request_id=request_id,
                    note=(f"In-flight order not in the broker's complete order history "
                          f"(scan {len(times)} over {round(span_s)}s, age "
                          f"{age_s if age_s is None else round(age_s)}s); retained until absence is established."),
                )
            # Absent from complete scans spread over the minimum span. The
            # intent is released, but not the exposure: the order stays on
            # the late-fill watch, its daily slot stays reserved and its
            # quantity still counts against max_open_contracts for the rest
            # of its session, so a late fill can never be doubled.
            return self._blocked(
                request_id,
                f"Order not found in {len(times)} complete order-history scans over {round(span_s)}s; "
                "recorded as not placed (not replayed). Its exposure stays reserved for the session "
                "and it is still watched for a late fill.",
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
    """A finite float, or None. NaN and infinity are not account values."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ts(iso: Any) -> datetime:
    try:
        out = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=clock.ET)
    return out if out.tzinfo else out.replace(tzinfo=clock.ET)


def _et_date(iso: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(clock.ET).date()
    except (TypeError, ValueError):
        return None


def _leg_filled(order: dict[str, Any], leg: dict[str, Any]) -> int:
    fills = leg.get("fills")
    try:
        if isinstance(fills, list) and fills:
            return sum(int(float(f.get("quantity") or 0)) for f in fills if isinstance(f, dict))
        if leg.get("remaining-quantity") is not None and leg.get("quantity") is not None:
            return max(int(float(leg["quantity"])) - int(float(leg["remaining-quantity"])), 0)
        if order.get("status") == FILLED and leg.get("quantity") is not None:
            return int(float(leg["quantity"]))
    except (TypeError, ValueError):
        return 0
    return 0


def _non_bot_closes(orders: list[dict[str, Any]], bot_ids: set[str]) -> dict[str, list[tuple[str, int]]]:
    """Closing fills on the account that this bot did not send:
    symbol -> [(broker timestamp, contracts)]. Evidence for a manual close."""
    out: dict[str, list[tuple[str, int]]] = {}
    for order in orders:
        if order.get("external-identifier") in bot_ids:
            continue
        when = order.get("terminal-at") or order.get("updated-at") or order.get("received-at")
        for leg in order.get("legs") or []:
            if not isinstance(leg, dict) or not str(leg.get("action", "")).endswith("Close"):
                continue
            try:
                sym = to_crassus_symbol(str(leg.get("symbol", "")))
            except ValueError:
                continue
            qty = _leg_filled(order, leg)
            if qty:
                out.setdefault(sym, []).append((str(when) if when else "", qty))
    return out


def _fill_notional(order: dict[str, Any] | None, filled: int | None) -> float | None:
    """Dollar value of `filled` contracts of this order, or None if unknown.

    Uses the legs' fill prices when they cover the whole filled quantity,
    otherwise the order's limit price (no better than the fill for a buy or
    for a sell, so the bot-loss stop errs high). A market order with no fill
    prices is unknown.
    """
    if not order or not filled:
        return None
    total_qty, total_value, complete = 0, 0.0, True
    for leg in order.get("legs") or []:
        for f in (leg.get("fills") or []) if isinstance(leg, dict) else []:
            if not isinstance(f, dict):
                continue
            q, px = _float(f.get("quantity")), _float(f.get("fill-price"))
            if q is None or px is None:
                complete = False
                continue
            total_qty += int(q)
            total_value += q * px
    if complete and total_qty == filled:
        return round(total_value * CONTRACT_MULTIPLIER, 2)
    limit = _float(order.get("price"))
    if limit is not None and limit > 0:
        return round(limit * filled * CONTRACT_MULTIPLIER, 2)
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


def assemble(account: Any, config: TastyConfig, *, state_dir: Path, quotes: QuoteProvider,
             rate_limiter: RateLimiter | None = None, http: Any = None,
             **executor_kw: Any) -> tuple[TastySession, TastyExecutionClient]:
    """Wire one routed account. Every persistent file goes in the directory
    `bind_state` gives for this alias, environment and account."""
    limits = resolve_limits(account.params)
    exit_policy, exit_minutes = resolve_exit_policy(account.params)
    basis = resolve_entry_stop_basis(account.params)
    scoped, identity = bind_state(state_dir, account.alias, config)
    ownership = OwnershipStore(account.alias, scoped, identity=identity)
    session = TastySession(account, config, rate_limiter=rate_limiter, http=http, ownership=ownership)
    guard = RiskGuard(account.alias, scoped, limits, ownership=ownership, basis=basis, identity=identity,
                      halt_dir=state_dir)
    executor = TastyExecutionClient(session, scoped, quotes, guard, exit_policy=exit_policy,
                                    exit_minutes=exit_minutes, **executor_kw)
    return session, executor


def build_for_account(account: Any, *, state_dir: Path, rate_limiter: RateLimiter,
                      quotes: QuoteProvider, http: Any = None) -> tuple[TastySession, TastyExecutionClient]:
    """Runner hook. Raises ConfigError (fail closed) on any missing approval
    or state that belongs to a different broker account."""
    config = TastyConfig.from_env()
    return assemble(account, config, state_dir=state_dir, quotes=quotes, rate_limiter=rate_limiter, http=http)
