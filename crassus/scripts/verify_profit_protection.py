#!/usr/bin/env python3
"""OA-236 profit protection / re-entry family (inactive): decision table.

    python scripts/verify_profit_protection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.profit_protection import (  # noqa: E402
    FAMILY, Observation, ProtectConfig, ProtectionChain, net_pct)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


def obs(i, phase="continuation", bid=1.0, ask=1.05, imb=0.45, thr=0.3, mtc=200.0, ev=None):
    return Observation(bar_index=i, phase=phase, recovery_event_id=ev, bid=bid, ask=ask, imbalance=imb,
                       imbalance_threshold=thr, minutes_to_close=mtc)


def chain(variant="protect_only", basis=1.00, contracts=1, **kw):
    return ProtectionChain("call", contracts, basis, entry_bar=0, original_skew_sign=1,
                           config=ProtectConfig(variant=variant, **kw))


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
for why in ("protective_stop", "eod_flatten", "signal_sell"):
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
check("family version recorded", chain().evaluate(obs(1)).metadata["family"] == FAMILY == "oa236-protect-v1")
put = ProtectionChain("put", 1, 2.0, 0, -1, ProtectConfig(variant="protect_reentry"))
put.evaluate(obs(1, bid=2.50, imb=-0.5))
put.evaluate(obs(2, phase="breakdown", bid=2.30, imb=-0.5))
check("put chain: original negative skew is the valid one",
      put.evaluate(obs(5, phase="recovery", ev="p1", imb=-0.5)).action == "reenter")

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
