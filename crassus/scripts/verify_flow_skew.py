"""flow_skew_qqq: projection parsing, the window reading and its gates, entry,
exit, retention on missing observations and per-account isolation. No network:
every order-flow projection here is synthetic.

    python scripts/verify_flow_skew.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_oi_skew import FLAT_ROWS, fresh_quote, make_ctx  # noqa: E402

from crassus import order_flow  # noqa: E402
from crassus.order_flow import ContractFlow, FlowSnapshot, OrderFlowError  # noqa: E402
from crassus.replay import reset_strategy_state  # noqa: E402
from crassus.strategies import flow_skew as fs  # noqa: E402
from crassus.strategy import REGISTRY  # noqa: E402

passed = failed = 0
T0 = 1704121200000  # 2024-01-01T15:00:00Z
MIN = 60_000
STRIKES = (393, 397, 400, 403, 407)


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -- {detail}")


def sym(kind: str, strike: float) -> str:
    return f"QQQ240101{kind}{int(strike * 1000):08d}"


def flow(t: int, per: dict | None = None, *, gaps=(), day="2024-01-01", strikes=STRIKES) -> FlowSnapshot:
    """`per` maps (kind, strike) -> (high_buy, high_sell, low, unsigned); every
    other contract in `strikes` exists with zero flow."""
    per = per or {}
    contracts = {}
    for k in strikes:
        for kind in "CP":
            hb, hs, lo, u = per.get((kind, k), (0, 0, 0, 0))
            s = sym(kind, k)
            contracts[s] = ContractFlow(s, "call" if kind == "C" else "put", float(k), hb, hs, lo, u)
    return FlowSnapshot(day, t, tuple(gaps), contracts)


def even(n: int, kind="C", side="buy", low=0, unsigned=0, strikes=STRIKES) -> dict:
    """n confidently signed contracts on every band strike for `kind`, plus a
    balanced 50/50 background on the other type so every strike trades."""
    other = "P" if kind == "C" else "C"
    out = {}
    for k in strikes:
        out[(kind, k)] = (n, 0, low, unsigned) if side == "buy" else (0, n, low, unsigned)
        out[(other, k)] = (n // 10, n // 10, 0, 0)
    return out


def scale(per: dict, f: int) -> dict:
    return {k: tuple(x * f for x in v) for k, v in per.items()}


def ctx(t: int, *, held: str | None = None, alias: str = "Flow Skew", phase: str = "open", spot_age_s: int = 30):
    rows = FLAT_ROWS
    quotes = {r["OptionSymbol"]: fresh_quote(r["OptionSymbol"]) for r in rows}
    trades = [{"sym": held, "side": "buy", "qty": 1, "price": 1.0}] if held else None
    stamp = datetime.fromtimestamp((t - spot_age_s * 1000) / 1000, timezone.utc).isoformat()
    c = make_ctx(rows=rows, timestamp=stamp, quote_map=quotes, trades=trades, session_phase=phase)
    return replace(c, now_et=datetime.fromtimestamp(t / 1000, timezone.utc), account_alias=alias)


def run(history: list[FlowSnapshot], *, held=None, alias="Flow Skew", error=None, **kw):
    """Feed earlier projections through the decision (as earlier cycles), return the last decision."""
    decision = None
    for i, f in enumerate(history):
        last = i == len(history) - 1
        decision = fs.decide_core(ctx(f.last_received_ms, held=held if last else None, alias=alias, **kw),
                                  None if (last and error) else f, error if last else None)
    return decision


def window(start_per, end_per, **kw):
    """Base at T0, reading 15 min later, with a mid-window reading (realistic cadence)."""
    return [flow(T0, start_per, **kw), flow(T0 + 5 * MIN, start_per, **kw), flow(T0 + 15 * MIN, end_per, **kw)]


print("parse")
good = {"lastReceived": T0, "session": {"day": "2024-01-01", "gaps": [{"start": 1, "end": None}], "contracts": {
    sym("C", 400): {"B": 9, "S": 4, "U": 1, "prints": 5,
                    "label": {"q1": [3, 1], "q5": [2, 1], "low": [4, 2], "U": 1}},
    "SPY240101C00400000": {"B": 1, "S": 0, "U": 0, "prints": 1, "label": {"q1": [1, 0], "q5": [0, 0], "low": [0, 0], "U": 0}}}}}
parsed = order_flow.parse(good)
c = parsed.contracts[sym("C", 400)]
check("tiers fold to high buy/sell, low and unsigned", (c.high_buy, c.high_sell, c.low, c.unsigned, c.strike, c.type) == (5, 2, 6, 1, 400.0, "call"), repr(c))
check("non-QQQ contracts are ignored", list(parsed.contracts) == [sym("C", 400)])
check("an open gap parses with end None", parsed.gaps == ((1, None),))
for name, mutate in (
    ("pre-label collector is rejected", lambda p: p["session"]["contracts"][sym("C", 400)].pop("label")),
    ("negative count is rejected", lambda p: p["session"]["contracts"][sym("C", 400)]["label"].__setitem__("U", -1)),
    ("missing lastReceived is rejected", lambda p: p.pop("lastReceived")),
):
    import copy
    bad = copy.deepcopy(good)
    mutate(bad)
    try:
        order_flow.parse(bad)
        check(name, False, "no error")
    except OrderFlowError:
        check(name, True)

print("registration")
check("flow_skew_qqq is registered", "flow_skew_qqq" in REGISTRY)

print("entry")
reset_strategy_state()
d = run(window({}, even(1000, "C", "buy")))
check("call buying opens one ATM call", d.action == "buy" and d.symbol == sym("C", 400) and d.quantity == 1, d.reason)
check("reading is recorded", d.metadata["flow"]["lean"] == "call" and d.metadata["flow"]["imbalance"] > 0.8)
reset_strategy_state()
d = run(window({}, even(1000, "P", "buy")))
check("put buying opens one ATM put", d.action == "buy" and d.symbol == sym("P", 400), d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "C", "sell")))
check("call selling reads bearish (opens a put)", d.action == "buy" and d.symbol == sym("P", 400), d.reason)

print("window arithmetic")
reset_strategy_state()
base = even(1000, "P", "buy")             # bearish before the window
end = {k: (v[0] + (1000 if k[0] == "C" else 0), v[1], v[2], v[3]) for k, v in base.items()}
d = run(window(base, end))
check("only flow inside the window counts", d.metadata["flow"]["lean"] == "call", d.reason)
reset_strategy_state()
per = even(1000, "C", "buy")
per[("C", 420)] = (0, 100000, 0, 0)       # huge out-of-band selling
d = run(window({}, per, strikes=STRIKES + (420,)))
check("out-of-band contracts are ignored", d.metadata["flow"]["lean"] == "call", d.reason)

print("no-call gates")
reset_strategy_state()
balanced = {(k2, k): (500, 500, 0, 0) for k in STRIKES for k2 in "CP"}
d = run(window({}, balanced))
check("balanced flow is neutral, no trade", d.action == "no_trade" and d.metadata["flow"]["lean"] == "neutral", d.reason)
reset_strategy_state()
d = run(window({}, even(100, "C", "buy")))
check("too few signed contracts is thin", d.metadata["flow"]["lean"] == "thin" and "signed" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "C", "buy", low=300, unsigned=300)))
check("too much low-confidence volume is thin", d.metadata["flow"]["lean"] == "thin" and "low-confidence" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "C", "buy", strikes=(400, 403))))
check("too few band strikes traded is thin", d.metadata["flow"]["lean"] == "thin", d.reason)

print("missing observations")
reset_strategy_state()
d = run([flow(T0 + 15 * MIN, even(1000))])
check("no 15-minute-old reading yet: warming up", d.metadata["flow"]["lean"] == "unavailable" and "warming" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000), gaps=((T0 + 7 * MIN, T0 + 8 * MIN),)))
check("gap inside the window: unavailable", d.metadata["flow"]["lean"] == "unavailable" and "gap" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000), gaps=((T0 - 10 * MIN, T0 - 9 * MIN),)))
check("gap before the window does not block", d.action == "buy", d.reason)
reset_strategy_state()
d = run(window({}, even(1000), strikes=(430, 431, 432, 433, 434)))
check("band outside the collector universe: unavailable", d.metadata["flow"]["lean"] == "unavailable" and "universe" in d.reason, d.reason)
reset_strategy_state()
hist = window({}, even(1000))
fs.decide_core(ctx(hist[0].last_received_ms), hist[0], None)
fs.decide_core(ctx(hist[1].last_received_ms), hist[1], None)
d = fs.decide_core(ctx(hist[2].last_received_ms + 200_000), hist[2], None)
check("projection older than max_feed_age_s: unavailable", d.metadata["flow"]["lean"] == "unavailable" and "stale feed" in d.reason, d.reason)
reset_strategy_state()
hist = window({}, even(1000))
for f in hist:
    fs.decide_core(ctx(f.last_received_ms), f, None)
d = fs.decide_core(ctx(hist[-1].last_received_ms + 1000), hist[-1], None)
check("same projection twice: stale", d.metadata["flow"]["lean"] == "stale", d.reason)
reset_strategy_state()
d = run(window({}, even(1000)), spot_age_s=900)
check("stale spot: unavailable", d.metadata["flow"]["lean"] == "unavailable" and "spot" in d.reason, d.reason)
reset_strategy_state()
d = run(window(even(2000), even(1000)))
check("totals going backwards: unavailable", d.metadata["flow"]["lean"] == "unavailable", d.reason)

print("exits and retention")
call = sym("C", 400)
reset_strategy_state()
d = run(window({}, balanced), held=call)
check("held call closes on neutral flow", d.action == "sell" and d.symbol == call, d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "P", "buy")), held=call)
check("held call closes when flow flips bearish", d.action == "sell" and d.symbol == call, d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "C", "buy")), held=call)
check("held call is kept while flow supports it", d.action == "no_trade" and "still supports" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000)), held=call, error="ConnectionError: boom")
check("fetch error retains the held call", d.action == "no_trade" and "retaining" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000), gaps=((T0 + 7 * MIN, None),)), held=call)
check("open gap retains the held call", d.action == "no_trade" and "retaining" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000), strikes=(430, 431, 432, 433, 434)), held=call)
check("uncaptured band retains the held call", d.action == "no_trade" and "retaining" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000, "C", "buy", low=300, unsigned=300)), held=call)
check("thin (low-confidence) reading closes the held call", d.action == "sell", d.reason)

print("session and accounts")
reset_strategy_state()
hist = window({}, even(1000))
fs.decide_core(ctx(hist[0].last_received_ms, alias="A"), hist[0], None)
fs.decide_core(ctx(hist[1].last_received_ms, alias="A"), hist[1], None)
d_a = fs.decide_core(ctx(hist[2].last_received_ms, alias="A"), hist[2], None)
d_b = fs.decide_core(ctx(hist[2].last_received_ms, alias="B"), hist[2], None)
check("each account keeps its own window history", d_a.action == "buy" and d_b.metadata["flow"]["lean"] == "unavailable", f"{d_a.reason} | {d_b.reason}")
reset_strategy_state()
hist = window({}, even(1000))
for f in hist[:2]:
    fs.decide_core(ctx(f.last_received_ms), f, None)
nxt = replace(hist[2], day="2024-01-02")
d = fs.decide_core(ctx(nxt.last_received_ms), nxt, None)
check("a new session day drops the old history", d.metadata["flow"]["lean"] == "unavailable" and "warming" in d.reason, d.reason)
reset_strategy_state()
d = run(window({}, even(1000)), phase="pre")
check("closed market: no trade, but the reading is still observed", d.action == "no_trade" and d.metadata["flow"]["lean"] == "call", d.reason)
reset_strategy_state()
check("reset_strategy_state clears every tracker", not fs._trackers)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
