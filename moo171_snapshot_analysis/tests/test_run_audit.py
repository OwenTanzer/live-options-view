import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

import run_audit
from panel import SessionAudit, parse_snapshot_key, recompute_interval_volume
from r2_source import ManifestMismatch, SnapshotSource
from run_audit import (
    ReconciliationMismatch, _multiplier_evidence, _representative_reconciliation, _source_file_sha256,
    current_config, main, verify_frozen_identity,
)
from run_measures import check_reconciliation

DATE = "20260910"
HEADER = (
    "TradeDate,Expiration,Strike,Type,OptionSymbol,DTE,OpenInterest,Volume,VolDelta,"
    "Bid,Mid,Ask,Last,IV,Delta,Gamma,Theta,Vega,UnderlyingPrice"
)
CALL, PUT = "QQQ260910C00700000", "QQQ260910P00700000"


def _csv(rows: list[tuple]) -> bytes:
    """rows: (type, symbol, oi, volume, gamma, spot) -- gamma '' = missing."""
    lines = [HEADER]
    for typ, sym, oi, vol, gamma, spot in rows:
        lines.append(
            f"2026-09-10,2026-09-10,700.0,{typ},{sym},0,{oi},{vol},0,1.0,1.1,1.2,,0.2,0.5,{gamma},-0.1,0.01,{spot}"
        )
    return ("\n".join(lines) + "\n").encode()


# Three consecutive regular-hours snapshots, 30s apart. The put's Gamma is
# missing at 09:30:30, so it has an ok dV there but must be excluded from A
# by the joint dV/Gamma validity rule.
SNAPSHOTS = {
    "093000000000": [("call", CALL, 10, 10, 0.02, 700.0), ("put", PUT, 5, 5, 0.01, 700.0)],
    "093030000000": [("call", CALL, 10, 25, 0.02, 701.0), ("put", PUT, 5, 8, "", 701.0)],
    "093100000000": [("call", CALL, 10, 40, 0.03, 702.0), ("put", PUT, 5, 12, 0.01, 702.0)],
}


def _key(hhmmss: str) -> str:
    return f"intraday/{DATE}/snapshot_{hhmmss}.csv"


def _seed_cache(cache_dir: Path, snapshots: dict = SNAPSHOTS) -> None:
    listing = []
    for hhmmss, rows in snapshots.items():
        body = _csv(rows)
        path = cache_dir / _key(hhmmss)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        listing.append({"key": _key(hhmmss), "size": len(body), "etag": hashlib.md5(body).hexdigest()})
    (cache_dir / f"intraday/{DATE}/_listing.json").write_text(json.dumps(listing))


def _run(tmp_path: Path, *argv: str) -> int:
    source = SnapshotSource(cache_dir=tmp_path / "cache", s3=None)
    return main(list(argv), source=source, out_dir=tmp_path / "out", dates=[DATE])


def _option_rows() -> pd.DataFrame:
    """The SNAPSHOTS fixture as panel-shaped option rows with dV/dv_flag."""
    records = []
    for hhmmss, rows in SNAPSHOTS.items():
        key = _key(hhmmss)
        _, ts_et, _ = parse_snapshot_key(key)
        for typ, sym, oi, vol, gamma, spot in rows:
            records.append({
                "date": DATE, "snapshot_key": key, "ts_et": ts_et, "regular_hours": True,
                "Type": typ, "OptionSymbol": sym, "Strike": 700.0, "OpenInterest": float(oi),
                "Volume": float(vol), "Gamma": float("nan") if gamma == "" else float(gamma),
                "UnderlyingPrice": spot,
            })
    return recompute_interval_volume(pd.DataFrame(records))


def _blank_audit(date, rows_total=0, symbol_mismatch_rows=0):
    return SessionAudit(
        date=date, listed_objects=0, excluded_non_snapshot=[], snapshot_count=0, reported_count=0,
        first_ts_et=None, last_ts_et=None, premarket_snapshots=0, regular_hours_snapshots=0,
        afterhours_snapshots=0, gap_seconds=[], max_gap_seconds=None, distinct_strikes=0,
        distinct_contracts=0, rows_total=rows_total, field_coverage={}, nonfinite_greeks=0,
        negative_volumes=0, negative_open_interest=0, crossed_quotes=0,
        duplicate_snapshot_symbol_rows=0, symbol_mismatch_rows=symbol_mismatch_rows,
        spot_inconsistent_snapshots=0, contracts_with_oi_change=0, contracts_with_oi_baseline=0,
        max_repeated_mid_run=0,
    )


