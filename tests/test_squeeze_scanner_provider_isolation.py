"""Exercise real adapters and CSV export through a mocked HTTP boundary."""

import csv
import json
from copy import deepcopy

import pytest
import requests

from squeeze_scanner import scan as scanner
from squeeze_scanner import tradier_options as tradier
from squeeze_scanner.finviz_client import Candidate
from squeeze_scanner.scoring import FactorInputs


EXPIRATIONS = {"expirations": {"date": ["2099-10-16"]}}
QUOTE = {"quotes": {"quote": {"last": 10.0, "average_volume": 1_000_000}}}
CONTRACT = {
    "option_type": "call", "strike": 10.0, "open_interest": 200,
    "greeks": {"gamma": 0.1, "mid_iv": 0.9},
}
CHAIN = {"options": {"option": [CONTRACT]}}


def _candidate(ticker, price=10.0):
    return Candidate(ticker, ticker, price, FactorInputs(25.0, 5.0, 10_000_000, 2.0, 5.0))


def _response(payload, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("TRADIER_TOKEN", "test-only")
    monkeypatch.setattr(scanner.time, "sleep", lambda _: None)
    monkeypatch.setattr(scanner, "fetch_candidates", lambda **_: [_candidate("BAD"), _candidate("GOOD")])
    calls = []

    def install(endpoint, payload, status=200):
        def get(url, *, params, headers, timeout):
            assert timeout == tradier.REQUEST_TIMEOUT_SECONDS
            ticker = params.get("symbol", params.get("symbols"))
            path = url.rsplit("/", 1)[-1]
            calls.append((ticker, path))
            if ticker == "BAD" and path == endpoint:
                return _response(payload, status)
            return _response({"expirations": EXPIRATIONS, "quotes": QUOTE, "chains": CHAIN}[path])

        monkeypatch.setattr(tradier.requests, "get", get)
        return calls

    return install


MALFORMED = [
    ("quotes", None), ("quotes", []), ("quotes", {}),
    ("quotes", {"quotes": None}), ("quotes", {"quotes": []}),
    *[("quotes", {"quotes": {"quote": value}}) for value in
      (None, [], "bad", 10, [None], ["bad"], [{}, {}])],
    *[("quotes", {"quotes": {"quote": {"last": value, "average_volume": 1_000_000}}})
      for value in (float("nan"), float("inf"), -1, 0, True, "10", {}, 10**400)],
    *[("quotes", {"quotes": {"quote": {"last": 10, "average_volume": value}}})
      for value in (float("nan"), -1, True, "bad", [])],
    ("expirations", None), ("expirations", {}),
    *[("expirations", {"expirations": value}) for value in ([], "bad", 0, {})],
    *[("expirations", {"expirations": {"date": value}})
      for value in (0, {}, [None], [True], "", "not-a-date", ["2099-10-16", "bad"], ["2099-02-30"])],
    ("chains", None), ("chains", []), ("chains", {}),
    ("chains", {"options": None}), ("chains", {"options": {}}),
    *[("chains", {"options": {"option": value}})
      for value in ("bad", 0, [None], ["bad"], [{}])],
]


@pytest.mark.parametrize("endpoint,payload", MALFORMED)
def test_bad_provider_payload_isolated_and_good_candidate_exported(provider, monkeypatch, tmp_path, endpoint, payload):
    calls = provider(endpoint, payload)
    output = tmp_path / "scan.csv"
    monkeypatch.setattr("sys.argv", ["scan", "--out", str(output)])
    with pytest.raises(SystemExit) as exc:
        scanner.main()
    assert exc.value.code == 1  # incomplete scan must not look successful
    with output.open() as stream:
        rows = {row["ticker"]: row for row in csv.DictReader(stream)}
    assert rows["BAD"]["options_status"] == "error"
    assert rows["GOOD"]["options_status"] == "valid"
    assert ("GOOD", "chains") in calls


@pytest.mark.parametrize("field,value", [
    ("greeks", []), ("greeks", "bad"), ("greeks", False),
    ("strike", None), ("strike", 0), ("strike", -10), ("strike", float("nan")),
    ("open_interest", -1), ("open_interest", 0.5), ("open_interest", True),
    ("open_interest", "100"), ("open_interest", float("inf")),
    ("option_type", "invalid"), ("option_type", None),
    ("gamma", -0.1), ("gamma", float("nan")), ("gamma", "bad"), ("gamma", True),
    ("mid_iv", -0.9), ("mid_iv", float("inf")), ("smv_vol", {}),
])
def test_bad_contract_or_greeks_does_not_escape_adapter(provider, field, value):
    contract = deepcopy(CONTRACT)
    target = contract["greeks"] if field in ("gamma", "mid_iv", "smv_vol") else contract
    target[field] = value
    provider("chains", {"options": {"option": [contract]}})
    rows = {row["ticker"]: row for row in scanner.scan()}
    assert rows["BAD"]["options_status"] == "error"
    assert rows["GOOD"]["options_status"] == "valid"


@pytest.mark.parametrize("endpoint,payload", [
    ("expirations", {"expirations": None}),
    ("expirations", {"expirations": {"date": None}}),
    ("expirations", {"expirations": {"date": []}}),
    ("expirations", {"expirations": {"date": "2000-01-01"}}),
    ("chains", {"options": {"option": None}}),
    ("chains", {"options": {"option": []}}),
    ("chains", {"options": {"option": {**CONTRACT, "greeks": None}}}),
    ("quotes", {"quotes": {"quote": {"last": 10.0}}}),
])
def test_genuine_missing_data_remains_unavailable(provider, endpoint, payload):
    provider(endpoint, payload)
    rows = {row["ticker"]: row for row in scanner.scan()}
    assert rows["BAD"]["options_status"] == "unavailable"
    assert rows["GOOD"]["options_status"] == "valid"


@pytest.mark.parametrize("endpoint,payload", [
    ("quotes", {"quotes": {"quote": [QUOTE["quotes"]["quote"]]}}),
    ("quotes", {"quotes": {"quote": {"last": None, "average_volume": 1_000_000}}}),
    ("expirations", {"expirations": {"date": "2099-10-16"}}),
    ("chains", {"options": {"option": CONTRACT}}),
])
def test_valid_singleton_shapes_and_missing_quote_price_fallback(provider, endpoint, payload):
    provider(endpoint, payload)
    assert all(row["options_status"] == "valid" for row in scanner.scan())


@pytest.mark.parametrize("endpoint", ["expirations", "quotes", "chains"])
def test_rate_limit_stops_requests_but_preserves_all_candidates(provider, monkeypatch, tmp_path, endpoint):
    calls = provider(endpoint, {"error": "rate limit"}, status=429)
    monkeypatch.setattr(scanner, "fetch_candidates", lambda **_: [_candidate("BEFORE"), _candidate("BAD"), _candidate("AFTER")])
    output = tmp_path / "limited.csv"
    monkeypatch.setattr("sys.argv", ["scan", "--out", str(output)])
    with pytest.raises(SystemExit) as exc:
        scanner.main()
    assert exc.value.code == 1
    assert calls[-1] == ("BAD", endpoint)
    assert not any(ticker == "AFTER" for ticker, _ in calls)
    with output.open() as stream:
        rows = {row["ticker"]: row for row in csv.DictReader(stream)}
    assert rows["BEFORE"]["options_status"] == "valid"
    assert rows["BAD"]["options_status"] == "error"
    assert rows["AFTER"]["options_status"] == "skipped_rate_limit"


@pytest.mark.parametrize("mode", ["invalid_json", "timeout", "http_error"])
def test_transport_failures_preserve_later_candidates(provider, monkeypatch, mode):
    provider("unused", None)
    original_get = tradier.requests.get

    def get(url, **kwargs):
        if kwargs["params"].get("symbols") == "BAD":
            if mode == "timeout":
                raise requests.Timeout("timeout")
            response = _response({}, status=503 if mode == "http_error" else 200)
            if mode == "invalid_json":
                response._content = b"not json"
            return response
        return original_get(url, **kwargs)

    monkeypatch.setattr(tradier.requests, "get", get)
    rows = {row["ticker"]: row for row in scanner.scan()}
    assert rows["BAD"]["options_status"] == "error"
    assert rows["GOOD"]["options_status"] == "valid"


@pytest.mark.parametrize("price", [float("nan"), -1, "bad"])
def test_invalid_finviz_fallback_price_is_isolated(provider, monkeypatch, price):
    provider("quotes", {"quotes": {"quote": {"last": None, "average_volume": 1_000_000}}})
    monkeypatch.setattr(scanner, "fetch_candidates", lambda **_: [_candidate("BAD", price), _candidate("GOOD")])
    rows = {row["ticker"]: row for row in scanner.scan()}
    assert rows["BAD"]["options_status"] == "error"
    assert rows["GOOD"]["options_status"] == "valid"


@pytest.mark.parametrize("endpoint,payload", [
    ("quotes", {"quotes": {"quote": {"last": 1e200, "average_volume": 1e200}}}),
    ("quotes", {"quotes": {"quote": {"last": 1.0, "average_volume": 5e-324}}}),
    ("chains", {"options": {"option": {**CONTRACT, "open_interest": 1e308}}}),
    ("chains", {"options": {"option": [
        {**CONTRACT, "open_interest": 1e308, "greeks": {"gamma": 0, "mid_iv": 0.9}},
        {**CONTRACT, "open_interest": 1e308, "greeks": {"gamma": 0, "mid_iv": 0.9}},
    ]}}),
])
def test_derived_numeric_overflow_is_isolated(provider, endpoint, payload):
    provider(endpoint, payload)
    rows = {row["ticker"]: row for row in scanner.scan()}
    assert rows["BAD"]["options_status"] == "error"
    assert rows["GOOD"]["options_status"] == "valid"


@pytest.mark.parametrize("mode", ["valid", "empty", "unavailable"])
def test_complete_cli_exports_without_error_exit(provider, monkeypatch, tmp_path, mode):
    provider("unused", None)
    if mode == "empty":
        monkeypatch.setattr(scanner, "fetch_candidates", lambda **_: [])
    elif mode == "unavailable":
        provider("expirations", {"expirations": None})
    output = tmp_path / "complete.csv"
    monkeypatch.setattr("sys.argv", ["scan", "--out", str(output)])
    scanner.main()
    with output.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == (0 if mode == "empty" else 2)


@pytest.mark.parametrize("error", [ValueError("bug"), RuntimeError("bug"), TypeError("bug")])
def test_unexpected_programming_errors_are_not_silenced(provider, monkeypatch, error):
    provider("unused", None)

    def broken_summary(*args, **kwargs):
        raise error

    monkeypatch.setattr(scanner, "summarize_chain", broken_summary)
    with pytest.raises(type(error), match="bug"):
        scanner.scan()
