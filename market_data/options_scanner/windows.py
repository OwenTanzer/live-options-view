#!/usr/bin/env python3
"""OA-203 multi-session return windows (#130). Pure functions only.

Builds weekly, overnight and rolling two/three-session windows over the
daily OA-203 archive, and measures one contract's return across a window
without stitching contracts, looking ahead, or interpolating endpoints. See
docs/specs/OA203_MULTI_SESSION_WINDOWS.md for the design and the open decisions D1-D9.

**Every D1-D9 choice is an explicit WindowPolicy field.** The defaults are the
design doc's *recommended* values. D1-D5, D7 and bounded D8 are owner-approved.
D6 (rolling 2/3-session windows), D9 (expiration-day PM exit with known
AM-settled roots excluded), the 900 s opening tolerance, and the same-OCC
continuity algorithm are Chej engineering-reviewed definitions for
inert/offline use only (2026-11-09); they are NOT Owen release approval.
Cheapest-entry results stay separately labeled as retrospective hindsight.
The 30-minute quote-side freshness limit is unchanged.

Window kinds (separately named so a hindsight variant never replaces a
fixed-endpoint statistic):

- ``weekly_cheapest`` (W1): cheapest eligible entry observed in the
  early-week sessions (D1, D2) -> the week's last session close (D3, D4).
  Retrospective: the cheapest point is only known after the fact.
- ``weekly_fixed`` (W1b): the week's open endpoint -> the same close exit.
- ``weekly_cheapest_best_exit`` (W1c): W1's entry -> the highest strictly
  later exit through the week's close. Hindsight on both ends.
- ``overnight`` (W2): close endpoint of a session -> open endpoint of the next
  trading session, weekends/holidays included with the gap recorded (D5).
- ``sessions_2`` / ``sessions_3`` (W3): open endpoint of the first session ->
  close endpoint of the last, rolling (D6).

Largest intraday flips (W4) stay the existing per-session ``trough_to_peak``
in oa203_returns.py; no second definition is added here.

Bases, each labeled, none a guaranteed fill:

- ``mid``: two-sided, uncrossed midpoint at both ends.
- ``exec``: ask at entry, bid at exit, uncrossed.
- ``qualified``: ``exec`` where each endpoint passes the existing
  oa203-qualified-ask-bid-v1 endpoint checks (spread, sizes, quote age,
  timestamps) using only that observation's own quote evidence, plus the
  entry premium floor. Intraday prefix checks (samples/coverage so far,
  observed volume) are not applied across sessions; their absence is
  recorded under ``qualification_scope``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_data.options_scanner.returns import (ContractInfo, Observation, QualifiedPolicy,
                           _qualified_endpoint, _relative_spread)

WINDOW_POLICY_VERSION = "oa203-windows-v1"
BASES = ("mid", "exec", "qualified")
KINDS = ("weekly_cheapest", "weekly_fixed", "weekly_cheapest_best_exit",
         "overnight", "sessions_2", "sessions_3")
MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class WindowPolicy:
    """D1-D9 as explicit, versioned parameters (defaults = recommended, provisional)."""

    early_week_sessions: int = 1                # D1: first N trading sessions of the week
    cheapest_entry_qualified: bool = True       # D2: qualified basis requires qualified entries
    weekly_exit: str = "last_session_close"     # D3/D4: close of the week's last trading session
    overnight_across_gaps: bool = True          # D5: Fri->Mon and holiday gaps included
    multi_session_mode: str = "rolling"         # D6
    multi_session_lengths: tuple[int, ...] = (2, 3)
    close_tolerance_s: int = 900                # D7: owner-approved 15 min before close
    open_tolerance_s: int = 900                 # D7: open endpoint within 15 min after open
    allow_expiration_day_exit: bool = True      # D9
    # Roots whose standard series settle AM on expiration day (no PM quotes).
    # Weekly PM-settled roots (SPXW, NDXP, RUTW) are not listed.
    am_settled_roots: frozenset[str] = frozenset({"SPX", "NDX", "RUT", "VIX"})
    qualified: QualifiedPolicy = field(default_factory=QualifiedPolicy)
    decisions_status: str = ("D1-D5_D7_D8_approved; "
                             "D6_D9_opening_tolerance_continuity_chej_engineering_reviewed_offline_only")

    def __post_init__(self) -> None:
        if (type(self.early_week_sessions) is not int or not 1 <= self.early_week_sessions <= 4
                or self.weekly_exit != "last_session_close"
                or self.multi_session_mode != "rolling"
                or any(type(n) is not int or not 2 <= n <= 5 for n in self.multi_session_lengths)
                or not 0 < self.close_tolerance_s <= 3600 or not 0 < self.open_tolerance_s <= 3600):
            raise ValueError("invalid window policy")

    def as_dict(self) -> dict[str, Any]:
        return {"version": WINDOW_POLICY_VERSION, "decisions_status": self.decisions_status,
                "early_week_sessions": self.early_week_sessions,
                "cheapest_entry_qualified": self.cheapest_entry_qualified,
                "weekly_exit": self.weekly_exit, "overnight_across_gaps": self.overnight_across_gaps,
                "multi_session_mode": self.multi_session_mode,
                "multi_session_lengths": list(self.multi_session_lengths),
                "close_tolerance_s": self.close_tolerance_s, "open_tolerance_s": self.open_tolerance_s,
                "allow_expiration_day_exit": self.allow_expiration_day_exit,
                "am_settled_roots": sorted(self.am_settled_roots),
                "qualified_endpoint_policy": self.qualified.as_dict(),
                "qualification_scope": "endpoint quote evidence only; intraday prefix checks not applied"}


@dataclass(frozen=True)
class Session:
    """One exchange session (from the XNYS calendar), times in epoch ms."""

    day: str            # YYYY-MM-DD, exchange date
    open_ms: int
    close_ms: int
    early_close: bool = False

    def __post_init__(self) -> None:
        day = date.fromisoformat(self.day)
        if (type(self.open_ms) is not int or type(self.close_ms) is not int or
                self.open_ms < 0 or self.open_ms >= self.close_ms):
            raise ValueError("invalid session interval")
        if (datetime.fromtimestamp(self.open_ms / 1000, MARKET_TZ).date() != day or
                datetime.fromtimestamp((self.close_ms - 1) / 1000, MARKET_TZ).date() != day):
            raise ValueError("session bounds disagree with exchange date")


@dataclass(frozen=True)
class Window:
    kind: str
    window_id: str
    sessions: tuple[Session, ...]
    entry_sessions: tuple[Session, ...]
    exit_session: Session

    def __post_init__(self) -> None:
        if (not self.sessions or self.exit_session != self.sessions[-1] or
                not self.entry_sessions or self.entry_sessions != self.sessions[:len(self.entry_sessions)] or
                any(a.close_ms > b.open_ms for a, b in zip(self.sessions, self.sessions[1:])) or
                len({s.day for s in self.sessions}) != len(self.sessions)):
            raise ValueError("invalid window session order")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "window_id": self.window_id,
                "sessions": [s.day for s in self.sessions],
                "entry_sessions": [s.day for s in self.entry_sessions],
                "exit_session": self.exit_session.day,
                "early_closes": [s.day for s in self.sessions if s.early_close]}


# -- window construction ---------------------------------------------------


def _iso_week(day: str) -> tuple[int, int]:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return y, w


def weekly_windows(sessions: Sequence[Session], policy: WindowPolicy = WindowPolicy()) -> tuple[list[Window], list[dict]]:
    """W1/W1b/W1c per ISO week. Returns (windows, skipped weeks with reasons).

    The exit is the week's last trading session (a Friday holiday makes it
    Thursday: D4). A week needs more sessions than the early-week entry
    window, or it is skipped, never shortened silently.
    """
    weeks: dict[tuple[int, int], list[Session]] = {}
    for s in sorted(sessions, key=lambda s: s.day):
        weeks.setdefault(_iso_week(s.day), []).append(s)
    windows, skipped = [], []
    for (y, w), days in sorted(weeks.items()):
        wid = f"weekly-{y}-W{w:02d}"
        if len(days) <= policy.early_week_sessions:
            skipped.append({"window_id": wid, "reason": "too_few_sessions", "sessions": [d.day for d in days]})
            continue
        entry = tuple(days[:policy.early_week_sessions])
        for kind in ("weekly_cheapest", "weekly_fixed", "weekly_cheapest_best_exit"):
            windows.append(Window(kind, f"{wid}-{kind.removeprefix('weekly_')}", tuple(days), entry, days[-1]))
    return windows, skipped


def overnight_windows(sessions: Sequence[Session], policy: WindowPolicy = WindowPolicy()) -> list[Window]:
    days = sorted(sessions, key=lambda s: s.day)
    out = []
    for a, b in zip(days, days[1:]):
        gap_days = (date.fromisoformat(b.day) - date.fromisoformat(a.day)).days
        if gap_days > 1 and not policy.overnight_across_gaps:
            continue
        out.append(Window("overnight", f"overnight-{a.day}_{b.day}", (a, b), (a,), b))
    return out


def multi_session_windows(sessions: Sequence[Session], policy: WindowPolicy = WindowPolicy()) -> list[Window]:
    days = sorted(sessions, key=lambda s: s.day)
    out = []
    for n in policy.multi_session_lengths:
        for i in range(len(days) - n + 1):
            span = tuple(days[i:i + n])
            out.append(Window(f"sessions_{n}", f"sessions{n}-{span[0].day}_{span[-1].day}", span, (span[0],), span[-1]))
    return out


# -- endpoint values ------------------------------------------------------


def _finite_pos(v: Any) -> bool:
    return type(v) in (int, float) and math.isfinite(v) and v > 0


def _quote_structure_reasons(obs: Observation, side: str) -> list[str]:
    """Evidence possible at acquisition time, independent of freshness limits."""
    reasons = []
    if type(obs.t) is not int or obs.t < 0:
        reasons.append(f"{side}_observation_time_invalid")
    for name in ("bid", "ask"):
        value = getattr(obs, name)
        if value is None:
            reasons.append(f"{side}_{name}_missing")
        elif not _finite_pos(value):
            reasons.append(f"{side}_{name}_invalid")
    if not any(x.endswith(("_missing", "_invalid")) for x in reasons):
        if obs.ask <= obs.bid:
            reasons.append(f"{side}_{'crossed' if obs.ask < obs.bid else 'locked'}")
    for name in ("bid_ms", "ask_ms"):
        stamp = getattr(obs, name)
        if stamp is None:
            reasons.append(f"{side}_{name}_missing")
        elif type(stamp) is not int or stamp < 0:
            reasons.append(f"{side}_{name}_invalid")
        elif type(obs.t) is int and stamp > obs.t:
            reasons.append(f"{side}_{name}_future")
    return reasons


def entry_value(obs: Observation, basis: str, policy: WindowPolicy) -> tuple[float | None, list[str]]:
    """Entry price for one observation, using only that observation's evidence."""
    if basis not in BASES:
        raise ValueError(f"unknown basis: {basis}")
    structural = _quote_structure_reasons(obs, "entry")
    if structural:
        return None, structural
    if basis == "mid":
        return (obs.mid, []) if _finite_pos(obs.mid) else (None, ["entry_mid_invalid"])
    if basis == "exec":
        return obs.ask, []
    bad, missing = _qualified_endpoint(obs, policy.qualified, "entry")
    if obs.ask < policy.qualified.min_entry_premium:
        bad.append("entry_premium")
    return (obs.ask, []) if not bad and not missing else (None, bad + missing)


