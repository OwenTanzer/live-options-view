#!/usr/bin/env python3
"""Parity fixtures for the OI skew meter (site/shared/oi-skew.js).

Runs the REAL bot (`crassus/crassus/strategies/oi_skew.py` `_decide_core`)
and the REAL collector reading (`market_data/overview/readings.py`
`compute_reference_oi_skew`) over hand-built boards, and records what each
concluded. tests/oi_skew_meter.test.js then feeds the same inputs to the
browser's classifier and asserts identical states and values, so the meter
cannot drift from the bot's gates or the published reading.

The bot's effective state is read from its decision: buy call -> bullish,
buy put -> bearish, no_trade -> neutral (flat book, fresh quotes, so the only
thing deciding is the signal). No network, no runner, no production data.

    python tests/oi_skew_meter_parity.py          # verify fixture is current
    python tests/oi_skew_meter_parity.py --write  # regenerate it

Lives under tests/ (not crassus/) on purpose: crassus/** is the
crassus-runner deploy watch scope, and this display work must not make the
runner a deployment candidate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "crassus"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategies import oi_skew as oisk  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402
from market_data.overview.readings import compute_reference_oi_skew  # noqa: E402
from crassus.oi_skew_projection import build_projection  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "oi_skew_parity.json"
UNDERLYING = 400.0
TS_ANCHOR = "2026-11-13T11:00:00+00:00"
TS_NOW = "2026-11-13T17:30:00+00:00"


def board(pairs: list[tuple[float, int, int]]) -> list[dict]:
    """(strike, call_oi, put_oi) -> chain rows with quotable bids/asks."""
    rows = []
    for strike, call_oi, put_oi in pairs:
        code = f"{int(round(strike * 1000)):08d}"
        for kind, letter, oi in (("call", "C", call_oi), ("put", "P", put_oi)):
            rows.append({"OptionSymbol": f"QQQ261113{letter}{code}", "Strike": strike, "Type": kind,
                         "Bid": 1.0, "Ask": 1.1, "OpenInterest": oi})
    return rows


def spread(call_total: int, put_total: int, strikes=(393.0, 397.0, 400.0, 403.0, 407.0)) -> list[dict]:
    """Spread totals over in-band strikes; out-of-band strikes add noise the band must ignore."""
    n = len(strikes)
    pairs = []
    for i, strike in enumerate(strikes):
        c = call_total // n + (call_total % n if i == 0 else 0)
        p = put_total // n + (put_total % n if i == 0 else 0)
        pairs.append((strike, c, p))
    pairs += [(380.0, 0, 9000), (420.0, 9000, 0)]  # 5% from spot: outside every band used here
    return board(pairs)


CASES = [
    # name, rows, anchor rows (None = this is the session's first reading), params
    ("bullish_directional", spread(2400, 500), spread(1000, 1000), {}),
    ("bearish_directional", spread(500, 2400), spread(1000, 1000), {}),
    ("neutral_below_threshold", spread(1200, 1000), spread(1000, 1000), {}),
    ("neutral_skewed_since_open", spread(2400, 500), spread(2400, 520), {}),
    ("neutral_first_reading_is_anchor", spread(2400, 500), None, {}),
    ("boundary_exact_threshold_directional", spread(650, 350), spread(1000, 1000), {}),
    ("boundary_threshold_just_below", spread(6499, 3501), spread(1000, 1000), {}),
    # 0.3 - 0.2 == 0.09999999999999998 in IEEE doubles: the bot calls this
    # NOT moved enough, so the meter must too.
    ("boundary_session_change_float", spread(650, 350), spread(600, 400), {}),
    ("bearish_after_session_move", spread(350, 650), spread(450, 550), {}),
    # 0.4 - 0.3 == 0.10000000000000003: just clears the change gate.
    ("boundary_session_change_float_passes", spread(700, 300), spread(650, 350), {}),
    ("neutral_thin_band", spread(2400, 500, strikes=(400.0, 403.0, 407.0)), spread(1000, 1000, strikes=(400.0, 403.0, 407.0)), {}),
    ("missing_no_open_interest", board([(400.0, 0, 0), (403.0, 0, 0)]), spread(1000, 1000), {}),
    ("override_wider_band_and_threshold", spread(2400, 500), spread(1000, 1000),
     {"near_money_pct": 0.06, "imbalance_threshold": 0.5}),
    ("override_stricter_change", spread(2400, 500), spread(1500, 1000), {"min_change_from_session_start": 0.5}),
]


def snapshot(rows: list[dict], ts: str) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        url="test://oi-skew-parity",
        payload={"timestamp": ts, "snapshot_time": ts, "expiration": "2026-11-13",
                 "underlying_price": UNDERLYING, "rows": rows},
        raw=b"{}")


def ctx(rows: list[dict], ts: str, params: dict) -> StrategyContext:
    def quotes(symbols):
        return {s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts="2026-11-13T17:30:00",
                         server_ts="2026-11-13T17:30:02") for s in symbols}
    return StrategyContext(snapshot=snapshot(rows, ts), account_state={}, book=Book([]),
                           now_et=None, session_phase="open", quotes=quotes, params=params)


def run_case(name, rows, anchor_rows, params) -> dict:
    tracker = oisk.SessionImbalanceTracker()
    if anchor_rows is not None:
        oisk._decide_core(ctx(anchor_rows, TS_ANCHOR, params), tracker)
    decision = oisk._decide_core(ctx(rows, TS_NOW, params), tracker)
    meta = decision.metadata or {}
    if decision.action == "buy":
        state = "bullish" if decision.symbol[9] == "C" else "bearish"
    else:
        state = "neutral"
    reference = compute_reference_oi_skew(rows, UNDERLYING)
    # The inactive public projection, built by the real builder from a
    # ledger-shaped record of this exact decision. The browser must reach the
    # same state from it as the bot did.
    record = {"strategy_id": oisk.STRATEGY_ID, "strategy_version": oisk.STRATEGY_VERSION,
              "run_id": "parity-run", "code_revision": "0" * 40,
              "timestamp_utc": "2026-11-13T17:30:05+00:00", "market_snapshot_timestamp": TS_NOW,
              "decision": {"action": decision.action, "metadata": meta}}
    projection = build_projection(record, published_at=datetime(2026, 11, 13, 17, 30, 6, tzinfo=timezone.utc))
    return {
        "projection": projection,
        "name": name,
        "params": params,
        "bot": {
            "action": decision.action,
            "state": state,
            "reason": decision.reason,
            "imbalance_ratio": meta.get("imbalance_ratio"),
            "session_start_imbalance": meta.get("session_start_imbalance"),
            "delta_from_session_start": meta.get("delta_from_session_start"),
            "band_strikes": meta.get("band_strikes"),
        },
        "reference": reference,
        "strategy_version": oisk.STRATEGY_VERSION,
    }


def build() -> dict:
    return {
        "schema": "oi_skew_meter_parity.v1",
        "generated_by": "tests/oi_skew_meter_parity.py",
        "underlying": UNDERLYING,
        "cases": [run_case(*case) for case in CASES],
    }


def main(argv: list[str]) -> int:
    doc = build()
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    states = {c["name"]: c["bot"]["state"] for c in doc["cases"]}
    # Sanity: the cases must actually exercise every state.
    assert {"bullish", "bearish", "neutral"} <= set(states.values()), states
    if "--write" in argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(doc['cases'])} cases)")
        for name, st in states.items():
            print(f"  {st:8s} {name}")
        return 0
    current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""
    if current != text:
        print("FAIL: tests/fixtures/oi_skew_parity.json is out of date with the bot/collector; "
              "run python tests/oi_skew_meter_parity.py --write")
        return 1
    print(f"OK: parity fixture current ({len(doc['cases'])} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
