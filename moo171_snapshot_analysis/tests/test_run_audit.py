import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from panel import SessionAudit
from run_audit import _multiplier_evidence, _representative_reconciliation, _source_file_sha256


def _option_row(date, snap, strike, symbol, oi, gamma, spot, regular_hours=True):
    return {
        "date": date, "snapshot_key": snap, "regular_hours": regular_hours,
        "OptionSymbol": symbol, "Strike": strike, "OpenInterest": oi, "Gamma": gamma,
        "UnderlyingPrice": spot,
    }


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


def test_representative_reconciliation_matches_hand_computed_c_for_single_contract_strike():
    option_rows = pd.DataFrame([
        _option_row("20260910", "snap1", 700.0, "QQQ260910C00700000", oi=10, gamma=0.02, spot=700.0),
    ])
    result = _representative_reconciliation(option_rows)
    assert result["manual_result"] == (700.0 ** 2) * 100 * 10 * 0.02
    assert result["n_valid_contracts_at_strike"] == 1
    assert result["contributing_rows"][0]["OptionSymbol"] == "QQQ260910C00700000"


def test_representative_reconciliation_sums_every_valid_contract_at_the_strike():
    """A real 0DTE strike almost always has both a call and a put contract
    -- the reconciliation must sum ALL of them (showing each contributing
    row), not require the contrived case of exactly one valid contract."""
    option_rows = pd.DataFrame([
        _option_row("20260910", "snap1", 700.0, "QQQ260910C00700000", oi=10, gamma=0.02, spot=700.0),
        _option_row("20260910", "snap1", 700.0, "QQQ260910P00700000", oi=5, gamma=0.01, spot=700.0),
    ])
    result = _representative_reconciliation(option_rows)
    expected = (700.0 ** 2) * 100 * (10 * 0.02 + 5 * 0.01)
    assert result["manual_result"] == expected
    assert result["n_valid_contracts_at_strike"] == 2
    assert len(result["contributing_rows"]) == 2


def test_multiplier_evidence_reports_zero_mismatches_as_supportive_not_conclusive():
    audits = [_blank_audit("20260910", rows_total=100, symbol_mismatch_rows=0)]
    evidence = _multiplier_evidence(audits)
    assert evidence["symbol_mismatch_rows_across_all_sessions"] == 0
    assert evidence["total_option_rows_checked"] == 100
    assert "not an independent proof" in evidence["interpretation"]