def exit_value(obs: Observation, basis: str, policy: WindowPolicy) -> tuple[float | None, list[str]]:
    if basis not in BASES:
        raise ValueError(f"unknown basis: {basis}")
    structural = _quote_structure_reasons(obs, "exit")
    if structural:
        return None, structural
    if basis == "mid":
        return (obs.mid, []) if _finite_pos(obs.mid) else (None, ["exit_mid_invalid"])
    if basis == "exec":
        return obs.bid, []
    bad, missing = _qualified_endpoint(obs, policy.qualified, "exit")
    return (obs.bid, []) if not bad and not missing else (None, bad + missing)


def _in_session(path: Sequence[Observation], s: Session) -> list[Observation]:
    return sorted((o for o in path if type(o.t) is int and s.open_ms <= o.t < s.close_ms), key=lambda o: o.t)


def _invalid_path_time(path: Sequence[Observation]) -> list[str]:
    return ["observation_time_invalid"] if any(type(o.t) is not int or o.t < 0 for o in path) else []


def _first_after_open(path, s: Session, value_fn, basis: str, policy: WindowPolicy):
    """First valid observation within open_tolerance_s after the session open."""
    reasons: list[str] = []
    for o in _in_session(path, s):
        if o.t > s.open_ms + policy.open_tolerance_s * 1000:
            break
        v, why = value_fn(o, basis, policy)
        if v is not None:
            return o, v, []
        reasons += why
    return None, None, ["open_endpoint_missing"] + sorted(set(reasons + _invalid_path_time(path)))


