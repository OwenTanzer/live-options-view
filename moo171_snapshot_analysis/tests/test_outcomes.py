import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from outcomes import (
    assert_non_overlapping,
    build_outcome_dataset,
    generate_anchors,
    select_strikes_for_anchor,
    toward,
)


def _spot_rows(date, times_and_prices):
    return pd.DataFrame([
        {
            "date": date, "snapshot_key": f"snap_{t}", "ts_et": pd.Timestamp(f"2026-09-10 {t}", tz="America/New_York"),
            "regular_hours": True, "underlying_price": price,
        }
        for t, price in times_and_prices
    ])


def test_toward_formula_sign_and_scale():
    # spot moves from 700 to 701, strike at 705: distance shrinks 5 -> 4, positive "toward"
    assert toward(700.0, 701.0, 705.0) == (5 - 4) / 700.0
    # spot moves away from the strike -> negative
    assert toward(700.0, 699.0, 705.0) < 0


def test_generate_anchors_resolves_5min_bins_within_staleness():
    spot = _spot_rows("20260910", [
        ("09:30:00", 700.0),
        ("09:35:05", 701.0),
        ("09:40:02", 702.0),
    ])
    anchors, excluded = generate_anchors(spot)
    assert len(anchors) >= 1
    first = anchors.iloc[0]
    assert first["anchor_spot"] == 700.0
    assert first["outcome_spot"] == 701.0  # first valid snapshot >= 09:35:00
    assert_non_overlapping(anchors)


def test_generate_anchors_skips_bin_with_no_close_enough_snapshot():
    # Gap of >90s around the 09:35 bin boundary -> that anchor's "before"
    # snapshot is far (09:30:10, 290s stale) so it must be skipped, not guessed.
    spot = _spot_rows("20260910", [
        ("09:30:10", 700.0),
        ("09:41:00", 705.0),
    ])
    anchors, excluded = generate_anchors(spot)
    # Every nominal bin is too far from the sparse data (>90s in at least
    # one direction) -- none should be retained, and every drop must be
    # recorded with a reason rather than silently vanishing.
    assert len(anchors) == 0
    assert len(excluded) > 0
    assert set(excluded["reason"]) <= {"no_anchor_snapshot", "no_outcome_snapshot"}


def test_generate_anchors_rejects_a_candidate_that_overlaps_the_prior_window():
    """The review's exact reproduction: a nominal 5-minute grid does not
    guarantee non-overlapping realized windows once anchor resolution goes
    backward and outcome resolution goes forward. A candidate whose actual
    anchor timestamp falls before the previous retained window's outcome
    timestamp must be excluded, not retained."""
    spot = _spot_rows("20260910", [
        ("09:30:00", 700.0),   # bin 09:30 anchor
        ("09:34:00", 700.5),   # would resolve as bin 09:35's "before" anchor
        ("09:36:00", 701.0),   # resolves as bin 09:30's outcome (target 09:35, 60s late, ok)
        ("09:39:00", 703.0),   # resolves as bin 09:40's "before" anchor
        ("09:44:00", 704.0),   # resolves as bin 09:40's outcome
    ])
    anchors, excluded = generate_anchors(spot)
    assert_non_overlapping(anchors)
    # bin 09:30 -> anchor 09:30:00, outcome 09:36:00 (retained).
    # bin 09:35 -> would anchor at 09:34:00, which is BEFORE the prior
    # window's outcome (09:36:00) -- must be excluded as overlapping.
    # bin 09:40 -> anchor 09:39:00 (after 09:36:00, no overlap), outcome 09:44:00 (retained).
    retained_anchor_ts = set(anchors["anchor_ts_et"])
    assert pd.Timestamp("2026-09-10 09:34:00", tz="America/New_York") not in retained_anchor_ts
    assert len(anchors) == 2
    assert "overlaps_prior_window" in excluded["reason"].tolist()


def test_assert_non_overlapping_raises_on_a_constructed_overlap():
    """Direct unit test of the checker itself, independent of
    generate_anchors, per the review's request for a generated-data
    assertion of the non-overlap property."""
    bad_anchors = pd.DataFrame([
        {"date": "20260910", "anchor_ts_et": pd.Timestamp("2026-09-10 09:30:00", tz="America/New_York"),
         "outcome_ts_et": pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York")},
        {"date": "20260910", "anchor_ts_et": pd.Timestamp("2026-09-10 09:34:00", tz="America/New_York"),
         "outcome_ts_et": pd.Timestamp("2026-09-10 09:39:00", tz="America/New_York")},
    ])
    with pytest_raises_assertion():
        assert_non_overlapping(bad_anchors)


def pytest_raises_assertion():
    import pytest
    return pytest.raises(AssertionError)


def test_select_strikes_picks_three_nearest_each_side_only():
    measures = pd.DataFrame([
        {"Strike": s, "C": 1.0, "A": 1.0, "side": "above" if s > 700 else "below", "distance": s - 700}
        for s in [690, 695, 698, 699, 701, 702, 705, 710]
    ])
    selected = select_strikes_for_anchor(measures, spot=700.0)
    above = sorted(selected[selected["Strike"] > 700]["Strike"].tolist())
    below = sorted(selected[selected["Strike"] < 700]["Strike"].tolist())
    assert above == [701, 702, 705]
    assert below == [695, 698, 699]


def test_select_strikes_excludes_rows_missing_features():
    measures = pd.DataFrame([
        {"Strike": 701, "C": None, "A": 1.0, "side": "above", "distance": 1},
        {"Strike": 702, "C": 1.0, "A": 1.0, "side": "above", "distance": 2},
    ])
    selected = select_strikes_for_anchor(measures, spot=700.0)
    assert list(selected["Strike"]) == [702]


def test_build_outcome_dataset_end_to_end():
    anchors = pd.DataFrame([{
        "date": "20260910", "anchor_bin": pd.Timestamp("2026-09-10 09:30", tz="America/New_York"),
        "anchor_snapshot_key": "snap_a", "anchor_ts_et": pd.Timestamp("2026-09-10 09:30:00", tz="America/New_York"),
        "anchor_spot": 700.0, "outcome_snapshot_key": "snap_b",
        "outcome_ts_et": pd.Timestamp("2026-09-10 09:35:00", tz="America/New_York"),
        "outcome_spot": 703.0, "realized_horizon_seconds": 300.0,
    }])
    measures = pd.DataFrame([
        {"date": "20260910", "snapshot_key": "snap_a", "Strike": 705.0, "side": "above", "distance": 5.0,
         "C": 10.0, "A": 5.0, "total_gamma": 0.1, "total_oi": 100, "total_dv": 20,
         "n_contracts": 2, "n_oi_gamma_valid": 2, "n_dv_valid": 2, "n_dv_excluded": 0},
        {"date": "20260910", "snapshot_key": "snap_a", "Strike": 695.0, "side": "below", "distance": -5.0,
         "C": 8.0, "A": 4.0, "total_gamma": 0.08, "total_oi": 90, "total_dv": 15,
         "n_contracts": 2, "n_oi_gamma_valid": 2, "n_dv_valid": 2, "n_dv_excluded": 0},
    ])
    out = build_outcome_dataset(anchors, measures)
    assert len(out) == 2
    row_705 = out[out["strike"] == 705.0].iloc[0]
    assert row_705["toward"] == (5.0 - 2.0) / 700.0  # |700-705|=5 -> |703-705|=2
