"""OA-236 profit protection and re-entry for OI Skew positions.

A small versioned family, `oa236-protect-v1.1`, with two variants:

* `protect_only`    -- protect net-profitable positions; never re-enter.
* `protect_reentry` -- protect, then re-enter the same side after a confirmed
                       favourable recovery while the ORIGINAL skew still holds.

v1.1 is the D3 handoff of v1: every v1 threshold and rule is unchanged. It
makes the integration explicit -- the D1 causal direction contract (status,
event-time freshness), skew-reading freshness, the re-entry contract, a guard
so an exit is not undone by an unchanged signal, and a named state machine
(`STATES`, `TRANSITIONS`). The forward paper cohort imports this kernel. The kernel itself performs no I/O.

Inputs (all causal at the observation's `now`):

Direction  The OA-229 / D1 `direction_asof` result for the chain's held side:
           `direction_status` (ok | stale | unknown | session_closed), `phase`
           (only meaningful when status is ok) and the `event_end` of the
           completed bar it came from. D1's status measures RECEIPT age; v1.1
           adds EVENT-time freshness: the bar's `event_end` must be no later
           than `now` and no older than `max_event_age_s`. A late-received old
           bar is therefore `stale` here even if D1 called it fresh.
Skew       The bot's own reading (imbalance, applied threshold) and the time
           it was observed. Used only for re-entry and the entry guard; a
           reading older than `max_skew_age_s` (or from the future) is not a
           valid skew.
Quotes     Executable bid for the held contract; executable ask for the
           re-entry contract (`ask_symbol`). `None` = not executable.

Rules (thresholds are the declared v1 configuration, not fitted):

Arm      Armed once NET profit at an executable bid (premium P&L minus
         round-trip fees, as a fraction of the position's own entry basis)
         reaches `arm_net_pct`. Armed stays armed for that position.
Protect  An armed, still net-profitable position exits WHOLE (every
         contract) when (a) net <= `floor_net_pct` (price floor) or (b) the
         effective direction phase is `breakdown`. `continuation`,
         `pullback`, `recovery` and `no_trend` hold: an intact move keeps its
         room. Net <= 0 is the stop-loss domain, never this family's.
Stale    Effective phase `stale`/`unknown` (D1 not ok, event time missing,
         in the future or too old) never triggers a direction exit; the
         price floor still applies. No executable bid while armed -> pending
         (retry next bar), never a guessed price.
Re-enter (`protect_reentry` only) after a PROTECTION exit, buy the same
         contract (`reentry_contract = "same_symbol"`: the exited option
         symbol, same contract count) only when ALL hold:
           - effective phase is a fresh `recovery` on a bar strictly after
             the exit, carrying a `recovery_event_id` (D1 row_id) not used
             before;
           - the original skew is still valid: fresh reading, same sign as at
             the original entry, |imbalance| >= threshold (no new OI flip);
           - >= `min_bars_after_exit` completed bars since the exit;
           - > `last_entry_minutes_before_close` minutes to the close;
           - re-entries < `max_reentries_per_session` and protection exits
             < `max_protect_exits_per_session`;
           - an executable ask for that same symbol.
Guard    `strategy_entry_allowed`: after a protection exit or a stop-loss
         exit in the chain, the strategy's own same-side buy is blocked for
         the rest of the session unless the skew has been invalid (fresh
         reading below threshold or opposite sign) at an observed bar since
         that exit -- i.e. a genuinely new signal. An unchanged signal can
         never undo a stop or a protection exit; only the recovery path above
         re-enters after protection. Opposite-side entries are a new OI flip
         and are not this guard's business. (D2's 15-minute cooldown still
         applies first and is unchanged.)
External A stop-loss (the D2 35% stop), EOD flatten or the strategy's signal
         sell ends the chain (`external_exit`); this family never re-enters
         after someone else's exit.

Precedence with the existing exit layer (unchanged): EOD flatten > protective
stop (35%) > THIS protection > strategy signal sell > re-entry. Each re-entry
is a new position with its own entry basis.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

FAMILY = "oa236-protect-v1.1"
VARIANTS = ("protect_only", "protect_reentry")
PHASES = ("continuation", "pullback", "breakdown", "recovery", "no_trend", "stale", "unknown")
DIRECTION_STATUSES = ("ok", "stale", "unknown", "session_closed")
EXTERNAL_EXITS = ("stop_loss", "eod_flatten", "signal_exit", "replay_end")
REENTRY_CONTRACTS = ("same_symbol",)
CONTRACT_MULTIPLIER = 100

#: Chain states. `held`/`armed` hold the contract; `flat_protected` is flat
#: after this family's own exit (re-entry possible for protect_reentry);
#: `ended` is flat after an external exit (no re-entry by this family).
STATES = ("held", "armed", "flat_protected", "ended")

#: Every transition the chain can make: (from, condition, to, action[:trigger]).
#: Rows are checked in order within a state; `verify_profit_protection.py`
#: exercises each one and fails if a row is never reached.
TRANSITIONS = (
    ("held", "no executable bid", "held", "hold"),
    ("held", "net < arm_net_pct", "held", "hold"),
    ("held", "net >= arm_net_pct", "armed", "hold"),
    ("held", "net >= arm_net_pct and effective phase breakdown on the same bar", "flat_protected",
     "exit:direction_breakdown"),
    ("armed", "no executable bid", "armed", "pending"),
    ("armed", "net <= 0 (stop-loss domain)", "armed", "hold"),
    ("armed", "net <= floor_net_pct", "flat_protected", "exit:price_floor"),
    ("armed", "effective phase breakdown", "flat_protected", "exit:direction_breakdown"),
    ("armed", "effective phase stale/unknown", "armed", "hold"),
    ("armed", "continuation/pullback/recovery/no_trend", "armed", "hold"),
    ("flat_protected", "protect_only", "flat_protected", "flat"),
    ("flat_protected", "any re-entry gate fails", "flat_protected", "blocked"),
    ("flat_protected", "all re-entry gates pass", "held", "reenter:recovery_reentry"),
    ("ended", "any", "ended", "flat"),
    ("held|armed|flat_protected", "external_exit(stop_loss|eod_flatten|signal_exit|replay_end)", "ended", "-"),
)


@dataclass(frozen=True)
class ProtectConfig:
    variant: str = "protect_only"
    arm_net_pct: float = 0.20
    floor_net_pct: float = 0.05
    fee_per_contract_per_side: float = 0.0     # premium dollars; paper = 0, broker set explicitly
    min_bars_after_exit: int = 2
    last_entry_minutes_before_close: float = 30.0
    max_reentries_per_session: int = 2
    max_protect_exits_per_session: int = 3
    # v1.1 integration declarations (not economic thresholds):
    max_event_age_s: int = 360                 # one 5-minute bar + 60 s, as D1's receipt rule
    max_skew_age_s: int = 360                  # one runner cycle (300 s) + 60 s
    reentry_contract: str = "same_symbol"
    version: str = FAMILY

    def __post_init__(self):
        if self.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        if self.reentry_contract not in REENTRY_CONTRACTS:
            raise ValueError(f"reentry_contract must be one of {REENTRY_CONTRACTS}")
        for name in ("arm_net_pct", "floor_net_pct", "fee_per_contract_per_side", "last_entry_minutes_before_close"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.floor_net_pct < self.arm_net_pct:
            raise ValueError("require 0 <= floor_net_pct < arm_net_pct")
        if self.fee_per_contract_per_side < 0 or self.last_entry_minutes_before_close < 0:
            raise ValueError("fees and the entry cutoff must be >= 0")
        for name in ("min_bars_after_exit", "max_reentries_per_session", "max_protect_exits_per_session",
                     "max_event_age_s", "max_skew_age_s"):
            v = getattr(self, name)
            if type(v) is not int or v < 0:
                raise ValueError(f"{name} must be an int >= 0")
        if self.min_bars_after_exit < 1:
            raise ValueError("min_bars_after_exit must be >= 1 (never re-enter on the exit bar)")
        if self.max_event_age_s < 1 or self.max_skew_age_s < 1:
            raise ValueError("freshness limits must be >= 1 s")


@dataclass(frozen=True)
class Observation:
    """One completed-bar evaluation for the chain (all causal at `now`)."""
    bar_index: int                 # completed-bar sequence in the session
    phase: str | None              # D1 phase for the chain's side (used only when status is ok)
    recovery_event_id: str | None  # D1 row_id of the recovery row (phase == recovery)
    bid: float | None              # executable bid for the held contract (None = not executable)
    ask: float | None              # executable ask for `ask_symbol` (None = not executable)
    imbalance: float | None        # the bot's current skew reading
    imbalance_threshold: float     # the bot's applied threshold
    minutes_to_close: float
    now: datetime | None = None                  # decision instant (tz-aware)
    direction_status: str = "ok"                 # D1 status for the chain's side
    direction_event_end: datetime | None = None  # event_end of the bar the phase came from
    skew_observed_at: datetime | None = None     # when the skew reading was observed
    ask_symbol: str | None = None                # contract the ask is for

    def __post_init__(self):
        if self.direction_status not in DIRECTION_STATUSES:
            raise ValueError(f"unknown direction_status {self.direction_status!r}")
        if self.phase is not None and self.phase not in PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        if self.direction_status == "ok" and self.phase is None:
            raise ValueError("direction_status ok requires a phase")
        for name in ("now", "direction_event_end", "skew_observed_at"):
            t = getattr(self, name)
            if t is not None and t.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass
class Decision:
    action: str                    # hold | exit | pending | reenter | blocked | flat
    reason: str
    contracts: int = 0
    trigger: str | None = None     # direction_breakdown | price_floor | recovery_reentry
    metadata: dict[str, Any] = field(default_factory=dict)


def _exact(x: float) -> Fraction:
    return Fraction(repr(float(x)))


def net_pct(basis: float, bid: float, fee_per_side: float) -> Fraction:
    """Net premium P&L per contract as an exact fraction of the entry basis."""
    b, p = _exact(basis), _exact(bid)
    fees = 2 * _exact(fee_per_side) / CONTRACT_MULTIPLIER  # round trip, in premium points
    return (p - b - fees) / b


def effective_phase(obs: Observation, config: ProtectConfig) -> tuple[str, str | None]:
    """The phase the policy may act on, and why it was degraded (if it was)."""
    if obs.direction_status != "ok":
        return ("stale" if obs.direction_status in ("stale", "session_closed") else "unknown",
                f"direction_status_{obs.direction_status}")
    if obs.now is None or obs.direction_event_end is None:
        return "unknown", "direction_event_time_not_supplied"
    age = (obs.now - obs.direction_event_end).total_seconds()
    if age < 0:
        return "unknown", "direction_event_end_after_now"
    if age > config.max_event_age_s:
        return "stale", f"direction_event_age_{int(age)}s_exceeds_{config.max_event_age_s}s"
    return obs.phase, None


def skew_state(imbalance, threshold, observed_at, now, sign: int, config: ProtectConfig) -> tuple[bool, str]:
    """(valid, reason): is this a fresh reading of the given skew sign at/over threshold?"""
    if now is None or observed_at is None:
        return False, "skew observation time not supplied"
    age = (now - observed_at).total_seconds()
    if age < 0:
        return False, "skew observed after now"
    if age > config.max_skew_age_s:
        return False, f"skew reading {int(age)}s old exceeds {config.max_skew_age_s}s"
    if imbalance is None or not math.isfinite(imbalance):
        return False, "skew reading missing"
    s = 1 if imbalance > 0 else -1 if imbalance < 0 else 0
    if s != sign or abs(imbalance) < threshold:
        return False, "skew sign differs or |imbalance| below threshold"
    return True, "skew valid"


class ProtectionChain:
    """Per-session, per-side state: one held position at a time, then re-entries."""

    def __init__(self, side: str, contracts: int, entry_basis: float, entry_bar: int,
                 original_skew_sign: int, config: ProtectConfig = ProtectConfig(), *,
                 contract_symbol: str | None = None):
        if side not in ("call", "put"):
            raise ValueError("side must be call or put")
        if type(contracts) is not int or contracts < 1:
            raise ValueError("contracts must be a positive int")
        if not (math.isfinite(entry_basis) and entry_basis > 0):
            raise ValueError("entry_basis must be positive")
        if original_skew_sign not in (1, -1):
            raise ValueError("original_skew_sign must be +1 or -1")
        self.side, self.contracts, self.config = side, contracts, config
        self.contract_symbol = contract_symbol
        self.original_skew_sign = original_skew_sign
        self.held = True
        self.basis = float(entry_basis)
        self.entry_bar = entry_bar
        self.armed = False
        self.protect_exits = 0
        self.reentries = 0
        self.last_exit_bar: int | None = None
        self.last_exit_trigger: str | None = None
        self.used_recovery_events: set[str] = set()
        self.closed_reason: str | None = None   # chain ended (stop / flatten / signal)
        self.exited_by: str | None = None       # last exit in the chain: protection | stop_loss | ...
        self.skew_invalidated_since_exit = False
        self._last_bar: int | None = None

    @property
    def state(self) -> str:
        if self.held:
            return "armed" if self.armed else "held"
        return "ended" if self.closed_reason is not None else "flat_protected"

    # -- external exits ------------------------------------------------------
    def external_exit(self, kind: str) -> None:
        """A stop-loss, EOD flatten or signal sell closed the position: the
        chain ends; this family never re-enters after someone else's exit."""
        if kind not in EXTERNAL_EXITS:
            raise ValueError(f"external exit kind must be one of {EXTERNAL_EXITS}")
        self.held = False
        self.closed_reason = kind
        self.exited_by = kind
        self.skew_invalidated_since_exit = False

    # -- the anti-churn entry guard -----------------------------------------
    def strategy_entry_allowed(self, side: str, imbalance, threshold, observed_at, now) -> tuple[bool, str]:
        """May the strategy's OWN buy on `side` go ahead this cycle?

        Blocks a same-side buy after a protection or stop-loss exit until the
        skew has been invalid at an observed bar since that exit (a new
        signal). Call `evaluate` every bar while flat so that history is seen.
        """
        if side not in ("call", "put"):
            raise ValueError("side must be call or put")
        if self.held:
            return True, "chain holds the position; not an entry"
        if side != self.side:
            return True, "opposite side is a new OI flip"
        if self.exited_by == "eod_flatten":
            return False, "session flattened"
        if self.exited_by not in ("protection", "stop_loss"):
            return True, f"chain exited by {self.exited_by}; signal changed"
        if self.skew_invalidated_since_exit:
            valid, why = skew_state(imbalance, threshold, observed_at, now, self.original_skew_sign, self.config)
            return (valid, "new signal after the skew reset" if valid else why)
        return False, f"unchanged signal cannot undo a {self.exited_by} exit"

    # -- the decision ----------------------------------------------------------
    def evaluate(self, obs: Observation) -> Decision:
        if self._last_bar is not None and obs.bar_index <= self._last_bar:
            raise ValueError("observations must arrive in strictly increasing bar order")
        self._last_bar = obs.bar_index
        cfg = self.config
        phase, degraded = effective_phase(obs, cfg)
        state_from = self.state
        meta = {"family": cfg.version, "variant": cfg.variant, "phase": phase, "input_phase": obs.phase,
                "direction_status": obs.direction_status, "phase_degraded": degraded,
                "bar_index": obs.bar_index, "armed": self.armed,
                "protect_exits": self.protect_exits, "reentries": self.reentries, "state_from": state_from}
        if self.held:
            d = self._evaluate_held(obs, phase, meta)
        else:
            if not self.skew_invalidated_since_exit:
                valid, _ = skew_state(obs.imbalance, obs.imbalance_threshold, obs.skew_observed_at,
                                      obs.now, self.original_skew_sign, cfg)
                # Only a FRESH reading that is below threshold or opposite counts
                # as a reset; a stale/missing reading proves nothing either way.
                fresh = (obs.now is not None and obs.skew_observed_at is not None
                         and 0 <= (obs.now - obs.skew_observed_at).total_seconds() <= cfg.max_skew_age_s
                         and obs.imbalance is not None and math.isfinite(obs.imbalance))
                if fresh and not valid:
                    self.skew_invalidated_since_exit = True
            d = self._evaluate_flat(obs, phase, meta)
        d.metadata["state_to"] = self.state
        return d

    def _evaluate_held(self, obs: Observation, phase: str, meta: dict) -> Decision:
        cfg = self.config
        if obs.bid is None or not (math.isfinite(obs.bid) and obs.bid > 0):
            return Decision("pending" if self.armed else "hold",
                            "no executable bid; protection cannot be evaluated this bar" if self.armed
                            else "no executable bid; not armed", metadata=meta)
        net = net_pct(self.basis, obs.bid, cfg.fee_per_contract_per_side)
        meta.update(net_pct=float(net), basis=self.basis, bid=obs.bid)
        if not self.armed and net >= _exact(cfg.arm_net_pct):
            self.armed = True
            meta["armed"] = True
            meta["armed_now"] = True
        if not self.armed:
            return Decision("hold", f"not armed (net {float(net):+.1%} < {cfg.arm_net_pct:+.0%})", metadata=meta)
        if net <= 0:
            # Loss territory belongs to the stop-loss layer, not to profit protection.
            return Decision("hold", "armed but no longer net-profitable; stop-loss domain", metadata=meta)
        trigger = None
        if net <= _exact(cfg.floor_net_pct):
            trigger = "price_floor"
        elif phase == "breakdown":
            trigger = "direction_breakdown"
        if trigger is None:
            why = (f"direction unknown/stale ({phase}, {meta['phase_degraded'] or 'reported'}): "
                   "no direction exit; floor not reached"
                   if phase in ("stale", "unknown")
                   else f"armed, {phase}: move intact, holding")
            return Decision("hold", why, metadata=meta)
        self.held = False
        self.protect_exits += 1
        self.last_exit_bar = obs.bar_index
        self.last_exit_trigger = trigger
        self.exited_by = "protection"
        self.skew_invalidated_since_exit = False
        meta.update(protect_exits=self.protect_exits, trigger=trigger)
        return Decision("exit", f"protect {trigger} at net {float(net):+.1%}; exiting all {self.contracts} contract(s)",
                        contracts=self.contracts, trigger=trigger, metadata=meta)

    def _evaluate_flat(self, obs: Observation, phase: str, meta: dict) -> Decision:
        cfg = self.config
        if self.closed_reason is not None:
            return Decision("flat", f"chain ended by {self.closed_reason}; no re-entry", metadata=meta)
        if cfg.variant != "protect_reentry":
            return Decision("flat", "protect_only: no re-entry", metadata=meta)
        blockers = []
        if phase != "recovery":
            blockers.append(f"no confirmed recovery (phase {phase}"
                            + (f", {meta['phase_degraded']})" if meta["phase_degraded"] else ")"))
        elif not obs.recovery_event_id:
            blockers.append("recovery without an event identity")
        elif obs.recovery_event_id in self.used_recovery_events:
            blockers.append("recovery event already used")
        if self.last_exit_bar is None or obs.bar_index - self.last_exit_bar < cfg.min_bars_after_exit:
            blockers.append(f"fewer than {cfg.min_bars_after_exit} bars since the exit")
        valid, why = skew_state(obs.imbalance, obs.imbalance_threshold, obs.skew_observed_at, obs.now,
                                self.original_skew_sign, cfg)
        if not valid:
            blockers.append(f"original skew no longer valid: {why}")
        if obs.minutes_to_close <= cfg.last_entry_minutes_before_close:
            blockers.append(f"within {cfg.last_entry_minutes_before_close:.0f} min of the close")
        if self.reentries >= cfg.max_reentries_per_session:
            blockers.append("session re-entry limit reached")
        if self.protect_exits >= cfg.max_protect_exits_per_session:
            blockers.append("session protection-exit limit reached")
        if self.contract_symbol is None:
            blockers.append("re-entry contract unknown (no contract_symbol)")
        elif obs.ask_symbol != self.contract_symbol:
            blockers.append(f"ask is for {obs.ask_symbol!r}, not the exited contract {self.contract_symbol!r}")
        if obs.ask is None or not (math.isfinite(obs.ask) and obs.ask > 0):
            blockers.append("no executable ask")
        if blockers:
            return Decision("blocked", "; ".join(blockers), metadata={**meta, "blockers": blockers})
        self.used_recovery_events.add(obs.recovery_event_id)
        self.reentries += 1
        self.held, self.armed = True, False
        self.basis = float(obs.ask)       # each re-entry carries its own basis
        self.entry_bar = obs.bar_index
        self.exited_by = None
        meta.update(reentries=self.reentries, new_basis=self.basis, recovery_event_id=obs.recovery_event_id,
                    contract_symbol=self.contract_symbol)
        return Decision("reenter", f"confirmed recovery with original skew valid; re-entering {self.contracts} "
                        f"{self.contract_symbol} at {obs.ask:.2f}", contracts=self.contracts,
                        trigger="recovery_reentry", metadata=meta)

    def as_metadata(self) -> dict[str, Any]:
        return {"config": asdict(self.config), "side": self.side, "contracts": self.contracts,
                "contract_symbol": self.contract_symbol, "state": self.state,
                "held": self.held, "armed": self.armed, "basis": self.basis,
                "protect_exits": self.protect_exits, "reentries": self.reentries,
                "closed_reason": self.closed_reason, "exited_by": self.exited_by,
                "skew_invalidated_since_exit": self.skew_invalidated_since_exit}
