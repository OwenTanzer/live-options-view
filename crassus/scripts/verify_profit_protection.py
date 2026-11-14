#!/usr/bin/env python3
"""OA-236 profit protection / re-entry family (inactive): decision table.

    python scripts/verify_profit_protection.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.profit_protection import (  # noqa: E402
    FAMILY, STATES, TRANSITIONS, Observation, ProtectConfig, ProtectionChain, net_pct)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


SYM = "QQQ240102C00400000"
T0 = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
SEEN = []   # (state_from, action[:trigger], state_to) of every decision, for the transition table


def at(i):
    return T0 + timedelta(minutes=5 * i)


def obs(i, phase="continuation", bid=1.0, ask=1.05, imb=0.45, thr=0.3, mtc=200.0, ev=None, *,
        status="ok", event_age_s=0, skew_age_s=0, ask_symbol=SYM, now="auto"):
    """A fresh observation by default: the bar ended at `now`, the skew was read at `now`."""
    t = at(i) if now == "auto" else now
    return Observation(bar_index=i, phase=phase, recovery_event_id=ev, bid=bid, ask=ask, imbalance=imb,
                       imbalance_threshold=thr, minutes_to_close=mtc, now=t, direction_status=status,
                       direction_event_end=None if t is None or event_age_s is None else t - timedelta(seconds=event_age_s),
                       skew_observed_at=None if t is None or skew_age_s is None else t - timedelta(seconds=skew_age_s),
                       ask_symbol=ask_symbol)


class _Recorded(ProtectionChain):
    def evaluate(self, o):
        d = super().evaluate(o)
        SEEN.append((d.metadata["state_from"], d.action + (f":{d.trigger}" if d.trigger else ""),
                     d.metadata["state_to"]))
        return d


def chain(variant="protect_only", basis=1.00, contracts=1, symbol=SYM, **kw):
    return _Recorded("call", contracts, basis, entry_bar=0, original_skew_sign=1,
                     config=ProtectConfig(variant=variant, **kw), contract_symbol=symbol)


# -- arming -------------------------------------------------------------------
c = chain()
d = c.evaluate(obs(1, bid=1.19))
check("below +20% net: not armed, hold", d.action == "hold" and not c.armed, d.reason)
d = c.evaluate(obs(2, bid=1.20))
check("exactly +20% net arms (inclusive, exact)", c.armed and d.metadata.get("armed_now") and d.action == "hold")
check("armed survives a dip", c.evaluate(obs(3, bid=1.10, phase="pullback")).action == "hold" and c.armed)

# Fees count: +20% gross with $1/contract/side is not +20% net.
cf = chain(fee_per_contract_per_side=1.0)
cf.evaluate(obs(1, bid=1.20))
check("fees prevent arming at +20% gross", not cf.armed, float(net_pct(1.0, 1.20, 1.0)))
cf.evaluate(obs(2, bid=1.22))
check("arms once net of fees clears +20%", cf.armed)

# -- intact moves keep their room -----------------------------------------------
c = chain()
c.evaluate(obs(1, bid=1.50))
for i, ph in enumerate(("continuation", "pullback", "recovery", "no_trend"), start=2):
    d = c.evaluate(obs(i, phase=ph, bid=1.30))
    check(f"armed + {ph}: hold", d.action == "hold" and c.held, d.reason)

# -- protection exits ---------------------------------------------------------------
c = chain(contracts=3)
c.evaluate(obs(1, bid=1.50))
d = c.evaluate(obs(2, phase="breakdown", bid=1.30))
check("armed + breakdown + net-profitable: exit all contracts",
      d.action == "exit" and d.contracts == 3 and d.trigger == "direction_breakdown" and not c.held, d.reason)

c = chain()
c.evaluate(obs(1, bid=1.50))
d = c.evaluate(obs(2, phase="continuation", bid=1.05))
check("price floor exactly +5% exits even while continuing", d.action == "exit" and d.trigger == "price_floor")
c = chain()
c.evaluate(obs(1, bid=1.50))
check("one cent above floor holds", c.evaluate(obs(2, bid=1.06)).action == "hold")

c = chain()
c.evaluate(obs(1, bid=1.50))
d = c.evaluate(obs(2, phase="breakdown", bid=0.98))
check("armed but net <= 0: stop-loss domain, no protection exit", d.action == "hold" and c.held, d.reason)

c = chain()
d = c.evaluate(obs(1, phase="breakdown", bid=1.10))
check("breakdown while NOT armed: hold (not this family's)", d.action == "hold")

# -- stale / unknown / missing quotes -------------------------------------------------
for ph in ("stale", "unknown"):
    c = chain()
    c.evaluate(obs(1, bid=1.50))
    d = c.evaluate(obs(2, phase=ph, bid=1.30))
    check(f"armed + {ph}: no direction exit", d.action == "hold" and "stale" in d.reason)
    d = c.evaluate(obs(3, phase=ph, bid=1.04))
    check(f"armed + {ph}: price floor still applies", d.action == "exit" and d.trigger == "price_floor")
c = chain()
c.evaluate(obs(1, bid=1.50))
d = c.evaluate(obs(2, phase="breakdown", bid=None))
check("armed, no executable bid: pending, still held", d.action == "pending" and c.held)
check("not armed, no bid: hold", chain().evaluate(obs(1, bid=None)).action == "hold")


# -- re-entry -------------------------------------------------------------------
def protected_out(variant="protect_reentry", **kw):
    c = chain(variant, **kw)
    c.evaluate(obs(1, bid=1.50))
    assert c.evaluate(obs(2, phase="breakdown", bid=1.30)).action == "exit"
    return c


c = protected_out("protect_only")
check("protect_only never re-enters", c.evaluate(obs(5, phase="recovery", ev="r1")).action == "flat")

c = protected_out()
d = c.evaluate(obs(3, phase="recovery", ev="r1"))
check("recovery one bar after exit: blocked by min bars", d.action == "blocked" and "bars since the exit" in d.reason)
d = c.evaluate(obs(4, phase="continuation"))
check("unchanged valid skew without recovery: blocked (signal alone never re-enters)",
      d.action == "blocked" and "no confirmed recovery" in d.reason)
d = c.evaluate(obs(5, phase="recovery", ev="r1", ask=1.40))
check("confirmed recovery + skew valid + cooldown: re-enter same contracts",
      d.action == "reenter" and d.contracts == 1 and c.held and c.basis == 1.40 and not c.armed, d.reason)
check("re-entry has its own basis (not armed from the old one)",
      c.evaluate(obs(6, bid=1.50)).action == "hold" and not c.armed)

c = protected_out()
check("skew sign flipped: blocked",
      "no longer valid" in c.evaluate(obs(5, phase="recovery", ev="r1", imb=-0.45)).reason)
c = protected_out()
check("skew below threshold: blocked",
      "no longer valid" in c.evaluate(obs(5, phase="recovery", ev="r1", imb=0.29)).reason)
c = protected_out()
check("skew unknown: blocked", "no longer valid" in c.evaluate(obs(5, phase="recovery", ev="r1", imb=None)).reason)
c = protected_out()
check("skew exactly at threshold: allowed (no new flip required)",
      c.evaluate(obs(5, phase="recovery", ev="r1", imb=0.3)).action == "reenter")
c = protected_out()
check("near close: blocked", "of the close" in c.evaluate(obs(5, phase="recovery", ev="r1", mtc=30.0)).reason)
c = protected_out()
check("no executable ask: blocked", "no executable ask" in c.evaluate(obs(5, phase="recovery", ev="r1", ask=None)).reason)
c = protected_out()
check("recovery without event id: blocked", c.evaluate(obs(5, phase="recovery", ev=None)).action == "blocked")

# One recovery event re-enters at most once (anti-churn).
c = protected_out()
c.evaluate(obs(5, phase="recovery", ev="r1"))
c.evaluate(obs(6, bid=1.80))                      # arms on the new basis 1.05 -> +71%
assert c.evaluate(obs(7, phase="breakdown", bid=1.60)).action == "exit"
d = c.evaluate(obs(10, phase="recovery", ev="r1"))
check("same recovery event cannot re-enter twice", d.action == "blocked" and "already used" in d.reason)
check("a new recovery event can", c.evaluate(obs(11, phase="recovery", ev="r2")).action == "reenter")

# Session limits.
c = protected_out(max_reentries_per_session=1)
c.evaluate(obs(5, phase="recovery", ev="r1"))
c.evaluate(obs(6, bid=1.80))
c.evaluate(obs(7, phase="breakdown", bid=1.60))
check("re-entry limit reached: blocked", "re-entry limit" in c.evaluate(obs(10, phase="recovery", ev="r2")).reason)
c = protected_out(max_protect_exits_per_session=1)
check("protection-exit limit reached: blocked",
      "protection-exit limit" in c.evaluate(obs(5, phase="recovery", ev="r1")).reason)

# Someone else's exit ends the chain.
for why in ("stop_loss", "eod_flatten", "signal_exit"):
    c = chain("protect_reentry")
    c.evaluate(obs(1, bid=1.50))
    c.external_exit(why)
    d = c.evaluate(obs(5, phase="recovery", ev="r1"))
    check(f"after {why}: no re-entry", d.action == "flat" and why in d.reason)

# -- contract / config ------------------------------------------------------------
c = chain()
c.evaluate(obs(1))
try:
    c.evaluate(obs(1))
    check("out-of-order bars rejected", False)
except ValueError:
    check("out-of-order bars rejected", True)
for bad in ({"variant": "x"}, {"floor_net_pct": 0.25}, {"floor_net_pct": -0.1}, {"min_bars_after_exit": 0},
            {"max_reentries_per_session": -1}, {"arm_net_pct": float("nan")}, {"fee_per_contract_per_side": -1}):
    try:
        ProtectConfig(**bad)
        check(f"config rejects {bad}", False)
    except ValueError:
        check(f"config rejects {bad}", True)
try:
    Observation(1, "sideways", None, 1.0, 1.0, 0.4, 0.3, 100)
    check("unknown phase rejected", False)
except ValueError:
    check("unknown phase rejected", True)
check("family version recorded", chain().evaluate(obs(1)).metadata["family"] == FAMILY == "oa236-protect-v1.1")
put = _Recorded("put", 1, 2.0, 0, -1, ProtectConfig(variant="protect_reentry"), contract_symbol=SYM)
put.evaluate(obs(1, bid=2.50, imb=-0.5))
put.evaluate(obs(2, phase="breakdown", bid=2.30, imb=-0.5))
check("put chain: original negative skew is the valid one",
      put.evaluate(obs(5, phase="recovery", ev="p1", imb=-0.5)).action == "reenter")

# == v1.1: D1 contract, freshness, contract selection, entry guard, state machine ==

def armed_chain(variant="protect_only", **kw):
    c = chain(variant, **kw)
    assert c.evaluate(obs(1, bid=1.50)).action == "hold" and c.armed
    return c


# D1 status: anything but ok can never be a direction exit; the floor still applies.
for st in ("stale", "unknown", "session_closed"):
    c = armed_chain()
    d = c.evaluate(obs(2, phase=None, status=st, bid=1.30))
    check(f"D1 status {st}: no direction exit", d.action == "hold" and d.metadata["phase_degraded"] == f"direction_status_{st}")
    check(f"D1 status {st}: price floor still applies", c.evaluate(obs(3, phase=None, status=st, bid=1.05)).trigger == "price_floor")
try:
    Observation(1, None, None, 1.0, 1.0, 0.4, 0.3, 100, now=T0, direction_status="ok", direction_event_end=T0)
    check("status ok without a phase rejected", False)
except ValueError:
    check("status ok without a phase rejected", True)

# Event-time freshness (receipt-fresh old bars are stale here).
c = armed_chain()
check("breakdown from a bar that ended exactly 360 s ago exits",
      c.evaluate(obs(2, phase="breakdown", bid=1.30, event_age_s=360)).trigger == "direction_breakdown")
c = armed_chain()
d = c.evaluate(obs(2, phase="breakdown", bid=1.30, event_age_s=361))
check("breakdown from a bar 361 s old: stale, no direction exit",
      d.action == "hold" and d.metadata["phase"] == "stale" and "direction_event_age_361s" in d.metadata["phase_degraded"])
c = armed_chain()
d = c.evaluate(obs(2, phase="breakdown", bid=1.30, event_age_s=-1))
check("event_end after now: unknown", d.action == "hold" and d.metadata["phase_degraded"] == "direction_event_end_after_now")
c = armed_chain()
d = c.evaluate(obs(2, phase="breakdown", bid=1.30, event_age_s=None))
check("event time not supplied: unknown", d.action == "hold" and d.metadata["phase_degraded"] == "direction_event_time_not_supplied")
c = armed_chain()
d = c.evaluate(obs(2, phase="breakdown", bid=1.30, now=None))
check("no decision instant: unknown", d.action == "hold" and d.metadata["phase"] == "unknown")
try:
    Observation(1, "continuation", None, 1.0, 1.0, 0.4, 0.3, 100, now=T0.replace(tzinfo=None))
    check("naive timestamps rejected", False)
except ValueError:
    check("naive timestamps rejected", True)

# Re-entry needs a FRESH recovery and a FRESH valid skew.
c = protected_out()
d = c.evaluate(obs(5, phase="recovery", ev="r1", event_age_s=361))
check("old recovery bar cannot re-enter", d.action == "blocked" and "no confirmed recovery (phase stale" in d.reason)
c = protected_out()
check("skew read exactly 360 s ago: allowed",
      c.evaluate(obs(5, phase="recovery", ev="r1", skew_age_s=360)).action == "reenter")
for age, why in ((361, "exceeds 360s"), (-1, "observed after now"), (None, "not supplied")):
    c = protected_out()
    d = c.evaluate(obs(5, phase="recovery", ev="r1", skew_age_s=age))
    check(f"skew reading age {age}: blocked", d.action == "blocked" and why in d.reason, d.reason)

# Contract selection: re-entry buys the exited symbol, nothing else.
c = protected_out()
d = c.evaluate(obs(5, phase="recovery", ev="r1", ask_symbol="QQQ240102C00401000"))
check("ask for another strike: blocked", d.action == "blocked" and "not the exited contract" in d.reason)
c = protected_out(symbol=None)
check("unknown contract symbol: blocked",
      "re-entry contract unknown" in c.evaluate(obs(5, phase="recovery", ev="r1")).reason)
c = protected_out()
d = c.evaluate(obs(5, phase="recovery", ev="r1"))
check("re-entry records the contract", d.action == "reenter" and d.metadata["contract_symbol"] == SYM)
try:
    ProtectConfig(reentry_contract="nearest_atm")
    check("only same_symbol re-entry is declared", False)
except ValueError:
    check("only same_symbol re-entry is declared", True)

# Entry guard: an unchanged signal can never undo a protection or stop exit.
def guard(c, i, side="call", imb=0.45, age=0):
    return c.strategy_entry_allowed(side, imb, 0.3, at(i) - timedelta(seconds=age), at(i))

c = chain("protect_reentry")
check("guard: holding is not an entry", guard(c, 1)[0])
c = protected_out()
ok, why = guard(c, 3)
check("guard: unchanged signal after a protection exit is blocked", not ok and "unchanged signal" in why)
c.evaluate(obs(3, phase="continuation"))
c.evaluate(obs(4, phase="continuation"))
check("guard: still blocked while the skew stays valid", not guard(c, 4)[0])
c.evaluate(obs(5, phase="continuation", imb=0.45, skew_age_s=900))
check("guard: a stale reading is not a reset", not c.skew_invalidated_since_exit and not guard(c, 5)[0])
c.evaluate(obs(6, phase="continuation", imb=0.10))
check("guard: a fresh sub-threshold reading resets the signal", c.skew_invalidated_since_exit)
check("guard: reset but currently invalid: still blocked", not guard(c, 6, imb=0.10)[0])
check("guard: a new valid signal after the reset is allowed", guard(c, 7, imb=0.45)[0])
check("guard: a new signal must itself be fresh", not guard(c, 7, imb=0.45, age=400)[0])
c = protected_out()
check("guard: the opposite side is a new flip", guard(c, 3, side="put", imb=-0.5)[0])
for kind, allowed in (("stop_loss", False), ("eod_flatten", False), ("signal_exit", True)):
    c = chain("protect_reentry")
    c.evaluate(obs(1, bid=1.50))
    c.external_exit(kind)
    check(f"guard after {kind}: same-side unchanged signal {'allowed' if allowed else 'blocked'}",
          guard(c, 2)[0] is allowed)
c = chain("protect_reentry")
c.external_exit("stop_loss")
c.evaluate(obs(3, imb=-0.40))
check("guard: a stop is undone only by a new signal (reset then valid)", guard(c, 4)[0])
try:
    chain().external_exit("protective_stop")
    check("external exit kinds are explicit", False)
except ValueError:
    check("external exit kinds are explicit", True)

# Remaining transitions not reached above.
c = chain()
c.evaluate(obs(1, bid=1.20, phase="breakdown"))
check("arming and breakdown on the same bar exit (v1 behaviour kept)", c.state == "flat_protected")
c = armed_chain()
c.evaluate(obs(2, bid=0.90))
check("armed below basis stays armed (stop-loss domain)", c.state == "armed")

# The state machine: every evaluated transition is a declared row, and every row is reached.
declared = {(f, a, t) for f, _, t, a in TRANSITIONS if f in STATES}
seen = set(SEEN)
check("every observed transition is declared", seen <= declared, sorted(seen - declared))
check("every declared transition is exercised", declared <= seen, sorted(declared - seen))
c = chain("protect_reentry")
states = [c.state]
c.evaluate(obs(1, bid=1.50)); states.append(c.state)
c.evaluate(obs(2, phase="breakdown", bid=1.30)); states.append(c.state)
c.external_exit("eod_flatten"); states.append(c.state)
check("external exit ends the chain from any state", states == ["held", "armed", "flat_protected", "ended"], states)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
