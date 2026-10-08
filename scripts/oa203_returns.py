#!/usr/bin/env python3
"""OA-203 return definitions for the daily options-return scanner.

Pure functions only: no network, no storage. ``oa203_return_scanner.py``
archives every sampled chain row; this module turns one contract's archived
path into explicit, chronological return measurements, and ranks contracts.
The same code builds the live end-of-day leaderboard and any offline rebuild
from the archive, so a published ranking can always be reproduced.

Every return is chronological: the exit observation is strictly later than
the entry observation. An unordered daily high/low ratio is never used.

Bases (each labeled on every output row; none implies a guaranteed fill):

- ``mid``: descriptive midpoint, (bid + ask) / 2, from quotes that are
  two-sided (bid > 0, ask > 0) and not crossed (ask >= bid).
- ``exec``: ask-entry / bid-exit comparison. Entry needs ask > 0, exit needs
  bid > 0, and the quote must not be crossed.
- ``trade_1min`` (after-close backfill only): Tradier 1-minute trade bars.
  Entry is the first bar's open (the session's first trade). Exits use bar
  highs. A trough is a bar low, and only highs from strictly later bars can
  pair with it, because the order of high and low inside one bar is unknown.

Measurements for each basis:

- ``first_to_max``: entry at the first valid regular-session observation,
  exit at the highest strictly later observation. This is the proposed
  headline leaderboard metric.
- ``open_to_close``: first valid observation to last valid observation.
- ``trough_to_peak``: the largest gain from any observation to any strictly
  later one (running minimum of entry values).

Flags never remove a contract. They are reported beside the return, and the
"clean" view simply excludes flagged rows:

- ``tiny_entry``: entry price below ``min_entry_premium``.
- ``entry_stale`` / ``exit_stale``: the older side of that quote last
  changed more than ``stale_quote_s`` before the sample was taken.
- ``exit_isolated_spike`` / ``entry_isolated_dip``: the endpoint differs from
  both valid neighbours by more than ``spike_ratio`` (a lone suspect print).
- ``low_coverage``: fewer valid observations than ``min_coverage`` of the
  sweeps in which the contract's chain was fetched successfully.
- ``crossed_quotes_seen``: at least one crossed quote in the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Sequence

POLICY_VERSION = "oa203-returns-v1"
QUALIFIED_VERSION = "oa203-qualified-ask-bid-v1"
QUALIFIED = "qualified_first_ask_to_later_bid"


@dataclass(frozen=True)
class QualifiedPolicy:
    """Provisional contract screen. Prices are USD/share; age is seconds."""

    min_entry_premium: float = 0.10
    max_relative_spread: float = 0.30  # (ask - bid) / midpoint, at each endpoint
    max_quote_age_s: float = 1800.0
    min_ask_size: int = 1
    min_bid_size: int = 1
    min_entry_volume: int = 1  # cumulative volume observed at entry, not day volume
    min_samples: int = 3
    min_coverage: float = 0.5
    min_open_interest: int | None = None  # lagged OI; disabled by default

    def __post_init__(self) -> None:
        finite = lambda value: (type(value) in (int, float) and math.isfinite(value))
        integer = lambda value: type(value) is int
        if (not finite(self.min_entry_premium) or self.min_entry_premium <= 0 or
            not finite(self.max_relative_spread) or not 0 < self.max_relative_spread < 2 or
            not finite(self.max_quote_age_s) or self.max_quote_age_s < 0 or
            not integer(self.min_ask_size) or self.min_ask_size < 1 or
            not integer(self.min_bid_size) or self.min_bid_size < 1 or
            not integer(self.min_entry_volume) or self.min_entry_volume < 0 or
            not integer(self.min_samples) or self.min_samples < 2 or
            not finite(self.min_coverage) or not 0 < self.min_coverage <= 1 or
            (self.min_open_interest is not None and
             (not integer(self.min_open_interest) or self.min_open_interest < 0))):
            raise ValueError("invalid qualified ask-bid policy")

    def as_dict(self) -> dict[str, Any]:
        return {"version": QUALIFIED_VERSION, **self.__dict__}


@dataclass(frozen=True)
class ReturnPolicy:
    min_entry_premium: float = 0.05
    stale_quote_s: float = 1800.0
    spike_ratio: float = 3.0
    min_coverage: float = 0.5

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "min_entry_premium": self.min_entry_premium,
            "stale_quote_s": self.stale_quote_s,
            "spike_ratio": self.spike_ratio,
            "min_coverage": self.min_coverage,
        }


@dataclass(frozen=True)
class Observation:
    """One archived sample of one contract (times in epoch milliseconds)."""

    t: int
    bid: float | None
    ask: float | None
    bid_ms: int | None = None
    ask_ms: int | None = None
    underlying_price: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    chain_ok_sweeps_so_far: int | None = None

    @property
    def crossed(self) -> bool:
        return (self.bid is not None and self.ask is not None
                and self.bid > 0 and self.ask > 0 and self.ask < self.bid)

    @property
    def two_sided(self) -> bool:
        return (self.bid is not None and self.ask is not None
                and self.bid > 0 and self.ask > 0 and self.ask >= self.bid)

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.two_sided else None

    @property
    def quote_age_s(self) -> float | None:
        stamps = [s for s in (self.bid_ms, self.ask_ms) if s]
        if not stamps:
            return None
        return max(0.0, (self.t - min(stamps)) / 1000.0)


@dataclass
class Leg:
    """One chronological return measurement."""

    entry_index: int
    exit_index: int
    entry_value: float
    exit_value: float

    @property
    def pct(self) -> float:
        return self.exit_value / self.entry_value - 1.0

    @property
    def abs_change(self) -> float:
        return self.exit_value - self.entry_value


# --------------------------------------------------------------------------
# Generic chronological measurements over (entry_values, exit_values).
# ``None`` marks an observation that is not valid for that side.
# --------------------------------------------------------------------------

def first_to_max(entry: Sequence[float | None], exit_: Sequence[float | None]) -> Leg | None:
    i = next((k for k, v in enumerate(entry) if v is not None and v > 0), None)
    if i is None:
        return None
    best: int | None = None
    for j in range(i + 1, len(exit_)):
        v = exit_[j]
        if v is not None and (best is None or v > exit_[best]):
            best = j
    if best is None:
        return None
    return Leg(i, best, entry[i], exit_[best])


def open_to_close(entry: Sequence[float | None], exit_: Sequence[float | None]) -> Leg | None:
    i = next((k for k, v in enumerate(entry) if v is not None and v > 0), None)
    if i is None:
        return None
    j = next((k for k in range(len(exit_) - 1, i, -1) if exit_[k] is not None), None)
    if j is None:
        return None
    return Leg(i, j, entry[i], exit_[j])


def trough_to_peak(entry: Sequence[float | None], exit_: Sequence[float | None]) -> Leg | None:
    best: Leg | None = None
    low: int | None = None
    for j in range(len(entry)):
        # Pair exit j only with an entry strictly before it.
        if low is not None and exit_[j] is not None:
            pct = exit_[j] / entry[low] - 1.0
            if best is None or pct > best.pct:
                best = Leg(low, j, entry[low], exit_[j])
        v = entry[j]
        if v is not None and v > 0 and (low is None or v < entry[low]):
            low = j
    return best


def _isolated(values: Sequence[float | None], index: int, ratio: float, *, high: bool) -> bool:
    """True when values[index] is a lone outlier against its valid neighbours."""
    v = values[index]
    if v is None:
        return False
    prev = next((values[k] for k in range(index - 1, -1, -1) if values[k] is not None), None)
    nxt = next((values[k] for k in range(index + 1, len(values)) if values[k] is not None), None)
    neighbours = [n for n in (prev, nxt) if n is not None and n > 0]
    if not neighbours:
        return False
    if high:
        return v > ratio * max(neighbours)
    return v * ratio < min(neighbours)


# --------------------------------------------------------------------------
# Quote-path contract metrics
# --------------------------------------------------------------------------

@dataclass
class ContractInfo:
    symbol: str
    underlying: str
    option_type: str
    strike: float
    expiration: str
    root: str | None = None
    contract_size: int = 100
    volume: int | None = None
    open_interest: int | None = None
    chain_ok_sweeps: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _series(path: Sequence[Observation], basis: str) -> tuple[list[float | None], list[float | None]]:
    if basis == "mid":
        mids = [o.mid for o in path]
        return mids, mids
    if basis == "exec":
        entry = [o.ask if (o.ask and o.ask > 0 and not o.crossed) else None for o in path]
        exit_ = [o.bid if (o.bid and o.bid > 0 and not o.crossed) else None for o in path]
        return entry, exit_
    raise ValueError(f"unknown basis {basis!r}")


def _leg_row(prefix: str, leg: Leg | None, path: Sequence[Observation], info: ContractInfo,
             policy: ReturnPolicy, entry_vals: Sequence[float | None],
             exit_vals: Sequence[float | None], spike_check: bool) -> dict[str, Any]:
    if leg is None:
        return {f"{prefix}_pct": None}
    entry_obs, exit_obs = path[leg.entry_index], path[leg.exit_index]
    flags = []
    if leg.entry_value < policy.min_entry_premium:
        flags.append("tiny_entry")
    for name, obs in (("entry", entry_obs), ("exit", exit_obs)):
        age = obs.quote_age_s
        if age is not None and age > policy.stale_quote_s:
            flags.append(f"{name}_stale")
    if spike_check:
        if _isolated(exit_vals, leg.exit_index, policy.spike_ratio, high=True):
            flags.append("exit_isolated_spike")
        if _isolated(entry_vals, leg.entry_index, policy.spike_ratio, high=False):
            flags.append("entry_isolated_dip")
    return {
        f"{prefix}_pct": round(leg.pct, 6),
        f"{prefix}_entry": leg.entry_value,
        f"{prefix}_exit": leg.exit_value,
        f"{prefix}_abs_change": round(leg.abs_change, 6),
        f"{prefix}_abs_change_per_contract": round(leg.abs_change * info.contract_size, 4),
        f"{prefix}_entry_ms": entry_obs.t,
        f"{prefix}_exit_ms": exit_obs.t,
        f"{prefix}_entry_quote_age_s": entry_obs.quote_age_s,
        f"{prefix}_exit_quote_age_s": exit_obs.quote_age_s,
        f"{prefix}_entry_spread": (None if entry_obs.bid is None or entry_obs.ask is None
                                   else round(entry_obs.ask - entry_obs.bid, 6)),
        f"{prefix}_flags": flags,
    }


def contract_metrics(info: ContractInfo, path: Sequence[Observation],
                     policy: ReturnPolicy = ReturnPolicy(),
                     qualified_policy: QualifiedPolicy = QualifiedPolicy()) -> dict[str, Any]:
    """All quote-path measurements for one contract, as one flat output row."""
    path = sorted(path, key=lambda o: o.t)
    valid = [o for o in path if o.two_sided]
    spreads = sorted((o.ask - o.bid) / o.mid for o in valid if o.mid)
    row: dict[str, Any] = {
        "symbol": info.symbol,
        "underlying": info.underlying,
        "root": info.root,
        "option_type": info.option_type,
        "strike": info.strike,
        "expiration": info.expiration,
        "contract_size": info.contract_size,
        "samples": len(path),
        "valid_samples": len(valid),
        "chain_ok_sweeps": info.chain_ok_sweeps,
        "crossed_samples": sum(1 for o in path if o.crossed),
        "median_spread_pct": round(spreads[len(spreads) // 2], 6) if spreads else None,
        "day_volume": info.volume,
        "open_interest": info.open_interest,
    }
    contract_flags = []
    denominator = info.chain_ok_sweeps or len(path)
    if denominator and len(valid) / denominator < policy.min_coverage:
        contract_flags.append("low_coverage")
    if row["crossed_samples"]:
        contract_flags.append("crossed_quotes_seen")
    row["contract_flags"] = contract_flags

    for basis in ("mid", "exec"):
        entry_vals, exit_vals = _series(path, basis)
        for name, fn in (("first_to_max", first_to_max), ("open_to_close", open_to_close),
                         ("trough_to_peak", trough_to_peak)):
            leg = fn(entry_vals, exit_vals)
            row.update(_leg_row(f"{basis}_{name}", leg, path, info, policy,
                                entry_vals, exit_vals, spike_check=(basis == "mid")))

    # Strike relative to spot at the headline entry (mid first_to_max).
    entry_ms = row.get("mid_first_to_max_entry_ms")
    spot = next((o.underlying_price for o in path if o.t == entry_ms and o.underlying_price), None)
    row["spot_at_entry"] = spot
    row["strike_vs_spot_pct"] = round(info.strike / spot - 1.0, 6) if spot else None
    row["clean"] = not contract_flags and not row.get("mid_first_to_max_flags")
    row.update(qualified_metrics(info, path, qualified_policy))
    return row


def _qualified_endpoint(obs: Observation, policy: QualifiedPolicy, side: str) -> tuple[list[str], list[str]]:
    """Return definite exclusions and missing-evidence reasons separately."""
    excluded, unknown = [], []
    for name, value in (("bid", obs.bid), ("ask", obs.ask)):
        if value is None:
            unknown.append(f"{side}_{name}_missing")
        elif not isinstance(value, (int, float)) or not math.isfinite(value):
            excluded.append(f"{side}_{name}_nonfinite")
        elif value <= 0:
            excluded.append(f"{side}_{name}_nonpositive")
    if not excluded and not unknown:
        if obs.ask <= obs.bid:
            excluded.append(f"{side}_{'crossed' if obs.ask < obs.bid else 'locked'}")
        elif _relative_spread(obs) > policy.max_relative_spread:
            excluded.append(f"{side}_spread")
    for name, stamp in (("bid", obs.bid_ms), ("ask", obs.ask_ms)):
        if stamp is None or not isinstance(stamp, int):
            unknown.append(f"{side}_{name}_timestamp_missing")
        elif stamp > obs.t:
            excluded.append(f"{side}_{name}_timestamp_future")
        elif (obs.t - stamp) / 1000 > policy.max_quote_age_s:
            excluded.append(f"{side}_{name}_timestamp_stale")
    size_name = "ask_size" if side == "entry" else "bid_size"
    size = getattr(obs, size_name)
    if size is None:
        unknown.append(f"{side}_{size_name}_missing")
    elif not isinstance(size, int) or size < (policy.min_ask_size if side == "entry" else policy.min_bid_size):
        excluded.append(f"{side}_{size_name}_insufficient")
    return excluded, unknown


def _relative_spread(obs: Observation) -> float:
    ratio = obs.bid / obs.ask
    return 2 * (1 - ratio) / (1 + ratio)


def qualified_metrics(info: ContractInfo, path: Sequence[Observation],
                      policy: QualifiedPolicy = QualifiedPolicy()) -> dict[str, Any]:
    """First qualifying sampled ask to highest strictly later qualified bid."""
    prefix = QUALIFIED
    row: dict[str, Any] = {f"{prefix}_pct": None, f"{prefix}_rank": None,
                           f"{prefix}_policy_version": QUALIFIED_VERSION,
                           f"{prefix}_status": "excluded", f"{prefix}_reasons": []}
    excluded, unknown = set(), set()
    contract_excluded, contract_unknown = set(), set()
    if policy.min_open_interest is not None:
        if info.open_interest is None:
            contract_unknown.add("open_interest_missing_lagged")
        elif info.open_interest < policy.min_open_interest:
            contract_excluded.add("open_interest_insufficient_lagged")
    entry_index = None
    plausible_entry = False
    for i, obs in enumerate(path):
        bad, missing = _qualified_endpoint(obs, policy, "entry")
        if obs.ask is not None and isinstance(obs.ask, (int, float)) and math.isfinite(obs.ask) and obs.ask < policy.min_entry_premium:
            bad.append("entry_premium")
        if obs.volume is None:
            missing.append("entry_volume_missing")
        elif not isinstance(obs.volume, int) or obs.volume < policy.min_entry_volume:
            bad.append("entry_volume_insufficient")
        # Only prefix evidence can delay entry. Completed-day counts never
        # retroactively make an earlier sampled ask qualify.
        if i + 1 < policy.min_samples:
            bad.append("entry_insufficient_samples_so_far")
        sweeps_so_far = obs.chain_ok_sweeps_so_far
        if sweeps_so_far is None or sweeps_so_far <= 0:
            missing.append("entry_chain_coverage_missing")
        elif (i + 1) / sweeps_so_far < policy.min_coverage:
            bad.append("entry_insufficient_coverage_so_far")
        excluded.update(bad); unknown.update(missing)
        plausible_entry |= not bad
        if not bad and not missing:
            entry_index = i
            break
    best = None
    plausible_exit = False
    if entry_index is not None:
        for j in range(entry_index + 1, len(path)):
            if path[j].t <= path[entry_index].t:
                continue
            bad, missing = _qualified_endpoint(path[j], policy, "exit")
            excluded.update(bad); unknown.update(missing)
            plausible_exit |= not bad
            if not bad and not missing and (best is None or path[j].bid > path[best].bid):
                best = j
    if best is not None and not contract_excluded and not contract_unknown:
        entry, exit_ = path[entry_index], path[best]
        pct = exit_.bid / entry.ask - 1
        if not math.isfinite(pct):
            contract_excluded.add("return_nonfinite")
        else:
            contract_change = (exit_.bid - entry.ask) * info.contract_size
            row.update({f"{prefix}_pct": round(pct, 6),
                        f"{prefix}_status": "eligible", f"{prefix}_reasons": [],
                        f"{prefix}_entry_delay_s": (entry.t - path[0].t) / 1000,
                        f"{prefix}_entry_ms": entry.t, f"{prefix}_exit_ms": exit_.t,
                        f"{prefix}_entry": entry.ask, f"{prefix}_exit": exit_.bid,
                        f"{prefix}_abs_change_per_contract": (round(contract_change, 4)
                                                                if math.isfinite(contract_change) else None),
                        f"{prefix}_entry_observed_volume": entry.volume})
            for side, obs in (("entry", entry), ("exit", exit_)):
                for name in ("bid", "ask", "bid_ms", "ask_ms", "bid_size", "ask_size"):
                    row[f"{prefix}_{side}_{name}"] = getattr(obs, name)
                row[f"{prefix}_{side}_spread"] = round(obs.ask - obs.bid, 6)
                row[f"{prefix}_{side}_relative_spread"] = round(_relative_spread(obs), 6)
            return row
    if entry_index is None:
        excluded.add("no_qualified_entry")
    elif best is None:
        excluded.add("no_strictly_later_qualified_exit")
    possible = (plausible_entry if entry_index is None else
                plausible_exit if best is None else True)
    row[f"{prefix}_status"] = ("unknown" if not contract_excluded and possible and
                                (unknown or contract_unknown) else "excluded")
    row[f"{prefix}_reasons"] = sorted(excluded | unknown | contract_excluded | contract_unknown)
    return row


# --------------------------------------------------------------------------
# Trade-bar (after-close backfill) metrics
# --------------------------------------------------------------------------

def trade_bar_metrics(bars: Sequence[dict[str, Any]], contract_size: int = 100,
                      policy: ReturnPolicy = ReturnPolicy()) -> dict[str, Any]:
    """Chronological trade-basis returns from Tradier 1-minute bars."""
    bars = sorted((b for b in bars if b.get("open") is not None), key=lambda b: b["timestamp"])
    out: dict[str, Any] = {"trade_bars": len(bars),
                           "trade_volume": sum(int(b.get("volume") or 0) for b in bars)}
    if not bars:
        out["trade_1min_first_to_max_pct"] = None
        out["trade_1min_trough_to_peak_pct"] = None
        return out
    entry = float(bars[0]["open"])
    # The first bar's own high is at or after its open, so it may be the exit.
    best_j = max(range(len(bars)), key=lambda j: float(bars[j]["high"]))
    exit_ = float(bars[best_j]["high"])
    flags = ["tiny_entry"] if entry < policy.min_entry_premium else []
    if len(bars) < 3:
        flags.append("few_trade_bars")
    out.update({
        "trade_1min_first_to_max_pct": round(exit_ / entry - 1.0, 6) if entry > 0 else None,
        "trade_1min_first_to_max_entry": entry,
        "trade_1min_first_to_max_exit": exit_,
        "trade_1min_first_to_max_entry_s": bars[0]["timestamp"],
        "trade_1min_first_to_max_exit_s": bars[best_j]["timestamp"],
        "trade_1min_first_to_max_abs_change_per_contract": round((exit_ - entry) * contract_size, 4),
        "trade_1min_first_to_max_flags": flags,
    })
    lows = [float(b["low"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    # Entry values are bar lows, exits are highs; trough_to_peak pairs exit j
    # only with entries at indexes < j, i.e. strictly later bars.
    leg = trough_to_peak(lows, highs)
    if leg is not None and leg.entry_value > 0:
        out.update({
            "trade_1min_trough_to_peak_pct": round(leg.pct, 6),
            "trade_1min_trough_to_peak_entry": leg.entry_value,
            "trade_1min_trough_to_peak_exit": leg.exit_value,
            "trade_1min_trough_to_peak_entry_s": bars[leg.entry_index]["timestamp"],
            "trade_1min_trough_to_peak_exit_s": bars[leg.exit_index]["timestamp"],
        })
    else:
        out["trade_1min_trough_to_peak_pct"] = None
    return out


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

HEADLINE = "mid_first_to_max_pct"


def rank(rows: Iterable[dict[str, Any]], metric: str = HEADLINE) -> list[dict[str, Any]]:
    """Rank every row with a defined metric; rows without one keep rank None.

    Adds ``rank`` (all contracts), ``rank_in_type`` (within calls or puts),
    ``clean_rank`` and ``clean_rank_in_type`` (rows with ``clean`` true).
    Nothing is dropped: non-winners and undefined rows stay in the output.
    """
    rows = list(rows)
    ordered = sorted((r for r in rows if r.get(metric) is not None),
                     key=lambda r: (-r[metric], r["symbol"]))
    counters: dict[str, int] = {}
    for r in rows:
        r["rank"] = r["rank_in_type"] = r["clean_rank"] = r["clean_rank_in_type"] = None
    for r in ordered:
        for key, scope in (("rank", "all"), ("rank_in_type", r["option_type"])):
            counters[key + scope] = counters.get(key + scope, 0) + 1
            r[key] = counters[key + scope]
        if r.get("clean"):
            for key, scope in (("clean_rank", "all"), ("clean_rank_in_type", r["option_type"])):
                counters[key + scope] = counters.get(key + scope, 0) + 1
                r[key] = counters[key + scope]
    ranked = ordered + [r for r in rows if r.get(metric) is None]
    qualified = sorted((r for r in rows if r.get(f"{QUALIFIED}_status") == "eligible"),
                       key=lambda r: (-r[f"{QUALIFIED}_pct"], r["symbol"]))
    for position, row in enumerate(qualified, 1):
        row[f"{QUALIFIED}_rank"] = position
    return ranked
