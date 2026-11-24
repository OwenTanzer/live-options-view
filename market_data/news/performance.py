"""Forward performance after news (media performance, news-performance/v1).

For each news event, this answers one narrow question: how much did the
mapped instrument move *after* the story became available, at fixed
horizons, using only prices that were themselves available by the time
the move is evaluated?

It is the forward half of the M3 join. The backward half stays in
``join.py`` and is unchanged: ``join.match_price`` gives the base price
(the newest price available by ``event_at``). The batch join selects this base separately for each mapped instrument.
Publication-time rows carry no performance claim.

Event-known / no-lookahead rules
--------------------------------
* ``event_at`` comes from ``join.news_events`` in ``collector`` or
  ``archive`` mode only. ``publication`` mode is hindsight: its rows
  are labelled ``hindsight`` and carry NO return. A caller that passes
  ``mode="publication"`` gets rows with ``status="hindsight"`` and
  ``return_pct`` / ``return_bps`` set to ``None``.
* The base price must satisfy ``available_at <= event_at`` (that is
  ``join.match_price`` by construction). If the base is ``missing``,
  there is no performance row with a return — the row reports
  ``no_base_price``. If the base is ``stale``, the return is still
  computed from that base but the row is flagged ``base_stale`` and
  the honesty label says so; the stale base is never silently
  presented as fresh.
* A forward price must satisfy BOTH:
  ``price_time >= event_at + horizon`` AND ``available_at <= cutoff``.
  The forward price is the EARLIEST record meeting both. It is never
  the latest price before the target, never an interpolation, never
  a carried-forward stale price.
* If no such record exists by the cutoff, the horizon reports
  ``missing`` with a reason, never a zero:
  ``not_yet_available`` (cutoff is before the target),
  ``no_series`` (no price records for this instrument/series at all),
  ``no_forward_price`` (cutoff is past the target but no record
  qualifies — e.g. a market-close / weekend gap, or a thin series).
  There is no exchange calendar in this repo (see the news README),
  so "market closed" is never asserted from a calendar; a gap is
  reported as ``no_forward_price`` with the window, and the panel
  may render "market closed?" only as a question, never as a fact.
  (The panel honesty label for this state is "not yet available".)

Horizons
--------
``5m`` (300 s), ``30m`` (1800 s), ``1h`` (3600 s), ``1d`` (86400 s).
For ``1d`` the same rule applies, but the preferred series is
``bar_1d`` when the caller supplies it: the forward record is then
the earliest daily bar whose ``bar_end`` is at or after the target
and whose ``available_at`` is at or before the cutoff. If the caller
supplies an underlying-quote / 1-minute-bar series instead, ``1d``
uses that series under the same earliest-after-target rule and the
row records which series was used. Either way the rule is identical;
only the series differs, and it is recorded.

Returns
-------
Simple return ``(forward - base) / base``, as a fraction, in percent
(``return_pct``) and in basis points (``return_bps``). ``base == 0``
is rejected (a zero base price is not a price). NaN / infinite prices
are already rejected by ``prices.parse_record``; this module rejects
them again defensively and never emits them.

Schema: ``news-performance/v1``. One row per
(event, instrument, horizon). Full provenance is kept on every row:
event and revision ids, instrument and mapping basis, the base record
summary, the forward record summary (or ``None``), cutoff, mode,
hindsight flag, and coverage.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .companies import map_company
from .join import PriceSeries, match_price
from .normalize import iso, parse_iso

PERFORMANCE_SCHEMA = "news-performance/v1"

HORIZONS: dict[str, int] = {
    "5m": 300,
    "30m": 1800,
    "1h": 3600,
    "1d": 86400,
}

HORIZON_ORDER = ("5m", "30m", "1h", "1d")


def _to_datetime(value) -> datetime:
    return parse_iso(value) if isinstance(value, str) else value


def forward_after(series: PriceSeries, target: datetime, cutoff: datetime):
    """Earliest record with price_time >= target and available_at <= cutoff.

    ``PriceSeries.records`` is sorted by (available_at, price_time,
    record_id), so the earliest *price_time* meeting both conditions
    is found by scanning for the minimum price_time among qualifying
    records. Returns ``None`` when no record qualifies.
    """
    best = None
    for record in series.records:
        if record.available_at > cutoff:
            continue
        if record.price_time < target:
            continue
        if best is None or (record.price_time, record.available_at,
                            record.record_id) < (best.price_time,
                                                best.available_at,
                                                best.record_id):
            best = record
    return best


def forward_return(base_price: float, forward_price: float) -> tuple[float, float]:
    """(return_fraction, return_bps). Raises on a non-finite or zero base."""
    if base_price == 0 or not math.isfinite(base_price) or not math.isfinite(forward_price):
        raise ValueError("base price must be finite and non-zero; forward price must be finite")
    ret = (forward_price - base_price) / base_price
    if not math.isfinite(ret):
        raise ValueError("forward return is not finite")
    return ret, ret * 10000.0


def performance_for_event(event: dict, base: dict, series_by_instrument: dict,
                          cutoff, mode: str, horizons=HORIZON_ORDER) -> list[dict]:
    """Forward rows for one join event.

    ``event`` is one row from ``join.join()["rows"]`` (it carries
    ``event_at``, ``item_id``, ``revision_id`` and, when mapped, the
    caller should also pass the mapping dict from ``companies`` — see
    ``performance_for_join``). ``base`` is that row's ``price`` dict
    from ``join.match_price``. ``series_by_instrument`` maps
    ``instrument_id`` -> ``PriceSeries``. ``cutoff`` is a datetime or
    an ISO string. ``mode`` is ``"collector"`` or ``"archive"`` for a
    claim; ``"publication"`` rows are labelled hindsight with no return.
    """
    if mode not in {"collector", "archive", "publication"}:
        raise ValueError("unknown performance clock mode")
    cutoff_dt = _to_datetime(cutoff)
    event_at = _to_datetime(event["event_at"])
    instrument_id = event.get("instrument_id")
    mapping = event.get("instrument_mapping_detail") or {}
    rows: list[dict] = []
    base_status = base.get("status") if isinstance(base, dict) else None
    base_record = base.get("record") if isinstance(base, dict) else None

    for horizon in horizons:
        seconds = HORIZONS[horizon]
        target = event_at + timedelta(seconds=seconds)
        common = {
            "schema_version": PERFORMANCE_SCHEMA,
            "event_id": f"{event.get('item_id')}:{event.get('revision_id')}",
            "item_id": event.get("item_id"),
            "revision_id": event.get("revision_id"),
            "title": event.get("title"),
            "event_at": iso(event_at),
            "mode": mode,
            "hindsight": mode == "publication",
            "instrument_id": instrument_id,
            "mapping_basis": mapping.get("mapping_basis", "none"),
            "matched_text": mapping.get("matched_text", ""),
            "horizon": horizon,
            "horizon_seconds": seconds,
            "target_at": iso(target),
            "cutoff": iso(cutoff_dt),
            "base": {"status": base_status, "record": base_record,
                     "age_seconds": base.get("age_seconds") if isinstance(base, dict) else None},
            "forward": None,
            "return_pct": None,
            "return_bps": None,
            "status": None,
            "missing_reason": None,
            "base_stale": base_status == "stale",
            "coverage": {
                "series": None,
                "records_in_series": 0,
            },
        }
        if mode == "publication":
            common.update(status="hindsight",
                          missing_reason="publication mode is hindsight; not a performance claim")
            rows.append(common)
            continue
        if instrument_id is None:
            common.update(status="missing",
                          missing_reason="unknown mapping: no instrument to price")
            rows.append(common)
            continue
        series = series_by_instrument.get(instrument_id)
        if series is None or series.instrument_id != instrument_id:
            common.update(status="missing", missing_reason="no_series")
            rows.append(common)
            continue
        if base_record and (base_record.get("instrument_id") != instrument_id
                            or not base_record.get("available_at")
                            or _to_datetime(base_record["available_at"]) > min(event_at, cutoff_dt)):
            common.update(status="missing", missing_reason="ineligible_base")
            rows.append(common)
            continue
        if base_status != "matched" and base_status != "stale":
            common.update(status="missing",
                          missing_reason="no_base_price: no price was available by the event")
            rows.append(common)
            continue
        common["coverage"]["series"] = series.series
        common["coverage"]["records_in_series"] = len(series.records)
        if cutoff_dt < target:
            common.update(status="missing", missing_reason="not_yet_available")
            rows.append(common)
            continue
        forward = forward_after(series, target, cutoff_dt)
        if forward is None:
            common.update(status="missing", missing_reason="no_forward_price")
            rows.append(common)
            continue
        if base_record is None or base_record.get("price") is None:
            common.update(status="missing", missing_reason="no_base_price")
            rows.append(common)
            continue
        ret, bps = forward_return(float(base_record["price"]), float(forward.price))
        common.update(
            forward={**forward.summary(), "price_time": iso(forward.price_time)},
            return_pct=ret * 100.0,
            return_bps=bps,
            status="matched",
            missing_reason=None,
        )
        common["coverage"]["forward_age_seconds"] = (cutoff_dt - forward.price_time).total_seconds()
        # How far past the horizon target the forward price actually is.
        # A gap (e.g. 10:10-10:19 with no NVDA quotes) makes this large;
        # the panel shows the forward price_time, never just "5m".
        common["coverage"]["forward_lag_seconds"] = (forward.price_time - target).total_seconds()
        rows.append(common)
    return rows


def performance_for_join(join_result: dict, series_by_instrument: dict,
                         cutoff=None, horizons=HORIZON_ORDER) -> dict:
    """Forward performance for every row of a ``join.join()`` result.

    The join result already carries ``mode``, ``cutoff`` and per-row
    ``price`` (the base). Rows whose event has no mapping
    (``instrument_id`` absent) still get rows — with
    ``status="missing"`` and ``missing_reason="unknown mapping"`` —
    so the output never silently drops an event.
    """
    mode = join_result.get("mode", "collector")
    cutoff_dt = _to_datetime(cutoff if cutoff is not None else join_result["cutoff"])
    all_rows: list[dict] = []
    eligible = [row for row in join_result.get("rows", []) if _to_datetime(row["event_at"]) <= cutoff_dt]
    for row in eligible:
        mapping = row.get("company_mapping") or row.get("instrument_mapping_detail") or map_company(row.get("title", ""))
        instrument = mapping["instrument_id"]
        series = series_by_instrument.get(instrument)
        base = {}
        if series and series.instrument_id == instrument:
            tolerance = timedelta(seconds=join_result.get("price_series", {}).get("tolerance_s", 120))
            base = match_price(series, _to_datetime(row["event_at"]), cutoff_dt, tolerance)
        mapped = dict(row, instrument_id=instrument, instrument_mapping_detail=mapping)
        all_rows.extend(performance_for_event(mapped, base, series_by_instrument, cutoff_dt, mode, horizons))
    matched = sum(1 for r in all_rows if r["status"] == "matched")
    return {
        "schema_version": PERFORMANCE_SCHEMA,
        "mode": mode,
        "hindsight": mode == "publication",
        "cutoff": iso(cutoff_dt),
        "horizons": list(horizons),
        "rows": all_rows,
        "summary": {
            "events": len(eligible),
            "rows": len(all_rows),
            "matched": matched,
            "missing": sum(1 for r in all_rows if r["status"] == "missing"),
            "hindsight": sum(1 for r in all_rows if r["status"] == "hindsight"),
        },
    }
