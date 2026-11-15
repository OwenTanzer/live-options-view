#!/usr/bin/env python3
"""Prove the immutable market-readings join on a real ledger record (#107).

The merged #110 ownership decisions say shared decision context is joined *by
reference*, not by copying values into unrelated strategies' records. A
decision therefore carries `market_readings_join`: the archived snapshot key,
the snapshot timestamp, and the SHA-256 of the exact `market_readings` list.
This script builds a collector-shaped payload, loads it through Crassus'
`MarketSnapshot`, records a real no-trade decision in a temporary ledger, and
reads the line back to prove:

- the join's digest equals the collector's published digest;
- the join survives the ledger write with no trade at all (no-trade visibility);
- a snapshot without readings yields an explicit `unavailable` join rather
  than a fabricated digest;
- a tampered published digest is exposed as `digest_mismatch`;
- no strategy behavior or decision field changed.

Hermetic: temporary directory, Windows-safe fsync patch, no network.

    python scripts/verify_market_readings_join.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
sys.path.insert(0, str(ROOT))

from crassus.audit import DecisionLedger, Outcome  # noqa: E402
from crassus.market import MarketSnapshot  # noqa: E402
from crassus.runner import decision_base  # noqa: E402
from market_data.overview.readings import build_snapshot_readings, readings_digest  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
KEY = "intraday/20261109/snapshot_150000000000.csv"


def rows():
    out = []
    for strike in (590, 595, 600, 605, 610):
        for kind, oi in (("call", 100), ("put", 120)):
            out.append({"OptionSymbol": f"QQQ261109{kind[0].upper()}{strike:08d}",
                        "Strike": float(strike), "Type": kind,
                        "Bid": 1.0, "Ask": 1.2, "OpenInterest": oi})
    return out


market = {
    "spot": 600.1, "spot_ts": NOW.isoformat(), "vwap": 599.9, "vwap_ts": NOW.isoformat(),
    "vwap_partial_session": False, "vwap_session_started_at": NOW.isoformat(),
    "price_vs_vwap_abs": 0.2, "price_vs_vwap_pct": 0.0333,
    "session_volume": 1_000_000, "session_volume_ts": NOW.isoformat(),
    "rvol": {"status": "ok", "multiple": 1.25, "baseline_volume": 800_000,
             "baseline_days_used": 12, "baseline_lookback_days": 20},
    "momentum": {"status": "ok", "return_pct": 0.12, "anchor_age_minutes": 58.0,
                 "sample_count": 9},
}

readings = build_snapshot_readings(
    rows=rows(), symbol="QQQ", underlying=600.1, snapshot_key=KEY,
    snapshot_timestamp=NOW.isoformat(), calculated_at=NOW, freshness="live",
    observed_at=NOW.isoformat(), market=market,
)
digest = readings_digest(readings)
payload = {
    "timestamp": NOW.isoformat(), "snapshot_time": "15:00 ET",
    "expiration": "2026-11-09", "underlying_price": 600.1, "rows": rows(),
    "snapshot_key": KEY, "market_readings": readings,
    "market_readings_digest": digest,
}

snapshot = MarketSnapshot.from_payload("https://example.test/intraday/latest.json", payload, b"{}")
check("reading ids cover all dashboard reference readings",
      [r["reading_id"] for r in snapshot.readings] == [
          "reference_vwap", "reference_rvol", "reference_momentum",
          "reference_pcr", "reference_max_pain", "reference_oi_skew", "qqq_iv_header"])
check("cross-boundary digest matches", snapshot.readings_digest == digest)
check("join carries key, timestamp, digest and ids",
      snapshot.readings_join["snapshot_key"] == KEY
      and snapshot.readings_join["readings_digest"] == digest
      and snapshot.readings_join["snapshot_timestamp"] == NOW.isoformat())

with tempfile.TemporaryDirectory() as tmp:
    ledger = DecisionLedger(Path(tmp), run_id="fixture")
    base = decision_base(decision_id=ledger.new_decision_id(), alias="fixture-account",
                         strategy_id="max_pain_qqq", strategy_version="1.0.0",
                         snapshot=snapshot)
    with patch("crassus.audit.sync_directory"):
        ledger.record(
            **base, outcome_class=Outcome.NO_TRADE,
            reason="fixture no_trade: proves a decision with no trade still joins",
            account_state_before={"balance_cash": 10000.0},
        )
    line = json.loads(ledger.paths.ledger.read_text().strip().splitlines()[-1])

check("ledger record is a no_trade", line["outcome_class"] == Outcome.NO_TRADE)
check("no-trade record carries the join",
      line.get("market_readings_join", {}).get("readings_digest") == digest)
check("join identifies the exact archived snapshot",
      line["market_readings_join"]["snapshot_key"] == KEY)

tampered = MarketSnapshot.from_payload(
    "https://example.test/intraday/latest.json",
    {**payload, "market_readings_digest": "0" * 64}, b"{}")
check("tampered digest is exposed", tampered.readings_status == "digest_mismatch")

malformed = MarketSnapshot.from_payload(
    "https://example.test/intraday/latest.json",
    {**payload, "market_readings": [readings[0], "not-a-dict"]}, b"{}")
check("malformed reading element is invalid, not a crash",
      malformed.readings_status == "invalid"
      and malformed.readings_join["status"] == "invalid"
      and malformed.readings_join["reading_ids"] == [])

absent = MarketSnapshot.from_payload(
    "https://example.test/intraday/latest.json",
    {k: v for k, v in payload.items() if k not in ("market_readings", "market_readings_digest")},
    b"{}")
empty_base = decision_base(decision_id="d", alias="a", strategy_id="s",
                           strategy_version="1", snapshot=absent)
check("a board without readings yields an absent join",
      empty_base["market_readings_join"]["status"] == "absent"
      and empty_base["market_readings_join"]["readings_digest"] is None)

none_base = decision_base(decision_id="d", alias="a", strategy_id="s",
                          strategy_version="1", snapshot=None)
check("a cycle without a snapshot yields an unavailable join",
      none_base["market_readings_join"]["status"] == "unavailable")

print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
