"""Project an owner-supplied paper decision ledger into the One Clock contract.

No credentials or transport. Archive replay requires an explicit simulated
availability time; a provider publication takes its availability from the
immutable commit receipt. Never send a private ledger line as public evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .audit import Outcome, SCHEMA_VERSION
from .oi_skew_projection import build_projection

TRACKED = json.loads((Path(__file__).resolve().parents[1] / "accounts.example.json").read_text())["accounts"]
PUBLIC = {a["username"]: a["strategy_id"] for a in TRACKED
          if not a["strategy_id"].startswith("reddit_sentiment_qqq")}
# The runner's paper catalog currently routes these two strategy families away
# from the QQQ snapshot. Reject unfamiliar strategies until mapped explicitly.
ROOTS = {
    **dict.fromkeys(("oi_skew_ibit_adaptive",), "IBIT"),
    **dict.fromkeys(("spy_sma200_double_or_out", "oi_skew_spy"), "SPY"),
    **dict.fromkeys(("smoke_atm_roundtrip", "smoke_atm_roundtrip_fixed_window",
                     "smoke_atm_roundtrip_phelps", "momentum_qqq", "momentum_qqq_fixed_window",
                     "momentum_qqq_phelps", "trump_whisperer_qqq", "trump_whisperer_qqq_phelps",
                     "trump_whisperer_qqq_fixed_window", "max_pain_qqq", "oi_skew_qqq", "flow_skew_qqq",
                     "put_call_ratio_qqq", "canopus_down_day_14", "looking_glass_straddle",
                     "phelps_pure_qqq", "momentum_puts_only_qqq", "momentum_puts_only_qqq_phelps",
                     "qqq_gated_strangle"), "QQQ")
}
NY = ZoneInfo("America/New_York")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def project(record):
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return None
    # A missing reconciled account is common for startup failures. Only an
    # explicitly retained account username can qualify those records.
    identities = [record.get("paper_account_username")]
    for key in ("account_state_before", "account_state_after"):
        state = record.get(key)
        if isinstance(state, dict):
            identities.append(state.get("username"))
    names = {v for v in identities if isinstance(v, str) and v}
    if len(names) != 1:
        return None
    bot = names.pop()
    strategy = record.get("account_strategy_id") or record.get("strategy_id")
    if bot not in PUBLIC or PUBLIC[bot] != strategy or strategy not in ROOTS:
        return None
    at = timestamp(record.get("timestamp_utc"))
    outcome = record.get("outcome_class")
    try:
        private_id = str(uuid.UUID(record["decision_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    if at is None or outcome not in Outcome.ALL:
        return None
    # A recovery record may reuse decision_id. Each durable audit event is
    # identified by its decision, outcome and record time, including retries.
    digest = hashlib.sha256(f"one-clock.bot.v1|{bot}|{private_id}|{at.isoformat()}|{outcome}".encode()).hexdigest()
    day = at.astimezone(NY).date().isoformat()
    base = {"id": f"{bot}/{day}/{digest}", "revision": 1, "eventAt": at.isoformat(), "session": day}
    decision = record.get("decision")
    action = decision.get("action") if isinstance(decision, dict) else None
    snap = timestamp(record.get("market_snapshot_timestamp"))
    value = {"bot": bot, "outcome": outcome,
             "action": action if action in ("buy", "sell", "no_trade") else None,
             "inputAgeMs": round((at-snap).total_seconds()*1000) if snap and snap <= at else None}
    rows = [{**base, "source": "bot-decision", "value": value}]
    # A ledger row and a strategy evaluation have different identities.
    # Preserve the original decision time across recovery; older records with
    # no such clock remain available as ordinary audit rows only.
    decided_at = timestamp(record.get("strategy_decided_at"))
    if decided_at and decided_at <= at and action in ("buy", "sell", "no_trade"):
        key = hashlib.sha256(f"one-clock.evaluation.v1|{bot}|{private_id}".encode()).hexdigest()
        rows.append({**base, "source": "bot-evaluation", "eventAt": decided_at.isoformat(),
                     "session": decided_at.astimezone(NY).date().isoformat(),
                     "value": {"bot": bot, "action": action, "decisionKey": key}})
    if bot == "crassus_oiskew":
        meter = build_projection(record, published_at=at)
        anchor_at = timestamp(meter["anchor_observed_at"])
        qualified = (meter["status"]["code"] == "ok" and snap and anchor_at
                     and anchor_at <= snap <= at and meter["session_key"] == snap.date().isoformat())
        anchor = None
        if qualified:
            anchor = {"anchor": meter["session_start_imbalance"], "imbalance": meter["imbalance_ratio"],
                      "delta": meter["delta_from_session_start"], "strikes": meter["band_strikes"],
                      "anchorAt": anchor_at.isoformat(), "run": meter["run_identity"],
                      "threshold": meter["params"]["imbalance_threshold"],
                      "minChange": meter["params"]["min_change_from_session_start"]}
        rows.append({**base, "source": "bot-anchored-skew", "value": anchor})
    return ROOTS[strategy], rows


def replay_rows(record, *, origin, available_at):
    if origin not in {"synthetic", "reconstructed"}:
        raise ValueError("offline replay requires a synthetic or reconstructed origin")
    available = timestamp(available_at)
    if available is None:
        raise ValueError("supply an offset-qualified simulated availability")
    projection = project(record)
    if not projection:
        return []
    instrument, rows = projection
    if available < timestamp(rows[0]["eventAt"]):
        raise ValueError("availability precedes decision")
    return [{**row, "instrument": instrument, "origin": origin, "availableAt": available.isoformat(),
             "evidence": f"{origin}:bot-projection:{row['id']}"} for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--origin", choices=["synthetic", "reconstructed"], required=True)
    parser.add_argument("--available-at", required=True, help="Explicit simulated publication time for this batch")
    args = parser.parse_args()
    with args.ledger.open() as stream:
        for line in stream:
            for row in replay_rows(json.loads(line), origin=args.origin, available_at=args.available_at):
                print(json.dumps(row, allow_nan=False))


if __name__ == "__main__":
    main()
