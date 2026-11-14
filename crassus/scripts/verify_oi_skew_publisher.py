#!/usr/bin/env python3
"""Fail-safe OI skew projection publisher (H1 delivery): focused checks.

No network: a fake S3 client records put_object calls. Covers configuration
gating, the single public key, sanitization before upload, failure isolation
from the ledger/trading path, latest-wins non-blocking hand-off, and that
credentials never reach logs.

    python scripts/verify_oi_skew_publisher.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":  # POSIX directory fsync has no meaning here
    import crassus.audit as _audit
    import crassus.durability as _durability
    _durability.sync_directory = lambda path: None
    _audit.sync_directory = lambda path: None

from crassus import oi_skew_publisher as pub  # noqa: E402
from crassus.audit import DecisionLedger, Outcome  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(("  [OK] " if ok else "  [FAIL] ") + name + (f" -- {detail}" if detail and not ok else ""))


SECRET = "S3CR3T-never-log-this"
GOOD_ENV = {"CRASSUS_OI_SKEW_PUBLIC_ACCOUNT_ID": "a" * 32, "CRASSUS_OI_SKEW_PUBLIC_BUCKET": "public-bucket",
            "CRASSUS_OI_SKEW_PUBLIC_ACCESS_KEY_ID": "AKID", "CRASSUS_OI_SKEW_PUBLIC_SECRET_ACCESS_KEY": SECRET}


class FakeS3:
    def __init__(self, fail=None, delay=0.0):
        self.puts, self.fail, self.delay = [], fail, delay
        self.lock = threading.Lock()

    def put_object(self, **kw):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        with self.lock:
            self.puts.append(kw)


def record(**over):
    meta = {"imbalance_ratio": -0.45, "session_start_imbalance": -0.2, "delta_from_session_start": -0.25,
            "band_strikes": 27, "session_key": "2026-11-13", "anchor_observed_at": "2026-11-13T14:31:00+00:00",
            "params": {"near_money_pct": 0.02, "imbalance_threshold": 0.3,
                       "min_change_from_session_start": 0.1, "min_band_strikes": 4}}
    rec = {"strategy_id": "oi_skew_qqq", "strategy_version": "1.0.0", "run_id": "0cea9b6bad38",
           "code_revision": "f" * 40, "timestamp_utc": "2026-11-13T17:34:05+00:00",
           "market_snapshot_timestamp": "2026-11-13T17:33:59+00:00", "account_alias": "OI Skew",
           "account_state_before": {"username": "crassus_oiskew", "balance_cash": 11087.0},
           "decision": {"action": "no_trade", "metadata": meta}}
    rec.update(over)
    return rec


def wait_for(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


# -- configuration gating --------------------------------------------------------
s, why = pub.config_from_environment({})
check("no settings -> off (not_configured)", s is None and why == "not_configured")
partial = dict(GOOD_ENV); del partial["CRASSUS_OI_SKEW_PUBLIC_SECRET_ACCESS_KEY"]
s, why = pub.config_from_environment(partial)
check("partial settings -> off, names the missing variable",
      s is None and "CRASSUS_OI_SKEW_PUBLIC_SECRET_ACCESS_KEY" in why and SECRET not in why)
s, why = pub.config_from_environment({**GOOD_ENV, "CRASSUS_OI_SKEW_PUBLIC_ACCOUNT_ID": "nothex"})
check("invalid account id -> off", s is None and why == "invalid_account_id")
s, why = pub.config_from_environment({**GOOD_ENV, "CRASSUS_ARCHIVE_BUCKET": "public-bucket"})
check("same bucket as the private archive -> off", s is None and why == "bucket_is_the_private_archive_bucket")
s, why = pub.config_from_environment(GOOD_ENV)
check("complete settings -> configured", s is not None and why == "configured")
archive_only = {"CRASSUS_ARCHIVE_BUCKET": "priv", "CRASSUS_ARCHIVE_ACCESS_KEY_ID": "x",
                "CRASSUS_ARCHIVE_SECRET_ACCESS_KEY": "y", "CRASSUS_ARCHIVE_ACCOUNT_ID": "b" * 32}
check("archive credentials are never borrowed", pub.config_from_environment(archive_only)[0] is None)
check("from_environment never raises on a broken client factory",
      pub.from_environment(GOOD_ENV, client_factory=lambda s: (_ for _ in ()).throw(RuntimeError("boom"))) is None)
check("from_environment returns None when unconfigured", pub.from_environment({}) is None)

# -- publish: one key, sanitized, exact body ---------------------------------------
s3 = FakeS3()
p = pub.OiSkewPublisher(s3, "public-bucket")
ok = p.publish_now(record())
put = s3.puts[0] if s3.puts else {}
body = json.loads(put.get("Body", b"{}"))
check("publishes once to the fixed key", ok and len(s3.puts) == 1 and put["Key"] == "crassus/oi-skew/v1/latest.json"
      and put["Bucket"] == "public-bucket")
check("content type json, not cacheable", put.get("ContentType") == "application/json"
      and "no-store" in put.get("CacheControl", ""))
check("body is the v1 projection with status ok", body.get("schema_version") == "oi_skew_meter.v1"
      and body.get("status", {}).get("code") == "ok", body.get("status"))
raw = put.get("Body", b"").decode()
for secret in ("crassus_oiskew", "OI Skew", "0cea9b6bad38", "11087", "balance_cash"):
    check(f"privacy: {secret!r} not uploaded", secret not in raw)

# Unsanitized document is dropped, never uploaded.
s3b = FakeS3()
pb = pub.OiSkewPublisher(s3b, "public-bucket", build=lambda r: {"schema_version": "oi_skew_meter.v1",
                                                               "account_alias": "leak"})
check("a document failing sanitization is not uploaded", pb.publish_now(record()) is False
      and not s3b.puts and pb.dropped_unsanitized == 1)

# Upload failure is counted, not raised.
pf = pub.OiSkewPublisher(FakeS3(fail=ConnectionError("down")), "public-bucket")
check("upload failure returns False, counted", pf.publish_now(record()) is False and pf.failures == 1
      and pf.last_error == "put:ConnectionError")

# Legacy record publishes an explicit unavailable state (truthful), not a guess.
s3c = FakeS3()
legacy = record(); legacy["decision"] = {"action": "no_trade", "metadata": {"imbalance_ratio": 0.1}}
legacy.pop("code_revision")
pub.OiSkewPublisher(s3c, "b").publish_now(legacy)
check("incomplete provenance publishes status unavailable",
      json.loads(s3c.puts[0]["Body"])["status"]["code"] == "unavailable")

# -- offer(): filtering, latest-wins, never blocks --------------------------------
slow = FakeS3(delay=0.3)
pl = pub.OiSkewPublisher(slow, "b").start()
t0 = time.monotonic()
for i in range(20):
    pl.offer(record(timestamp_utc=f"2026-11-13T17:{i:02d}:05+00:00"))
check("offer() never waits on the upload", time.monotonic() - t0 < 0.1, f"{time.monotonic() - t0:.3f}s")
check("latest wins: the final record is published", wait_for(lambda: any(
    json.loads(p_["Body"])["decision_at"] == "2026-11-13T17:19:05+00:00" for p_ in slow.puts)))
check("intermediate records are coalesced", len(slow.puts) < 20, len(slow.puts))
pl.close()
filt = FakeS3(); pfl = pub.OiSkewPublisher(filt, "b").start()
pfl.offer(record(strategy_id="momentum_qqq"))
pfl.offer(record(decision=None))
pfl.offer("not a record")
time.sleep(0.2)
check("other strategies, decision-less records and junk are ignored", filt.puts == [])
pfl.close()

# -- ledger observer hook: never affects the write ----------------------------------
with tempfile.TemporaryDirectory() as tmp:
    ledger = DecisionLedger(Path(tmp), run_id="r1")
    seen = []
    ledger.observers.append(lambda rec: seen.append(rec["decision_id"]))
    ledger.observers.append(lambda rec: (_ for _ in ()).throw(RuntimeError("observer bug")))
    rec = ledger.record(decision_id="d1", account_alias="OI Skew", strategy_id="oi_skew_qqq",
                        strategy_version="1.0.0", outcome_class=Outcome.NO_TRADE,
                        decision={"action": "no_trade", "metadata": {}}, reason="r")
    lines = ledger.paths.ledger.read_text().splitlines()
    check("record is durably written even when an observer raises", len(lines) == 1 and rec["decision_id"] == "d1")
    check("observers see the record after the write", seen == ["d1"])

# -- credentials never reach logs ------------------------------------------------------
buf = io.StringIO()
h = logging.StreamHandler(buf); logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.DEBUG)
pub.from_environment(partial)
pub.from_environment(GOOD_ENV, client_factory=lambda s: (_ for _ in ()).throw(RuntimeError(SECRET)))
pub.OiSkewPublisher(FakeS3(fail=PermissionError(SECRET)), "b").publish_now(record())
logging.getLogger().removeHandler(h)
check("secret never appears in logs", SECRET not in buf.getvalue(), buf.getvalue()[-300:])

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
