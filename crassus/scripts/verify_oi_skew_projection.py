#!/usr/bin/env python3
"""The inactive oi_skew_meter.v1 projection: provenance, privacy, unavailability.

Builds projections from ledger records written by the real DecisionLedger for
decisions made by the real strategy, plus legacy-shaped and malformed records.
No provider access, no writer.

    python scripts/verify_oi_skew_projection.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":  # POSIX directory fsync has no meaning here
    import crassus.audit as _audit
    import crassus.durability as _durability
    _durability.sync_directory = lambda path: None
    _audit.sync_directory = lambda path: None

from crassus import oi_skew_projection as proj  # noqa: E402
from crassus.audit import DecisionLedger, Outcome  # noqa: E402
from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.runner import decision_base  # noqa: E402
from crassus.strategies import oi_skew as oisk  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

passed = failed = 0
PUB = datetime(2024, 1, 1, 15, 0, 30, tzinfo=timezone.utc)


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


def snap(ts, call_oi, put_oi):
    rows = []
    for strike in (393.0, 397.0, 400.0, 403.0, 407.0):
        code = f"{int(strike * 1000):08d}"
        for kind, letter, oi in (("call", "C", call_oi), ("put", "P", put_oi)):
            rows.append({"OptionSymbol": f"QQQ240101{letter}{code}", "Strike": strike, "Type": kind,
                         "Bid": 1.0, "Ask": 1.1, "OpenInterest": oi})
    return MarketSnapshot.from_payload(url="https://example.invalid/latest.json", raw=b"{}", payload={
        "timestamp": ts, "snapshot_time": ts, "expiration": "2024-01-01", "underlying_price": 400.0, "rows": rows})


def real_record(tmp):
    """A ledger row exactly as the runner writes it, for a real decision."""
    tracker = oisk.SessionImbalanceTracker()
    q = lambda syms: {s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts="2024-01-01T15:00:00",
                               server_ts="2024-01-01T15:00:02") for s in syms}
    for ts, c, p in (("2024-01-01T14:00:00+00:00", 100, 100), ("2024-01-01T15:00:00+00:00", 400, 100)):
        s = snap(ts, c, p)
        ctx = StrategyContext(snapshot=s, account_state={"username": "crassus_oiskew"}, book=Book([]),
                              now_et=None, session_phase="open", quotes=q, params={})
        decision = oisk._decide_core(ctx, tracker)
    ledger = DecisionLedger(Path(tmp), run_id="0cea9b6bad38")
    with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "f" * 40}):
        base = decision_base(decision_id=ledger.new_decision_id(), alias="OI Skew", strategy_id="oi_skew_qqq",
                             strategy_version=oisk.STRATEGY_VERSION, snapshot=s)
    return ledger.record(**base, outcome_class=Outcome.NO_TRADE if decision.action == "no_trade" else Outcome.FILLED,
                         decision=decision.__dict__ if hasattr(decision, "__dict__") else dict(decision),
                         reason=decision.reason,
                         account_state_before={"username": "crassus_oiskew", "balance_cash": 11087.0,
                                               "open_positions": {}},
                         account_state_after={"username": "crassus_oiskew", "balance_cash": 10887.0,
                                              "open_positions": {"QQQ240101C00400000": 1}},
                         execution_request_id="req-123", server_response={"ok": True})


with tempfile.TemporaryDirectory() as tmp:
    rec = real_record(tmp)
    doc = proj.build_projection(rec, published_at=PUB)
    meta = rec["decision"]["metadata"]
    check("complete record -> status ok", doc["status"]["code"] == "ok", doc["status"])
    check("values carried exactly", doc["imbalance_ratio"] == meta["imbalance_ratio"]
          and doc["session_start_imbalance"] == meta["session_start_imbalance"]
          and doc["delta_from_session_start"] == meta["delta_from_session_start"]
          and doc["band_strikes"] == meta["band_strikes"])
    check("anchor identity carried", doc["session_key"] == "2024-01-01"
          and doc["anchor_observed_at"] == "2024-01-01T14:00:00+00:00")
    check("applied params carried", doc["params"] == {"near_money_pct": 0.02, "imbalance_threshold": 0.3,
                                                       "min_change_from_session_start": 0.1, "min_band_strikes": 4})
    check("times carried", doc["observed_snapshot_timestamp"] == "2024-01-01T15:00:00+00:00"
          and doc["decision_at"] == rec["timestamp_utc"] and doc["published_at"] == PUB.isoformat())
    check("revision carried", doc["code_revision"] == "f" * 40)
    check("run identity is a digest, not the run_id", doc["run_identity"].startswith("run:")
          and "0cea9b6bad38" not in json.dumps(doc))
    text = json.dumps(doc)
    for secret in ("crassus_oiskew", "OI Skew", rec["decision_id"], "req-123", "11087", "10887",
                   "QQQ240101C00400000", "open_positions", "balance_cash"):
        check(f"privacy: {secret!r} absent", secret not in text)
    try:
        proj.assert_sanitized(doc)
        check("assert_sanitized passes", True)
    except ValueError as exc:
        check("assert_sanitized passes", False, str(exc))
    check("output keys are exactly the allowlist", set(doc) == {
        "schema_version", "strategy_id", "strategy_version", "code_revision", "run_identity", "session_key",
        "anchor_observed_at", "session_start_imbalance", "imbalance_ratio", "delta_from_session_start",
        "band_strikes", "params", "session_phase", "observed_snapshot_timestamp", "decision_at",
        "published_at", "status"}, sorted(doc))
    check("deterministic for a fixed published_at", proj.build_projection(rec, published_at=PUB) == doc)
    check("run identity distinguishes a restart",
          proj.run_identity("0cea9b6bad38") != proj.run_identity("4417efd90ef4"))

    # Legacy ledger row (pre-provenance shape, as in the Sep-Nov export).
    legacy = {k: v for k, v in rec.items() if k != "code_revision"}
    legacy["decision"] = dict(rec["decision"], metadata={k: v for k, v in meta.items()
                                                         if k not in ("session_key", "anchor_observed_at", "params")})
    ldoc = proj.build_projection(legacy, published_at=PUB)
    check("legacy row -> unavailable, not guessed", ldoc["status"]["code"] == "unavailable")
    for f in ("code_revision", "session_key", "anchor_observed_at", "params"):
        check(f"legacy row names missing {f}", f in ldoc["status"]["reason"], ldoc["status"]["reason"])
    check("legacy row leaves unknowns null", ldoc["anchor_observed_at"] is None and ldoc["params"] is None)

    # Revision alone missing is still unavailable (no guessed source revision).
    norev = dict(rec, code_revision=None)
    check("unknown revision -> unavailable", proj.build_projection(norev, published_at=PUB)["status"]["reason"]
          == "missing_or_invalid:code_revision")

    # Review 5:42 PM: empty/malformed provenance is not provenance. Each case
    # must be unavailable and name exactly the offending field.
    def with_meta(**kw):
        return dict(rec, decision=dict(rec["decision"], metadata=dict(meta, **kw)))
    malformed = [
        ("code_revision", dict(rec, code_revision="")),
        ("code_revision", dict(rec, code_revision="not-a-sha")),
        ("code_revision", dict(rec, code_revision="F" * 40)),          # uppercase is not git's form
        ("strategy_version", dict(rec, strategy_version="  ")),
        ("session_key", with_meta(session_key="")),
        ("session_key", with_meta(session_key="2024-02-30")),          # not a real date
        ("session_key", with_meta(session_key="2024-1-1")),
        ("anchor_observed_at", with_meta(anchor_observed_at="")),
        ("anchor_observed_at", with_meta(anchor_observed_at="yesterday")),
        ("anchor_observed_at", with_meta(anchor_observed_at="2024-01-01T14:00:00")),  # naive
        ("anchor_observed_at", with_meta(anchor_observed_at="2026-02-30T17:30:00Z")),  # impossible date
        ("anchor_observed_at", with_meta(anchor_observed_at="2026-11-13T24:00:00Z")),
        ("observed_snapshot_timestamp", dict(rec, market_snapshot_timestamp="")),
        ("observed_snapshot_timestamp", dict(rec, market_snapshot_timestamp="2024-13-01T00:00:00Z")),
        ("decision_at", dict(rec, timestamp_utc="not a time")),
        ("band_strikes", with_meta(band_strikes=-1)),
    ]
    for field, bad in malformed:
        d = proj.build_projection(bad, published_at=PUB)
        check(f"malformed {field} ({str((bad.get(field) if field in bad else '') or '')[:20]!r}) -> unavailable",
              d["status"]["code"] == "unavailable"
              and d["status"]["reason"] == f"missing_or_invalid:{field}", d["status"])
    zulu = dict(rec, timestamp_utc="2024-01-01T15:00:05Z")
    check("Z-suffixed instants are valid", proj.build_projection(zulu, published_at=PUB)["status"]["code"] == "ok")
    check("short SHA accepted", proj.build_projection(dict(rec, code_revision="abc1234"),
                                                     published_at=PUB)["status"]["code"] == "ok")

    other = dict(rec, strategy_id="momentum_qqq")
    check("other strategy -> unavailable", "not_an_oi_skew_decision" in
          proj.build_projection(other, published_at=PUB)["status"]["reason"])

    for bad in ({}, {"decision": "x"}, {"decision": {"metadata": []}}, None.__class__ and {"strategy_id": None},
                dict(rec, decision=dict(rec["decision"], metadata=dict(meta, imbalance_ratio=float("nan"),
                                                                        params={"near_money_pct": "0.02"})))):
        try:
            d = proj.build_projection(bad, published_at=PUB)
            ok = d["status"]["code"] == "unavailable" or d["imbalance_ratio"] is None
            proj.assert_sanitized(d)
        except Exception as exc:  # must never raise
            ok = False
        check(f"malformed input handled: {str(bad)[:40]}", ok)

    later = dict(rec, timestamp_utc="2024-01-01T15:05:00+00:00")
    earlier = dict(rec, timestamp_utc="2024-01-01T14:55:00+00:00")
    noise = dict(rec, strategy_id="max_pain_qqq", timestamp_utc="2024-01-01T16:00:00+00:00")
    check("latest OI Skew record selected", proj.latest_oi_skew_record([earlier, noise, later]) is later)
    check("no OI Skew record -> None", proj.latest_oi_skew_record([noise]) is None)

    leaked = dict(doc, account_alias="OI Skew")
    try:
        proj.assert_sanitized(leaked)
        check("assert_sanitized rejects a forbidden key", False)
    except ValueError:
        check("assert_sanitized rejects a forbidden key", True)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