def test_source_file_sha256_includes_this_repos_own_files_and_is_content_addressed():
    hashes = _source_file_sha256()
    assert "run_audit.py" in hashes
    assert "measures.py" in hashes
    assert len(hashes["run_audit.py"]) == 64  # hex sha256


# --- C/A reconciliation -------------------------------------------------

def test_c_reconciliation_sums_every_valid_contract_and_agrees_with_measures():
    """A real 0DTE strike almost always has both a call and a put contract
    -- the reconciliation must sum ALL of them (showing each contributing
    row) and record agreement with the actual measures output."""
    c = _representative_reconciliation(_option_rows())["C"]
    assert c["snapshot_key"] == _key("093000000000")
    assert c["manual_result"] == pytest.approx((700.0 ** 2) * 100 * (10 * 0.02 + 5 * 0.01), rel=1e-12)
    assert c["n_valid_contracts_at_strike"] == 2
    assert len(c["contributing_rows"]) == 2
    assert c["agrees"] is True
    assert c["measure_output"] == pytest.approx(c["manual_result"], rel=1e-12)


def test_a_reconciliation_shows_consecutive_volumes_and_joint_validity_exclusion():
    a = _representative_reconciliation(_option_rows())["A"]
    assert a["snapshot_key"] == _key("093030000000")
    rows = {r["OptionSymbol"]: r for r in a["contract_rows"]}

    call = rows[CALL]
    assert call["prior_snapshot_key"] == _key("093000000000")
    assert (call["prior_cumulative_volume"], call["current_cumulative_volume"]) == (10.0, 25.0)
    assert call["elapsed_seconds"] == 30.0
    assert call["dV_derived_here"] == call["dV_from_panel"] == 15.0
    assert call["dv_flag"] == "ok" and call["included"] is True

    # Put has an ok dV (5 -> 8) but missing Gamma on the SAME contract: it
    # must be shown and excluded, not paired with the call's gamma.
    put = rows[PUT]
    assert put["dv_flag"] == "ok" and put["dV_derived_here"] == 3.0
    assert put["included"] is False and "joint dV/Gamma" in put["exclusion_reason"]

    assert a["n_included_contracts"] == 1
    assert a["manual_result"] == pytest.approx((701.0 ** 2) * 100 * 15 * 0.02, rel=1e-12)
    assert a["agrees"] is True


def test_reconciliation_raises_when_measures_output_disagrees(monkeypatch):
    real = run_audit.compute_concentration_and_activity

    def off_by_one_a(rows):
        out = real(rows)
        out["A"] = out["A"] + 1.0
        return out

    monkeypatch.setattr(run_audit, "compute_concentration_and_activity", off_by_one_a)
    with pytest.raises(ReconciliationMismatch, match="A: manual"):
        _representative_reconciliation(_option_rows())


def test_run_measures_check_reconciliation_compares_written_measures(tmp_path):
    option_rows = _option_rows()
    recon = _representative_reconciliation(option_rows)
    manifest = tmp_path / "source_manifest.json"
    manifest.write_text(json.dumps({"representative_reconciliation": recon}, default=str))
    measures = run_audit.compute_concentration_and_activity(option_rows[option_rows["regular_hours"]])
    assert check_reconciliation(measures, manifest) == []

    measures.loc[measures["snapshot_key"] == recon["C"]["snapshot_key"], "C"] *= 2
    problems = check_reconciliation(measures, manifest)
    assert len(problems) == 1 and problems[0].startswith("C:")