def _last_before_close(path, s: Session, value_fn, basis: str, policy: WindowPolicy):
    """Last valid observation within close_tolerance_s before the session close."""
    reasons: list[str] = []
    for o in reversed(_in_session(path, s)):
        if o.t < s.close_ms - policy.close_tolerance_s * 1000:
            break
        v, why = value_fn(o, basis, policy)
        if v is not None:
            return o, v, []
        reasons += why
    return None, None, ["close_endpoint_missing"] + sorted(set(reasons + _invalid_path_time(path)))


def open_endpoint(path, s: Session, basis: str, policy: WindowPolicy):
    return _first_after_open(path, s, entry_value, basis, policy)


def close_endpoint(path, s: Session, basis: str, policy: WindowPolicy):
    return _last_before_close(path, s, exit_value, basis, policy)


def cheapest_entry(path, sessions: Sequence[Session], basis: str, policy: WindowPolicy):
    """Lowest valid entry price observed inside the entry sessions (retrospective).

    Only observations inside the entry sessions are considered, and each is
    judged on its own quote evidence, so later observations can never change
    which entry is chosen. Ties keep the earliest observation.
    """
    best = None
    reasons: set[str] = set()
    for s in sessions:
        for o in _in_session(path, s):
            v, why = entry_value(o, basis, policy)
            if v is None:
                reasons.update(why)
            elif best is None or v < best[1]:
                best = (o, v)
    if best is None:
        return None, None, ["no_eligible_entry"] + sorted(reasons | set(_invalid_path_time(path)))
    return best[0], best[1], []


