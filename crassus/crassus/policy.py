"""The deterministic policy layer -- the only thing that can make a Crassus
AI parameter proposal effective.

Crassus AI (not built yet -- see PR2) may only ever write a *proposed*
override. This module is what decides whether a proposal ever reaches a
strategy's `ctx.params`, and it is the sole authority for that decision --
not the Worker, not Crassus AI, not the presence of a `status="accepted"`
row by itself. Every check here is fail-closed: any exception, missing
data, or ambiguity produces the account's own baseline `params` unchanged,
never the proposed values and never a crash that could take an account's
whole cycle down with it.

The schemas below are hand-authored, not derived from strategy source, on
purpose: the set of parameters a strategy is willing to have adjusted, and
the bounds within which adjustment is safe, is a reviewed decision, not
something that should silently grow because a strategy added a new
`params.get(...)` call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Per-strategy allowlist
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One overridable parameter's type and bounds.

    `max_rate_of_change` is a fraction of the *prior accepted* value (or the
    account's own baseline, if there is no prior accepted value yet) that a
    single override may move a numeric parameter by -- e.g. 0.5 permits at
    most a 50% change in either direction per accepted override. `None`
    means no rate-of-change cap (still bounded by min/max).
    """

    kind: type  # float or bool
    min: float | None = None
    max: float | None = None
    max_rate_of_change: float | None = None
    nullable: bool = False


# One entry per strategy_id in `crassus.strategy.REGISTRY`, sourced from the
# `params.get(...)` calls in each `crassus/crassus/strategies/*.py` module.
# Deliberately omits every strategy's non-numeric/non-bool params (e.g.
# canopus_down_day's HH:MM time-of-day strings) -- time-window edits change
# *when* a strategy trades, which is a materially different kind of risk
# than nudging a threshold, and isn't supported by this policy layer yet.
STRATEGY_PARAM_SCHEMAS: dict[str, dict[str, ParamSpec]] = {
    "momentum_qqq": {
        "bullish_threshold": ParamSpec(float, min=0.0005, max=0.02, max_rate_of_change=0.5),
        "bearish_threshold": ParamSpec(float, min=-0.02, max=-0.0005, max_rate_of_change=0.5),
        "vwap_confirmation_required": ParamSpec(bool),
        "rvol_floor": ParamSpec(float, min=0.5, max=5.0, nullable=True, max_rate_of_change=0.5),
    },
    "reddit_sentiment_qqq": {
        "min_sample_size": ParamSpec(float, min=1, max=50, max_rate_of_change=1.0),
        "bullish_threshold": ParamSpec(float, min=0.02, max=1.0, max_rate_of_change=0.5),
        "bearish_threshold": ParamSpec(float, min=-1.0, max=-0.02, max_rate_of_change=0.5),
    },
    "trump_whisperer_qqq": {
        "min_sample_size": ParamSpec(float, min=1, max=50, max_rate_of_change=1.0),
        "bullish_threshold": ParamSpec(float, min=0.02, max=1.0, max_rate_of_change=0.5),
        "bearish_threshold": ParamSpec(float, min=-1.0, max=-0.02, max_rate_of_change=0.5),
    },
    "max_pain_qqq": {
        "pin_threshold_pct": ParamSpec(float, min=0.02, max=1.0, max_rate_of_change=0.5),
        "min_strikes_with_oi": ParamSpec(float, min=2, max=20, max_rate_of_change=0.5),
    },
    "oi_skew_qqq": {
        "near_money_pct": ParamSpec(float, min=0.005, max=0.1, max_rate_of_change=0.5),
        "imbalance_threshold": ParamSpec(float, min=0.05, max=0.9, max_rate_of_change=0.5),
        "min_change_from_session_start": ParamSpec(float, min=0.02, max=0.5, max_rate_of_change=0.5),
        "min_band_strikes": ParamSpec(float, min=1, max=20, max_rate_of_change=0.5),
    },
    "put_call_ratio_qqq": {
        "extreme_z_threshold": ParamSpec(float, min=0.5, max=4.0, max_rate_of_change=0.5),
        "min_baseline_samples": ParamSpec(float, min=5, max=200, max_rate_of_change=0.5),
        "max_snapshot_age_minutes": ParamSpec(float, min=1.0, max=30.0, max_rate_of_change=0.5),
    },
    "canopus_down_day_14": {
        "down_threshold_pct": ParamSpec(float, min=0.0005, max=0.02, max_rate_of_change=0.5),
        "target_multiplier": ParamSpec(float, min=1.02, max=2.0, max_rate_of_change=0.5),
    },
    "phelps_pure_qqq": {
        "phelps_minutes": ParamSpec(float, min=5.0, max=90.0, max_rate_of_change=0.5),
        "displacement_threshold": ParamSpec(float, min=0.0003, max=0.01, max_rate_of_change=0.5),
        "displacement_window_minutes": ParamSpec(float, min=1.0, max=30.0, max_rate_of_change=0.5),
        "max_anchor_overshoot_minutes": ParamSpec(float, min=0.5, max=10.0, max_rate_of_change=0.5),
        "max_snapshot_age_minutes": ParamSpec(float, min=1.0, max=30.0, max_rate_of_change=0.5),
    },
}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class PolicyResult:
    effective_params: dict[str, Any]
    applied: bool
    override_id: str | None = None
    rejections: list[str] = field(default_factory=list)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_value(name: str, spec: ParamSpec, value: Any, prior: Any) -> str | None:
    """Returns a rejection reason, or None if `value` is acceptable."""
    if value is None:
        if spec.nullable:
            return None
        return f"{name}: null is not permitted for this parameter"

    if spec.kind is bool:
        if not isinstance(value, bool):
            return f"{name}: expected bool, got {type(value).__name__}"
        return None

    # spec.kind is float
    if not _is_number(value):
        return f"{name}: expected a finite number, got {value!r}"
    if spec.min is not None and value < spec.min:
        return f"{name}: {value} is below minimum {spec.min}"
    if spec.max is not None and value > spec.max:
        return f"{name}: {value} is above maximum {spec.max}"
    if spec.max_rate_of_change is not None and _is_number(prior) and prior != 0:
        change = abs(value - prior) / abs(prior)
        if change > spec.max_rate_of_change:
            return (
                f"{name}: change of {change:.2%} from prior value {prior} exceeds "
                f"the {spec.max_rate_of_change:.0%} rate-of-change cap"
            )
    return None


