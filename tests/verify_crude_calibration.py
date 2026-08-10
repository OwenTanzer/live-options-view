"""Prove crude_calibration.py's pure functions: STEO row parsing, vintage
comparison, and OVX regime classification.

Mirrors tests/verify_collector_vwap_rvol.py's conventions (import the module
directly, assert_equal/assert_true, run()) -- no network, no EIA API key,
no R2 access.

    python tests/verify_crude_calibration.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crude_calibration as cc  # noqa: E402


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


def test_parse_steo_rows_groups_by_period_across_series():
    rows = [
        {"period": "2026-07", "seriesId": "BREPUUS", "value": "105.0"},
        {"period": "2026-07", "seriesId": "WTIPUUS", "value": "99.5"},
        {"period": "2026-07", "seriesId": "PATC_WORLD", "value": "-7.6"},
        {"period": "2026-08", "seriesId": "BREPUUS", "value": "102.0"},
    ]
    vintage = cc.parse_steo_rows(rows, release_period="2026-06")
    assert_equal(vintage.release_period, "2026-06", "release_period carried through")
    assert_equal(len(vintage.points), 2, "two distinct forecast periods")

    jul = cc.point_for_period(vintage, "2026-07")
    assert_true(jul is not None, "July point present")
    assert_equal(jul.brent, 105.0, "July Brent")
    assert_equal(jul.wti, 99.5, "July WTI")
    assert_equal(jul.balance, -7.6, "July balance (draw)")

    aug = cc.point_for_period(vintage, "2026-08")
    assert_equal(aug.brent, 102.0, "August Brent")
    assert_equal(aug.wti, None, "August WTI absent from rows -- stays None")


def test_parse_steo_rows_ignores_unrelated_series_and_bad_values():
    rows = [
        {"period": "2026-07", "seriesId": "BREPUUS", "value": "not-a-number"},
        {"period": "2026-07", "seriesId": "SOME_OTHER_SERIES", "value": "999"},
        {"period": None, "seriesId": "WTIPUUS", "value": "50.0"},
    ]
    vintage = cc.parse_steo_rows(rows, release_period="2026-06")
    assert_equal(len(vintage.points), 1, "only the one valid period survives")
    jul = cc.point_for_period(vintage, "2026-07")
    assert_equal(jul.brent, None, "unparseable value becomes None, not a crash")


def test_point_for_period_missing_returns_none():
    vintage = cc.parse_steo_rows([], release_period="2026-06")
    assert_equal(cc.point_for_period(vintage, "2026-07"), None, "empty vintage has no periods")


def test_compare_vintages_computes_deltas_for_shared_period():
    # Mirrors the debate's own comparison: June STEO assumed continued
    # closure (bearish balance, high Brent); July STEO assumed normalization
    # (bullish balance, lower Brent) for the same forecast month.
    prior = cc.parse_steo_rows(
        [
            {"period": "2026-09", "seriesId": "BREPUUS", "value": "105.0"},
            {"period": "2026-09", "seriesId": "WTIPUUS", "value": "99.0"},
            {"period": "2026-09", "seriesId": "PATC_WORLD", "value": "-7.6"},
        ],
        release_period="2026-06",
    )
    current = cc.parse_steo_rows(
        [
            {"period": "2026-09", "seriesId": "BREPUUS", "value": "81.0"},
            {"period": "2026-09", "seriesId": "WTIPUUS", "value": "76.8"},
            {"period": "2026-09", "seriesId": "PATC_WORLD", "value": "2.2"},
        ],
        release_period="2026-07",
    )
    revision = cc.compare_vintages(prior, current, "2026-09")
    assert_true(revision is not None, "shared period produces a revision")
    assert_equal(revision.prior_release, "2026-06", "prior release label")
    assert_equal(revision.current_release, "2026-07", "current release label")
    assert_equal(revision.brent_delta, -24.0, "Brent revised down $24")
    assert_equal(revision.wti_delta, round(76.8 - 99.0, 4), "WTI delta")
    assert_equal(revision.balance_delta, round(2.2 - (-7.6), 4), "balance swung ~9.8M bbl/d toward build")


def test_compare_vintages_returns_none_for_period_absent_in_either_vintage():
    prior = cc.parse_steo_rows(
        [{"period": "2026-09", "seriesId": "BREPUUS", "value": "105.0"}], release_period="2026-06"
    )
    current = cc.parse_steo_rows(
        [{"period": "2026-10", "seriesId": "BREPUUS", "value": "90.0"}], release_period="2026-07"
    )
    assert_equal(cc.compare_vintages(prior, current, "2026-09"), None, "current vintage has no 2026-09")
    assert_equal(cc.compare_vintages(prior, current, "2026-10"), None, "prior vintage has no 2026-10")


def test_compare_vintages_handles_partial_missing_values_without_crashing():
    prior = cc.parse_steo_rows(
        [{"period": "2026-09", "seriesId": "BREPUUS", "value": "105.0"}], release_period="2026-06"
    )
    current = cc.parse_steo_rows(
        [{"period": "2026-09", "seriesId": "WTIPUUS", "value": "76.8"}], release_period="2026-07"
    )
    revision = cc.compare_vintages(prior, current, "2026-09")
    assert_true(revision is not None, "period present in both (even if empty fields) still compares")
    assert_equal(revision.brent_delta, None, "current has no Brent value for this period")
    assert_equal(revision.wti_delta, None, "prior has no WTI value for this period")


def test_classify_ovx_regime_bands():
    assert_equal(cc.classify_ovx_regime(None), "no_data", "missing value")
    assert_equal(cc.classify_ovx_regime(20.0), "calm", "below calm ceiling")
    assert_equal(cc.classify_ovx_regime(34.99), "calm", "just under calm ceiling")
    assert_equal(cc.classify_ovx_regime(35.0), "elevated", "at calm ceiling rolls into elevated")
    assert_equal(cc.classify_ovx_regime(54.99), "elevated", "just under elevated ceiling")
    assert_equal(cc.classify_ovx_regime(55.0), "high", "at elevated ceiling rolls into high")
    assert_equal(cc.classify_ovx_regime(90.0), "high", "well above elevated ceiling")


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
