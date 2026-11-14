"""Configurable profit-taking and protective exits for held option positions.

Every option strategy in this repo closes *reactively*: it waits for its own
signal to flip (see `reddit_sentiment`, `trump_whisperer`, `momentum_qqq`,
`oi_skew`, `max_pain`, `put_call_ratio`). Nothing closes a position because it
has made enough money, because it has lost too much, or because a contract
that is decaying toward expiration should be salvaged. The mandatory end-of-day
flatten (`flatten.py`) only bounds *time*, and Guideline Phelps (`phelps.py`)
deliberately withholds a signal-driven exit for one window. Between those two
controls sits the entire range of ordinary position management, and issue #86
asks for it explicitly.

This module adds that layer as a pre-strategy decision -- the same shape
`maybe_flatten` already uses -- so it can see the held book, the account's
documented average cost, and a fresh liquidation-side quote without any
strategy having to change:

* **Long** positions are valued at the quote's **bid** (what a sale would
  actually receive); **short** positions at the **ask** (what buying back would
  actually cost). Using the mid or the entry-side price would systematically
  overstate an exit's value. The quote must pass `Quote.is_executable`, the
  same 15-second, both-sides, server-clock freshness rule the Worker enforces
  on submission -- an exit is only proposed against a price that could actually
  be hit this cycle.
* **Cost basis** is `Book`'s average cost per contract, reconstructed from the
  server's authoritative trade history (see `client.Book`). Percentage P&L is
  therefore premium P&L; the Worker settles paper options in premium terms, so
  no contract multiplier enters the threshold arithmetic.
* **Triggers** are inclusive at the boundary: `pnl_pct >= profit_target_pct` and
  `pnl_pct <= stop_loss_pct`. A position sitting exactly on its threshold has
  reached it; the alternative makes the boundary depend on float noise. The
  comparison is therefore made in exact decimal arithmetic on the quoted
  values (`_threshold_reached`): in binary floats a bid of exactly 65% of a
  cent-denominated basis computes to e.g. -0.3499999999999999 for basis 1.40 /
  bid 0.91 and would miss a -35% stop (16 of the 100 exact-cent 65% pairs up
  to $20.00 do). The threshold price itself is recorded as
  `exit_threshold_price` so overshoot past it is auditable.

Precedence, highest first, is fixed here and documented for review:

1. **Session** -- the mandatory end-of-day flatten runs before this layer and
   always wins. Exits never preempt it, never duplicate it, and never open a
   position in the flatten window.
2. **Protective stop** -- the one exit whose purpose is bounding loss. By
   default it fires even inside a Guideline Phelps window (`stop_bypasses_phelps`
   defaults to true), because Phelps deliberately tolerates discomfort and has
   no loss bound of its own. An account that wants strict doctrine can set
   `stop_bypasses_phelps: false`, which defers the stop to the end of the
   window -- a choice with a real tail cost, so it is opt-in and visible.
3. **Profit target** -- by default deferred inside a Phelps window
   (`profit_bypasses_phelps` defaults to false): taking a profit is not the
   capitulation Phelps protects against, but releasing the position early also
   ends the convexity purchase the method paid for. Deferral is the
   doctrine-consistent default and can be switched off per account.
4. **Signal reversal** -- the base strategy's own sell, with Phelps's existing
   deferral unchanged. This layer never suppresses it.
5. **Re-entry** -- after an exit is submitted, the same contract may not be
   re-opened for `reentry_cooldown_minutes` (default 15). Without this, a
   strategy whose signal is still bullish buys straight back into the contract
   it just stopped out of, usually at a worse price, and the stop never
   actually bounds the day.

Persistence and duplicate protection are handled by machinery that already
exists and is already tested: the runner writes the decision envelope to a
durable intent before submitting, reconciles any pending intent on restart, and
records one ledger row per decision. This module adds only the re-entry guard's
own state (`ExitStateStore`, a small JSON file under the state directory); a
position that is already flat produces no exit, so settlement and a restart
after a fill cannot re-sell a position that no longer exists.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from fractions import Fraction
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .durability import atomic_json
from .strategy import Decision, StrategyContext

log = logging.getLogger("crassus.exits")

STRATEGY_ID = "exit_policy"
STRATEGY_VERSION = "1.2.0"

#: Strategies whose exits this layer owns by default: the signal-reversal
#: option books. `smoke_*` is excluded deliberately -- it is a pipeline
#: instrument whose round trip is the point, and an exit layer that closed it
#: on a 50% move would corrupt the fixtures that prove the lifecycle.
#: `canopus_down_day_14` and `looking_glass_straddle` are excluded because they
#: already carry their own terminal/profit exits, and duplicating them would
#: make one position subject to two independent policy owners.
DEFAULT_COVERED = frozenset({
    "reddit_sentiment_qqq",
    "reddit_sentiment_qqq_phelps",
    "reddit_sentiment_qqq_fixed_window",
    "trump_whisperer_qqq",
    "trump_whisperer_qqq_phelps",
    "trump_whisperer_qqq_fixed_window",
    "momentum_qqq",
    "momentum_qqq_phelps",
    "momentum_qqq_fixed_window",
    "momentum_puts_only_qqq",
    "momentum_puts_only_qqq_phelps",
    "max_pain_qqq",
    "oi_skew_qqq",
    "put_call_ratio_qqq",
    "phelps_pure_qqq",
})

PHELPS_SUFFIX = "_phelps"

#: Activation switch. The layer is implemented, tested and *inert* until an
#: operator sets this after the thresholds and precedence have been reviewed;
#: that is the difference between "the code exists" and "the bots trade a new
#: exit policy". Default off means a deploy of this branch changes no trading
#: behaviour, and rollback is the same single variable.
ACTIVATION_ENV = "CRASSUS_EXITS_ENABLED"

#: Approved defaults (Owen, 2026-11-09): a bounded-loss policy with profit-taking
#: disabled. These are the values the mechanism runs with when an account sets no
#: `exits` block; they are approved as a starting policy, not presented as a
#: calibrated optimum.
#:
#: * `profit_target_pct = None` -- profit-taking is deliberately *off*. A +50%
#:   target was the earlier placeholder and the only saved 0DTE sample in the
#:   tree never reached it from any intraday entry (median favourable excursion
#:   −5%, p90 +16%, max +39%), so it would have been decorative. `None` is not
#:   the same as a large number: `1.00` is still an enabled +100% target that
#:   fires on a doubling, while `None` can never fire.
#: * `stop_loss_pct = -0.50` -- half the debit. The same sample puts
#:   hold-to-close at a median −76% of premium, while a −50% stop moves the
#:   median to −55% and the 10th percentile from −95% to −75%. Tighter (−25%) is
#:   touched in 99% of entries and converts noise into realised losses; wider
#:   (−75%) permits a three-quarter loss of the debit.
#: * `reentry_cooldown_minutes = 15` -- one runner cycle is five minutes, so 15
#:   covers three cycles: long enough that the same stale signal cannot buy the
#:   same contract back immediately, short enough that a genuinely new setup
#:   later in the session is not blocked.
#: * `stop_bypasses_phelps = True`, `profit_bypasses_phelps = False` -- a stop
#:   exists to bound loss and Phelps has no loss bound; taking a profit early
#:   ends the convexity purchase the window paid for. The profit switch is inert
#:   while the target is `None`, and is kept for the account that re-enables one.
#: * `quote_max_age_s` is not configurable: it is the Worker's own 15-second
#:   execution freshness contract, and a looser local value would only produce
#:   409s.
DEFAULT_PROFIT_TARGET_PCT: float | None = None
DEFAULT_STOP_LOSS_PCT = -0.50
DEFAULT_REENTRY_COOLDOWN_MINUTES = 15.0
DEFAULT_STOP_BYPASSES_PHELPS = True
DEFAULT_PROFIT_BYPASSES_PHELPS = False

STATE_SCHEMA = "exit_state.v1"
STATE_FILE_NAME = "exits.json"
#: An exit older than this cannot plausibly still be cooling down; prune rather
#: than let the state file grow without bound on a long-lived runtime.
STATE_RETENTION_HOURS = 24.0


class ExitConfigError(ValueError):
    """A malformed `exits` block. Surfaced per account, never process-wide."""


def _finite(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExitConfigError(f"exits.{field_name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ExitConfigError(f"exits.{field_name} must be finite, got {value!r}")
    return number


def _strict_bool(block: dict[str, Any], field_name: str, default: bool) -> bool:
    """A precedence switch requires a real JSON boolean.

    `bool("false")` is True, so accepting strings here would let a quoted value
    silently invert the policy an operator thought they had set. The catalog is
    JSON, so there is no reason to accept anything but `true`/`false`.
    """
    if field_name not in block:
        return default
    value = block[field_name]
    if not isinstance(value, bool):
        raise ExitConfigError(
            f"exits.{field_name} must be true or false (JSON boolean), got {value!r}"
        )
    return value


@dataclass(frozen=True)
class ExitPolicy:
    #: `None` disables profit-taking entirely. That is deliberately distinct
    #: from a very large number: `+1.00` is still an enabled target that fires
    #: on a doubling, while `None` can never fire.
    profit_target_pct: float | None = DEFAULT_PROFIT_TARGET_PCT
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    reentry_cooldown_minutes: float = DEFAULT_REENTRY_COOLDOWN_MINUTES
    stop_bypasses_phelps: bool = DEFAULT_STOP_BYPASSES_PHELPS
    profit_bypasses_phelps: bool = DEFAULT_PROFIT_BYPASSES_PHELPS
    enabled: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def activation_from_environment(environ: dict[str, str] | None = None) -> bool:
    """Whether the exit layer is switched on for this process.

    Anything other than an explicit affirmative keeps it inert; a typo must not
    be able to start trading a policy that has not been reviewed.
    """
    raw = (os.environ if environ is None else environ).get(ACTIVATION_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def resolve_policy(
    strategy_id: str,
    params: dict[str, Any] | None,
    *,
    covered: frozenset[str] = DEFAULT_COVERED,
    activated: bool = False,
) -> ExitPolicy:
    """The policy for one account, or a disabled policy.

    `params["exits"]` may set any field; `params["exits"]["enabled"] = false`
    turns the layer off for that account. An account whose strategy is not
    covered is off unless it opts in explicitly, so adding a strategy to the
    repo does not silently acquire an exit policy. In every case the layer also
    requires `activated` -- the deployment-level switch -- so shipping this
    code changes nothing until `CRASSUS_EXITS_ENABLED` is set.
    """
    params = params or {}
    block = params.get("exits")
    if block is None:
        return ExitPolicy(enabled=bool(activated and strategy_id in covered))
    if not isinstance(block, dict):
        raise ExitConfigError("exits must be an object")
    unknown = set(block) - set(ExitPolicy.__dataclass_fields__)
    if unknown:
        raise ExitConfigError(
            f"unknown exits field(s) {sorted(unknown)}; allowed: "
            f"{sorted(ExitPolicy.__dataclass_fields__)}"
        )
    configured = block.get("enabled", strategy_id in covered)
    if not isinstance(configured, bool):
        raise ExitConfigError(f"exits.enabled must be true or false, got {configured!r}")
    enabled = bool(configured and activated)
    target_raw = block.get("profit_target_pct", DEFAULT_PROFIT_TARGET_PCT)
    target = None if target_raw is None else _finite(target_raw, "profit_target_pct")
    policy = ExitPolicy(
        profit_target_pct=target,
        stop_loss_pct=_finite(block.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT),
                              "stop_loss_pct"),
        reentry_cooldown_minutes=_finite(
            block.get("reentry_cooldown_minutes", DEFAULT_REENTRY_COOLDOWN_MINUTES),
            "reentry_cooldown_minutes",
        ),
        stop_bypasses_phelps=_strict_bool(
            block, "stop_bypasses_phelps", DEFAULT_STOP_BYPASSES_PHELPS
        ),
        profit_bypasses_phelps=_strict_bool(
            block, "profit_bypasses_phelps", DEFAULT_PROFIT_BYPASSES_PHELPS
        ),
        enabled=enabled,
    )
    if policy.stop_loss_pct >= 0:
        raise ExitConfigError("exits.stop_loss_pct must be < 0")
    if policy.profit_target_pct is not None and policy.profit_target_pct <= 0:
        raise ExitConfigError("exits.profit_target_pct must be > 0, or null to disable")
    if policy.reentry_cooldown_minutes < 0:
        raise ExitConfigError("exits.reentry_cooldown_minutes must be >= 0")
    return policy


def is_phelps(strategy_id: str) -> bool:
    return strategy_id.endswith(PHELPS_SUFFIX) or strategy_id == "phelps_pure_qqq"


def _elapsed_minutes(ctx: StrategyContext, symbol: str) -> float | None:
    """Minutes since the position's own fill, when the trade history says so.

    Imported lazily and defensively: `phelps` is a strategy module, and this
    layer must keep working (by not applying the window) if that helper ever
    changes shape.
    """
    try:
        from .phelps import position_entry_time
    except ImportError:  # pragma: no cover - defensive
        return None
    entry = position_entry_time(ctx, symbol)
    if entry is None:
        return None
    now = ctx.now_et
    if entry.tzinfo is None or now.tzinfo is None:
        return None
    return (now - entry).total_seconds() / 60.0


def _phelps_window_minutes(params: dict[str, Any]) -> float:
    from .phelps import resolve_phelps_minutes

    return resolve_phelps_minutes(params)


def liquidation_price(quote: Any, quantity: int) -> float | None:
    """The price this position would actually transact at, or None.

    Long exits hit the bid; short exits pay the ask. A quote missing the needed
    side is not usable for this position even if the other side is present.
    """
    if quote is None:
        return None
    price = quote.bid if quantity > 0 else quote.ask
    return float(price) if price is not None else None


def pnl_pct(cost_basis: float, price: float, quantity: int) -> float:
    """Premium P&L as a fraction of the documented average cost."""
    if cost_basis <= 0:
        raise ValueError("cost basis must be positive")
    if quantity > 0:
        return (price - cost_basis) / cost_basis
    return (cost_basis - price) / cost_basis


@dataclass
class Candidate:
    symbol: str
    quantity: int
    cost_basis: float
    price: float
    pnl_pct: float
    trigger: str  # "protective_stop" | "profit_target"
    bid: float | None
    ask: float | None
    quote_age_s: float | None
    #: How the Phelps window actually bore on this exit: "not_applicable",
    #: "window_elapsed" or "bypassed_by_config". Recorded so the audit trail
    #: states the precedence that was really applied rather than a fixed
    #: sentence about the default.
    window_state: str = "not_applicable"

    @property
    def priority(self) -> tuple[int, float]:
        # Lower sorts first: stops always outrank profit targets; within a
        # trigger class the larger absolute move wins, so the most urgent
        # position exits first.
        return (0 if self.trigger == "protective_stop" else 1, -abs(self.pnl_pct))

    def metadata(self, policy: ExitPolicy, username: str | None = None) -> dict[str, Any]:
        return {
            "exit_trigger": self.trigger,
            "exit_symbol": self.symbol,
            "exit_held_quantity": self.quantity,
            "exit_cost_basis": round(self.cost_basis, 6),
            "exit_liquidation_price": self.price,
            "exit_bid": self.bid,
            "exit_ask": self.ask,
            "exit_quote_age_s": None if self.quote_age_s is None else round(self.quote_age_s, 3),
            "exit_pnl_pct": round(self.pnl_pct, 6),
            "exit_threshold_pct": (
                policy.stop_loss_pct if self.trigger == "protective_stop"
                else policy.profit_target_pct
            ),
            # The price the threshold corresponds to for this position's own
            # basis; liquidation_price below it is overshoot (gap, latency).
            "exit_threshold_price": round(threshold_price(
                self.cost_basis,
                policy.stop_loss_pct if self.trigger == "protective_stop"
                else policy.profit_target_pct,
                self.quantity), 6),
            "exit_phelps_window": self.window_state,
            # The account's server-side identity, so a durable ledger row can
            # reconstruct this exit's cooldown after a corrupt or lost state
            # file (see ExitStateStore._recover_from_durable_sources).
            "exit_account_username": username,
            "exit_policy": policy.as_metadata(),
        }


def _exact(value: float) -> Fraction:
    """The decimal a float was written as (`repr` round-trips), exactly."""
    return Fraction(repr(float(value)))


def threshold_price(cost_basis: float, fraction: float, quantity: int) -> float:
    """The liquidation price at which `pnl_pct` equals `fraction`.

    Long: basis * (1 + fraction); short: basis * (1 - fraction). For a -0.35
    stop on a long that is 65% of the position's own entry basis.
    """
    sign = 1 if quantity > 0 else -1
    return float(_exact(cost_basis) * (1 + sign * _exact(fraction)))


def _threshold_reached(cost_basis: float, price: float, quantity: int, fraction: float) -> bool:
    """`pnl_pct(...) <= fraction` (stops, fraction < 0) or `>= fraction`
    (targets, fraction > 0), decided exactly rather than in binary floats."""
    basis, px, f = _exact(cost_basis), _exact(price), _exact(fraction)
    change = (px - basis) / basis if quantity > 0 else (basis - px) / basis
    return change <= f if f < 0 else change >= f


def _classify(position: Any, quote: Any, policy: ExitPolicy) -> Candidate | None:
    price = liquidation_price(quote, position.quantity)
    if price is None or position.average_cost <= 0:
        return None
    change = pnl_pct(position.average_cost, price, position.quantity)
    if _threshold_reached(position.average_cost, price, position.quantity, policy.stop_loss_pct):
        trigger = "protective_stop"
    elif (policy.profit_target_pct is not None
          and _threshold_reached(position.average_cost, price, position.quantity,
                                 policy.profit_target_pct)):
        trigger = "profit_target"
    else:
        return None
    return Candidate(
        symbol=position.symbol,
        quantity=position.quantity,
        cost_basis=position.average_cost,
        price=price,
        pnl_pct=change,
        trigger=trigger,
        bid=getattr(quote, "bid", None),
        ask=getattr(quote, "ask", None),
        quote_age_s=getattr(quote, "age_seconds", None),
    )


def evaluate(
    ctx: StrategyContext,
    policy: ExitPolicy,
    *,
    strategy_id: str | None = None,
) -> Decision | None:
    """A close decision for the most urgent triggered position, else None.

    Needs no market snapshot: a risk exit is decided from the held book, the
    documented cost basis and a fresh liquidation-side quote. The scanner
    snapshot feeds entry signals, not the decision to stop losing money, so a
    snapshot outage must not suspend protective exits (the runner orders this
    accordingly).

    Returns a blocking `no_trade` when a trigger has fired but no triggered
    position has an executable quote, or while the Phelps window withholds the
    only triggered exit: the position is waiting to be closed, and letting the
    strategy open something else in the meantime is exactly the "unintended
    re-entry" this layer exists to prevent.
    """
    if not policy.enabled or ctx.book is None:
        return None
    positions = [
        position for position in ctx.book.positions.values()
        if position.quantity != 0
    ]
    if not positions:
        return None

    wanted = bool(strategy_id) and is_phelps(strategy_id or "")
    username = str((ctx.account_state or {}).get("username", "")) or None
    quotes: dict[str, Any] = {}
    try:
        quotes = ctx.quotes([p.symbol for p in positions]) or {}
    except Exception as exc:  # quote failures are a finding, not a crash
        return Decision.no_trade(
            reason=f"Exit check could not read quotes: {type(exc).__name__}; holding.",
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata={"exit_quotes_error": type(exc).__name__},
        )

    executable: list[Candidate] = []
    pending: list[dict[str, str]] = []
    for position in positions:
        quote = quotes.get(position.symbol)
        # No quote, or no price on the side this exit would actually transact
        # at: the position cannot be valued at all this cycle, which is a
        # different condition from "valued but too stale to submit".
        if liquidation_price(quote, position.quantity) is None:
            pending.append({"symbol": position.symbol, "reason": "missing_liquidation_quote"})
            continue
        candidate = _classify(position, quote, policy)
        if candidate is None:
            continue
        if not getattr(quote, "is_executable", False):
            pending.append({"symbol": position.symbol, "reason": "stale_quote"})
            continue
        if wanted:
            state, deferred = _window_state(ctx, candidate, policy)
            candidate.window_state = state
            if deferred:
                pending.append({"symbol": position.symbol, "reason": "phelps_window"})
                continue
        executable.append(candidate)

    if not executable:
        if pending:
            reasons = ", ".join(f"{item['symbol']} ({item['reason']})" for item in pending)
            return Decision.no_trade(
                reason=(
                    f"Exit pending for {reasons}; waiting rather than holding or "
                    f"adding to the book."
                ),
                strategy_id=STRATEGY_ID,
                strategy_version=STRATEGY_VERSION,
                metadata={
                    "exit_pending": pending,
                    "exit_pending_symbols": [item["symbol"] for item in pending],
                    "exit_quote_max_age_s": 15.0,
                },
            )
        return None

    best = min(executable, key=lambda c: c.priority)
    action = "sell" if best.quantity > 0 else "buy"
    return Decision(
        action=action,
        symbol=best.symbol,
        quantity=abs(best.quantity),
        reason=(
            f"Exit policy: {best.trigger} at {best.pnl_pct:+.1%} of {best.cost_basis:.2f} "
            f"cost basis, liquidating at {best.price:.2f}"
            f"{_window_phrase(best)}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata=best.metadata(policy, username),
    )


def _window_phrase(candidate: Candidate) -> str:
    """The audit sentence for the precedence that was actually applied."""
    if candidate.window_state == "bypassed_by_config":
        which = "stop_bypasses_phelps" if candidate.trigger == "protective_stop" \
            else "profit_bypasses_phelps"
        return f" (Phelps window bypassed by configuration: {which}=true)"
    if candidate.window_state == "window_elapsed":
        return " (Phelps window already elapsed)"
    if candidate.window_state == "no_entry_time":
        return " (Phelps window not applied: entry time unavailable)"
    return ""


def _window_state(ctx: StrategyContext, candidate: Candidate, policy: ExitPolicy) -> tuple[str, bool]:
    """How the Phelps window bears on this candidate, and whether to defer it.

    Returns `(state, deferred)` where state is one of `window_elapsed`,
    `bypassed_by_config`, `no_entry_time` or the deferral itself. The state is
    reported from the *configured* policy, so an audit row can never claim a
    stop bypassed a window it was configured to respect.

    If the fill time cannot be established the window is not applied: an
    unparseable trade record must not hold a losing position open past its
    stop. That case is still narrated honestly as `no_entry_time` rather than
    passed off as an elapsed window.
    """
    bypass = (policy.stop_bypasses_phelps if candidate.trigger == "protective_stop"
              else policy.profit_bypasses_phelps)
    elapsed = _elapsed_minutes(ctx, candidate.symbol)
    if elapsed is None:
        return "no_entry_time", False
    if elapsed >= _phelps_window_minutes(ctx.params):
        return "window_elapsed", False
    if bypass:
        return "bypassed_by_config", False
    return "deferred", True


# --------------------------------------------------------------------------
# Re-entry guard
# --------------------------------------------------------------------------


class ExitStateStore:
    """Durable record of recent exits, used to block immediate re-entry.

    Two failure modes get deliberately different treatment.

    A **write** must never leave a half-serialized file, because a truncated
    record is indistinguishable from "no exit happened" on the next boot --
    exactly the state that permits re-buying a contract that was just stopped
    out. Saves therefore go through `durability.atomic_json`: temp file, fsync,
    atomic replace, directory sync, the same mechanism the execution intent and
    the decision ledger already use. A crash mid-save leaves the previous good
    file intact.

    A **missing or corrupt file** means the cooldown record cannot be trusted,
    so the store rebuilds from the durable sources that already exist: the
    ledger's exit rows (written after every resolved exit) and any in-flight
    intent (written before every submission). That reconstruction is sound for
    the failure that matters -- an exit that actually filled necessarily has
    either a ledger row or an intent on disk, because the intent is fsynced
    before the order is sent. If those sources cannot be read at all, the
    store declares itself untrusted, and an untrusted store blocks *new
    entries* while leaving protective exits untouched (see `reentry_blocked`).
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        now: Callable[[], datetime] | None = None,
        ledger_dir: Path | None = None,
        intent_dir: Path | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STATE_FILE_NAME
        self.ledger_dir = Path(ledger_dir) if ledger_dir is not None else None
        self.intent_dir = Path(intent_dir) if intent_dir is not None else self.state_dir
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        #: None means trustworthy; any string is the reason it is not.
        self.degraded_reason: str | None = None
        #: Set when a corrupt file was rebuilt from ledger/intents.
        self.recovered_from: str | None = None
        self._load()

    @property
    def trusted(self) -> bool:
        return self.degraded_reason is None

    # -- loading ----------------------------------------------------------

    def _load(self) -> None:
        raw: Any = None
        missing = False
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            missing = True
        except OSError as exc:
            self.degraded_reason = f"state_unreadable:{type(exc).__name__}"
            log.warning("exit state unreadable (%s); rebuilding from durable sources",
                        type(exc).__name__)
        except ValueError:
            self.degraded_reason = "state_corrupt"
            log.warning("exit state is corrupt JSON; rebuilding from durable sources")

        if not missing and self.degraded_reason is None:
            accounts = raw.get("accounts") if isinstance(raw, dict) else None
            if (isinstance(raw, dict) and raw.get("schema") == STATE_SCHEMA
                    and isinstance(accounts, dict)):
                self._data = self._normalize(accounts)
                self._prune()
                return
            self.degraded_reason = "state_unrecognized"
            log.warning("exit state has an unrecognised shape; rebuilding from durable sources")

        if missing:
            self._data = {}
            return

        recovered, sources_readable = self._recover_from_durable_sources()
        self._data = recovered
        if not sources_readable:
            # Nothing durable to rebuild from; block new entries until an
            # operator resolves it rather than risk a duplicate re-entry.
            self.degraded_reason = self.degraded_reason or "state_unrecoverable"
            log.error(
                "exit state is unusable and no ledger/intent evidence was readable; "
                "new entries are blocked until this is resolved"
            )
            return
        self.recovered_from = "durable_sources"
        if self._save():
            self.degraded_reason = None

    @staticmethod
    def _normalize(accounts: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            str(account): {
                str(symbol): dict(record)
                for symbol, record in (entries or {}).items()
                if isinstance(record, dict)
            }
            for account, entries in accounts.items()
            if isinstance(entries, dict)
        }

    def _recover_from_durable_sources(self) -> tuple[dict[str, dict[str, dict[str, Any]]], bool]:
        """Rebuild recent exits from the ledger and in-flight intents.

        Returns `(records, sources_readable)`. An empty record set with readable
        sources is meaningful: every submitted exit is fsynced as an intent
        before it is sent, so no fill can be hiding unrecorded.
        """
        found: dict[str, dict[str, dict[str, Any]]] = {}
        readable = False

        if self.ledger_dir is not None:
            try:
                paths = sorted(Path(self.ledger_dir).glob("decisions-*.jsonl"))
                readable = True
            except OSError:
                paths = []
            for path in paths:
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    self._absorb(found, record.get("decision"), record.get("timestamp_utc"))

        try:
            intents = sorted(Path(self.intent_dir).glob("*.inflight.json"))
            readable = True
        except OSError:
            intents = []
        for path in intents:
            try:
                intent = json.loads(path.read_text(encoding="utf-8"))
                stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except (OSError, ValueError):
                continue
            self._absorb(found, intent.get("decision"), stamp.isoformat(timespec="seconds"))

        return found, readable

    @staticmethod
    def _absorb(found: dict[str, dict[str, dict[str, Any]]], decision: Any, stamp: Any) -> None:
        if not isinstance(decision, dict):
            return
        metadata = decision.get("metadata") or {}
        if not isinstance(metadata, dict) or not metadata.get("exit_trigger"):
            return
        symbol = decision.get("symbol") or metadata.get("exit_symbol")
        username = metadata.get("exit_account_username")
        if not symbol or not username:
            return
        try:
            exited_at = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return
        if exited_at.tzinfo is None:
            exited_at = exited_at.replace(tzinfo=timezone.utc)
        previous = found.setdefault(str(username), {}).get(str(symbol))
        if previous is not None:
            try:
                if datetime.fromisoformat(str(previous["exited_at"])) >= exited_at:
                    return
            except (KeyError, ValueError):
                pass
        found[str(username)][str(symbol)] = {
            "exited_at": exited_at.isoformat(timespec="seconds"),
            "trigger": str(metadata.get("exit_trigger")),
            "quantity": decision.get("quantity"),
            "decision_id": metadata.get("decision_id"),
            "recovered": True,
        }

    # -- saving -----------------------------------------------------------

    def _save(self) -> bool:
        payload = {"schema": STATE_SCHEMA, "accounts": self._data}
        try:
            atomic_json(self.path, payload)
        except OSError as exc:
            if self.degraded_reason is None:
                log.warning(
                    "exit state could not be persisted (%s); new entries are blocked "
                    "until a save succeeds", type(exc).__name__,
                )
            self.degraded_reason = f"state_write_failed:{type(exc).__name__}"
            return False
        if self.degraded_reason and self.degraded_reason.startswith("state_write_failed"):
            self.degraded_reason = None
        return True

    # -- mutation ---------------------------------------------------------

    def record(self, username: str, symbol: str, *, trigger: str, quantity: int,
               decision_id: str | None = None) -> None:
        account = self._data.setdefault(str(username), {})
        account[symbol] = {
            "exited_at": self._now().isoformat(timespec="seconds"),
            "trigger": trigger,
            "quantity": quantity,
            "decision_id": decision_id,
        }
        self._prune()
        self._save()

    # -- queries ----------------------------------------------------------

    def exited_at(self, username: str, symbol: str) -> datetime | None:
        record = self._data.get(str(username), {}).get(symbol)
        if not record:
            return None
        try:
            return datetime.fromisoformat(str(record.get("exited_at")))
        except ValueError:
            return None

    def cooldown_remaining_s(self, username: str, symbol: str, minutes: float) -> float:
        if minutes <= 0:
            return 0.0
        exited = self.exited_at(username, symbol)
        if exited is None:
            return 0.0
        elapsed = (self._now() - exited).total_seconds()
        return max(0.0, minutes * 60.0 - elapsed)

    def _prune(self) -> None:
        cutoff = self._now() - timedelta(hours=STATE_RETENTION_HOURS)
        for account in list(self._data):
            for symbol in list(self._data[account]):
                try:
                    exited = datetime.fromisoformat(
                        str(self._data[account][symbol].get("exited_at"))
                    )
                except ValueError:
                    del self._data[account][symbol]
                    continue
                if exited < cutoff:
                    del self._data[account][symbol]
            if not self._data[account]:
                del self._data[account]


