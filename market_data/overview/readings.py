"""Reference market readings computed from the collector's own snapshot rows.

Issue #107 separates a market-wide *measurement* from any bot's interpretation
of it, and the merged #110 ownership table decides which readings get a
standing reference series, which stay bot-local, and which are shared only as
decision context. This module publishes exactly those reference readings from
the same board and underlying-market values the strategies see:

| Reading | Shared reference series | Bot-local remainder |
|---|---|---|
| VWAP | yes | `momentum_qqq` gate verdict/thresholds |
| RVOL | yes | `rvol_floor` gate |
| Reference momentum | yes | each account's own lookback/thresholds |
| PCR | yes (ratio + OI totals) | z-score baseline, threshold |
| Max pain | yes (strike + coverage) | pin threshold, min strikes |
| 2% OI skew | yes at the reference band | account band, drift rule |

Trading momentum and the snapshot-IV/Black-Scholes comparison are deliberately
**not** standing series: #110 keeps trading momentum per-account, and keeps the
Black-Scholes diagnostic on proposed buys only until Greeks carry observation
times. Those routes stay in the private ledger; the immutable join published
here lets any such decision be tied back to the exact snapshot and reference
readings it was made against.

The envelope is `market_reading.v1` from
`crassus/docs/2026-10-market-reading-contract.md`. Unknown fields stay null
with a stated reason, `interpretation` is always `reference`, and the OI
zero-vs-missing ambiguity travels as a caveat rather than being resolved.

The formulas mirror `crassus/crassus/strategies/pcr.py`, `max_pain.py` and
`oi_skew.py`; `tests/test_market_readings.py` runs both implementations over
the same fixtures so drift is caught rather than silently published under a
shared name.
"""

from __future__ import annotations

import hashlib
import json
from math import isfinite
from statistics import median
from zoneinfo import ZoneInfo
from datetime import datetime
from typing import Any

READING_SCHEMA = "market_reading.v1"
READING_JOIN_SCHEMA = "market_readings_join.v1"
REFERENCE_OI_SKEW_BAND = 0.02

CALCULATION_VERSIONS = {
    "qqq_iv_header": "qqq_iv_header.v1",
    "reference_vwap": "reference_vwap.v1",
    "reference_rvol": "reference_rvol.v1",
    "reference_momentum": "reference_momentum.v1",
    "reference_pcr": "reference_pcr.v1",
    "reference_max_pain": "reference_max_pain.v1",
    "reference_oi_skew": "reference_oi_skew.v1",
}

# Defaults match market_data/live_market/config.py; the collector passes the
# effective values it actually used, so a config change cannot leave the
# published parameters claiming an old setting.
DEFAULT_PARAMETERS = {
    "vwap": {"method": "snapshot_weighted_volume_delta"},
    "rvol": {"bucket_minutes": 5, "lookback_days": 20, "min_days_required": 5},
    "momentum": {"lookback_minutes": 60.0, "max_anchor_overshoot_minutes": 10.0,
                 "neutral_band_pct": 0.05, "neutral_band_use": "display_only"},
}

_OI_CAVEATS = ["oi_zero_or_missing_indistinguishable", "oi_prior_day_settled"]