def test_multiplier_evidence_reports_zero_mismatches_as_supportive_not_conclusive():
    audits = [_blank_audit("20260910", rows_total=100, symbol_mismatch_rows=0)]
    evidence = _multiplier_evidence(audits)
    assert evidence["symbol_mismatch_rows_across_all_sessions"] == 0
    assert evidence["total_option_rows_checked"] == 100
    assert "not an independent proof" in evidence["interpretation"]


# --- frozen-manifest reproduction path ----------------------------------

def test_verify_run_without_a_frozen_manifest_refuses(tmp_path):
    _seed_cache(tmp_path / "cache")
    assert _run(tmp_path) == 2
    assert not (tmp_path / "out").exists()


def test_new_run_then_verify_with_unchanged_bytes_retains_manifest(tmp_path):
    _seed_cache(tmp_path / "cache")
    assert _run(tmp_path, "--new-run") == 0
    manifest_path = tmp_path / "out" / "source_manifest.json"
    frozen_bytes = manifest_path.read_bytes()
    frozen = json.loads(frozen_bytes)
    assert all(e["sha256"] for e in frozen["objects"][DATE])
    assert frozen["representative_reconciliation"]["A"]["agrees"] is True

    # Evidence is LF on every platform, so its hash matches a fresh checkout.
    assert b"\r" not in frozen_bytes
    assert b"\r" not in (tmp_path / "out" / "audit_report.json").read_bytes()

    assert _run(tmp_path) == 0
    assert manifest_path.read_bytes() == frozen_bytes  # retained, not rewritten
    assert b"\r" not in (tmp_path / "out" / "reproduction_check.json").read_bytes()
    check = json.loads((tmp_path / "out" / "reproduction_check.json").read_text())
    assert check["objects_verified"] == len(SNAPSHOTS)
    assert check["frozen_manifest_sha256"] == hashlib.sha256(frozen_bytes).hexdigest()


def test_verify_fails_on_modified_cached_bytes_and_replaces_no_evidence(tmp_path):
    """The review's repro end to end: same key and listing (old ETag kept),
    Gamma changed 0.02 -> 9.99 in the cached bytes."""
    _seed_cache(tmp_path / "cache")
    assert _run(tmp_path, "--new-run") == 0
    out = tmp_path / "out"
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    tampered = tmp_path / "cache" / _key("093030000000")
    tampered.write_bytes(tampered.read_bytes().replace(b",0.02,", b",9.99,"))

    assert _run(tmp_path) == 1
    after = {p.name: p.read_bytes() for p in out.iterdir()}
    assert after == before  # manifest, audit report, parquets all untouched
    assert "reproduction_check.json" not in after


def test_verify_fails_when_the_listed_snapshot_set_changes(tmp_path):
    _seed_cache(tmp_path / "cache")
    assert _run(tmp_path, "--new-run") == 0
    frozen_bytes = (tmp_path / "out" / "source_manifest.json").read_bytes()

    extra = dict(SNAPSHOTS)
    extra["093130000000"] = SNAPSHOTS["093100000000"]
    _seed_cache(tmp_path / "cache", extra)

    assert _run(tmp_path) == 1
    assert (tmp_path / "out" / "source_manifest.json").read_bytes() == frozen_bytes


def test_verify_frozen_identity_reports_config_and_source_mismatches():
    config = current_config()
    hashes = {"measures.py": "a" * 64}
    frozen = {
        "config": config, "source_file_sha256": hashes,
        "objects": {DATE: [{"key": _key("093000000000"), "sha256": "b" * 64}]},
    }
    assert verify_frozen_identity(frozen, config, hashes, [DATE]) == {_key("093000000000"): "b" * 64}

    changed_config = {**config, "MAX_INTERVAL_SECONDS": 120.0}
    with pytest.raises(ManifestMismatch, match="config differs"):
        verify_frozen_identity(frozen, changed_config, hashes, [DATE])
    with pytest.raises(ManifestMismatch, match="source file measures.py"):
        verify_frozen_identity(frozen, config, {"measures.py": "c" * 64}, [DATE])

    unhashed = {**frozen, "objects": {DATE: [{"key": _key("093000000000"), "sha256": None}]}}
    with pytest.raises(ManifestMismatch, match="unverifiable"):
        verify_frozen_identity(unhashed, config, hashes, [DATE])
