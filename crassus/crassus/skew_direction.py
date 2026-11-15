"""OA-229 challenger from directional-move-lab@1c967eda. See forward-skew.md."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from statistics import mean
import hashlib
import json

PHASES = ("continuation", "pullback", "breakdown", "recovery", "no_trend", "stale", "unknown")
UNSAFE_FLAGS = frozenset({"partial", "invalid", "stale"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class FutureDataError(ValueError):
    """A bar was offered that was not yet available at the caller's cutoff."""


@dataclass(frozen=True)
class Position:
    """A held option's direction context. Only side/entry time/price matter here."""
    side: str            # "call" | "put"
    entry_asof: datetime  # when the position became held (aware)
    entry_price: float   # underlying reference at entry
    session: str

    def __post_init__(self):
        if self.side not in ("call", "put"):
            raise ValueError("side must be call or put")
        if self.entry_asof.tzinfo is None:
            raise ValueError("entry_asof must be timezone-aware")
        if not (isfinite(self.entry_price) and self.entry_price > 0):
            raise ValueError("positive finite entry_price required")

    @property
    def sign(self) -> int:
        return 1 if self.side == "call" else -1


@dataclass(frozen=True)
class ChallengerConfig:
    version: str = "oa229-direction-challenger-v1"
    atr_bars: int = 6              # prior completed bars in the scale (current bar excluded)
    pullback_atr: float = 0.75
    breakdown_atr: float = 1.5     # same multiple as the baseline's retrace_atr
    recovery_atr: float = 1.0
    recovery_confirm_bars: int = 2
    horizon_minutes: int = 30      # evaluation horizon declared for comparisons; no forecast is made

    def __post_init__(self):
        if type(self.atr_bars) is not int or self.atr_bars < 2:
            raise ValueError("atr_bars must be an int >= 2")
        if type(self.recovery_confirm_bars) is not int or self.recovery_confirm_bars < 1:
            raise ValueError("recovery_confirm_bars must be an int >= 1")
        for name in ("pullback_atr", "breakdown_atr", "recovery_atr"):
            v = getattr(self, name)
            if not (isfinite(v) and v > 0):
                raise ValueError(f"{name} must be positive and finite")
        if not self.pullback_atr < self.breakdown_atr:
            raise ValueError("pullback_atr must be below breakdown_atr")
        if type(self.horizon_minutes) is not int or self.horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be a positive int")


def _check_session(bar, position):
    if bar.session != position.session:
        raise ValueError("bar from a different session; positions do not carry across sessions")


class ChallengerDirection:
    """Incremental, causal tracker. Feed completed bars in order with ``step``."""

    def __init__(self, position: Position, config: ChallengerConfig = ChallengerConfig()):
        self.position = position
        self.config = config
        self.config_hash = digest(asdict(config))
        self._history = []          # contiguous closed bars (pre- and post-entry) for the scale
        self._last = None           # last accepted bar
        self._extreme = position.entry_price
        self._extreme_at = position.entry_asof
        self._phase = None
        self._trough = None
        self._confirm = 0
        self._parent = None

    def _atr(self):
        n = self.config.atr_bars
        h = self._history
        if len(h) < n + 1:
            return None
        trail = h[-n - 1:-1]          # prior bars only; current bar excluded
        prev = h[-n - 2] if len(h) >= n + 2 else None
        trs = []
        for i, b in enumerate(trail):
            p = trail[i - 1] if i else prev
            trs.append(b.high - b.low if p is None else
                       max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)))
        atr = mean(trs)
        return atr if atr > 0 else None

    def step(self, bar, cutoff: datetime | None = None):
        """Consume one completed bar; return a row, or None for pre-entry context bars."""
        if cutoff is not None and bar.available_at > cutoff:
            raise FutureDataError(f"bar available {bar.available_at.isoformat()} after cutoff {cutoff.isoformat()}")
        _check_session(bar, self.position)
        if self._last is not None and bar.available_at <= self._last.available_at:
            raise ValueError("bars must arrive in strictly increasing available_at order")
        gap = self._last is not None and bar.event_start != self._last.event_end
        unsafe = sorted(UNSAFE_FLAGS & set(bar.quality_flags))
        if gap or unsafe:
            self._history = []        # observability cleared; scale must rebuild
        if not unsafe:
            self._history.append(bar)
        self._last = bar
        if bar.available_at <= self.position.entry_asof:
            return None               # context only: available at/before entry

        cfg, s = self.config, self.position.sign
        reasons, atr = [], None
        fav = s * bar.close
        if gap or unsafe:
            phase = "stale"
            reasons.append("missing_bars_before_this_bar" if gap else "unsafe_quality_flag:" + ",".join(unsafe))
            self._confirm = 0
        else:
            atr = self._atr()
            if atr is None:
                phase = "unknown"
                reasons.append("insufficient_contiguous_history_for_scale")
                self._confirm = 0
                if fav > s * self._extreme:
                    self._extreme, self._extreme_at = bar.close, bar.available_at
            else:
                new_extreme = fav > s * self._extreme
                if new_extreme:
                    self._extreme, self._extreme_at = bar.close, bar.available_at
                giveback = (s * self._extreme - fav) / atr
                if self._phase == "breakdown":
                    self._trough = min(self._trough, fav)
                    bounce = (fav - self._trough) / atr
                    self._confirm = self._confirm + 1 if bounce >= cfg.recovery_atr else 0
                    if self._confirm >= cfg.recovery_confirm_bars:
                        phase = "recovery"
                        reasons.append(f"recovered_{bounce:.2f}atr_off_trough_for_{self._confirm}_bars")
                        # A recovery starts a new leg measured from here.
                        self._extreme, self._extreme_at = bar.close, bar.available_at
                        self._trough, self._confirm = None, 0
                    else:
                        phase = "breakdown"
                        reasons.append(f"holding_breakdown_bounce_{bounce:.2f}atr")
                elif giveback >= cfg.breakdown_atr:
                    phase = "breakdown"
                    reasons.append(f"giveback_{giveback:.2f}atr_from_extreme")
                    self._trough, self._confirm = fav, 0
                elif giveback >= cfg.pullback_atr:
                    phase = "pullback"
                    reasons.append(f"giveback_{giveback:.2f}atr_within_pullback_band")
                else:
                    phase = "continuation"
                    reasons.append("new_favourable_extreme" if new_extreme else f"giveback_{giveback:.2f}atr_below_pullback")
        self._phase = phase if phase in ("breakdown", "continuation", "pullback") else (
            "continuation" if phase == "recovery" else self._phase)
        if bar.available_at >= bar.session_close:
            reasons.append("censored_at_session_close")
        return self._row(bar, phase, reasons, atr)

    def _row(self, bar, phase, reasons, atr):
        s = self.position.sign
        payload = {
            "producer": "challenger",
            "config_version": self.config.version, "config_hash": self.config_hash,
            "session": bar.session, "side": self.position.side,
            "entry_asof": self.position.entry_asof.isoformat(), "entry_price": self.position.entry_price,
            "asof": bar.available_at.isoformat(), "event_end": bar.event_end.isoformat(),
            "completed_bar_cutoff": bar.event_end.isoformat(),
            "phase": phase, "reasons": reasons,
            "close": bar.close, "atr_prior": atr,
            "favourable_extreme": self._extreme, "favourable_extreme_at": self._extreme_at.isoformat(),
            "move_from_entry": s * (bar.close - self.position.entry_price),
            "horizon_minutes": self.config.horizon_minutes,
            "parent_id": self._parent,
        }
        payload["row_id"] = digest(payload)
        self._parent = payload["row_id"]
        return payload