def compute_reference_pcr(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Put/call open-interest ratio over the whole board.

    `None` when call open interest is zero: undefined, not zero or infinite.
    Mirrors `put_call_ratio_qqq.compute_pcr`.
    """
    put_oi = sum(r.get("OpenInterest") or 0 for r in rows if r.get("Type") == "put")
    call_oi = sum(r.get("OpenInterest") or 0 for r in rows if r.get("Type") == "call")
    ratio = put_oi / call_oi if call_oi > 0 else None
    nonzero = sum(1 for r in rows if (r.get("OpenInterest") or 0) > 0)
    return {
        "values": {
            "put_call_oi_ratio": ratio,
            "put_open_interest": put_oi,
            "call_open_interest": call_oi,
            "rows_with_nonzero_oi": nonzero,
        },
        "units": {
            "put_call_oi_ratio": "dimensionless",
            "put_open_interest": "contracts",
            "call_open_interest": "contracts",
            "rows_with_nonzero_oi": "count",
        },
        "status": ({"code": "undefined", "reason": "zero_call_oi"} if ratio is None
                   else {"code": "ok", "reason": None}),
    }


def compute_reference_max_pain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Max-pain strike over every candidate strike on the board.

    Mirrors `max_pain_qqq._compute_max_pain` for every board that carries open
    interest, so the supported formula stays parity-checked. A board with no
    open-interest rows at all -- everything missing or zero, which this feed
    cannot distinguish -- has no loss surface to minimise; returning the lowest
    strike there would present an unsupported level as a reading. That case is
    `no_data/no_open_interest_surface` with a null strike. The strategy's own
    guard remains strategy-owned and is not changed by this reading.
    """
    calls = [r for r in rows if r.get("Type") == "call" and r.get("OpenInterest")]
    puts = [r for r in rows if r.get("Type") == "put" and r.get("OpenInterest")]
    strikes = sorted({r["Strike"] for r in rows if r.get("Strike") is not None})
    both_sides = len({r["Strike"] for r in calls} & {r["Strike"] for r in puts})
    with_oi = len(calls) + len(puts)

    def payout_at(strike: float) -> float:
        total = 0.0
        for row in calls:
            total += max(0.0, strike - row["Strike"]) * row["OpenInterest"]
        for row in puts:
            total += max(0.0, row["Strike"] - strike) * row["OpenInterest"]
        return total

    if with_oi == 0:
        strike = None
        status = {"code": "no_data", "reason": "no_open_interest_surface"}
    elif not strikes:
        strike = None
        status = {"code": "no_data", "reason": "no_strike_data"}
    else:
        strike = min(strikes, key=lambda k: (payout_at(k), k))
        status = {"code": "ok", "reason": None}
    return {
        "values": {
            "max_pain_strike": strike,
            "candidate_strikes": len(strikes),
            "strikes_with_oi_both_sides": both_sides,
            "rows_with_open_interest": with_oi,
        },
        "units": {
            "max_pain_strike": "USD",
            "candidate_strikes": "count",
            "strikes_with_oi_both_sides": "count",
            "rows_with_open_interest": "count",
        },
        "status": status,
    }


def compute_reference_oi_skew(
    rows: list[dict[str, Any]], underlying: float | None,
    near_money_pct: float = REFERENCE_OI_SKEW_BAND,
) -> dict[str, Any]:
    """Near-money OI imbalance in the fixed 2% reference band.

    Mirrors `oi_skew_qqq._near_money_oi` with the reference band; account
    bands and drift rules remain bot-local.
    """
    call_oi = 0.0
    put_oi = 0.0
    band_strikes: set[float] = set()
    if underlying:
        for row in rows:
            strike = row.get("Strike")
            if strike is None:
                continue
            if abs(strike - underlying) / underlying > near_money_pct:
                continue
            oi = row.get("OpenInterest") or 0
            if oi <= 0:
                continue
            band_strikes.add(strike)
            if row.get("Type") == "call":
                call_oi += oi
            elif row.get("Type") == "put":
                put_oi += oi
    total = call_oi + put_oi
    imbalance = (call_oi - put_oi) / total if total > 0 else None
    return {
        "values": {
            "near_money_call_oi": call_oi,
            "near_money_put_oi": put_oi,
            "imbalance_ratio": imbalance,
            "band_strikes": len(band_strikes),
        },
        "units": {
            "near_money_call_oi": "contracts",
            "near_money_put_oi": "contracts",
            "imbalance_ratio": "dimensionless",
            "band_strikes": "count",
        },
        "status": ({"code": "ok", "reason": None} if imbalance is not None
                   else {"code": "no_data", "reason": "no_open_interest_in_reference_band"}),
        "parameters": {"near_money_pct": near_money_pct, "band": "reference",
                       "account_specific": False},
    }


def readings_digest(readings: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical reading list.

    The same canonicalization is recomputed on the Crassus side for the
    immutable join; whitespace, insertion order and float formatting are fixed
    by `sort_keys` + compact separators rather than by Python repr.
    """
    canonical = json.dumps(readings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _envelope(
    *, reading_id: str, symbol: str, snapshot_key: str, snapshot_timestamp: str,
    calculated_at: datetime, values: dict[str, Any], units: dict[str, Any],
    status: dict[str, Any], calculation_parameters: dict[str, Any],
    freshness: str, observed_at: str | None, caveats: list[str],
    coverage: dict[str, Any], input_times: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": READING_SCHEMA,
        "reading_id": reading_id,
        "symbol": symbol,
        "observed_at": observed_at,
        "input_observed_at": {
            "spot": observed_at, "quote": None, "greeks": None, "anchor": None,
            **(input_times or {}),
        },
        "calculated_at": calculated_at.isoformat(),
        "published_at": None,
        "publication": {"status": "pending", "reason": None},
        "source": {"producer": "live_market_collector", "feed": "dxlink"},
        "input": {
            "snapshot_key": snapshot_key,
            "snapshot_key_status": "valid",
            "snapshot_timestamp": snapshot_timestamp,
            "payload_url": None,
            "payload_sha256": None,
        },
        "calculation": {
            "version": CALCULATION_VERSIONS[reading_id],
            "parameters": calculation_parameters,
        },
        "interpretation": {"kind": "reference", "account_alias": None},
        "values": values,
        "units": units,
        "status": status,
        "freshness": {
            "status": freshness,
            "evaluated_at": calculated_at.isoformat(),
            "parameters": {"definition": "underlying_selected_receipt"},
        },
        "coverage": coverage,
        "caveats": caveats,
    }


def _market_reading(
    *, reading_id: str, market: dict[str, Any], symbol: str, snapshot_key: str,
    snapshot_timestamp: str, calculated_at: datetime, freshness: str,
    parameters: dict[str, Any], coverage: dict[str, Any],
) -> dict[str, Any]:
    """One of the three readings that already live in `underlying_market`."""
    if reading_id == "reference_vwap":
        vwap = market.get("vwap")
        values = {
            "vwap": vwap,
            "spot": market.get("spot"),
            "price_vs_vwap_abs": market.get("price_vs_vwap_abs"),
            "price_vs_vwap_pct": market.get("price_vs_vwap_pct"),
            "session_volume": market.get("session_volume"),
        }
        return _envelope(
            reading_id=reading_id, symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=values,
            units={"vwap": "USD_per_share", "spot": "USD_per_share",
                   "price_vs_vwap_abs": "USD_per_share", "price_vs_vwap_pct": "percent",
                   "session_volume": "shares"},
            status={"code": "ok", "reason": None} if vwap is not None else
                   {"code": "no_data", "reason": "no_vwap_observation"},
            calculation_parameters={**DEFAULT_PARAMETERS["vwap"], **parameters},
            freshness=freshness, observed_at=market.get("spot_ts"),
            caveats=["snapshot_weighted_approximation"],
            coverage={**coverage, "partial": bool(market.get("vwap_partial_session")),
                      "reason": "partial_session" if market.get("vwap_partial_session") else None,
                      "details": {"vwap_ts": market.get("vwap_ts"),
                                  "vwap_session_started_at": market.get("vwap_session_started_at")}},
            input_times={"vwap": market.get("vwap_ts"), "anchor": market.get("vwap_session_started_at")},
        )
    if reading_id == "reference_rvol":
        rvol = market.get("rvol") or {}
        status = rvol.get("status") or "no_data"
        values = {
            "rvol_multiple": rvol.get("multiple"),
            "session_volume": market.get("session_volume"),
            "baseline_volume": rvol.get("baseline_volume"),
            "baseline_days_used": rvol.get("baseline_days_used"),
        }
        return _envelope(
            reading_id=reading_id, symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=values,
            units={"rvol_multiple": "dimensionless", "session_volume": "shares",
                   "baseline_volume": "shares", "baseline_days_used": "count"},
            status={"code": "ok" if status == "ok" else status, "reason": None if status != "no_data" else "no_volume_baseline"},
            calculation_parameters={**DEFAULT_PARAMETERS["rvol"], **parameters,
                                    "effective_days_used": rvol.get("baseline_days_used"),
                                    "baseline_updated_through": rvol.get("baseline_updated_through")},
            freshness=freshness, observed_at=market.get("session_volume_ts"),
            caveats=["calendar_day_lookback", "insufficient_history_is_explicit"],
            coverage={**coverage, "details": {"baseline_bucket": rvol.get("bucket_label"),
                                               "baseline_days_used": rvol.get("baseline_days_used")}},
        )
    momentum = market.get("momentum") or {}
    return _envelope(
        reading_id=reading_id, symbol=symbol, snapshot_key=snapshot_key,
        snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
        values={"return_pct": momentum.get("return_pct"),
                "anchor_age_minutes": momentum.get("anchor_age_minutes"),
                "sample_count": momentum.get("sample_count")},
        units={"return_pct": "percent", "anchor_age_minutes": "minutes", "sample_count": "count"},
        status={"code": momentum.get("status") or "no_data", "reason": None},
        calculation_parameters={**DEFAULT_PARAMETERS["momentum"], **parameters},
        freshness=freshness, observed_at=market.get("spot_ts"),
        caveats=["display_reference_not_trading_signal"],
        coverage={**coverage, "details": {"sample_count": momentum.get("sample_count"),
                                           "anchor_age_minutes": momentum.get("anchor_age_minutes")}},
        input_times={"anchor": momentum.get("anchor_observed_at")},
    )


def qqq_header_values(rows, stamp):
    local = stamp.astimezone(ZoneInfo("America/New_York"))
    day = local.date().isoformat()
    bands = {}
    for row in rows:
        if row.get("Expiration") != day or not (570 <= local.hour*60+local.minute < 960):
            continue
        iv, delta, bid, ask = (row.get(k) for k in ("IV", "Delta", "Bid", "Ask"))
        if not all(type(v) in (int, float) and isfinite(v) for v in (iv, delta, bid, ask)):
            continue
        if iv <= 0 or bid <= 0 or ask < bid or 2*(ask-bid)/(ask+bid) > 0.25:
            continue
        for band, lo, hi in (("near", .35, .65), ("wing", .15, .35)):
            if lo <= abs(delta) <= hi:
                bands.setdefault((band, row.get("Type")), []).append(100*iv)
    def pair(band, combine):
        calls, puts = (bands.get((band, side)) for side in ("call", "put"))
        return combine(median(calls), median(puts)) if calls and puts else None
    return {"near_money_iv_pct": pair("near", lambda c, p: (c+p)/2),
            "put_premium_iv_points": pair("wing", lambda c, p: p-c)}


def build_snapshot_readings(
    *,
    rows: list[dict[str, Any]],
    symbol: str,
    underlying: float | None,
    snapshot_key: str,
    snapshot_timestamp: str,
    calculated_at: datetime,
    freshness: str,
    observed_at: str | None,
    market: dict[str, Any] | None = None,
    parameters: dict[str, dict[str, Any]] | None = None,
    partial_session: bool = False,
) -> list[dict[str, Any]]:
    """The reference readings for one snapshot, in `market_reading.v1`.

    `market` is the snapshot's own `underlying_market` block; the VWAP, RVOL and
    reference-momentum readings describe exactly those published values rather
    than recomputing them. `parameters` carries the effective collector
    settings; a config change therefore changes the recorded parameters, not
    just the values.
    """
    market = market or {}
    parameters = parameters or {}
    coverage = {
        "partial": partial_session,
        "reason": "partial_session" if partial_session else None,
        "details": {"row_count": len(rows)},
    }
    pcr = compute_reference_pcr(rows)
    max_pain = compute_reference_max_pain(rows)
    skew = compute_reference_oi_skew(rows, underlying)
    readings = [
        _market_reading(reading_id="reference_vwap", market=market, symbol=symbol,
                        snapshot_key=snapshot_key, snapshot_timestamp=snapshot_timestamp,
                        calculated_at=calculated_at, freshness=freshness,
                        parameters=parameters.get("vwap", {}), coverage=coverage),
        _market_reading(reading_id="reference_rvol", market=market, symbol=symbol,
                        snapshot_key=snapshot_key, snapshot_timestamp=snapshot_timestamp,
                        calculated_at=calculated_at, freshness=freshness,
                        parameters=parameters.get("rvol", {}), coverage=coverage),
        _market_reading(reading_id="reference_momentum", market=market, symbol=symbol,
                        snapshot_key=snapshot_key, snapshot_timestamp=snapshot_timestamp,
                        calculated_at=calculated_at, freshness=freshness,
                        parameters=parameters.get("momentum", {}), coverage=coverage),
        _envelope(
            reading_id="reference_pcr", symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=pcr["values"], units=pcr["units"], status=pcr["status"],
            calculation_parameters={"formula": "sum_put_oi_over_sum_call_oi"},
            freshness=freshness, observed_at=observed_at, caveats=list(_OI_CAVEATS),
            coverage=coverage,
        ),
        _envelope(
            reading_id="reference_max_pain", symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=max_pain["values"], units=max_pain["units"], status=max_pain["status"],
            calculation_parameters={"tie_break": "lower_strike", "candidate_set": "all_board_strikes"},
            freshness=freshness, observed_at=observed_at, caveats=list(_OI_CAVEATS),
            coverage=coverage,
        ),
        _envelope(
            reading_id="reference_oi_skew", symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=skew["values"], units=skew["units"], status=skew["status"],
            calculation_parameters=skew["parameters"],
            freshness=freshness, observed_at=observed_at,
            caveats=list(_OI_CAVEATS) + ["reference_band_independent_of_account_band"],
            coverage=coverage,
        ),
    ]
    if symbol == "QQQ":
        values = qqq_header_values(rows, calculated_at)
        count = sum(v is not None for v in values.values())
        readings.append(_envelope(
            reading_id="qqq_iv_header", symbol=symbol, snapshot_key=snapshot_key,
            snapshot_timestamp=snapshot_timestamp, calculated_at=calculated_at,
            values=values, units={"near_money_iv_pct": "percent", "put_premium_iv_points": "percentage_points"},
            status={"code": ("no_data", "partial", "ok")[count],
                    "reason": None if count == 2 else "missing_eligible_pairs"},
            calculation_parameters={"near_absolute_delta": [.35, .65], "wing_absolute_delta": [.15, .35],
                                    "max_relative_spread": .25, "expiration": "same_day"},
            freshness=freshness, observed_at=None, caveats=["snapshot_greeks_without_per_leg_event_time"],
            coverage=coverage))
    return readings


def finish_publication(readings: list[dict[str, Any]], *, published_at: datetime | None,
                       status: str, reason: str | None = None) -> None:
    """Stamp the reading envelopes once the snapshot write has resolved."""
    for reading in readings:
        reading["published_at"] = published_at.isoformat() if published_at else None
        reading["publication"] = {"status": status, "reason": reason}


def readings_join(readings: list[dict[str, Any]], *, snapshot_key: str | None,
                  snapshot_timestamp: str, status: str = "valid") -> dict[str, Any]:
    """The immutable reference a decision can carry instead of copied values.

    Values are not copied into unrelated strategy records; the digest plus the
    exact snapshot key identify the observation, and a replay can fetch that
    snapshot's `market_readings` and re-hash them.
    """
    return {
        "schema_version": READING_JOIN_SCHEMA,
        "snapshot_key": snapshot_key,
        "snapshot_key_status": "valid" if snapshot_key else "absent",
        "snapshot_timestamp": snapshot_timestamp,
        "readings_digest": readings_digest(readings) if readings else None,
        "reading_ids": [r.get("reading_id") for r in readings],
        "status": status,
    }