def best_later_exit(path, after_t: int, window: Window, basis: str, policy: WindowPolicy):
    best = None
    for o in sorted((o for o in path if type(o.t) is int), key=lambda o: o.t):
        if o.t <= after_t or not any(s.open_ms <= o.t < s.close_ms for s in window.sessions):
            continue
        v, _ = exit_value(o, basis, policy)
        if v is not None and (best is None or v > best[1]):
            best = (o, v)
    return (best[0], best[1], []) if best else (None, None, ["no_strictly_later_exit"] +
                                               _invalid_path_time(path))


# -- evaluation -----------------------------------------------------------


def _root(info: ContractInfo) -> str:
    return info.root or info.symbol[:len(info.symbol) - 15]


def continuity(window: Window, info: ContractInfo, sampled_days: Iterable[str],
               policy: WindowPolicy) -> tuple[list[str], list[str]]:
    """Contract-level exclusions and flags for holding one symbol across the window.

    ``sampled_days`` are the sessions in which *this contract's* chain (its
    underlying and expiry) was sampled -- not merely its underlying, whose
    nearest expiry may have been a different contract that day.
    """
    excluded, flags = [], []
    sampled = set(sampled_days)
    for s in window.sessions:
        if s.day not in sampled:
            excluded.append(f"missing_session:{s.day}")
    exit_day = window.exit_session.day
    if info.expiration < exit_day:
        excluded.append("expires_before_exit")
    elif info.expiration == exit_day:
        if _root(info) in policy.am_settled_roots:
            excluded.append("am_settled_on_exit_day")
        elif not policy.allow_expiration_day_exit:
            excluded.append("exit_on_expiration_day_disallowed")
        else:
            flags.append("exit_on_expiration_day")
    root = _root(info)
    if root != info.underlying and root[-1:].isdigit():
        # Fail closed: an adjusted root (corporate action) means the economic
        # deliverable is unknown from the quote alone. A quote comparison
        # cannot establish that an adjusted deliverable (e.g. fewer shares
        # plus cash) is economically equivalent to the standard 100-share
        # deliverable, so the path cannot become an ordinary
        # continuity-qualified result without deliverable evidence. The
        # contract is EXCLUDED, not deleted: raw observations are preserved
        # in the output with this reason, and the flag stays for visibility.
        excluded.append("adjusted_deliverable_unknown")
        flags.append("adjusted_root_check_deliverable")
    return excluded, flags


