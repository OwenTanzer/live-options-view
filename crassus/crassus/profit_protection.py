"""OA-236 profit protection and re-entry for OI Skew positions (INACTIVE).

A small versioned family, `oa236-protect-v1`, with two variants:

* `protect_only`    -- protect net-profitable positions; never re-enter.
* `protect_reentry` -- protect, then re-enter the same side after a confirmed
                       favourable recovery while the ORIGINAL skew still holds.

Nothing calls this module: there is no runner call site, no activation switch
and no provider access. It is a pure decision function plus the per-session
chain state it needs, so the step-4 comparison can replay it and a later,
separately approved change can wire it in.

Direction input is the OA-229 position-relative contract
(directional-move-lab research/oa229-direction-v1): one of
continuation / pullback / breakdown / recovery / no_trend / stale / unknown,
computed causally from completed bars for the held side. Skew input is the
bot's own current reading (imbalance and its threshold). Neither is
recomputed here.

Rules (all thresholds are the declared v1 configuration, not fitted):

Arm      A position is armed once its NET profit at an executable bid
         (premium P&L minus round-trip fees, as a fraction of its own entry
         basis) reaches `arm_net_pct`. Armed stays armed for that position.
Protect  An armed position exits WHOLE (every contract) when
         (a) direction is `breakdown` and it is still net-profitable, or
         (b) net profit falls to `floor_net_pct` or below (price floor).
         `continuation`, `pullback`, `recovery` and `no_trend` hold: an intact
         move keeps its room. A non-net-profitable position is the stop-loss
         domain, never this family's.
Stale    `stale`/`unknown` direction never triggers a direction exit; the
         price floor still applies. No executable bid -> exit pending (retry
         next cycle), never a guessed price.
Re-enter (`protect_reentry` only) after a PROTECTION exit, buy the same side
         and the same contract count again only when ALL hold:
           - direction reports `recovery` on a bar strictly after the exit
             (the OA-229 challenger's confirmed 1.0 ATR / 2-bar recovery);
           - that recovery event has not already been used;
           - the original skew is still valid (same sign as at the original
             entry, |imbalance| >= threshold) -- no new OI flip required;
           - >= `min_bars_after_exit` completed bars since the exit;
           - > `last_entry_minutes_before_close` minutes to the close;
           - session limits: re-entries < `max_reentries_per_session`,
             protection exits so far < `max_protect_exits_per_session`;
           - an executable ask exists.
         An unchanged skew signal alone can never re-enter (anti-churn).
         A stop-loss exit (e.g. the 35% entry-premium stop) or an EOD flatten
         ends the chain for the session.

Precedence with the existing exit layer (unchanged): EOD flatten > protective
stop (35%) > THIS protection > strategy signal sell > re-entry. Each re-entry
is a new position with its own entry basis.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any

FAMILY = "oa236-protect-v1"
VARIANTS = ("protect_only", "protect_reentry")
PHASES = ("continuation", "pullback", "breakdown", "recovery", "no_trend", "stale", "unknown")
CONTRACT_MULTIPLIER = 100


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
    version: str = FAMILY

    def __post_init__(self):
        if self.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        for name in ("arm_net_pct", "floor_net_pct", "fee_per_contract_per_side", "last_entry_minutes_before_close"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.floor_net_pct < self.arm_net_pct:
            raise ValueError("require 0 <= floor_net_pct < arm_net_pct")
        if self.fee_per_contract_per_side < 0 or self.last_entry_minutes_before_close < 0:
            raise ValueError("fees and the entry cutoff must be >= 0")
        for name in ("min_bars_after_exit", "max_reentries_per_session", "max_protect_exits_per_session"):
            v = getattr(self, name)
            if type(v) is not int or v < 0:
                raise ValueError(f"{name} must be an int >= 0")
        if self.min_bars_after_exit < 1:
            raise ValueError("min_bars_after_exit must be >= 1 (never re-enter on the exit bar)")


@dataclass(frozen=True)
class Observation:
    """One completed-bar evaluation for the chain (all causal at `bar_index`)."""
    bar_index: int                 # completed-bar sequence in the session
    phase: str                     # OA-229 position-relative phase for the chain's side
    recovery_event_id: str | None  # identity of the recovery that produced phase=recovery
    bid: float | None              # executable bid for the held contract (None = not executable)
    ask: float | None              # executable ask for the re-entry contract (None = not executable)
    imbalance: float | None        # the bot's current skew reading
    imbalance_threshold: float     # the bot's applied threshold
    minutes_to_close: float

    def __post_init__(self):
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")


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


class ProtectionChain:
    """Per-session, per-side state: one held position at a time, then re-entries."""

    def __init__(self, side: str, contracts: int, entry_basis: float, entry_bar: int,
                 original_skew_sign: int, config: ProtectConfig = ProtectConfig()):
        if side not in ("call", "put"):
            raise ValueError("side must be call or put")
        if type(contracts) is not int or contracts < 1:
            raise ValueError("contracts must be a positive int")
        if not (math.isfinite(entry_basis) and entry_basis > 0):
            raise ValueError("entry_basis must be positive")
        if original_skew_sign not in (1, -1):
            raise ValueError("original_skew_sign must be +1 or -1")
        self.side, self.contracts, self.config = side, contracts, config
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
        self.closed_reason: str | None = None   # chain ended (stop / flatten / limit)
        self._last_bar: int | None = None

    # -- external exits ------------------------------------------------------
    def external_exit(self, reason: str) -> None:
        """A stop-loss, EOD flatten or signal sell closed the position: the
        chain ends; this family never re-enters after someone else's exit."""
        self.held = False
        self.closed_reason = reason

    # -- the decision ----------------------------------------------------------
    def evaluate(self, obs: Observation) -> Decision:
        if self._last_bar is not None and obs.bar_index <= self._last_bar:
            raise ValueError("observations must arrive in strictly increasing bar order")
        self._last_bar = obs.bar_index
        cfg = self.config
        meta = {"family": cfg.version, "variant": cfg.variant, "phase": obs.phase,
                "bar_index": obs.bar_index, "armed": self.armed,
                "protect_exits": self.protect_exits, "reentries": self.reentries}
        if self.held:
            return self._evaluate_held(obs, meta)
        return self._evaluate_flat(obs, meta)

    def _evaluate_held(self, obs: Observation, meta: dict) -> Decision:
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
        elif obs.phase == "breakdown":
            trigger = "direction_breakdown"
        if trigger is None:
            why = ("direction unknown/stale: no direction exit; floor not reached"
                   if obs.phase in ("stale", "unknown")
                   else f"armed, {obs.phase}: move intact, holding")
            return Decision("hold", why, metadata=meta)
        self.held = False
        self.protect_exits += 1
        self.last_exit_bar = obs.bar_index
        self.last_exit_trigger = trigger
        meta.update(protect_exits=self.protect_exits, trigger=trigger)
        return Decision("exit", f"protect {trigger} at net {float(net):+.1%}; exiting all {self.contracts} contract(s)",
                        contracts=self.contracts, trigger=trigger, metadata=meta)

    def _evaluate_flat(self, obs: Observation, meta: dict) -> Decision:
        cfg = self.config
        if cfg.variant != "protect_reentry":
            return Decision("flat", "protect_only: no re-entry", metadata=meta)
        if self.closed_reason is not None:
            return Decision("flat", f"chain ended by {self.closed_reason}; no re-entry", metadata=meta)
        blockers = []
        if obs.phase != "recovery":
            blockers.append(f"no confirmed recovery (phase {obs.phase})")
        elif not obs.recovery_event_id:
            blockers.append("recovery without an event identity")
        elif obs.recovery_event_id in self.used_recovery_events:
            blockers.append("recovery event already used")
        if self.last_exit_bar is None or obs.bar_index - self.last_exit_bar < cfg.min_bars_after_exit:
            blockers.append(f"fewer than {cfg.min_bars_after_exit} bars since the exit")
        if (obs.imbalance is None or not math.isfinite(obs.imbalance)
                or (1 if obs.imbalance > 0 else -1 if obs.imbalance < 0 else 0) != self.original_skew_sign
                or abs(obs.imbalance) < obs.imbalance_threshold):
            blockers.append("original skew no longer valid (sign or |imbalance| below threshold)")
        if obs.minutes_to_close <= cfg.last_entry_minutes_before_close:
            blockers.append(f"within {cfg.last_entry_minutes_before_close:.0f} min of the close")
        if self.reentries >= cfg.max_reentries_per_session:
            blockers.append("session re-entry limit reached")
        if self.protect_exits >= cfg.max_protect_exits_per_session:
            blockers.append("session protection-exit limit reached")
        if obs.ask is None or not (math.isfinite(obs.ask) and obs.ask > 0):
            blockers.append("no executable ask")
        if blockers:
            return Decision("blocked", "; ".join(blockers), metadata={**meta, "blockers": blockers})
        self.used_recovery_events.add(obs.recovery_event_id)
        self.reentries += 1
        self.held, self.armed = True, False
        self.basis = float(obs.ask)       # each re-entry carries its own basis
        self.entry_bar = obs.bar_index
        meta.update(reentries=self.reentries, new_basis=self.basis, recovery_event_id=obs.recovery_event_id)
        return Decision("reenter", f"confirmed recovery with original skew valid; re-entering {self.contracts} "
                        f"contract(s) at {obs.ask:.2f}", contracts=self.contracts,
                        trigger="recovery_reentry", metadata=meta)

    def as_metadata(self) -> dict[str, Any]:
        return {"config": asdict(self.config), "side": self.side, "contracts": self.contracts,
                "held": self.held, "armed": self.armed, "basis": self.basis,
                "protect_exits": self.protect_exits, "reentries": self.reentries,
                "closed_reason": self.closed_reason}
