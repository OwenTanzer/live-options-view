#!/usr/bin/env python3
"""OI skew decision provenance for the read-only meter projection.

`oi_skew_qqq` metadata now carries `session_key`, `anchor_observed_at` and the
effective `params`; `decision_base` carries `code_revision`. These are
additive: decisions and reasons are unchanged (tests/oi_skew_meter_parity.py
pins them against the real strategy), and these checks prove the provenance
itself: anchor identity, restart reset, date rollover, overrides, and an
explicit null revision when the deploy does not provide one.

    python scripts/verify_oi_skew_provenance.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.runner import decision_base  # noqa: E402
from crassus.strategies import oi_skew as oisk  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


def rows(call_oi, put_oi):
    out = []
    for i, strike in enumerate((393.0, 397.0, 400.0, 403.0, 407.0)):
        code = f"{int(strike * 1000):08d}"
        for kind, letter, oi in (("call", "C", call_oi), ("put", "P", put_oi)):
            out.append({"OptionSymbol": f"QQQ240101{letter}{code}", "Strike": strike, "Type": kind,
                        "Bid": 1.0, "Ask": 1.1, "OpenInterest": oi})
    return out


def ctx(ts, call_oi, put_oi, params=None):
    snap = MarketSnapshot.from_payload(url="test://", raw=b"{}", payload={
        "timestamp": ts, "snapshot_time": ts, "expiration": "2024-01-01",
        "underlying_price": 400.0, "rows": rows(call_oi, put_oi)})
    q = lambda syms: {s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00",
                               server_ts="2024-01-01T15:00:02") for s in syms}
    return StrategyContext(snapshot=snap, account_state={}, book=Book([]), now_et=None,
                           session_phase="open", quotes=q, params=params or {})


tracker = oisk.SessionImbalanceTracker()
d1 = oisk._decide_core(ctx("2024-01-01T14:00:00+00:00", 100, 100), tracker)
d2 = oisk._decide_core(ctx("2024-01-01T15:00:00+00:00", 400, 100), tracker)
m1, m2 = d1.metadata, d2.metadata
check("session_key is the UTC date", m1["session_key"] == "2024-01-01", m1.get("session_key"))
check("anchor_observed_at is the first reading's snapshot time",
      m1["anchor_observed_at"] == m2["anchor_observed_at"] == "2024-01-01T14:00:00+00:00", m2.get("anchor_observed_at"))
check("effective default params recorded",
      m2["params"] == {"near_money_pct": 0.02, "imbalance_threshold": 0.3,
                       "min_change_from_session_start": 0.1, "min_band_strikes": 4}, m2.get("params"))
check("decision itself unchanged (bullish buy on moved skew)", d2.action == "buy" and "bullish" in d2.reason)

# A runner restart = a fresh tracker mid-session: a new anchor, same session key.
restarted = oisk.SessionImbalanceTracker()
d3 = oisk._decide_core(ctx("2024-01-01T16:00:00+00:00", 400, 100), restarted)
check("restart resets the anchor time", d3.metadata["anchor_observed_at"] == "2024-01-01T16:00:00+00:00")
check("restart keeps the session key", d3.metadata["session_key"] == "2024-01-01")
check("restart anchor makes delta 0 -> neutral (no trade)", d3.action == "no_trade")

# Date rollover resets both.
d4 = oisk._decide_core(ctx("2024-01-02T14:00:00+00:00", 100, 100), tracker)
check("rollover changes session_key", d4.metadata["session_key"] == "2024-01-02")
check("rollover re-anchors", d4.metadata["anchor_observed_at"] == "2024-01-02T14:00:00+00:00")

# Overrides are what is recorded, not the defaults.
d5 = oisk._decide_core(ctx("2024-01-03T14:00:00+00:00", 400, 100,
                           {"near_money_pct": 0.04, "imbalance_threshold": 0.5}), oisk.SessionImbalanceTracker())
check("overrides recorded", d5.metadata["params"]["near_money_pct"] == 0.04
      and d5.metadata["params"]["imbalance_threshold"] == 0.5
      and d5.metadata["params"]["min_band_strikes"] == 4, d5.metadata["params"])

# No timestamp: nothing recorded, anchor stays unknown (not guessed).
d6 = oisk._decide_core(ctx("", 400, 100), oisk.SessionImbalanceTracker())
check("missing timestamp leaves anchor unknown", d6.metadata["anchor_observed_at"] is None
      and d6.metadata["session_key"] is None)

with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "a" * 40}):
    check("code_revision from the deploy environment",
          decision_base(decision_id="d", alias="a", strategy_id="s", strategy_version="1",
                        snapshot=None)["code_revision"] == "a" * 40)
with mock.patch.dict(os.environ, {}, clear=True):
    check("code_revision explicitly null when unknown",
          decision_base(decision_id="d", alias="a", strategy_id="s", strategy_version="1",
                        snapshot=None)["code_revision"] is None)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