def _leg(window: Window, entry, exit_, info: ContractInfo, basis: str) -> dict[str, Any]:
    (eo, ev), (xo, xv) = entry, exit_
    pct = xv / ev - 1.0
    row = {"status": "eligible", "reasons": [], "pct": round(pct, 6), "entry": ev, "exit": xv,
           "abs_change": round(xv - ev, 6), "abs_change_per_contract": round((xv - ev) * info.contract_size, 4),
           "entry_ms": eo.t, "exit_ms": xo.t, "entry_session": None, "exit_session": None,
           "entry_delay_from_boundary_s": None, "exit_lead_before_close_s": None}
    for s in window.sessions:
        if s.open_ms <= eo.t < s.close_ms:
            row["entry_session"] = s.day
            row["entry_delay_from_boundary_s"] = (eo.t - s.open_ms) / 1000
        if s.open_ms <= xo.t < s.close_ms:
            row["exit_session"] = s.day
            row["exit_lead_before_close_s"] = (s.close_ms - xo.t) / 1000
    for side, o in (("entry", eo), ("exit", xo)):
        for name in ("bid", "ask", "bid_ms", "ask_ms", "bid_size", "ask_size", "underlying_price"):
            row[f"{side}_{name}"] = getattr(o, name)
        row[f"{side}_quote_age_s"] = o.quote_age_s
        row[f"{side}_bid_quote_age_s"] = (o.t - o.bid_ms) / 1000 if o.bid_ms is not None else None
        row[f"{side}_ask_quote_age_s"] = (o.t - o.ask_ms) / 1000 if o.ask_ms is not None else None
        row[f"{side}_relative_spread"] = (round(_relative_spread(o), 6)
                                          if _finite_pos(o.bid) and _finite_pos(o.ask) else None)
    return row


def evaluate(window: Window, info: ContractInfo, path: Sequence[Observation],
             sampled_days: Iterable[str], policy: WindowPolicy = WindowPolicy()) -> dict[str, Any]:
    """One contract over one window: identity, continuity, and every basis."""
    excluded, flags = continuity(window, info, sampled_days, policy)
    row: dict[str, Any] = {
        **window.as_dict(), "policy_version": WINDOW_POLICY_VERSION,
        "decisions_status": policy.decisions_status,
        "symbol": info.symbol, "underlying": info.underlying, "root": _root(info),
        "option_type": info.option_type, "strike": info.strike, "expiration": info.expiration,
        "contract_size": info.contract_size, "contract_exclusions": excluded, "flags": flags,
        "hindsight": window.kind in ("weekly_cheapest", "weekly_cheapest_best_exit"),
        "label": "quote comparison over sampled observations; not an executed fill",
    }
    for basis in BASES:
        if excluded:
            row[basis] = {"status": "excluded", "reasons": list(excluded), "pct": None}
            continue
        if window.kind in ("weekly_cheapest", "weekly_cheapest_best_exit"):
            ep = cheapest_entry(path, window.entry_sessions, basis, policy)
        elif window.kind == "overnight":
            ep = _last_before_close(path, window.entry_sessions[0], entry_value, basis, policy)
        else:
            ep = open_endpoint(path, window.entry_sessions[0], basis, policy)
        if ep[0] is None:
            row[basis] = {"status": "excluded", "reasons": ep[2], "pct": None}
            continue
        if window.kind == "weekly_cheapest_best_exit":
            xp = best_later_exit(path, ep[0].t, window, basis, policy)
        elif window.kind == "overnight":
            xp = _first_after_open(path, window.exit_session, exit_value, basis, policy)
        else:
            xp = close_endpoint(path, window.exit_session, basis, policy)
        if xp[0] is None:
            row[basis] = {"status": "excluded", "reasons": xp[2], "pct": None}
        elif xp[0].t <= ep[0].t:
            row[basis] = {"status": "excluded", "reasons": ["exit_not_after_entry"], "pct": None}
        else:
            row[basis] = _leg(window, ep[:2], xp[:2], info, basis)
    return row


# -- ranking --------------------------------------------------------------


def rank_window(rows: Sequence[Mapping[str, Any]], basis: str) -> list[dict[str, Any]]:
    """Rank every eligible path by pct over the full universe; keep all others.

    Zero and negative returns stay ranked; excluded rows keep rank None and
    their reasons. Ties break on symbol for determinism.
    """
    rows = [dict(r) for r in rows]
    eligible = sorted((r for r in rows if r[basis]["status"] == "eligible"),
                      key=lambda r: (-r[basis]["pct"], r["symbol"]))
    for i, r in enumerate(eligible, 1):
        r[f"{basis}_rank"] = i
    rest = [r for r in rows if r[basis]["status"] != "eligible"]
    for r in rest:
        r[f"{basis}_rank"] = None
    return eligible + rest