def reentry_blocked(
    decision: Decision,
    *,
    username: str,
    store: ExitStateStore,
    policy: ExitPolicy,
    book: Any,
) -> Decision | None:
    """Convert an entry into a no_trade while an exit cooldown is live.

    Only entries are blocked -- a decision that reduces an existing position is
    never suppressed by this guard, so protective liquidation keeps working
    even when the cooldown record itself is unusable. An untrusted store blocks
    every new entry rather than silently permitting the one it cannot rule out.
    Returns None when the decision may proceed.
    """
    if not policy.enabled or not decision.is_trade or not decision.symbol:
        return None
    position = book.position(decision.symbol) if book is not None else None
    held = position.quantity if position is not None else 0
    opens_or_adds = (
        (held == 0) or (held > 0 and decision.action == "buy") or
        (held < 0 and decision.action == "sell")
    )
    if not opens_or_adds:
        return None

    if not store.trusted:
        return Decision.no_trade(
            reason=(
                f"Exit state is untrusted ({store.degraded_reason}); refusing a new "
                f"position in {decision.symbol} rather than risk re-entering a "
                f"contract that may have just been exited. Protective exits are "
                f"unaffected."
            ),
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            metadata={
                "exit_state_degraded": store.degraded_reason,
                "blocked_action": decision.action,
                "blocked_symbol": decision.symbol,
                "blocked_strategy_id": decision.strategy_id,
            },
        )

    if policy.reentry_cooldown_minutes <= 0:
        return None
    remaining = store.cooldown_remaining_s(username, decision.symbol,
                                           policy.reentry_cooldown_minutes)
    if remaining <= 0:
        return None
    return Decision.no_trade(
        reason=(
            f"Exit cooldown: {decision.symbol} was exited less than "
            f"{policy.reentry_cooldown_minutes:.0f}m ago "
            f"({remaining:.0f}s remaining); not re-entering on the same signal."
        ),
        strategy_id=decision.strategy_id,
        strategy_version=decision.strategy_version,
        metadata={
            "exit_cooldown_remaining_s": round(remaining, 1),
            "blocked_action": decision.action,
            "blocked_symbol": decision.symbol,
            "blocked_strategy_id": decision.strategy_id,
        },
    )
