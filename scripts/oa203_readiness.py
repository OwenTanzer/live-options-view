#!/usr/bin/env python3
"""Bounded deployment readiness probe; never opens a stream or places orders."""
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
import oa203_return_scanner as s
from moo144_tradier_probe import r2_client

def main():
    required = ["TRADIER_TOKEN", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    assert all(os.getenv(k, "").strip() for k in required), "required credential variable missing"
    mount = Path("/data")
    assert os.path.ismount(mount), "/data is not a mounted volume"
    disk = shutil.disk_usage(mount)
    assert disk.free > 512 * 1024 * 1024, "less than 512 MiB free"
    marker = mount / "oa203-readiness-marker.json"
    previous = json.loads(marker.read_text()) if marker.exists() else None
    nonce = datetime.now(timezone.utc).isoformat()
    with s.published(marker) as tmp:
        tmp.write_text(json.dumps({"written_at": nonce}))
    assert json.loads(marker.read_text())["written_at"] == nonce
    day = datetime.now(s.ET).date()
    trade_day = day
    for _ in range(10):
        if s.nyse_session_bounds(trade_day):
            break
        trade_day += timedelta(days=1)
    else:
        raise RuntimeError("no upcoming exchange session")
    cfg = s.Config(universe_size=3, workers=1, spool_dir=mount / "oa203-readiness",
                   r2_prefix="oa203/readiness", upload=True)
    archive = s.DayArchive(cfg.spool_dir, day, cfg.r2_prefix, True)
    client = s.Tradier(s.load_token(), cfg.max_rpm, cfg.reserve)
    report_day = s.previous_session(trade_day)
    raw = s.fetch_occ_volume(report_day)
    ranked = s.rank_underlyings(s.parse_occ_volume(raw, report_day))
    assert len(ranked) >= 500, "OCC ranking contains fewer than 500 underlyings"
    selected = s.select_universe(client, [(x, {"contracts":0,"calls":0,"puts":0}) for x in ["SPY","QQQ","IBIT"]],
                                 trade_day, 3, 1)
    assert selected["selected_count"] == 3, "smoke universe incomplete"
    archive.write_json("universe.json", selected)
    for sweep in (1,2):
        name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
        entry = s.run_sweep(client, selected["selected"], sweep, archive.path(name), 50, 1)
        assert entry["status_counts"].get("ok") == 3 and entry["rows"] > 0, "chain sampling failed"
        archive.append_manifest(entry)
    rows = s.build_contract_rows(archive.dir, cfg.policy)
    assert rows, "no contract rows reconstructed"
    s.write_outputs(archive.dir, rows)
    assert not archive.reconcile(), "pending uploads"
    r2, bucket = r2_client()
    key = f"{archive.prefix}/sweeps/sweep_0001.jsonl.gz"
    remote = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert hashlib.sha256(remote).hexdigest() == s._sha256_file(archive.path("sweeps/sweep_0001.jsonl.gz")), "R2 readback mismatch"
    assert not client.stats.get("rate_limited"), "rate limited during readiness"
    result = {"event":"oa203_readiness_pass", "checked_at":nonce,
              "commit":os.getenv("RAILWAY_GIT_COMMIT_SHA"),
              "volume_mounted":True, "volume_free_bytes":disk.free,
              "previous_marker":previous, "next_session":trade_day.isoformat(),
              "occ_report_date":report_day.isoformat(), "occ_underlyings":len(ranked),
              "sampled_underlyings":3, "sweeps":2, "contracts":len(rows),
              "r2_readback_verified":True, "pending_uploads":[],
              "rate_stats":dict(client.stats), "market_closed":s.nyse_session_bounds(day) is None,
              "scope":"startup, storage, credentials, sample chains, OCC input and upload/readback; not full-session validation"}
    archive.write_json("readiness.json", result)
    assert archive.push("readiness.json"), "readiness report upload failed"
    print(json.dumps(result, sort_keys=True), flush=True)

if __name__ == "__main__":
    main()
