"""Sessions, execution, and reconciliation against the Worker.

This module owns every mandatory integrity invariant from MOO-24. The
strategy layer above it is free to be wrong, reckless, or badly calibrated --
fake-capital catastrophe is a permitted experimental outcome. What is *not*
permitted is losing the causal record of what happened, so the rules enforced
here are about evidence, not about risk:

  1. `execution_request_id` is persisted to disk *before* the request is sent.
  2. Only one outstanding mutation per account (the Worker's KV writes are
     best-effort, not compare-and-swap).
  3. After a timeout or 5xx, `/api/me` is queried before any retry.
  4. `balance_cash` is cash movement, never P&L or net liquidation value.
  5. Rate limits and `Retry-After` are honored across all accounts at once.
  6. HTTP 410 is terminal -- the Worker has deleted the user outright.
  7. Failures are surfaced with a classified outcome, never swallowed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .durability import durable_mkdir, sync_directory
from . import clock
from .audit import Outcome
from .config import BASE_URL, BOT_REGISTRATION_KEY
from .market import intent_lineage


class CrassusError(Exception):
    """Base for errors that carry an audit outcome class."""

    outcome_class = Outcome.RUNNER_ERROR


class AccountLiquidated(CrassusError):
    """HTTP 410 -- insolvent settlement deleted the user and its session.

    Terminal and unrecoverable for this login: the account no longer exists
    server-side. Permitted as an experiment, but it must be recorded as a
    margin call rather than surfacing as a generic HTTP failure.

    NOTE: this is verified against the documented contract (the mock
    Worker), not the real deployed Worker. `worker.js`'s settlement path
    currently deletes the user record outright rather than leaving a
    tombstone, so a session resolved *after* liquidation reports a plain
    401, not 410 -- this detector has never actually seen that happen. See
    `crassus/README.md` ("Known gaps") before treating this as validated
    end-to-end.
    """

    outcome_class = Outcome.ACCOUNT_LIQUIDATED


class TransportError(CrassusError):
    outcome_class = Outcome.TRANSPORT_ERROR


class ConfigError(CrassusError):
    """Misconfiguration that must stop the runner rather than degrade.

    Distinct from TransportError: nothing about retrying or reconciling helps,
    and continuing would produce a wrong-but-plausible result (an account
    registered as a human, silently missing from the roster).
    """

    outcome_class = Outcome.TRANSPORT_ERROR


class AmbiguousExecution(CrassusError):
    """The trade may or may not have happened. Requires reconciliation."""

    outcome_class = Outcome.AMBIGUOUS


#: Non-5xx statuses that mean "the Worker could not answer right now" rather
#: than "the credentials or the account are wrong". Every 5xx is transient too
#: (see `is_transient`): a 501 or 505 from an intermediary is no more a
#: provisioning signal than a 503, and treating it as a configuration error
#: would stop an account on a proxy fault.
TRANSIENT_HTTP = frozenset({408, 425, 429})

#: Bounded *rate*, not a capped attempt count: a first-use registration may be
#: retried after a recoverable failure, with the delay growing 300 -> 900 ->
#: 1800 s and then staying at 1800 s. Retries continue at that ceiling for as
#: long as the process lives, so a Worker that recovers is eventually picked up;
#: there is no permanent give-up and nothing here sleeps or spins. The runner's
#: own cycle is five minutes, so the first backoff is already one cycle.
PROVISION_BACKOFF_S = (300.0, 900.0, 1800.0)

#: After a positive "the account already exists but our password was rejected",
#: stop re-attempting registration for a while. One login per cycle continues,
#: so a fixed credential is picked up without any register traffic.
CREDENTIAL_MISMATCH_COOLDOWN_S = 3600.0


def is_transient(status: int) -> bool:
    """Any 5xx, plus the non-5xx statuses that mean "try again later".

    The documented contract is "5xx is the server's fault", and this treats it
    as one status class rather than enumerating codes: 501 and 505 are as
    transient as 503, and a 5xx must never be read as a provisioning signal.
    """
    return status in TRANSIENT_HTTP or 500 <= status <= 599


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class RateLimiter:
    """Token bucket shared by every account.

    The limit that matters is Cloudflare's, and it is per-IP: six bot accounts
    on one host share one budget. A per-account limiter would let six
    individually-polite bots collectively trip a 429, so this is deliberately
    global and process-wide.
    """

    def __init__(self, capacity: int = 8, refill_per_s: float = 0.8):
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._pause_until = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.refill_per_s
                )
                self._last = now

                sleep_for = max(0.0, self._pause_until - now)
                if sleep_for <= 0 and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                if sleep_for <= 0:
                    sleep_for = (1.0 - self._tokens) / self.refill_per_s
            time.sleep(sleep_for)
            waited += sleep_for

    def penalize(self, retry_after_s: float) -> None:
        """Honor a server-sent Retry-After for every account, not just this one."""
        with self._lock:
            self._pause_until = max(self._pause_until, time.monotonic() + retry_after_s)
            self._tokens = 0.0


# --------------------------------------------------------------------------
# Position book
# --------------------------------------------------------------------------


@dataclass
class Position:
    symbol: str
    quantity: int  # negative means short
    average_cost: float

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


class Book:
    """Average-cost positions derived from the server's trade history.

    The Worker exposes no positions endpoint and no server-side P&L, so the
    book is reconstructed from `/api/me` trades on every reconciliation. The
    server's trade list is authoritative; this object is a derived view and is
    never used as a source of truth.

    NOTE: the exact trade-record field names are unverified -- the account
    endpoints are not present on the deployed Worker (see P0 findings), so
    this normalizes across the plausible spellings rather than guessing one.
    Tighten `_normalize` once a real `/api/me` payload is observable.
    """

    def __init__(self, trades: list[dict[str, Any]] | None = None):
        self.trades = trades or []
        self.positions: dict[str, Position] = {}
        self._rebuild()

    @staticmethod
    def _normalize(trade: dict[str, Any]) -> tuple[str, str, int, float] | None:
        def pick(*keys: str) -> Any:
            for k in keys:
                if trade.get(k) is not None:
                    return trade[k]
            return None

        symbol = pick("sym", "symbol", "option_symbol", "OptionSymbol")
        side = pick("side", "action", "direction")
        qty = pick("qty", "quantity", "contracts")
        price = pick("price", "fill_price", "executed_price", "avg_price")
        if not symbol or not side or qty is None or price is None:
            return None
        return str(symbol), str(side).lower(), int(qty), float(price)

    def _rebuild(self) -> None:
        self.positions.clear()
        for trade in self.trades:
            parsed = self._normalize(trade)
            if not parsed:
                continue
            symbol, side, qty, price = parsed
            signed = qty if side == "buy" else -qty
            pos = self.positions.get(symbol) or Position(symbol, 0, 0.0)

            if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
                # Opening or adding: blend into the average cost.
                total = pos.quantity + signed
                if total != 0:
                    pos.average_cost = (
                        pos.average_cost * abs(pos.quantity) + price * abs(signed)
                    ) / abs(total)
                pos.quantity = total
            else:
                # Reducing or flipping: cost basis only resets on a flip.
                pos.quantity += signed
                if pos.quantity == 0:
                    pos.average_cost = 0.0
                elif (pos.quantity > 0) != (signed < 0):
                    pos.average_cost = price
            self.positions[symbol] = pos

        self.positions = {s: p for s, p in self.positions.items() if p.quantity != 0}

    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position(symbol, 0, 0.0)

    @property
    def is_flat(self) -> bool:
        return not self.positions

    def summary(self) -> dict[str, Any]:
        return {
            "open_positions": {s: p.quantity for s, p in self.positions.items()},
            "trade_count": len(self.trades),
        }


# --------------------------------------------------------------------------
# Account session
# --------------------------------------------------------------------------


@dataclass
class AccountState:
    """A reconciled read of `/api/me`."""

    username: str
    balance_cash: float
    trades: list[dict[str, Any]]
    fetched_at: str = field(default_factory=clock.iso_utc)
    account_closed: bool = False
    closure_reason: str | None = None
    trade_history_present: bool = True
    starting_balance: float | None = None
    is_bot: bool = False
    valuation_complete: bool = False

    def summary(self) -> dict[str, Any]:
        """Compact form for the audit record.

        `balance_cash` is labelled as cash deliberately. It is the movement of
        cash only -- buys debit it, sells credit it -- and open exposure is
        absent from it entirely. Treating it as P&L or net liquidation value
        would misreport every open position.
        """
        return {
            "username": self.username,
            "balance_cash": self.balance_cash,
            "starting_balance": self.starting_balance,
            "trade_count": len(self.trades),
            **({"trade_history_present": False} if not self.trade_history_present else {}),
            "fetched_at": self.fetched_at,
            **({"account_closed": True, "closure_reason": self.closure_reason} if self.account_closed else {}),
        }


class AccountSession:
    """One login, one cookie jar.

    The server has no `account_id` -- an account *is* a login -- so six bot
    accounts mean six registrations and six independent sessions. The
    `Origin` header is deliberately never set: the Worker rejects a *present*
    non-allowlisted origin but permits its absence, which is what lets a
    non-browser client in at all.
    """

    def __init__(
        self,
        account: Any,  # config.Account
        base_url: str = BASE_URL,
        rate_limiter: RateLimiter | None = None,
        timeout_s: float = 20.0,
        bot_registration_key: str | None = None,
        now: Any = None,
    ):
        self.account = account
        self.base_url = base_url.rstrip("/")
        # Injectable so tests and the invariant suite can exercise the bot
        # registration path without depending on the ambient environment.
        self.bot_registration_key = (
            bot_registration_key if bot_registration_key is not None else BOT_REGISTRATION_KEY
        )
        self.rate_limiter = rate_limiter or RateLimiter()
        self.timeout_s = timeout_s
        self.http = requests.Session()
        self.liquidated = False
        self.registration_allowed = True
        self._mutation_lock = threading.Lock()
        # Bounded provisioning state. `_now` is monotonic and injectable so the
        # backoff is testable without sleeping.
        self._now = now or time.monotonic
        self._provision_attempts = 0
        self._next_provision_at = 0.0
        self._credential_mismatch_until = 0.0
        self._provision_history = ""

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        self.rate_limiter.acquire()
        url = f"{self.base_url}{path}"
        try:
            resp = self.http.request(method, url, timeout=self.timeout_s, **kw)
        except requests.Timeout as exc:
            raise AmbiguousExecution(f"{method} {path} timed out: {exc}") from exc
        except requests.RequestException as exc:
            raise TransportError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code == 410:
            self.liquidated = True
            raise AccountLiquidated(
                f"Account {self.account.alias} was liquidated (margin call) -- "
                f"the Worker deleted the user and session"
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            self.rate_limiter.penalize(retry_after)
        return resp

    # -- auth -------------------------------------------------------------

    def _bot_headers(self) -> dict[str, str]:
        """Operator credential identifying this as a bot account.

        Absence is a hard error rather than a fallback: registering without it
        succeeds and produces a perfectly ordinary *human* account that never
        enters the /api/bots roster. That failure is invisible until someone
        notices the Automated tab is short an account, and it cannot be undone
        by re-registering -- the username is taken. Fail before the request.
        """
        if not self.bot_registration_key:
            raise ConfigError(
                f"BOT_REGISTRATION_KEY is not set, so {self.account.alias!r} "
                f"({self.account.username}) cannot be registered as a bot. Without it "
                f"the account would be created as an ordinary user and never appear in "
                f"/api/bots, and the username could not be reclaimed. Set the env var to "
                f"the Worker's BOT_REGISTRATION_KEY secret before starting the runner."
            )
        return {"X-Bot-Registration-Key": self.bot_registration_key}

    def register(self) -> requests.Response:
        return self._request(
            "POST",
            "/api/register",
            headers=self._bot_headers(),
            json={
                "username": self.account.username,
                "password": self.account.password,
                "alias": self.account.alias,
                "strategy_id": self.account.strategy_id,
                "paper_margin_limit": self.account.paper_margin_limit,
                "paper_financing_rate": self.account.paper_financing_rate,
            },
        )

    def login(self) -> requests.Response:
        return self._request(
            "POST",
            "/api/login",
            json={"username": self.account.username, "password": self.account.password},
        )

    def sync_metadata(self, strategy_state: dict[str, Any] | None = None) -> requests.Response:
        """Push this account's authoritative alias/strategy to the Worker.

        accounts.json owns the strategy assignment; the Worker only learns it
        when told. Registration captures it once, so without this an account
        moved to a new strategy would go on being credited to the old one in
        the public roster forever. Called on every startup, not just the first,
        so the roster tracks the config that is actually running.

        ``strategy_state`` is an optional small, already-sanitized public
        summary (signal/SMA/position/reason) for the Automated card. It is
        only sent when a strategy supplies one; existing strategies are
        unchanged.
        """
        payload = {
            "username": self.account.username,
            "alias": self.account.alias,
            "strategy_id": self.account.strategy_id,
            "paper_margin_limit": self.account.paper_margin_limit,
            "paper_financing_rate": self.account.paper_financing_rate,
        }
        if strategy_state is not None:
            payload["strategy_state"] = strategy_state
        return self._request(
            "POST",
            "/api/bot-metadata",
            headers=self._bot_headers(),
            json=payload,
        )

    def ensure_session(self) -> AccountState:
        """Log in, provisioning the account on first use, then read its state.

        Provisioning used to be attempted whenever *any* login status was >=400.
        That reads a Worker outage as "account missing", so a 500/503 on login
        produced a registration attempt that could only answer 409, doubling
        requests per failing account per cycle. The classification now is:

        * transient (408/425/429/5xx) -> `TransportError`, retried next cycle,
          and never a registration attempt;
        * 401 -> the only status that can mean "no such account yet" -> one
          bounded provisioning attempt, subject to the backoff below;
        * 409 after a 401 -> the account exists but our password was rejected:
          a credential mismatch, not a provisioning problem;
        * any other 4xx -> `ConfigError` (malformed catalog or key), no retries.

        A recoverable failure will not block provisioning forever: attempts are
        spaced by `PROVISION_BACKOFF_S` and the count resets on success, so a
        Worker that recovers within the window is picked up on a later cycle.
        """
        self._authenticate()
        return self.me()

    def _authenticate(self) -> None:
        """Login (and provision on first use). Never calls `/api/me`."""
        resp = self.login()
        status = resp.status_code
        if is_transient(status):
            raise TransportError(
                f"Login for {self.account.alias!r} is temporarily failing "
                f"(HTTP {status}); retrying on a later cycle without registering. "
                f"{resp.text[:160]}"
            )
        if status >= 400 and status != 401:
            raise ConfigError(
                f"Login for {self.account.alias!r} was rejected with HTTP {status} "
                f"and is not a provisioning case. {resp.text[:200]}"
            )
        if status == 401:
            self._provision()
            return

        # Registration already carried the current alias/strategy, so only an
        # existing account needs re-syncing.
        sync = self.sync_metadata()
        if sync.status_code == 403 and "invalid bot registration key" in sync.text.lower():
            raise ConfigError(
                f"Worker rejected BOT_REGISTRATION_KEY while syncing existing "
                f"bot {self.account.alias!r}. The Railway value and Worker "
                f"secret have drifted; rotate both and restart. Run "
                f"`python scripts/preflight_accounts.py --probe` "
                f"to distinguish a key mismatch from a missing account."
            )
        if is_transient(sync.status_code):
            raise TransportError(
                f"Bot metadata sync for {self.account.alias!r} is temporarily failing "
                f"(HTTP {sync.status_code}); retrying on a later cycle. {sync.text[:160]}"
            )
        if sync.status_code >= 400:
            raise ConfigError(
                f"Could not sync bot metadata for {self.account.alias!r}: "
                f"{sync.status_code} {sync.text[:200]}"
            )

    def _provision(self) -> None:
        """One bounded registration attempt, with backoff and a mismatch cooldown."""
        if not self.registration_allowed:
            raise ConfigError("Loss-retired account cannot be provisioned again.")
        now = self._now()
        if now < self._credential_mismatch_until:
            raise ConfigError(
                f"Account {self.account.alias!r} ({self.account.username}) exists on "
                f"the Worker but the configured password is rejected. Not re-attempting "
                f"registration for {self._credential_mismatch_until - now:.0f}s; "
                f"fix the credential or the catalog entry."
            )
        if now < self._next_provision_at:
            raise TransportError(
                f"Provisioning for {self.account.alias!r} is in backoff after a "
                f"recoverable failure; next attempt in "
                f"{self._next_provision_at - now:.0f}s. {self._provision_history}"
            )

        try:
            reg = self.register()
        except (TransportError, AmbiguousExecution) as exc:
            # A connection failure or a timeout on the registration POST is the
            # same kind of recoverable event as a 5xx, and it used to bypass the
            # backoff entirely: the exception escaped before any delay or
            # attempt accounting, so a connection-refused Worker could be
            # hammered once per cycle with no spacing. Route it through the
            # identical schedule. A timeout is genuinely ambiguous -- the
            # account may have been created -- and recovery handles that: the
            # next cycle's login succeeds and metadata sync repairs the record.
            self._schedule_retry(f"register transport failure: {type(exc).__name__}")
            raise TransportError(
                f"Provisioning {self.account.alias!r} failed in transport "
                f"({type(exc).__name__}); next attempt in "
                f"{self._next_provision_at - now:.0f}s. {str(exc)[:160]}"
            ) from exc
        status = reg.status_code
        if status in (200, 201):
            self._provision_attempts = 0
            self._next_provision_at = 0.0
            self._provision_history = ""
            return
        if status == 409:
            # The account exists, so the 401 was a rejected password, not a
            # missing account. Registering again can never help.
            self._credential_mismatch_until = now + CREDENTIAL_MISMATCH_COOLDOWN_S
            raise ConfigError(
                f"Credential mismatch for {self.account.alias!r} "
                f"({self.account.username}): login was rejected (401) but "
                f"registration says the username already exists (409). The "
                f"password in the catalog does not match the Worker's record. "
                f"Fix the credential; re-registering cannot repair it, and this "
                f"runner will not attempt it again for "
                f"{CREDENTIAL_MISMATCH_COOLDOWN_S:.0f}s."
            )
        if status == 403 and "invalid bot registration key" in reg.text.lower():
            raise ConfigError(
                f"Worker rejected BOT_REGISTRATION_KEY while registering "
                f"{self.account.alias!r} ({self.account.username}). The "
                f"Railway value does not match the Worker secret. Fix both "
                f"sides and restart; do not reuse the username as a human "
                f"account. Run `python scripts/preflight_accounts.py "
                f"--probe` for a non-mutating key check."
            )
        if is_transient(status):
            # Recoverable: allow another attempt later, bounded and spaced.
            delay = self._schedule_retry(f"last register failure: HTTP {status} {reg.text[:160]}")
            raise TransportError(
                f"Provisioning {self.account.alias!r} failed transiently "
                f"(HTTP {status}); next attempt in {delay:.0f}s. {reg.text[:160]}"
            )
        raise ConfigError(
            f"Registration for {self.account.alias!r} was rejected with HTTP "
            f"{status} and is not recoverable by retrying. {reg.text[:200]}"
        )

    def _schedule_retry(self, history: str) -> float:
        """Space the next provisioning attempt and record why. Returns the delay."""
        index = min(self._provision_attempts, len(PROVISION_BACKOFF_S) - 1)
        delay = PROVISION_BACKOFF_S[index]
        self._provision_attempts += 1
        self._next_provision_at = self._now() + delay
        self._provision_history = history
        return delay

    def me(self, *, _retry_after_reauth: bool = True) -> AccountState:
        """Authoritative account state, re-authenticating once if the session died.

        A Worker restart, a cleared session store, or simply session expiry
        leaves the cookie stale while the account is perfectly healthy. Raising
        a transport error on that 401 every cycle would leave the account dark
        forever, so one bounded re-authentication is attempted before giving up;
        a second 401 after a fresh login means the credentials are wrong, which
        is a configuration problem rather than something retrying can fix.
        """
        resp = self._request("GET", "/api/me")
        if resp.status_code == 401 and _retry_after_reauth:
            self._authenticate()
            return self.me(_retry_after_reauth=False)
        if resp.status_code == 401:
            raise ConfigError(
                f"Worker rejected {self.account.alias!r} even after a fresh login "
                f"(/api/me 401). The configured credential is wrong for this "
                f"account; no further automatic attempts this cycle."
            )
        if is_transient(resp.status_code):
            raise TransportError(
                f"GET /api/me for {self.account.alias!r} is temporarily failing "
                f"(HTTP {resp.status_code}); retrying on a later cycle."
            )
        if resp.status_code >= 400:
            raise TransportError(f"GET /api/me -> {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        return AccountState(
            username=payload.get("username", self.account.username),
            balance_cash=float(payload.get("balance_cash", 0.0)),
            trades=payload.get("trades", []) or [],
            trade_history_present=isinstance(payload.get("trades"), list),
            account_closed=payload.get("account_closed") is True,
            closure_reason=payload.get("closure_reason"),
            starting_balance=payload.get("starting_balance"),
            is_bot=payload.get("is_bot") is True,
            valuation_complete=(payload.get("trade_history_complete") is True
                                and payload.get("username") == self.account.username
                                and type(payload.get("balance_cash")) in (int, float)),
        )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    outcome_class: str
    execution_request_id: str
    http_status: int | None = None
    server_response: Any = None
    latency_ms: float | None = None
    state_after: AccountState | None = None
    note: str | None = None


class ExecutionClient:
    """Submits UUID-keyed intents and reconciles what actually happened.

    The client -- not the strategy -- owns the request id, the retry policy,
    and the decision about whether an intent that timed out should be
    replayed. The Worker's fills are idempotent on `execution_request_id`, so
    a replay with the same id returns the original fill instead of doubling
    the position; that property is only useful if the id survives the crash,
    which is why it is written to disk first.
    """

    def __init__(
        self,
        session: AccountSession,
        state_dir: Path,
        max_attempts: int = 3,
        backoff_base_s: float = 2.0,
    ):
        self.session = session
        self.state_dir = Path(state_dir)
        durable_mkdir(self.state_dir)
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s

    # -- in-flight intent persistence -------------------------------------

    @property
    def _inflight_path(self) -> Path:
        return self.state_dir / f"{self.session.account.alias}.inflight.json"

    def _persist_intent(self, intent: dict[str, Any]) -> None:
        """Write the intent to disk and fsync *before* it is ever sent.

        A timeout must not be able to erase the identity needed to work out
        whether the trade happened. Written to a temp file and atomically
        renamed so a crash mid-write cannot leave a half-parsed record.
        """
        tmp = self._inflight_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(intent, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._inflight_path)
        sync_directory(self.state_dir)

    def _clear_intent(self) -> None:
        self._inflight_path.unlink(missing_ok=True)
        sync_directory(self.state_dir)

    def finalize(self, execution_request_id: str) -> None:
        """Clear the persisted intent -- but only once its outcome is durably
        recorded elsewhere (the decision ledger).

        `submit()` deliberately does *not* clear the intent itself on a
        terminal outcome: if the caller crashed between receiving that
        outcome and writing it to the ledger, the causal record would be
        gone with nothing on disk to reconstruct it. The caller is
        responsible for writing the ledger record first and calling this
        only after that write has returned.
        """
        intent = self.pending_intent()
        if intent and intent.get("execution_request_id") == execution_request_id:
            self._clear_intent()

    def pending_intent(self) -> dict[str, Any] | None:
        if not self._inflight_path.exists():
            return None
        try:
            return json.loads(self._inflight_path.read_text())
        except json.JSONDecodeError:
            return None

    # -- reconciliation ---------------------------------------------------

    @staticmethod
    def _trade_matches(trade: dict[str, Any], execution_request_id: str) -> bool:
        # Defensive across field spellings -- see Book._normalize.
        for key in ("execution_request_id", "executionRequestId", "request_id", "id"):
            if trade.get(key) == execution_request_id:
                return True
        return execution_request_id in json.dumps(trade, default=str)

    def find_trade(self, execution_request_id: str, state: AccountState | None = None) -> dict[str, Any] | None:
        state = state or self.session.me()
        return next(
            (t for t in state.trades if self._trade_matches(t, execution_request_id)), None
        )

    def recover_pending(self) -> ExecutionResult | None:
        """Resolve an intent left in flight by a crash or timeout.

        Follows the MOO-24 recovery procedure exactly: reload the persisted
        envelope, ask the server what it knows, and only replay if the trade
        is genuinely absent. A found trade is authoritative and is never
        resubmitted -- that is the whole point of persisting the id first.

        Does NOT clear the intent itself -- the caller must durably record
        the outcome (the decision ledger) and only then call `finalize()`.
        A crash between this returning and that write leaves the envelope on
        disk, so the next recovery pass can retry the write, not lose it.
        """
        intent = self.pending_intent()
        if not intent:
            return None

        request_id = intent["execution_request_id"]
        state = self.session.me()
        found = self.find_trade(request_id, state)
        if found:
            return ExecutionResult(
                outcome_class=Outcome.RECONCILED_AFTER_AMBIGUITY,
                execution_request_id=request_id,
                server_response=found,
                state_after=state,
                note="Recovered in-flight intent: server already holds this trade; not resubmitted.",
            )

        result = self.submit(
            symbol=intent["symbol"],
            side=intent["side"],
            quantity=intent["quantity"],
            instrument_type=intent.get("instrument_type"),
            limit_price=intent.get("limit_price"),
            execution_request_id=request_id,
            decision_id=intent.get("decision_id"),
            strategy_id=intent.get("strategy_id"),
            strategy_version=intent.get("strategy_version"),
            account_strategy_id=intent.get("account_strategy_id"),
            reason=intent.get("reason"),
            decision=intent.get("decision"),
            market_snapshot_timestamp=intent.get("market_snapshot_timestamp"),
            market_snapshot_url_or_hash=intent.get("market_snapshot_url_or_hash"),
            market_snapshot_lineage=intent_lineage(intent.get("market_snapshot_lineage")),
            account_state_before=intent.get("account_state_before"),
        )
        result.note = "Replayed a persisted in-flight intent that the server had not recorded."
        return result

    # -- submission -------------------------------------------------------

    def submit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        instrument_type: str | None = None,
        limit_price: float | None = None,
        execution_request_id: str | None = None,
        decision_id: str | None = None,
        strategy_decided_at: str | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        account_strategy_id: str | None = None,
        reason: str | None = None,
        decision: dict[str, Any] | None = None,
        market_snapshot_timestamp: str | None = None,
        market_snapshot_url_or_hash: str | None = None,
        market_snapshot_lineage: dict[str, Any] | None = None,
        account_state_before: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Execute one intent, with bounded retry and mandatory reconciliation.

        Serialized per account: the Worker has no compare-and-swap, so two
        concurrent mutations on one login can interleave and lose a write.

        The persisted intent carries the *complete decision envelope*, not
        just the execution id -- everything the decision ledger needs to
        record this outcome. If the process dies after a terminal outcome
        but before the ledger write, the envelope on disk is enough to
        reconstruct that ledger record on the next recovery pass rather than
        just noticing a trade happened with no reason attached to it.
        """
        request_id = execution_request_id or str(uuid.uuid4())
        if instrument_type not in (None, "option", "share"):
            raise ValueError("instrument_type must be 'option' or 'share'")

        with self.session._mutation_lock:  # invariant 2: one mutation per account
            intent = {
                "execution_request_id": request_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "instrument_type": instrument_type,
                "limit_price": limit_price,
                "decision_id": decision_id,
                "strategy_decided_at": strategy_decided_at,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "account_strategy_id": account_strategy_id,
                "reason": reason,
                "decision": decision,
                "market_snapshot_timestamp": market_snapshot_timestamp,
                "market_snapshot_url_or_hash": market_snapshot_url_or_hash,
                "market_snapshot_lineage": intent_lineage(market_snapshot_lineage),
                "account_state_before": account_state_before,
                "created_utc": clock.iso_utc(),
            }
            self._persist_intent(intent)  # invariant 1: persist before sending

            body = {
                "execution_request_id": request_id,
                "sym": symbol,
                "side": side,
                "qty": quantity,
            }
            if limit_price is not None:
                body.update(order_type="limit", limit_price=limit_price)
            if instrument_type is not None:
                body["instrument_type"] = instrument_type

            last: ExecutionResult | None = None
            for attempt in range(1, self.max_attempts + 1):
                started = time.monotonic()
                try:
                    resp = self.session._request("POST", "/api/paper-trade", json=body)
                except AccountLiquidated:
                    # Terminal, but not yet acknowledged -- the caller must
                    # record the margin call in the ledger and call
                    # `finalize()` before this intent is cleared.
                    raise
                except AmbiguousExecution as exc:
                    # Invariant 3: never retry blind after a timeout -- ask the
                    # server whether the trade landed before sending anything.
                    latency = (time.monotonic() - started) * 1000
                    resolved = self._reconcile_after_ambiguity(request_id, latency, str(exc))
                    if resolved:
                        return resolved
                    last = ExecutionResult(
                        outcome_class=Outcome.AMBIGUOUS,
                        execution_request_id=request_id,
                        latency_ms=latency,
                        note=str(exc),
                    )
                    self._sleep_backoff(attempt)
                    continue
                except TransportError as exc:
                    latency = (time.monotonic() - started) * 1000
                    last = ExecutionResult(
                        outcome_class=Outcome.TRANSPORT_ERROR,
                        execution_request_id=request_id,
                        latency_ms=latency,
                        note=str(exc),
                    )
                    self._sleep_backoff(attempt)
                    continue

                latency = (time.monotonic() - started) * 1000
                payload = self._body(resp)

                if resp.status_code < 300:
                    # Not cleared here -- the caller records this in the
                    # ledger first and clears via `finalize()` afterwards.
                    return ExecutionResult(
                        outcome_class=Outcome.FILLED,
                        execution_request_id=request_id,
                        http_status=resp.status_code,
                        server_response=payload,
                        latency_ms=latency,
                        # Invariant 4: re-read /api/me rather than trusting the
                        # balance echoed back by the execution response.
                        state_after=self._safe_me(),
                    )

                if resp.status_code == 429:
                    last = ExecutionResult(
                        outcome_class=Outcome.RATE_LIMITED,
                        execution_request_id=request_id,
                        http_status=429,
                        server_response=payload,
                        latency_ms=latency,
                    )
                    self._sleep_backoff(attempt)
                    continue

                if resp.status_code >= 500:
                    resolved = self._reconcile_after_ambiguity(request_id, latency, f"HTTP {resp.status_code}")
                    if resolved:
                        return resolved
                    last = ExecutionResult(
                        outcome_class=Outcome.AMBIGUOUS,
                        execution_request_id=request_id,
                        http_status=resp.status_code,
                        server_response=payload,
                        latency_ms=latency,
                    )
                    self._sleep_backoff(attempt)
                    continue

                # 4xx: the server made a decision and it was "no". A stale
                # quote or a duplicate id is a rejection, not an ambiguity.
                # Not cleared here -- see `finalize()`.
                return ExecutionResult(
                    outcome_class=Outcome.REJECTED,
                    execution_request_id=request_id,
                    http_status=resp.status_code,
                    server_response=payload,
                    latency_ms=latency,
                    state_after=self._safe_me(),
                )

            # Attempts exhausted. The intent file stays on disk on purpose:
            # the next cycle or restart will reconcile it.
            return last or ExecutionResult(
                outcome_class=Outcome.RUNNER_ERROR,
                execution_request_id=request_id,
                note="Exhausted retries with no classified result",
            )

    def _reconcile_after_ambiguity(
        self, request_id: str, latency_ms: float, cause: str
    ) -> ExecutionResult | None:
        try:
            state = self.session.me()
        except CrassusError:
            return None
        found = self.find_trade(request_id, state)
        if not found:
            return None
        # Not cleared here -- see `finalize()`.
        return ExecutionResult(
            outcome_class=Outcome.RECONCILED_AFTER_AMBIGUITY,
            execution_request_id=request_id,
            server_response=found,
            latency_ms=latency_ms,
            state_after=state,
            note=f"{cause}; trade confirmed present in /api/me, not resubmitted.",
        )

    def _safe_me(self) -> AccountState | None:
        try:
            return self.session.me()
        except CrassusError:
            return None

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self.backoff_base_s * (2 ** (attempt - 1)))

    @staticmethod
    def _body(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text[:500]