class OverridePolicy:
    """Fail-closed gate between a stored override envelope and a strategy's
    `ctx.params`. See module docstring for the trust posture.
    """

    def evaluate(
        self,
        account: Any,  # config.Account
        baseline_params: dict[str, Any],
        envelope: dict[str, Any] | None,
        prior_accepted_params: dict[str, Any] | None,
        *,
        kill_switch: bool | None,
        frozen: bool | None,
    ) -> PolicyResult:
        baseline_params = dict(baseline_params or {})

        def deny(*reasons: str) -> PolicyResult:
            return PolicyResult(effective_params=baseline_params, applied=False, rejections=list(reasons))

        try:
            # Ambiguity about the kill switch or freeze state is treated as
            # "on" -- an unreachable control endpoint must never be read as
            # permission to trade on an override.
            if kill_switch is None or kill_switch:
                return deny("kill switch is engaged or its state could not be confirmed")
            if frozen is None or frozen:
                return deny(f"account {getattr(account, 'alias', '?')} is frozen or its state could not be confirmed")

            if not isinstance(envelope, dict):
                return deny("no override envelope present")

            strategy_id = getattr(account, "strategy_id", None)
            schema = STRATEGY_PARAM_SCHEMAS.get(strategy_id)
            if not schema:
                return deny(f"no override schema registered for strategy_id {strategy_id!r}")

            if envelope.get("status") != "accepted":
                return deny(f"envelope status is {envelope.get('status')!r}, not 'accepted'")

            if envelope.get("account_alias") != getattr(account, "alias", None):
                return deny("envelope account_alias does not match this account")

            expires_utc = envelope.get("expires_utc")
            if not isinstance(expires_utc, str) or not self._is_future(expires_utc):
                return deny("envelope is expired or has no valid expires_utc")

            proposed = envelope.get("proposed_params")
            if not isinstance(proposed, dict) or not proposed:
                return deny("envelope has no proposed_params")

            prior = dict(prior_accepted_params or {})
            rejections: list[str] = []
            for name, value in proposed.items():
                spec = schema.get(name)
                if spec is None:
                    rejections.append(f"{name}: not an overridable parameter for {strategy_id}")
                    continue
                reason = _check_value(name, spec, value, prior.get(name, baseline_params.get(name)))
                if reason:
                    rejections.append(reason)

            if rejections:
                # Whole-envelope rejection -- no partial application. One
                # fail-closed code path to reason about and test, not two.
                return deny(*rejections)

            effective = dict(baseline_params)
            effective.update(proposed)
            return PolicyResult(
                effective_params=effective,
                applied=True,
                override_id=envelope.get("id"),
                rejections=[],
            )
        except Exception as exc:  # policy evaluation must never crash a cycle
            return deny(f"policy evaluation raised: {exc}")

    @staticmethod
    def _is_future(iso_timestamp: str) -> bool:
        from datetime import datetime, timezone

        try:
            ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts > datetime.now(timezone.utc)
