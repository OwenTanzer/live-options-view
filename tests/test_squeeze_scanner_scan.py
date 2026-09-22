"""Regression tests for squeeze_scanner.scan's per-candidate isolation.

PR #99 follow-up review, gap 1: a malformed provider payload for one
candidate (previously an uncaught AttributeError from a null 'quotes' or
'options' object) aborted scan() entirely, so main() never wrote the
accumulated output for every OTHER candidate already processed. These
tests mock the network boundary (fetch_candidates, get_nearest_expiration,
get_quote, get_option_chain) to prove a malformed first candidate doesn't
prevent the second from being scored and returned.
"""

from unittest.mock import patch

from squeeze_scanner.finviz_client import Candidate
from squeeze_scanner.scan import (
    OPTIONS_STATUS_ERROR,
    OPTIONS_STATUS_VALID,
    scan,
)
from squeeze_scanner.scoring import FactorInputs
from squeeze_scanner.tradier_options import TradierDataError

_FACTOR_INPUTS = FactorInputs(
    short_float_pct=25.0, days_to_cover=5.0, float_shares=10_000_000,
    relative_volume=2.0, price_change_5d_pct=5.0,
)


def _candidate(ticker: str) -> Candidate:
    return Candidate(ticker=ticker, company=f"{ticker} Inc", price=10.0, factor_inputs=_FACTOR_INPUTS)


def _valid_chain():
    return [
        {"option_type": "call", "strike": 10.0, "open_interest": 200,
         "greeks": {"gamma": 0.1, "mid_iv": 0.9}},
        {"option_type": "put", "strike": 10.0, "open_interest": 100,
         "greeks": {"gamma": 0.1, "mid_iv": 0.9}},
    ]


def test_malformed_first_candidate_does_not_block_the_second():
    candidates = [_candidate("BAD"), _candidate("GOOD")]

    def fake_get_nearest_expiration(ticker):
        return "2026-10-17"

    def fake_get_quote(ticker):
        if ticker == "BAD":
            # What get_quote itself now raises for a malformed Tradier
            # payload -- previously this path was a bare AttributeError
            # from `None.get(...)`, uncaught by scan.py.
            raise TradierDataError("Tradier returned no quote data for BAD")
        return {"last": 10.0, "average_volume": 1_000_000}

    def fake_get_option_chain(ticker, expiration):
        return _valid_chain()

    with patch("squeeze_scanner.scan.fetch_candidates", return_value=candidates), \
         patch("squeeze_scanner.scan.get_nearest_expiration", side_effect=fake_get_nearest_expiration), \
         patch("squeeze_scanner.scan.get_quote", side_effect=fake_get_quote), \
         patch("squeeze_scanner.scan.get_option_chain", side_effect=fake_get_option_chain):
        results = scan(limit=10)

    tickers_seen = {r["ticker"] for r in results}
    assert tickers_seen == {"BAD", "GOOD"}, "the second candidate must still be scored and returned"

    by_ticker = {r["ticker"]: r for r in results}
    assert by_ticker["BAD"]["options_status"] == OPTIONS_STATUS_ERROR
    assert by_ticker["GOOD"]["options_status"] == OPTIONS_STATUS_VALID


def test_malformed_option_chain_payload_does_not_block_later_candidates():
    candidates = [_candidate("A"), _candidate("B"), _candidate("C")]

    def fake_get_option_chain(ticker, expiration):
        if ticker == "B":
            raise TradierDataError(f"Tradier chain response for {ticker}@{expiration} had no usable 'options' object")
        return _valid_chain()

    with patch("squeeze_scanner.scan.fetch_candidates", return_value=candidates), \
         patch("squeeze_scanner.scan.get_nearest_expiration", return_value="2026-10-17"), \
         patch("squeeze_scanner.scan.get_quote", return_value={"last": 10.0, "average_volume": 1_000_000}), \
         patch("squeeze_scanner.scan.get_option_chain", side_effect=fake_get_option_chain):
        results = scan(limit=10)

    assert {r["ticker"] for r in results} == {"A", "B", "C"}
    by_ticker = {r["ticker"]: r for r in results}
    assert by_ticker["B"]["options_status"] == OPTIONS_STATUS_ERROR
    assert by_ticker["A"]["options_status"] == OPTIONS_STATUS_VALID
    assert by_ticker["C"]["options_status"] == OPTIONS_STATUS_VALID
